"""
Polymarket Position Redemption
================================
Adapted from RobotTraders/bits_and_bobs/polymarket_redeem.py.

Redeems resolved winning positions back to USDC.e in the funder wallet.
Called automatically after every WIN in the bot loop.

Two market types handled:
  - Standard binary:  redeemPositions() on CTF contract
  - Neg-risk:         redeemPositions() on NegRisk adapter
"""

import asyncio
import logging
import time
from datetime import datetime

import requests

from core.config import CFG

log = logging.getLogger("hybrid.redeem")

# ── Polygon contract addresses ────────────────────────────────────────
USDC_ADDRESS    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS     = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

RELAYER_RETRY_WAIT = 60  # seconds to wait on rate limit

_ts = lambda: datetime.now().strftime("%H:%M:%S")

# ── Lazy imports (optional deps) ──────────────────────────────────────
try:
    from eth_abi import encode as eth_encode
    from eth_utils import keccak
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import (
        RelayerTxType, OperationType, SafeTransaction,
    )
    from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

    REDEEM_SELECTOR      = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    NEG_RISK_REDEEM_SELECTOR = keccak(text="redeemPositions(bytes32,uint256[])")[:4]

    HAS_RELAYER = True
except ImportError:
    HAS_RELAYER = False
    log.warning(
        "Redemption deps not installed. Run:\n"
        "  pip install eth-abi eth-utils\n"
        "  pip install git+https://github.com/Polymarket/py-builder-relayer-client.git\n"
        "  pip install git+https://github.com/Polymarket/py-builder-signing-sdk.git"
    )


def _build_client():
    """Build a RelayClient using credentials from CFG (.env)."""
    wallet_type = (
        RelayerTxType.PROXY if CFG.sig_type == 1 else RelayerTxType.SAFE
    )
    return RelayClient(
        "https://relayer-v2.polymarket.com",
        chain_id=137,
        private_key=CFG.private_key,
        builder_config=BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=CFG.api_key,
                secret=CFG.api_secret,
                passphrase=CFG.api_passphrase,
            )
        ),
        relay_tx_type=wallet_type,
    )


def _fetch_redeemable_positions() -> list:
    """Fetch all resolved positions with tokens still held."""
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={
                "user":          CFG.funder_address,
                "redeemable":    "true",
                "sizeThreshold": 0,
            },
            timeout=15,
        )
        if resp.status_code in (429, 1015):
            log.warning("Data API rate limited — waiting %ds", RELAYER_RETRY_WAIT)
            time.sleep(RELAYER_RETRY_WAIT)
            resp = requests.get(
                "https://data-api.polymarket.com/positions",
                params={
                    "user":          CFG.funder_address,
                    "redeemable":    "true",
                    "sizeThreshold": 0,
                },
                timeout=15,
            )
        positions = resp.json()
        # Filter out zero-size dust the API sometimes returns post-redemption
        return [p for p in positions if float(p.get("size", 0)) > 0]
    except Exception as e:
        log.error("Failed to fetch redeemable positions: %s", e)
        return []


def _build_txn(pos: dict):
    """Build a SafeTransaction for one position. Returns None if unsupported."""
    cid = pos.get("conditionId", pos.get("condition_id", ""))
    if not cid:
        return None, None
    if not cid.startswith("0x"):
        cid = "0x" + cid

    condition_bytes = bytes.fromhex(cid[2:])
    neg_risk        = pos.get("negativeRisk")
    market          = pos.get("title", cid[:12])

    if neg_risk is True:
        size_raw      = int(float(pos.get("size", 0)) * 1e6)
        outcome_index = int(pos.get("outcomeIndex", 0))
        amounts       = [0, 0]
        amounts[outcome_index] = size_raw
        args = eth_encode(["bytes32", "uint256[]"], [condition_bytes, amounts])
        txn  = SafeTransaction(
            to=NEG_RISK_ADAPTER,
            operation=OperationType.Call,
            data="0x" + (NEG_RISK_REDEEM_SELECTOR + args).hex(),
            value="0",
        )
        return txn, market

    elif neg_risk is False:
        args = eth_encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_ADDRESS, b"\x00" * 32, condition_bytes, [1, 2]],
        )
        txn = SafeTransaction(
            to=CTF_ADDRESS,
            operation=OperationType.Call,
            data="0x" + (REDEEM_SELECTOR + args).hex(),
            value="0",
        )
        return txn, market

    else:
        log.warning("Skipping %s: unsupported market type (negativeRisk=%r)",
                    market, neg_risk)
        return None, market


def redeem_all() -> int:
    """
    Redeem all resolved positions. Returns count of redeemed positions.
    Blocking — run in a thread from async context.
    """
    if not HAS_RELAYER:
        log.warning("Skipping redemption: deps not installed")
        return 0

    if not CFG.private_key or not CFG.api_key:
        log.warning("Skipping redemption: credentials not configured")
        return 0

    positions = _fetch_redeemable_positions()
    if not positions:
        log.info("No positions to redeem")
        return 0

    log.info("Found %d redeemable positions", len(positions))
    client   = _build_client()
    redeemed = 0

    for pos in positions:
        txn, market = _build_txn(pos)
        if txn is None:
            continue
        try:
            resp = client.execute([txn], f"redeem {market}")
            resp.wait()
            redeemed += 1
            log.info("REDEEMED: %s", market)
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status in (429, 1015):
                log.warning("Relayer rate limited — waiting %ds", RELAYER_RETRY_WAIT)
                time.sleep(RELAYER_RETRY_WAIT)
                try:
                    resp = client.execute([txn], f"redeem {market}")
                    resp.wait()
                    redeemed += 1
                    log.info("REDEEMED (retry): %s", market)
                except Exception as e2:
                    log.error("Failed to redeem %s after retry: %s", market, e2)
            else:
                log.error("Failed to redeem %s: %s", market, e)

    log.info("Redemption complete: %d/%d positions", redeemed, len(positions))
    return redeemed


async def redeem_all_async() -> int:
    """
    Async wrapper — runs redeem_all() in a thread so it doesn't
    block the bot's event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, redeem_all)

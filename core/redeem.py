"""
Polymarket Position Redemption
================================
Redeems resolved winning positions back to USDC.e in the funder wallet.
Uses direct on-chain web3 calls — no Relayer or Builder API credentials needed.

Two market types handled:
  - Standard binary:  redeemPositions() on CTF contract
  - Neg-risk:         redeemPositions() on NegRisk adapter
"""

import asyncio
import logging
import time

import requests
from web3 import Web3

from core.config import CFG

log = logging.getLogger("hybrid.redeem")

# ── Polygon RPCs ──────────────────────────────────────────────────────
POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon-rpc.com",
]

# ── Contract addresses (Polygon) ──────────────────────────────────────
USDC_ADDRESS      = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDRESS       = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADAPTER  = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

DATA_API_RETRY_WAIT = 60

# ── Contract ABIs (only functions we use) ────────────────────────────
CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]

NEG_RISK_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amounts",     "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


def _connect() -> Web3:
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None


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
            log.warning("Data API rate limited — waiting %ds", DATA_API_RETRY_WAIT)
            time.sleep(DATA_API_RETRY_WAIT)
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
        return [p for p in positions if float(p.get("size", 0)) > 0]
    except Exception as e:
        log.error("Failed to fetch redeemable positions: %s", e)
        return []


def _redeem_one(w3: Web3, wallet: str, pos: dict, nonce: int, gas_price: int) -> bool:
    """Submit a redemption transaction for a single position."""
    cid = pos.get("conditionId", pos.get("condition_id", ""))
    if not cid:
        return False
    if not cid.startswith("0x"):
        cid = "0x" + cid

    condition_id = bytes.fromhex(cid[2:])
    neg_risk     = pos.get("negativeRisk")
    market       = pos.get("title", cid[:12])

    try:
        if neg_risk is True:
            # Neg-risk: redeemPositions(bytes32 conditionId, uint256[] amounts)
            size_raw      = int(float(pos.get("size", 0)) * 1e6)
            outcome_index = int(pos.get("outcomeIndex", 0))
            amounts       = [0, 0]
            amounts[outcome_index] = size_raw

            contract = w3.eth.contract(address=NEG_RISK_ADAPTER, abi=NEG_RISK_ABI)
            tx = contract.functions.redeemPositions(
                condition_id, amounts
            ).build_transaction({
                "from":     wallet,
                "nonce":    nonce,
                "gas":      300_000,
                "gasPrice": gas_price,
                "chainId":  137,
            })

        elif neg_risk is False:
            # Standard binary: redeemPositions(address, bytes32, bytes32, uint256[])
            # indexSets [1, 2] covers both YES/NO — contract redeems whichever you hold
            contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
            tx = contract.functions.redeemPositions(
                USDC_ADDRESS, b"\x00" * 32, condition_id, [1, 2]
            ).build_transaction({
                "from":     wallet,
                "nonce":    nonce,
                "gas":      300_000,
                "gasPrice": gas_price,
                "chainId":  137,
            })

        else:
            log.warning("Skipping %s: unsupported market type (negativeRisk=%r)",
                        market, neg_risk)
            return False

        signed  = w3.eth.account.sign_transaction(tx, CFG.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status == 1:
            log.info("REDEEMED: %s | tx: 0x%s (block %d)",
                     market, tx_hash.hex(), receipt.blockNumber)
            return True
        else:
            log.error("Redeem REVERTED: %s | tx: 0x%s", market, tx_hash.hex())
            return False

    except Exception as e:
        log.error("Failed to redeem %s: %s", market, e)
        return False


def redeem_all() -> tuple[int, float]:
    """
    Redeem all resolved positions.
    Returns (count_redeemed, total_usdc) tuple.
    Blocking — run in a thread from async context.
    """
    if not CFG.private_key:
        log.warning("Skipping redemption: POLY_PRIVATE_KEY not configured")
        return 0, 0.0

    if not CFG.funder_address:
        log.warning("Skipping redemption: POLY_FUNDER_ADDRESS not configured")
        return 0, 0.0

    positions = _fetch_redeemable_positions()
    if not positions:
        log.info("No positions to redeem")
        return 0, 0.0

    log.info("Found %d redeemable position(s)", len(positions))
    total_usdc = sum(float(p.get("size", 0)) for p in positions)

    w3 = _connect()
    if not w3:
        log.error("Cannot connect to Polygon RPC — skipping redemption")
        return 0, 0.0

    wallet    = Web3.to_checksum_address(CFG.funder_address)
    redeemed  = 0
    # Fetch nonce and gas price once; increment nonce locally so rapid-fire
    # txs don't collide in the mempool (avoids "replacement underpriced" /
    # "nonce too low" errors when submitting many txs in a tight loop).
    nonce     = w3.eth.get_transaction_count(wallet, "pending")
    gas_price = w3.eth.gas_price

    for pos in positions:
        if _redeem_one(w3, wallet, pos, nonce, gas_price):
            redeemed += 1
        # Always advance the nonce — even on failure the slot is consumed
        # if the tx reached the mempool (skip-type failures return False
        # before any send, but nonce advancing on those is harmless).
        nonce += 1

    log.info("Redemption complete: %d/%d positions ($%.2f USDC.e)",
             redeemed, len(positions), total_usdc)
    return redeemed, total_usdc


async def redeem_all_async() -> tuple[int, float]:
    """
    Async wrapper — runs redeem_all() in a thread so it doesn't
    block the bot's event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, redeem_all)

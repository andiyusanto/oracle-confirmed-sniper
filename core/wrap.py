"""
Auto Wrap + Approve
====================
After CTF redemption, the wallet receives legacy USDC.e (the CTF settlement
token). Polymarket V2 (Apr 28 2026) requires pUSD as collateral, so the bot
must:
  1. wrap the freshly-received USDC.e → pUSD via the Collateral Onramp, and
  2. ensure pUSD is approved for the V2 exchange contracts.

If skipped, the bot's CLOB-side pUSD balance no longer matches its computed
portfolio; `capital_verifier` then trips the CRITICAL gap guard and pauses
trading. This module replaces the manual `wrap_pusd.py` + `approve_usdc.py`
flow and is invoked automatically after every successful auto-redeem.

Idempotent: a no-op when USDC.e balance is zero and pUSD is already approved
on both V2 exchanges. Approves are skipped when current allowance is already
near-MAX. Each tx waits for receipt; failures are logged and reraised to the
caller, which decides whether to retry.
"""

import asyncio
import logging
from typing import Optional

from web3 import Web3

from core.config import CFG

log = logging.getLogger("hybrid.wrap")

POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
]

USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
COLLATERAL_ONRAMP = Web3.to_checksum_address(
    "0x93070a847efEf7F70739046A929D47a521F5B8ee"
)

# V2 exchanges that pUSD must be approved to.
# NOTE: 0xe2222d… IS the NegRisk V2 Exchange (verified in py_clob_client_v2.config).
# 0xd91E80cF… is the V1 NegRisk Adapter — only used for CTF redemption, not pUSD spending,
# so it is NOT in this list.
PUSD_SPENDERS: list[tuple[str, str]] = [
    ("CTF Exchange V2", "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("NegRisk CTF Exchange V2", "0xe2222d279d744050d28e00520010520000310F59"),
]

MAX_UINT256 = 2**256 - 1
APPROVE_THRESHOLD = MAX_UINT256 // 2  # treat anything above this as "approved"
WRAP_DUST_THRESHOLD_RAW = 10_000  # ignore <$0.01 USDC.e dust

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

ONRAMP_ABI = [
    {
        "name": "wrap",
        "type": "function",
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_to", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


def _connect() -> Optional[Web3]:
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None


def _send_tx(w3: Web3, tx: dict, label: str) -> bool:
    signed = w3.eth.account.sign_transaction(tx, CFG.private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    log.info("%s tx sent: 0x%s", label, tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        log.info("%s confirmed (block %d)", label, receipt.blockNumber)
        return True
    log.error("%s REVERTED: 0x%s", label, tx_hash.hex())
    return False


def _wrap_and_approve_sync() -> tuple[float, int]:
    """Synchronous core: wraps any USDC.e, ensures pUSD approvals.

    Returns (wrapped_usd, approvals_sent). A return of (0.0, 0) means
    everything was already in place — nothing to do.
    """
    if not CFG.private_key:
        log.warning("Skipping auto-wrap: POLY_PRIVATE_KEY not configured")
        return 0.0, 0

    w3 = _connect()
    if w3 is None:
        log.error("Auto-wrap: no Polygon RPC reachable — skipping")
        return 0.0, 0

    eoa = w3.eth.account.from_key(CFG.private_key).address
    wallet = Web3.to_checksum_address(CFG.funder_address) if CFG.funder_address else eoa

    usdc_e = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    pusd = w3.eth.contract(address=PUSD, abi=ERC20_ABI)
    onramp = w3.eth.contract(address=COLLATERAL_ONRAMP, abi=ONRAMP_ABI)

    usdc_e_raw = usdc_e.functions.balanceOf(wallet).call()
    gas_price = int(w3.eth.gas_price * 1.3)

    wrapped_usd = 0.0

    # ── Step 1: wrap USDC.e → pUSD if balance is above dust threshold ──
    if usdc_e_raw >= WRAP_DUST_THRESHOLD_RAW:
        # Ensure Onramp can pull USDC.e
        current = usdc_e.functions.allowance(wallet, COLLATERAL_ONRAMP).call()
        if current < usdc_e_raw:
            log.info("Auto-wrap: approving Collateral Onramp for USDC.e (MAX)")
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            tx = usdc_e.functions.approve(
                COLLATERAL_ONRAMP, MAX_UINT256
            ).build_transaction(
                {
                    "from": wallet,
                    "nonce": nonce,
                    "gas": 100_000,
                    "gasPrice": gas_price,
                    "chainId": 137,
                }
            )
            if not _send_tx(w3, tx, "USDC.e approve(Onramp)"):
                return 0.0, 0

        log.info("Auto-wrap: wrapping $%.4f USDC.e → pUSD", usdc_e_raw / 1e6)
        nonce = w3.eth.get_transaction_count(wallet, "pending")
        tx = onramp.functions.wrap(USDC_E, wallet, usdc_e_raw).build_transaction(
            {
                "from": wallet,
                "nonce": nonce,
                "gas": 200_000,
                "gasPrice": gas_price,
                "chainId": 137,
            }
        )
        if _send_tx(w3, tx, "Onramp.wrap"):
            wrapped_usd = usdc_e_raw / 1e6
        else:
            return 0.0, 0
    else:
        log.debug(
            "Auto-wrap: USDC.e balance below dust ($%.6f) — skipping wrap",
            usdc_e_raw / 1e6,
        )

    # ── Step 2: ensure pUSD approvals on V2 exchanges (idempotent) ──
    approvals_sent = 0
    for name, spender_raw in PUSD_SPENDERS:
        spender = Web3.to_checksum_address(spender_raw)
        current = pusd.functions.allowance(wallet, spender).call()
        if current >= APPROVE_THRESHOLD:
            continue
        log.info("Auto-wrap: approving pUSD → %s (MAX)", name)
        nonce = w3.eth.get_transaction_count(wallet, "pending")
        tx = pusd.functions.approve(spender, MAX_UINT256).build_transaction(
            {
                "from": wallet,
                "nonce": nonce,
                "gas": 100_000,
                "gasPrice": gas_price,
                "chainId": 137,
            }
        )
        if _send_tx(w3, tx, f"pUSD approve({name})"):
            approvals_sent += 1
        else:
            log.warning(
                "Auto-wrap: pUSD approve to %s failed — will retry next call", name
            )

    if wrapped_usd > 0 or approvals_sent > 0:
        log.info(
            "Auto-wrap complete: wrapped $%.4f, approvals=%d",
            wrapped_usd,
            approvals_sent,
        )
    return wrapped_usd, approvals_sent


async def auto_wrap_and_approve_async() -> tuple[float, int]:
    """Async wrapper — runs the on-chain flow in a thread pool.

    Safe to call after every successful redemption. Idempotent: if there's
    nothing to wrap and approvals are already in place, returns (0.0, 0)
    after only a few view calls. Exceptions are caught and logged so a
    transient RPC issue does not crash the bot loop.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _wrap_and_approve_sync)
    except Exception as e:
        log.error("Auto-wrap failed: %s", e)
        return 0.0, 0

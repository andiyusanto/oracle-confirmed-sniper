"""
Polymarket Position Redemption
================================
Redeems resolved winning positions back to USDC.e in the funder wallet.
Uses direct on-chain web3 calls — no Relayer or Builder API credentials needed.

Two market types handled:
  - Standard binary:  redeemPositions() on CTF contract
  - Neg-risk:         redeemPositions() on NegRisk adapter

Defenses active:
  - Null/zero address burn guard  (sys.exit — no fallback)
  - Dynamic gas escalator         (+20% every 60s, +50% final)
  - 3-block confirmation wait     (re-org safety)
  - Actual USDC.e Transfer parsing (not Data API estimate)
"""

import asyncio
import logging
import sys
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
USDC_ADDRESS     = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDRESS      = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
_NULL_ADDRESS    = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")

DATA_API_RETRY_WAIT = 60

# ── Gas escalation ────────────────────────────────────────────────────
_GAS_CHECK_INTERVAL = 15    # poll receipt every N seconds
_GAS_ESCALATE_AFTER = 60    # first bump after this many seconds
_GAS_BUMP_INTERVAL  = 60    # each subsequent bump interval
_GAS_BUMP_PCT       = 0.20  # +20% per normal bump
_GAS_FINAL_PCT      = 0.50  # +50% on final bump
_GAS_MAX_BUMPS      = 3
_GAS_TOTAL_TIMEOUT  = 300   # hard stop: 5 minutes

# ── Re-org safety ─────────────────────────────────────────────────────
_CONFIRM_BLOCKS  = 3   # blocks to wait after receipt
_CONFIRM_TIMEOUT = 30  # seconds max to wait

# ── USDC.e Transfer event ABI ─────────────────────────────────────────
USDC_TRANSFER_ABI = [
    {
        "name": "Transfer",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "from",  "type": "address", "indexed": True},
            {"name": "to",    "type": "address", "indexed": True},
            {"name": "value", "type": "uint256", "indexed": False},
        ],
    }
]

# ── Contract ABIs ─────────────────────────────────────────────────────
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


# ── Priority 4: Null / Zero address burn guard ────────────────────────

def _guard_address(addr: str, context: str) -> str:
    """Block execution if address is null, empty, or the zero address.

    Hard exit — no fallback, no retry. Prevents token burns to dead address.
    """
    if not addr:
        log.critical("BURN GUARD [%s]: address is None/empty — halting.", context)
        sys.exit(1)
    try:
        checksum = Web3.to_checksum_address(addr)
    except Exception:
        log.critical("BURN GUARD [%s]: invalid address %r — halting.", context, addr)
        sys.exit(1)
    if checksum == _NULL_ADDRESS:
        log.critical("BURN GUARD [%s]: zero address blocked — halting.", context)
        sys.exit(1)
    return checksum


# ── Helpers ───────────────────────────────────────────────────────────

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


def _parse_usdc_received(w3: Web3, receipt, wallet: str) -> float:
    """Extract actual USDC.e received by wallet from a redemption receipt."""
    try:
        usdc_contract = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_TRANSFER_ABI)
        events = usdc_contract.events.Transfer().process_receipt(receipt)
        total = 0.0
        for evt in events:
            if evt["args"]["to"].lower() == wallet.lower():
                total += evt["args"]["value"] / 1e6
        return total
    except Exception as e:
        log.debug("Could not parse Transfer events: %s", e)
        return 0.0


# ── Priority 1: Gas escalator ─────────────────────────────────────────

def _wait_with_escalation(w3: Web3, tx_hash, tx_dict: dict,
                           nonce: int, initial_gas: int):
    """Wait for receipt with automatic gas bumps if the tx is stuck.

    Schedule:
      t+60s  → bump #1: +20%
      t+120s → bump #2: +20%
      t+180s → bump #3: +50% of original (final)
      t+300s → give up, return None

    Returns the receipt, or None on total timeout.
    """
    sent_at        = time.time()
    bumps          = 0
    current_gas    = initial_gas
    current_hash   = tx_hash
    next_escalate  = sent_at + _GAS_ESCALATE_AFTER

    while True:
        elapsed = time.time() - sent_at
        if elapsed >= _GAS_TOTAL_TIMEOUT:
            log.error(
                "TX TIMEOUT after %.0fs (%d bump(s)) — nonce %d may be stuck. "
                "Verify on Polygonscan and re-run redeem_now.py if needed.",
                elapsed, bumps, nonce,
            )
            return None

        # Poll for receipt with a short window so we can check escalation
        try:
            receipt = w3.eth.wait_for_transaction_receipt(
                current_hash, timeout=_GAS_CHECK_INTERVAL
            )
            if bumps > 0:
                log.info("TX confirmed after %d bump(s) (%.0fs total)",
                         bumps, time.time() - sent_at)
            return receipt
        except Exception:
            pass  # not mined yet

        # Escalate if due and still under limit
        now = time.time()
        if now >= next_escalate and bumps < _GAS_MAX_BUMPS:
            if bumps < _GAS_MAX_BUMPS - 1:
                new_gas = int(current_gas * (1 + _GAS_BUMP_PCT))
                label   = f"+{int(_GAS_BUMP_PCT * 100)}%"
            else:
                new_gas = int(initial_gas * (1 + _GAS_FINAL_PCT))
                label   = "+50% FINAL"

            try:
                replacement = dict(tx_dict)
                replacement["gasPrice"] = new_gas
                signed   = w3.eth.account.sign_transaction(replacement, CFG.private_key)
                new_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                bumps        += 1
                current_gas   = new_gas
                current_hash  = new_hash
                next_escalate = now + _GAS_BUMP_INTERVAL
                log.warning(
                    "GAS BUMP #%d (%s) → %d gwei | tx: 0x%s",
                    bumps, label, new_gas // 10**9, new_hash.hex(),
                )
            except Exception as e:
                err = str(e).lower()
                if "replacement transaction underpriced" in err:
                    log.debug("Gas bump rejected (underpriced — already queued): %s", e)
                else:
                    log.warning("Gas bump error: %s", e)
                next_escalate = now + _GAS_BUMP_INTERVAL


# ── Priority 6: 3-block confirmation ─────────────────────────────────

def _wait_confirmations(w3: Web3, receipt) -> bool:
    """Block until _CONFIRM_BLOCKS blocks are stacked on the receipt block.

    Polygon blocks are ~2s each → 3 blocks ≈ 6s extra safety.
    Returns True if confirmed within timeout, False otherwise (proceeds anyway).
    """
    deadline = time.time() + _CONFIRM_TIMEOUT
    target   = receipt.blockNumber + _CONFIRM_BLOCKS
    while True:
        current = w3.eth.block_number
        confs   = current - receipt.blockNumber
        if confs >= _CONFIRM_BLOCKS:
            log.debug("Confirmed %d blocks (block %d)", confs, receipt.blockNumber)
            return True
        if time.time() >= deadline:
            log.warning(
                "Confirmation timeout: %d/%d blocks — proceeding anyway",
                confs, _CONFIRM_BLOCKS,
            )
            return False
        time.sleep(2)


# ── Core redemption ───────────────────────────────────────────────────

def _redeem_one(w3: Web3, wallet: str, pos: dict, nonce: int,
                gas_price: int) -> tuple[bool, float]:
    """Submit a redemption tx for a single position.

    Returns (success, actual_usdc_received).
    actual_usdc_received is parsed from on-chain Transfer events;
    0.0 means the tx ran but no real tokens were held (database-only).
    """
    cid = pos.get("conditionId", pos.get("condition_id", ""))
    if not cid:
        return False, 0.0
    if not cid.startswith("0x"):
        cid = "0x" + cid

    condition_id = bytes.fromhex(cid[2:])
    neg_risk     = pos.get("negativeRisk")
    market       = pos.get("title", cid[:12])

    try:
        if neg_risk is True:
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
            return False, 0.0

        signed  = w3.eth.account.sign_transaction(tx, CFG.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        # Priority 1: escalate gas if stuck
        receipt = _wait_with_escalation(w3, tx_hash, tx, nonce, gas_price)
        if receipt is None:
            return False, 0.0

        if receipt.status != 1:
            log.error("Redeem REVERTED: %s | tx: 0x%s", market, tx_hash.hex())
            return False, 0.0

        # Priority 6: 3-block confirmation before parsing payout
        _wait_confirmations(w3, receipt)

        actual_usdc = _parse_usdc_received(w3, receipt, wallet)
        if actual_usdc > 0:
            log.info(
                "REDEEMED: %s | $%.4f USDC.e | tx: 0x%s (block %d)",
                market, actual_usdc, tx_hash.hex(), receipt.blockNumber,
            )
        else:
            log.info(
                "REDEEMED (no tokens): %s | tx: 0x%s (block %d) "
                "— no USDC.e Transfer to wallet (database-only?)",
                market, tx_hash.hex(), receipt.blockNumber,
            )
        return True, actual_usdc

    except Exception as e:
        log.error("Failed to redeem %s: %s", market, e)
        return False, 0.0


def redeem_all() -> tuple[int, float]:
    """Redeem all resolved positions. Returns (count_redeemed, total_usdc_received)."""
    if not CFG.private_key:
        log.warning("Skipping redemption: POLY_PRIVATE_KEY not configured")
        return 0, 0.0

    # Priority 4: burn guard — hard exit on null/zero wallet
    wallet = _guard_address(CFG.funder_address, "redeem_all")

    positions = _fetch_redeemable_positions()
    if not positions:
        log.info("No positions to redeem")
        return 0, 0.0

    log.info("Found %d redeemable position(s)", len(positions))

    w3 = _connect()
    if not w3:
        log.error("Cannot connect to Polygon RPC — skipping redemption")
        return 0, 0.0

    redeemed   = 0
    total_usdc = 0.0
    # Fetch nonce once, increment locally to avoid mempool collisions
    nonce      = w3.eth.get_transaction_count(wallet, "pending")
    gas_price  = w3.eth.gas_price

    for pos in positions:
        ok, usdc_received = _redeem_one(w3, wallet, pos, nonce, gas_price)
        if ok:
            redeemed   += 1
            total_usdc += usdc_received
        # Always advance nonce — even skipped/failed positions consume the slot
        # if a tx reached the mempool before the error.
        nonce += 1

    log.info(
        "Redemption complete: %d/%d positions ($%.4f USDC.e actual on-chain)",
        redeemed, len(positions), total_usdc,
    )
    return redeemed, total_usdc


async def redeem_all_async() -> tuple[int, float]:
    """Async wrapper — runs redeem_all() in a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, redeem_all)

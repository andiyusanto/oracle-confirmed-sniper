"""
Polymarket Position Redemption
================================
Redeems resolved winning positions back to USDC.e in the funder wallet.
Uses direct on-chain web3 calls — no Relayer or Builder API credentials needed.

Two market types handled:
  - Standard binary:  redeemPositions() on CTF contract
  - Neg-risk:         redeemPositions() on NegRisk adapter

Defenses active:
  - On-chain oracle guard         (payoutNumerators > 0 required — no ghost burns)
  - Settlement buffer             (180s after market end before on-chain check)
  - Null/zero address burn guard  (sys.exit — no fallback)
  - Dynamic gas escalator         (+20% every 60s, +50% final)
  - 3-block confirmation wait     (re-org safety)
  - Actual USDC.e Transfer parsing (not Data API estimate)
  - Idempotency set               (session-level double-redeem protection)
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
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADAPTER = Web3.to_checksum_address(
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
)
_NULL_ADDRESS = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")

DATA_API_RETRY_WAIT = 60

# ── Gas escalation ────────────────────────────────────────────────────
_GAS_CHECK_INTERVAL = 15  # poll receipt every N seconds
_GAS_ESCALATE_AFTER = 60  # first bump after this many seconds
_GAS_BUMP_INTERVAL = 60  # each subsequent bump interval
_GAS_BUMP_PCT = 0.20  # +20% per normal bump
_GAS_FINAL_PCT = 0.50  # +50% on final bump
_GAS_MAX_BUMPS = 3
_GAS_TOTAL_TIMEOUT = 300  # hard stop: 5 minutes

# ── Re-org safety ─────────────────────────────────────────────────────
_CONFIRM_BLOCKS = 3  # blocks to wait after receipt
_CONFIRM_TIMEOUT = 30  # seconds max to wait

# ── USDC.e Transfer event ABI ─────────────────────────────────────────
USDC_TRANSFER_ABI = [
    {
        "name": "Transfer",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "from", "type": "address", "indexed": True},
            {"name": "to", "type": "address", "indexed": True},
            {"name": "value", "type": "uint256", "indexed": False},
        ],
    }
]

# ── Settlement buffer ─────────────────────────────────────────────────
# Minimum seconds after market end before attempting any on-chain check.
# Gives the oracle time to submit the report without hammering RPC.
_SETTLEMENT_BUFFER_SEC = 180

# ── Contract ABIs ─────────────────────────────────────────────────────
CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "payoutNumerators",
        "type": "function",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "index", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "getOutcomeSlotCount",
        "type": "function",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

NEG_RISK_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amounts", "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "payoutNumerators",
        "type": "function",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "index", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
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


# ── On-chain Oracle Resolution Gate ──────────────────────────────────


def _check_oracle_resolved(
    w3: Web3,
    contract_addr: str,
    abi: list,
    condition_id: bytes,
    outcome_index: int,
    cid_hex: str,
) -> tuple[bool, int, bool, bool]:
    """Check payoutNumerators on-chain before any redemption attempt.

    Returns (our_outcome_resolved, payout_numerator, oracle_resolved_against_us, is_cancelled).

      (True,  >0, False, False) → we won — safe to redeem
      (True,  >0, False, True)  → market cancelled (BOTH sides paid) — stake returned, not profit
      (False,  0, True,  False) → oracle settled but our outcome got nothing — LOST, skip permanently
      (False,  0, False, False) → oracle not yet settled — retry later

    Falls back to getOutcomeSlotCount (standard CTF only) to detect whether
    the condition was ever registered. If the RPC call itself fails, we block
    to prevent a burn on uncertain state.
    """
    try:
        contract = w3.eth.contract(address=contract_addr, abi=abi)
        payout = contract.functions.payoutNumerators(condition_id, outcome_index).call()
        if payout > 0:
            # Our side has payout — check if the OTHER side also paid (cancellation).
            # A normal WIN has only the winning side paid. A cancelled/voided market
            # pays out both YES and NO sides equally (stake returned, no profit).
            other_index = 1 - outcome_index  # flips 0↔1 for binary markets
            try:
                other_payout = contract.functions.payoutNumerators(
                    condition_id, other_index
                ).call()
                if other_payout > 0:
                    log.warning(
                        "[GUARD] conditionId=%s BOTH outcomes have payout "
                        "(our index=%d payout=%d, other index=%d payout=%d) "
                        "— market was CANCELLED, stake will be returned (not profit)",
                        cid_hex[:18],
                        outcome_index,
                        payout,
                        other_index,
                        other_payout,
                    )
                    return True, payout, False, True
            except Exception:
                pass  # cannot confirm — treat as normal WIN
            return True, payout, False, False

        # Our outcome payout is 0. Check if the OTHER outcome(s) have been paid
        # to distinguish "oracle not yet settled" from "oracle settled against us".
        # For a binary market (2 outcomes), check the opposite index.
        other_index = 1 - outcome_index  # flips 0↔1 for binary markets
        try:
            other_payout = contract.functions.payoutNumerators(
                condition_id, other_index
            ).call()
            if other_payout > 0:
                # Oracle HAS resolved — but not in our favour (we hold the losing side)
                log.info(
                    "[GUARD] conditionId=%s resolved AGAINST us "
                    "(our index=%d payout=0, other index=%d payout=%d) "
                    "— position is a LOSS, skipping permanently",
                    cid_hex[:18],
                    outcome_index,
                    other_index,
                    other_payout,
                )
                return False, 0, True, False
        except Exception:
            pass  # binary check failed — fall through to pending logic

        # Fallback: verify condition exists at all (standard CTF only)
        if "getOutcomeSlotCount" in [f["name"] for f in abi]:
            try:
                slots = contract.functions.getOutcomeSlotCount(condition_id).call()
                if slots == 0:
                    log.warning(
                        "[GUARD] conditionId=%s not registered on CTF "
                        "(getOutcomeSlotCount=0) — blocking redemption",
                        cid_hex[:18],
                    )
            except Exception:
                pass

        log.info("[GUARD] Waiting for Oracle resolution (conditionId=%s)", cid_hex[:18])
        return False, 0, False, False

    except Exception as e:
        log.warning(
            "[GUARD] payoutNumerators RPC call failed (conditionId=%s): %s "
            "— blocking redemption on uncertain state",
            cid_hex[:18],
            e,
        )
        return False, 0, False, False


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
                "user": CFG.funder_address,
                "redeemable": "true",
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
                    "user": CFG.funder_address,
                    "redeemable": "true",
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


def _wait_with_escalation(
    w3: Web3, tx_hash, tx_dict: dict, nonce: int, initial_gas: int
):
    """Wait for receipt with automatic gas bumps if the tx is stuck.

    Schedule:
      t+60s  → bump #1: +20%
      t+120s → bump #2: +20%
      t+180s → bump #3: +50% of original (final)
      t+300s → give up, return None

    Returns the receipt, or None on total timeout.
    """
    sent_at = time.time()
    bumps = 0
    current_gas = initial_gas
    current_hash = tx_hash
    next_escalate = sent_at + _GAS_ESCALATE_AFTER

    while True:
        elapsed = time.time() - sent_at
        if elapsed >= _GAS_TOTAL_TIMEOUT:
            log.error(
                "TX TIMEOUT after %.0fs (%d bump(s)) — nonce %d may be stuck. "
                "Verify on Polygonscan and re-run redeem_now.py if needed.",
                elapsed,
                bumps,
                nonce,
            )
            return None

        # Poll for receipt with a short window so we can check escalation
        try:
            receipt = w3.eth.wait_for_transaction_receipt(
                current_hash, timeout=_GAS_CHECK_INTERVAL
            )
            if bumps > 0:
                log.info(
                    "TX confirmed after %d bump(s) (%.0fs total)",
                    bumps,
                    time.time() - sent_at,
                )
            return receipt
        except Exception:
            pass  # not mined yet

        # Escalate if due and still under limit
        now = time.time()
        if now >= next_escalate and bumps < _GAS_MAX_BUMPS:
            if bumps < _GAS_MAX_BUMPS - 1:
                new_gas = int(current_gas * (1 + _GAS_BUMP_PCT))
                label = f"+{int(_GAS_BUMP_PCT * 100)}%"
            else:
                new_gas = int(initial_gas * (1 + _GAS_FINAL_PCT))
                label = "+50% FINAL"

            try:
                replacement = dict(tx_dict)
                replacement["gasPrice"] = new_gas
                signed = w3.eth.account.sign_transaction(replacement, CFG.private_key)
                new_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                bumps += 1
                current_gas = new_gas
                current_hash = new_hash
                next_escalate = now + _GAS_BUMP_INTERVAL
                log.warning(
                    "GAS BUMP #%d (%s) → %d gwei | tx: 0x%s",
                    bumps,
                    label,
                    new_gas // 10**9,
                    new_hash.hex(),
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
    target_block = receipt.blockNumber + _CONFIRM_BLOCKS
    while True:
        current = w3.eth.block_number
        confs = current - receipt.blockNumber
        if current >= target_block:
            log.debug("Confirmed %d blocks (block %d)", confs, receipt.blockNumber)
            return True
        if time.time() >= deadline:
            log.warning(
                "Confirmation timeout: %d/%d blocks — proceeding anyway",
                confs,
                _CONFIRM_BLOCKS,
            )
            return False
        time.sleep(2)


# ── Core redemption ───────────────────────────────────────────────────


def _redeem_one(
    w3: Web3, wallet: str, pos: dict, nonce: int, gas_price: int, force: bool = False
) -> tuple[bool, float, bool, bool, bool]:
    """Submit a redemption tx for a single position.

    Returns (success, actual_usdc_received, nonce_consumed, definitely_lost, is_cancelled).

    nonce_consumed=True   → a tx was sent to the mempool; caller must advance nonce.
    nonce_consumed=False  → no tx was submitted; caller must NOT advance nonce.
    definitely_lost=True  → oracle confirmed the other outcome won; caller should
                            add this conditionId to _redeemed_cids permanently so
                            future scans skip it without an RPC call.
    is_cancelled=True     → both YES and NO outcomes have payout (market was voided);
                            the redemption returns the original stake (not profit).
                            Caller must reverse any previously recorded PnL for this
                            conditionId — WIN PnL overstated, LOSS stake was refunded.

    Oracle guard: payoutNumerators is read on-chain BEFORE any tx is built.
    If the oracle has not reported, the position is blocked and returned as
    (False, 0.0, False, False, False) WITHOUT burning any tokens — the caller retries later.

    force=True: skip settlement buffer and oracle guard — submits the tx regardless.
    Use only when you know the market has resolved and want to bypass checks.
    WARNING: if the oracle settled against you, the tx will succeed but return $0.
    """
    cid = pos.get("conditionId", pos.get("condition_id", ""))
    if not cid:
        return False, 0.0, False, False, False
    if not cid.startswith("0x"):
        cid = "0x" + cid

    condition_id = bytes.fromhex(cid[2:])
    neg_risk = pos.get("negativeRisk")
    market = pos.get("title", cid[:12])
    outcome_index = int(pos.get("outcomeIndex", 0))

    # Unsupported market type — checked early to avoid wasting an RPC call
    if neg_risk not in (True, False):
        log.warning(
            "Skipping %s: unsupported market type (negativeRisk=%r)", market, neg_risk
        )
        return False, 0.0, False, False, False

    _is_cancelled = False

    if force:
        log.warning(
            "[FORCE] Skipping oracle guard for %s (conditionId=%s)",
            market,
            cid[:18],
        )
    else:
        # ── Settlement buffer: don't spam RPC immediately after end_time ──
        end_time = pos.get("endTime") or pos.get("end_time")
        if end_time:
            try:
                elapsed_since_end = time.time() - float(end_time)
                if elapsed_since_end < _SETTLEMENT_BUFFER_SEC:
                    log.info(
                        "[GUARD] Waiting for Oracle resolution (conditionId=%s) "
                        "— only %.0fs since market end (buffer=%ds)",
                        cid[:18],
                        elapsed_since_end,
                        _SETTLEMENT_BUFFER_SEC,
                    )
                    return False, 0.0, False, False, False
            except (TypeError, ValueError):
                pass  # endTime missing or not a number — skip buffer check

        # ── PRIMARY GUARD: on-chain payoutNumerators check ─────────────────
        # Must pass before ANY transaction is built. If the oracle has not
        # called reportPayouts, payoutNumerator == 0 and we would burn tokens
        # for zero USDC. Block here and let the caller retry.
        # If the other outcome already has payout > 0, this is a confirmed LOSS.
        # If BOTH outcomes have payout > 0, the market was cancelled — set
        # _is_cancelled so the caller can reverse any previously recorded PnL.
        guard_addr = NEG_RISK_ADAPTER if neg_risk is True else CTF_ADDRESS
        guard_abi = NEG_RISK_ABI if neg_risk is True else CTF_ABI
        resolved, _, definitely_lost, _is_cancelled = _check_oracle_resolved(
            w3, guard_addr, guard_abi, condition_id, outcome_index, cid
        )
        if not resolved:
            if definitely_lost:
                # Oracle confirmed we lost — no USDC recoverable, stop scanning this cid
                return False, 0.0, False, True, False
            log.warning(
                "[BLOCKED] Ghost redemption prevented — payoutNumerator=0 "
                "(conditionId=%s market=%s)",
                cid[:18],
                market,
            )
            return False, 0.0, False, False, False

    # ── Build and submit transaction ───────────────────────────────────
    # After this point a tx may reach the mempool → nonce_consumed = True.
    _nonce_consumed = False
    try:
        if neg_risk is True:
            size_raw = int(float(pos.get("size", 0)) * 1e6)
            amounts = [0, 0]
            amounts[outcome_index] = size_raw

            contract = w3.eth.contract(address=NEG_RISK_ADAPTER, abi=NEG_RISK_ABI)
            tx = contract.functions.redeemPositions(
                condition_id, amounts
            ).build_transaction(
                {
                    "from": wallet,
                    "nonce": nonce,
                    "gas": 300_000,
                    "gasPrice": gas_price,
                    "chainId": 137,
                }
            )

        else:  # neg_risk is False — standard binary CTF
            contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
            tx = contract.functions.redeemPositions(
                USDC_ADDRESS, b"\x00" * 32, condition_id, [1, 2]
            ).build_transaction(
                {
                    "from": wallet,
                    "nonce": nonce,
                    "gas": 300_000,
                    "gasPrice": gas_price,
                    "chainId": 137,
                }
            )

        signed = w3.eth.account.sign_transaction(tx, CFG.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        _nonce_consumed = True  # tx is in the mempool from this point

        # Priority 1: escalate gas if stuck
        receipt = _wait_with_escalation(w3, tx_hash, tx, nonce, gas_price)
        if receipt is None:
            return False, 0.0, True, False, False

        if receipt.status != 1:
            log.error("Redeem REVERTED: %s | tx: 0x%s", market, tx_hash.hex())
            return False, 0.0, True, False, False

        # Priority 6: 3-block confirmation before parsing payout
        _wait_confirmations(w3, receipt)

        actual_usdc = _parse_usdc_received(w3, receipt, wallet)
        if actual_usdc > 0:
            log.info(
                "REDEEMED%s: %s | $%.4f USDC.e | tx: 0x%s (block %d)",
                " [CANCELLED-MARKET]" if _is_cancelled else "",
                market,
                actual_usdc,
                tx_hash.hex(),
                receipt.blockNumber,
            )
        else:
            log.info(
                "REDEEMED (no tokens): %s | tx: 0x%s (block %d) "
                "— no USDC.e Transfer to wallet (database-only?)",
                market,
                tx_hash.hex(),
                receipt.blockNumber,
            )
        return True, actual_usdc, True, False, _is_cancelled

    except Exception as e:
        log.error("Failed to redeem %s: %s", market, e)
        return False, 0.0, _nonce_consumed, False, False


_redeemed_cids: set[str] = set()  # session-level guard against double-redemption


def redeem_all(force: bool = False) -> tuple[int, float, list[str], list[str]]:
    """Redeem all resolved positions.

    Returns (count_redeemed, total_usdc_received, confirmed_losses, cancelled_cids).

    confirmed_losses  — conditionIds where oracle confirmed we lost (WIN→LOSS correction).
    cancelled_cids    — conditionIds where BOTH outcomes paid out (market voided); any
                        previously recorded PnL (WIN profit or LOSS stake) must be reversed
                        because the actual payout was the stake returned, not a profit.

    force=True skips the settlement buffer and on-chain oracle guard.
    Use when positions are stuck in the redeemable list and normal checks block them.
    WARNING: a forced tx that the oracle settled against you will succeed on-chain
    but return $0 USDC.e — the tokens are burned, not recovered.
    """
    if not CFG.private_key:
        log.warning("Skipping redemption: POLY_PRIVATE_KEY not configured")
        return 0, 0.0, [], []

    # Priority 4: burn guard — hard exit on null/zero wallet
    wallet = _guard_address(CFG.funder_address, "redeem_all")

    positions = _fetch_redeemable_positions()
    if not positions:
        log.info("No positions to redeem")
        return 0, 0.0, [], []

    # Filter positions already redeemed this session — Data API can lag
    # and return the same position again before on-chain state propagates.
    skipped = []
    filtered = []
    for p in positions:
        cid_key = p.get("conditionId", p.get("condition_id", ""))
        if cid_key in _redeemed_cids:
            skipped.append(cid_key)
        else:
            filtered.append(p)
    for cid_key in skipped:
        log.info("[SKIP] Already redeemed conditionId %s", cid_key[:18])
    positions = filtered
    if not positions:
        log.info("No positions to redeem")
        return 0, 0.0, [], []

    log.info("Found %d redeemable position(s)", len(positions))

    w3 = _connect()
    if not w3:
        log.error("Cannot connect to Polygon RPC — skipping redemption")
        return 0, 0.0, [], []

    redeemed = 0
    total_usdc = 0.0
    confirmed_losses: list[str] = []
    cancelled_cids: list[str] = []
    # Fetch nonce once, increment locally to avoid mempool collisions
    nonce = w3.eth.get_transaction_count(wallet, "pending")
    gas_price = w3.eth.gas_price

    for pos in positions:
        ok, usdc_received, nonce_consumed, definitely_lost, is_cancelled = _redeem_one(
            w3, wallet, pos, nonce, gas_price, force=force
        )
        cid = pos.get("conditionId", pos.get("condition_id", ""))
        if ok:
            redeemed += 1
            total_usdc += usdc_received
            if cid:
                _redeemed_cids.add(cid)
            if is_cancelled and cid:
                # Market was voided — both sides got payout. Any PnL the bot
                # recorded (WIN profit or LOSS stake debit) must be reversed.
                cancelled_cids.append(cid)
                log.warning(
                    "[CANCEL] Market cancelled for conditionId=%s — "
                    "stake returned ($%.4f); caller will reverse recorded PnL",
                    cid[:18],
                    usdc_received,
                )
        elif definitely_lost and cid:
            # Oracle confirmed our outcome lost — no USDC.e recoverable.
            # Add to session skip-set so future periodic scans don't waste
            # an RPC call re-checking this position every 15 minutes.
            _redeemed_cids.add(cid)
            confirmed_losses.append(cid)
            log.info("[SKIP] Confirmed LOSS added to skip-set: %s", cid[:18])
        # Advance nonce ONLY if a tx reached the mempool — oracle-blocked
        # positions never submit, so their nonce slot must not be consumed.
        if nonce_consumed:
            nonce += 1

    log.info(
        "Redemption complete: %d/%d positions ($%.4f USDC.e actual on-chain)",
        redeemed,
        len(positions),
        total_usdc,
    )
    return redeemed, total_usdc, confirmed_losses, cancelled_cids


async def redeem_all_async(
    force: bool = False,
) -> tuple[int, float, list[str], list[str]]:
    """Async wrapper — runs redeem_all() in a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: redeem_all(force=force))

"""
On-chain Reconciliation
========================
For a chosen date range, compare every trade in `hybrid_trades.db` against
real pUSD / USDC.e ERC20 Transfer events on Polygon. Answers a single
question per trade: did money actually move?

Classifications
---------------
- REAL_FILL    : pUSD-out near opened_at within tolerance of size_usdc
- PHANTOM      : DB marked OPEN/EXPIRED but no on-chain spend
                 (the bug fixed by FOK + ROUND_UP in execution/executor.py)
- PARTIAL      : pUSD-out present but mismatched size by >10%
- GHOST_CLOSE  : on-chain spend with no matching DB row (rare)
- CANCELLED_OK : DB CANCELLED and no on-chain spend — expected

Usage
-----
    python -m analysis.reconcile --since 2026-05-07 --until 2026-05-16

Outputs `analysis/reconcile_<since>_to_<until>.csv` and prints a summary.

Notes
-----
- Reads from EOA derived from POLY_PRIVATE_KEY (sig_type=0, NOT a proxy).
- Uses free public Polygon RPCs. Chunks getLogs into ~3-day spans to avoid
  range limits and rate-limits between chunks.
- Block timestamps are second-precision; trade timestamps sub-second. ±90s
  matching window is wide enough given the bot's cooldown_sec gate.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from web3 import Web3

from core.config import CFG

log = logging.getLogger("reconcile")

# ── On-chain constants ───────────────────────────────────────────────
# Ordered for archive friendliness — publicnode is intentionally LAST
# because it prunes historical blocks (eth_getBlockByNumber and getLogs
# fail on anything older than ~128 blocks).
POLYGON_RPCS = [
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",  # pruned — last resort
]
# Average Polygon block time used to estimate block numbers from
# timestamps without needing a historical block lookup (which pruned
# RPCs reject). 2.1s is a stable long-run figure.
POLYGON_AVG_BLOCK_SEC = 2.1
PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# keccak("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Polygon ~2s block time. 10k blocks ≈ 6 hours — small enough that free
# RPCs accept it (drpc/llamarpc/1rpc free tiers reject ranges much larger).
# Scanning ~3 weeks at this chunk size = ~80 chunks per token ≈ 2-3 minutes.
CHUNK_BLOCKS = 10_000

# Match tolerance: trade.size_usdc vs on-chain amount.
# Bot rounds shares to 0.01 with ROUND_DOWN, then FOK can return slightly
# different filled amount. 10% covers any realistic deviation.
SIZE_TOL_FRAC = 0.10

# Time window around opened_at for matching pUSD-out events.
# Generous because we estimate block timestamps arithmetically (~2.1s
# per block) instead of fetching them — short-term jitter can put the
# estimate ±30s off. The bot's cooldown_sec gate prevents back-to-back
# trades within this window so ambiguity is not a real concern.
MATCH_WINDOW_SEC = 180


@dataclass
class Transfer:
    block: int
    ts: int
    tx: str
    direction: str  # "OUT" or "IN"
    counterparty: str
    amount_usdc: float
    token: str  # "pUSD" or "USDC.e"


# ── Web3 plumbing ────────────────────────────────────────────────────


def _inject_poa_middleware(w3: Web3) -> None:
    """Polygon is a PoA chain with extended `extraData` — web3.py raises
    `ExtraDataLengthError` on get_block() without this middleware. The
    import path differs across web3.py versions; try both."""
    try:
        # web3.py >= 6.x
        from web3.middleware import ExtraDataToPOAMiddleware

        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        return
    except ImportError:
        pass
    try:
        # web3.py 5.x / early 6.x
        from web3.middleware import geth_poa_middleware

        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    except ImportError:
        log.warning(
            "No PoA middleware found in web3.py — get_block() may fail "
            "on Polygon's extended extraData field."
        )


def connect(skip: Optional[set[str]] = None) -> tuple[Web3, str]:
    """Connect to the first reachable Polygon RPC not in `skip`.

    Returns (web3, rpc_url) so the caller can rotate to a different RPC
    when the current one returns pruning errors on historical queries.
    """
    skip = skip or set()
    for rpc in POLYGON_RPCS:
        if rpc in skip:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
            if w3.is_connected():
                _inject_poa_middleware(w3)
                log.info("RPC: %s", rpc)
                return w3, rpc
        except Exception:
            continue
    raise RuntimeError("No Polygon RPC reachable")


def eoa_from_pk(pk: str) -> str:
    from eth_account import Account

    return Account.from_key(pk).address


def _head_block_and_ts(w3: Web3) -> tuple[int, int]:
    """Get a (block_number, timestamp) pair for a recent block.

    Some free RPCs return `eth_blockNumber` for a head their own
    `eth_getBlockByNumber` can't yet serve (race between nodes in a load
    balancer pool). Try `latest`, then walk back a few stride sizes
    before giving up so the caller can rotate.
    """
    # Try the canonical "latest" first — usually safest, the same RPC will
    # return whatever block it actually has.
    try:
        blk = w3.eth.get_block("latest")
        return blk.number, blk.timestamp
    except Exception:
        pass
    head = w3.eth.block_number
    for back in (0, 5, 50, 500, 5_000):
        try:
            blk = w3.eth.get_block(max(1, head - back))
            return blk.number, blk.timestamp
        except Exception:
            continue
    raise RuntimeError("Cannot fetch a head block from this RPC")


def block_for_ts(w3: Web3, target_ts: int) -> int:
    """Estimate the block number at a target Unix timestamp.

    Uses arithmetic from chain head — head_block - (head_ts - target_ts) / 2.1.
    The previous binary search did `get_block(mid)` lookups against
    historical blocks, which pruned public RPCs reject with -32701
    ("History has been pruned"). The old code's `except: return lo`
    silently returned 1, producing a 1→1 scan and zero matches.

    Slight imprecision is fine: getLogs is then run over [start, end] of
    blocks and individual log matches are timestamped per-block on demand.
    A few hundred blocks of slack on either side costs nothing.
    """
    head, head_ts = _head_block_and_ts(w3)
    delta_sec = head_ts - target_ts
    blocks_back = int(delta_sec / POLYGON_AVG_BLOCK_SEC)
    return max(1, head - blocks_back)


def _addr_topic(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


class RpcRotator:
    """Holds a live web3 + tries fresh RPCs on pruning / fatal errors."""

    def __init__(self, w3: Web3, rpc: str):
        self.w3 = w3
        self.rpc = rpc
        self.dead: set[str] = set()
        self._head_block: Optional[int] = None
        self._head_ts: Optional[int] = None

    def rotate(self) -> bool:
        """Switch to a different RPC. Returns False if no more available."""
        self.dead.add(self.rpc)
        try:
            self.w3, self.rpc = connect(skip=self.dead)
        except RuntimeError:
            return False
        # Invalidate head cache — different RPC, may differ slightly.
        self._head_block = None
        return True

    def estimate_ts(self, block_num: int) -> int:
        """Estimate a block's timestamp without calling get_block."""
        if self._head_block is None:
            self._head_block = self.w3.eth.block_number
            self._head_ts = self.w3.eth.get_block(self._head_block).timestamp
        delta_blocks = self._head_block - block_num
        return int(self._head_ts - delta_blocks * POLYGON_AVG_BLOCK_SEC)


def _is_pruned_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "pruned" in msg or "-32701" in msg or "history" in msg


def _is_range_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "range" in msg
        or "limit" in msg
        or "exceed" in msg
        or "too large" in msg
        or "too many" in msg
        or "response size" in msg
        or "query returned more" in msg
    )


def _is_bad_request(e: Exception) -> bool:
    """400-class errors from free RPCs that don't say WHY they refused.
    Treat as 'this RPC doesn't want to serve us' → rotate."""
    msg = str(e).lower()
    return (
        "400 client error" in msg
        or "bad request" in msg
        or "403" in msg
        or "forbidden" in msg
        or "unauthorized" in msg
        or "rate limit" in msg
        or "429" in msg
    )


def fetch_transfers(
    rpc: RpcRotator,
    wallet: str,
    token_addr: str,
    token_label: str,
    start_block: int,
    end_block: int,
) -> list[Transfer]:
    """Pull ALL ERC20 Transfers IN+OUT for `wallet` on `token_addr`.

    Rotates RPC automatically on -32701 (history pruned). Estimates each
    log's timestamp from arithmetic instead of per-block lookups (which
    public RPCs frequently refuse on pruned ranges).
    """
    wallet_cs = Web3.to_checksum_address(wallet)
    topic_wallet = _addr_topic(wallet_cs)
    transfers: list[Transfer] = []

    def _get_logs(from_b: int, to_b: int, indexed_pos: int) -> list:
        """indexed_pos=1 → from==wallet (OUT); 2 → to==wallet (IN)."""
        topics: list = [TRANSFER_TOPIC, None, None]
        topics[indexed_pos] = topic_wallet
        while topics and topics[-1] is None:
            topics.pop()
        return rpc.w3.eth.get_logs(
            {
                "fromBlock": from_b,
                "toBlock": to_b,
                "address": token_addr,
                "topics": topics,
            }
        )

    cursor = start_block
    current_chunk = CHUNK_BLOCKS
    while cursor <= end_block:
        chunk_end = min(cursor + current_chunk - 1, end_block)
        for indexed_pos, direction in ((1, "OUT"), (2, "IN")):
            logs = None
            for attempt in range(6):
                try:
                    logs = _get_logs(cursor, chunk_end, indexed_pos)
                    break
                except Exception as e:
                    if _is_pruned_error(e):
                        log.warning("RPC %s prunes this range — rotating", rpc.rpc)
                        if not rpc.rotate():
                            log.error(
                                "All RPCs exhausted on pruning errors. "
                                "Use a paid archive RPC (Alchemy/Infura)."
                            )
                            return transfers
                        continue
                    if _is_bad_request(e):
                        # Free RPCs that refuse the request without a reason —
                        # try a smaller chunk once, then rotate.
                        if current_chunk > 2000:
                            new_chunk = max(2000, current_chunk // 2)
                            chunk_end = cursor + new_chunk - 1
                            current_chunk = new_chunk
                            log.warning(
                                "RPC %s 400 Bad Request — shrinking to %d "
                                "blocks before rotating",
                                rpc.rpc,
                                new_chunk,
                            )
                            continue
                        log.warning(
                            "RPC %s rejects 2k-block chunks — rotating", rpc.rpc
                        )
                        if not rpc.rotate():
                            log.error(
                                "All RPCs refused the request. "
                                "Try a paid RPC (Alchemy/Infura free tier works)."
                            )
                            return transfers
                        # After rotate, retry with original chunk size on new RPC.
                        current_chunk = CHUNK_BLOCKS
                        chunk_end = min(cursor + current_chunk - 1, end_block)
                        continue
                    if _is_range_error(e):
                        new_chunk = max(1000, (chunk_end - cursor + 1) // 2)
                        chunk_end = cursor + new_chunk - 1
                        current_chunk = new_chunk
                        log.warning(
                            "RPC range limit, shrinking chunk to %d blocks",
                            new_chunk,
                        )
                        continue
                    log.warning(
                        "getLogs %s %s-%s attempt %d: %s",
                        token_label,
                        cursor,
                        chunk_end,
                        attempt + 1,
                        e,
                    )
                    time.sleep(1.5 * (attempt + 1))
            if logs is None:
                log.error(
                    "Giving up on %s blocks %s-%s %s",
                    token_label,
                    cursor,
                    chunk_end,
                    direction,
                )
                continue

            for lg in logs:
                blk = lg["blockNumber"]
                ts = rpc.estimate_ts(blk)
                from_addr = "0x" + lg["topics"][1].hex()[-40:]
                to_addr = "0x" + lg["topics"][2].hex()[-40:]
                counterparty = to_addr if direction == "OUT" else from_addr
                amount_raw = int(lg["data"].hex() or "0x0", 16)
                transfers.append(
                    Transfer(
                        block=blk,
                        ts=ts,
                        tx=lg["transactionHash"].hex(),
                        direction=direction,
                        counterparty=Web3.to_checksum_address(counterparty),
                        amount_usdc=amount_raw / 1e6,
                        token=token_label,
                    )
                )
        log.info(
            "  %s: scanned blocks %d-%d (%d transfers so far)",
            token_label,
            cursor,
            chunk_end,
            len(transfers),
        )
        cursor = chunk_end + 1
        time.sleep(0.4)  # be polite to public RPCs

    return transfers


# ── Matching ─────────────────────────────────────────────────────────


def classify(trade: dict, transfers: list[Transfer]) -> tuple[str, Optional[Transfer]]:
    """Find the best on-chain match for one DB trade row."""
    opened = trade["opened_at"]
    size = trade["size_usdc"] or 0.0
    status = trade["status"]

    # Candidates: OUT transfers within ±MATCH_WINDOW_SEC of opened_at.
    # pUSD is V2 collateral; USDC.e is legacy. Either could appear.
    candidates = [
        t
        for t in transfers
        if t.direction == "OUT" and abs(t.ts - opened) <= MATCH_WINDOW_SEC
    ]

    # Best match: closest in size, then closest in time.
    def score(t: Transfer) -> tuple[float, float]:
        return (abs(t.amount_usdc - size), abs(t.ts - opened))

    candidates.sort(key=score)
    best = candidates[0] if candidates else None

    if status == "CANCELLED":
        # Expected: no on-chain spend. If we DO find one, that's suspicious.
        if best and abs(best.amount_usdc - size) <= max(0.05, size * SIZE_TOL_FRAC):
            return "GHOST_CLOSE", best
        return "CANCELLED_OK", None

    # status in (OPEN, EXPIRED): we expected a fill.
    if not best:
        return "PHANTOM", None

    diff_frac = abs(best.amount_usdc - size) / size if size > 0 else 1.0
    if diff_frac <= SIZE_TOL_FRAC:
        return "REAL_FILL", best
    return "PARTIAL", best


# ── DB ───────────────────────────────────────────────────────────────


def load_trades(db_path: str, since: float, until: float) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT id, asset, direction, side, entry_price, size_usdc,
               pnl, status, mode, opened_at, closed_at, condition_id
        FROM trades
        WHERE mode='LIVE' AND opened_at >= ? AND opened_at < ?
        ORDER BY opened_at
        """,
        (since, until),
    )
    return [dict(r) for r in cur.fetchall()]


# ── CLI ──────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--until", required=True, help="YYYY-MM-DD (UTC, exclusive)")
    p.add_argument("--db", default=CFG.db_path)
    p.add_argument(
        "--wallet",
        default=None,
        help="Override wallet address. Default: EOA from POLY_PRIVATE_KEY.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="CSV output path. Default: analysis/reconcile_<since>_to_<until>.csv",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args()

    since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    until_dt = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    since_ts = int(since_dt.timestamp())
    until_ts = int(until_dt.timestamp())

    if args.wallet:
        wallet = Web3.to_checksum_address(args.wallet)
    elif CFG.funder_address:
        wallet = Web3.to_checksum_address(CFG.funder_address)
    elif CFG.private_key:
        wallet = eoa_from_pk(CFG.private_key)
    else:
        log.error("No wallet: pass --wallet or set POLY_PRIVATE_KEY in .env")
        return 2

    log.info("Wallet:      %s", wallet)
    log.info("Period:      %s → %s (UTC)", args.since, args.until)
    log.info("DB:          %s", args.db)

    trades = load_trades(args.db, since_ts, until_ts)
    log.info("DB trades:   %d in range", len(trades))
    if not trades:
        log.warning("Nothing to reconcile. Done.")
        return 0

    # Pick an RPC that can actually serve a head block (some free RPCs
    # return eth_blockNumber for a block their getBlockByNumber can't yet
    # answer — rotate past those).
    dead: set[str] = set()
    while True:
        w3, rpc_url = connect(skip=dead)
        try:
            start_block = block_for_ts(w3, since_ts - 60)
            end_block = block_for_ts(w3, until_ts + 60)
            break
        except Exception as e:
            log.warning("RPC %s cannot serve head block (%s) — rotating", rpc_url, e)
            dead.add(rpc_url)
            if len(dead) >= len(POLYGON_RPCS):
                log.error("All RPCs failed to serve a head block. Aborting.")
                return 3
    log.info(
        "Block range: %d → %d (~%d blocks)",
        start_block,
        end_block,
        end_block - start_block,
    )

    rpc = RpcRotator(w3, rpc_url)
    log.info("Fetching pUSD transfers...")
    pusd_x = fetch_transfers(rpc, wallet, PUSD, "pUSD", start_block, end_block)
    log.info("Fetching USDC.e transfers...")
    usdce_x = fetch_transfers(rpc, wallet, USDC_E, "USDC.e", start_block, end_block)
    transfers = pusd_x + usdce_x
    log.info(
        "On-chain:    %d pUSD + %d USDC.e = %d transfers",
        len(pusd_x),
        len(usdce_x),
        len(transfers),
    )

    # Classify
    out_path = (
        Path(args.out)
        if args.out
        else Path("analysis") / f"reconcile_{args.since}_to_{args.until}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    matched_txs: set[str] = set()
    summary = {
        "REAL_FILL": 0,
        "PHANTOM": 0,
        "PARTIAL": 0,
        "GHOST_CLOSE": 0,
        "CANCELLED_OK": 0,
    }
    real_pnl = 0.0
    db_pnl = 0.0
    phantom_pnl = 0.0  # PnL booked on phantom trades — pure fiction

    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "trade_id",
                "opened_at_utc",
                "asset",
                "direction",
                "status",
                "db_size_usdc",
                "onchain_size_usdc",
                "db_pnl",
                "classification",
                "tx_hash",
                "token",
            ]
        )
        for tr in trades:
            cls, match = classify(tr, transfers)
            summary[cls] += 1
            db_pnl += tr["pnl"] or 0.0
            if cls == "REAL_FILL":
                real_pnl += tr["pnl"] or 0.0
            elif cls == "PHANTOM":
                phantom_pnl += tr["pnl"] or 0.0
            if match:
                matched_txs.add(match.tx)
            w.writerow(
                [
                    tr["id"],
                    datetime.fromtimestamp(tr["opened_at"], timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    tr["asset"],
                    tr["direction"],
                    tr["status"],
                    f"{tr['size_usdc']:.4f}" if tr["size_usdc"] else "0.0000",
                    f"{match.amount_usdc:.4f}" if match else "0.0000",
                    f"{(tr['pnl'] or 0.0):+.4f}",
                    cls,
                    match.tx if match else "",
                    match.token if match else "",
                ]
            )

    # ── Summary ──────────────────────────────────────────────────────
    n = len(trades)
    print()
    print("=" * 64)
    print(f" RECONCILIATION  {args.since} → {args.until}")
    print("=" * 64)
    print(f" Wallet:            {wallet}")
    print(f" DB trades:         {n}")
    for k in ("REAL_FILL", "PHANTOM", "PARTIAL", "GHOST_CLOSE", "CANCELLED_OK"):
        v = summary[k]
        pct = 100 * v / n if n else 0
        print(f"   {k:14}   {v:4d}   ({pct:5.1f}%)")
    print()
    print(f" DB-booked PnL:     ${db_pnl:+10.2f}")
    print(f" Real PnL (fills):  ${real_pnl:+10.2f}")
    print(f" Phantom PnL:       ${phantom_pnl:+10.2f}   ← fictional")
    print(f" Diff (db - real):  ${db_pnl - real_pnl:+10.2f}")
    print()
    # Unmatched on-chain OUTs — could be redemptions, wraps, or manual txs
    unmatched_out = [
        t for t in transfers if t.direction == "OUT" and t.tx not in matched_txs
    ]
    print(f" Unmatched on-chain OUTs: {len(unmatched_out)}")
    if unmatched_out:
        print("   (counterparties — likely redemptions, wraps, manual txs)")
        from collections import Counter

        cc = Counter(t.counterparty for t in unmatched_out)
        for addr, cnt in cc.most_common(8):
            tot = sum(t.amount_usdc for t in unmatched_out if t.counterparty == addr)
            print(f"     {addr}  {cnt:3d} tx  total=${tot:.2f}")
    print()
    print(f" CSV:  {out_path}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
]
PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# keccak("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Polygon ~2s block time. Use 3-day chunks for getLogs (≈130k blocks).
CHUNK_BLOCKS = 130_000

# Match tolerance: trade.size_usdc vs on-chain amount.
# Bot rounds shares to 0.01 with ROUND_DOWN, then FOK can return slightly
# different filled amount. 10% covers any realistic deviation.
SIZE_TOL_FRAC = 0.10

# Time window around opened_at for matching pUSD-out events.
MATCH_WINDOW_SEC = 90


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


def connect() -> Web3:
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
            if w3.is_connected():
                log.info("RPC: %s", rpc)
                return w3
        except Exception:
            continue
    raise RuntimeError("No Polygon RPC reachable")


def eoa_from_pk(pk: str) -> str:
    from eth_account import Account

    return Account.from_key(pk).address


def block_for_ts(w3: Web3, target_ts: int) -> int:
    """Binary search block number nearest to a Unix timestamp.

    Polygon has stable ~2s blocks but we don't trust that for cross-day
    spans (validator gaps happen). 8 round-trips is enough.
    """
    lo, hi = 1, w3.eth.block_number
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        try:
            blk_ts = w3.eth.get_block(mid).timestamp
        except Exception:
            return lo
        if blk_ts < target_ts:
            lo = mid
        else:
            hi = mid
    return hi


def _addr_topic(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def fetch_transfers(
    w3: Web3,
    wallet: str,
    token_addr: str,
    token_label: str,
    start_block: int,
    end_block: int,
) -> list[Transfer]:
    """Pull ALL ERC20 Transfers IN+OUT for `wallet` on `token_addr`."""
    wallet_cs = Web3.to_checksum_address(wallet)
    topic_wallet = _addr_topic(wallet_cs)
    transfers: list[Transfer] = []
    block_ts_cache: dict[int, int] = {}

    def _get_logs(from_b: int, to_b: int, indexed_pos: int) -> list:
        """indexed_pos=1 → from==wallet (OUT); 2 → to==wallet (IN)."""
        topics: list = [TRANSFER_TOPIC, None, None]
        topics[indexed_pos] = topic_wallet
        # Trim trailing Nones for cleaner request
        while topics and topics[-1] is None:
            topics.pop()
        return w3.eth.get_logs(
            {
                "fromBlock": from_b,
                "toBlock": to_b,
                "address": token_addr,
                "topics": topics,
            }
        )

    cursor = start_block
    while cursor <= end_block:
        chunk_end = min(cursor + CHUNK_BLOCKS - 1, end_block)
        for indexed_pos, direction in ((1, "OUT"), (2, "IN")):
            for attempt in range(4):
                try:
                    logs = _get_logs(cursor, chunk_end, indexed_pos)
                    break
                except Exception as e:
                    msg = str(e).lower()
                    if "range" in msg or "limit" in msg or "exceed" in msg:
                        # Shrink and retry
                        chunk_end = cursor + max(1, (chunk_end - cursor) // 2)
                        log.warning(
                            "RPC range limit, shrinking chunk to %d blocks",
                            chunk_end - cursor + 1,
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
            else:
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
                if blk not in block_ts_cache:
                    block_ts_cache[blk] = w3.eth.get_block(blk).timestamp
                from_addr = "0x" + lg["topics"][1].hex()[-40:]
                to_addr = "0x" + lg["topics"][2].hex()[-40:]
                counterparty = to_addr if direction == "OUT" else from_addr
                amount_raw = int(lg["data"].hex() or "0x0", 16)
                transfers.append(
                    Transfer(
                        block=blk,
                        ts=block_ts_cache[blk],
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

    w3 = connect()
    start_block = block_for_ts(w3, since_ts - 60)
    end_block = block_for_ts(w3, until_ts + 60)
    log.info(
        "Block range: %d → %d (~%d blocks)",
        start_block,
        end_block,
        end_block - start_block,
    )

    log.info("Fetching pUSD transfers...")
    pusd_x = fetch_transfers(w3, wallet, PUSD, "pUSD", start_block, end_block)
    log.info("Fetching USDC.e transfers...")
    usdce_x = fetch_transfers(w3, wallet, USDC_E, "USDC.e", start_block, end_block)
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

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

Data source
-----------
Polygonscan's `tokentx` REST endpoint — one paginated call per token
returns the entire ERC20 transfer history for a wallet, with block
timestamps included. This replaced the eth_getLogs approach because free
public Polygon RPCs are increasingly hostile to historical log queries
(pruning, 400 Bad Request on >1000-block chunks, etc).

No API key is required for ad-hoc runs (rate-limited to 1 req/5s by
Polygonscan). Set POLYGONSCAN_API_KEY in env for higher limits.

Wallet
------
Reads from EOA derived from POLY_PRIVATE_KEY (sig_type=0, NOT a proxy).
Override with --wallet for any other address.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from core.config import CFG

log = logging.getLogger("reconcile")

# ── Polygonscan ──────────────────────────────────────────────────────
POLYGONSCAN_URL = "https://api.polygonscan.com/api"
POLYGONSCAN_PAGE_SIZE = 10_000  # API max

# ── Token addresses ──────────────────────────────────────────────────
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Match tolerance: trade.size_usdc vs on-chain amount.
# Bot rounds shares to 0.01 with ROUND_DOWN, then FOK can return slightly
# different filled amount. 10% covers any realistic deviation.
SIZE_TOL_FRAC = 0.10

# Time window around opened_at for matching pUSD-out events.
# Polygonscan returns real block timestamps (no estimation needed) so we
# can keep this tight. The bot's cooldown_sec gate prevents back-to-back
# trades inside this window so ambiguity is not a concern.
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


def _checksum(addr: str) -> str:
    """Best-effort EIP-55 checksum without pulling in web3. We only
    use this for display; comparisons are done on lowercase."""
    return addr  # Polygonscan returns mixed case already; keep as-is.


def eoa_from_pk(pk: str) -> str:
    from eth_account import Account

    return Account.from_key(pk).address


# ── Polygonscan client ────────────────────────────────────────────────


def fetch_transfers(
    wallet: str,
    token_addr: str,
    token_label: str,
    since_ts: int,
    until_ts: int,
    api_key: str,
) -> list[Transfer]:
    """Fetch all ERC20 transfers for `wallet` on `token_addr` via Polygonscan.

    Paginates by page number until a page returns fewer than PAGE_SIZE
    records. Filters by timestamp client-side (Polygonscan accepts a block
    range, but we use 0..99999999 and filter to keep one source of truth
    on dates).
    """
    wallet_lc = wallet.lower()
    transfers: list[Transfer] = []
    page = 1
    while True:
        params = {
            "module": "account",
            "action": "tokentx",
            "address": wallet,
            "contractaddress": token_addr,
            "page": page,
            "offset": POLYGONSCAN_PAGE_SIZE,
            "startblock": 0,
            "endblock": 99_999_999,
            "sort": "asc",
        }
        if api_key:
            params["apikey"] = api_key

        log.info("  %s: fetching page %d...", token_label, page)
        for attempt in range(5):
            try:
                r = requests.get(POLYGONSCAN_URL, params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                wait = 5 * (attempt + 1)
                log.warning(
                    "Polygonscan %s p%d attempt %d failed (%s) — retrying in %ds",
                    token_label,
                    page,
                    attempt + 1,
                    e,
                    wait,
                )
                time.sleep(wait)
        else:
            log.error("Polygonscan %s p%d gave up after 5 attempts", token_label, page)
            break

        status = str(data.get("status"))
        message = str(data.get("message", ""))
        result = data.get("result", [])

        # status "0" + message "No transactions found" → empty result is OK.
        if status != "1":
            if isinstance(result, str) and "rate limit" in result.lower():
                log.warning("Polygonscan rate-limited — sleeping 10s")
                time.sleep(10)
                continue
            if "no transactions" in message.lower():
                log.info("  %s: no more transactions on page %d", token_label, page)
                break
            log.warning(
                "Polygonscan %s p%d: status=%s msg=%s",
                token_label,
                page,
                status,
                message,
            )
            break

        if not isinstance(result, list):
            log.warning("Unexpected result type for %s p%d", token_label, page)
            break

        n_window = 0
        for tx in result:
            try:
                ts = int(tx["timeStamp"])
            except (KeyError, ValueError):
                continue
            if ts < since_ts or ts >= until_ts:
                continue
            n_window += 1
            from_addr = tx.get("from", "").lower()
            to_addr = tx.get("to", "").lower()
            if from_addr == wallet_lc:
                direction = "OUT"
                counterparty = to_addr
            elif to_addr == wallet_lc:
                direction = "IN"
                counterparty = from_addr
            else:
                # Should not happen — Polygonscan only returns rows for this wallet.
                continue
            try:
                amount_raw = int(tx.get("value", "0"))
                decimals = int(tx.get("tokenDecimal", "6"))
            except ValueError:
                continue
            transfers.append(
                Transfer(
                    block=int(tx.get("blockNumber", "0")),
                    ts=ts,
                    tx=tx.get("hash", ""),
                    direction=direction,
                    counterparty=counterparty,
                    amount_usdc=amount_raw / (10**decimals),
                    token=token_label,
                )
            )

        log.info(
            "  %s: page %d returned %d rows (%d in window, %d total so far)",
            token_label,
            page,
            len(result),
            n_window,
            len(transfers),
        )

        # Don't bother paginating beyond the cutoff window. If the last row
        # on this page is past until_ts and we're sorting asc, there's
        # nothing newer worth keeping.
        if result and int(result[-1].get("timeStamp", 0)) >= until_ts:
            log.info("  %s: passed until_ts — stopping pagination", token_label)
            break

        if len(result) < POLYGONSCAN_PAGE_SIZE:
            break
        page += 1
        # Polite delay between pages (no-key tier is 1 req/5s)
        time.sleep(5 if not api_key else 0.3)

    # Verify we cover the requested window:
    if transfers:
        first_ts = min(t.ts for t in transfers)
        last_ts = max(t.ts for t in transfers)
        log.info(
            "  %s: %d transfers in [%s → %s]",
            token_label,
            len(transfers),
            datetime.fromtimestamp(first_ts, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            datetime.fromtimestamp(last_ts, timezone.utc).strftime("%Y-%m-%d %H:%M"),
        )
    else:
        log.warning("  %s: 0 transfers found in window", token_label)

    return transfers


# ── Matching ─────────────────────────────────────────────────────────


def classify(trade: dict, transfers: list[Transfer]) -> tuple[str, Optional[Transfer]]:
    """Find the best on-chain match for one DB trade row."""
    opened = trade["opened_at"]
    size = trade["size_usdc"] or 0.0
    status = trade["status"]

    # Candidates: OUT transfers within ±MATCH_WINDOW_SEC of opened_at.
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
    p.add_argument(
        "--api-key",
        default=os.getenv("POLYGONSCAN_API_KEY", ""),
        help="Polygonscan API key (optional). Falls back to POLYGONSCAN_API_KEY env var.",
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
        wallet = args.wallet
    elif CFG.funder_address:
        wallet = CFG.funder_address
    elif CFG.private_key:
        wallet = eoa_from_pk(CFG.private_key)
    else:
        log.error("No wallet: pass --wallet or set POLY_PRIVATE_KEY in .env")
        return 2

    log.info("Wallet:      %s", wallet)
    log.info("Period:      %s → %s (UTC)", args.since, args.until)
    log.info("DB:          %s", args.db)
    log.info(
        "API key:     %s",
        "set"
        if args.api_key
        else "not set (rate-limited 1 req/5s — set POLYGONSCAN_API_KEY for faster runs)",
    )

    trades = load_trades(args.db, since_ts, until_ts)
    log.info("DB trades:   %d in range", len(trades))
    if not trades:
        log.warning("Nothing to reconcile. Done.")
        return 0

    log.info("Fetching pUSD transfers from Polygonscan...")
    pusd_x = fetch_transfers(wallet, PUSD, "pUSD", since_ts, until_ts, args.api_key)
    log.info("Fetching USDC.e transfers from Polygonscan...")
    usdce_x = fetch_transfers(
        wallet, USDC_E, "USDC.e", since_ts, until_ts, args.api_key
    )
    transfers = pusd_x + usdce_x
    log.info(
        "On-chain:    %d pUSD + %d USDC.e = %d transfers",
        len(pusd_x),
        len(usdce_x),
        len(transfers),
    )

    # ── Classify ─────────────────────────────────────────────────────
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
    phantom_pnl = 0.0

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

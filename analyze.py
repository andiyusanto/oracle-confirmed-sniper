"""
Hybrid Sniper Trade Analyzer
Usage: python -m analysis.analyze [--days N] [--db hybrid_trades.db]
"""

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hybrid_trades.db")
    parser.add_argument("--days", type=int, default=0)
    args = parser.parse_args()

    if not Path(args.db).exists():
        console.print("[yellow]No database. Run the bot first.[/]")
        return

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    q = "SELECT * FROM trades WHERE status IN ('EXPIRED','CLOSED')"
    p = []
    if args.days > 0:
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=args.days)).timestamp()
        q += " AND opened_at >= ?"
        p.append(cutoff)
    q += " ORDER BY opened_at"

    trades = [dict(r) for r in conn.execute(q, p).fetchall()]
    if not trades:
        console.print("[yellow]No closed trades found.[/]")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    wr = len(wins) / len(trades) * 100 if trades else 0

    start = datetime.fromtimestamp(trades[0]["opened_at"], tz=timezone.utc).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(trades[-1]["opened_at"], tz=timezone.utc).strftime("%Y-%m-%d")

    console.print()
    console.print(Panel(
        f"[bold]Hybrid Sniper Analysis[/]  |  {len(trades)} trades  |  {start} -> {end}",
        style="bold cyan"))

    # Overall
    tc = "green" if total_pnl >= 0 else "red"
    s = Table(title="Overall performance", expand=True)
    s.add_column("Metric", style="bold"); s.add_column("Value", justify="right")
    s.add_row("Total P&L", f"[{tc}]${total_pnl:+,.4f}[/]")
    s.add_row("Win Rate", f"{wr:.1f}% ({len(wins)}/{len(trades)})")
    s.add_row("Expectancy", f"${total_pnl/len(trades):+,.4f}/trade" if trades else "—")
    if wins:
        s.add_row("Avg Win", f"${sum(t['pnl'] for t in wins)/len(wins):.4f}")
    if losses:
        s.add_row("Avg Loss", f"${sum(t['pnl'] for t in losses)/len(losses):.4f}")
    s.add_row("Avg Entry", f"${sum(t['entry_price'] for t in trades)/len(trades):.4f}")
    s.add_row("Avg Delta", f"{sum(abs(t['oracle_delta']) for t in trades)/len(trades):.4f}%")
    s.add_row("Avg Conf", f"{sum(t['confidence'] for t in trades)/len(trades):.1f}")
    s.add_row("Avg TTL", f"{sum(t['time_remaining'] for t in trades)/len(trades):.1f}s")
    console.print(s)

    # By combo
    _table("By asset + direction", trades, lambda t: f"{t['asset']}_{t['direction']}")
    # By entry price bucket
    _table("By entry price", trades, lambda t: f"${int(t['entry_price']*20)/20:.2f}")
    # By oracle delta bucket
    _table("By oracle delta", trades, lambda t: _delta_bucket(t["oracle_delta"]))
    # By time remaining bucket
    _table("By time remaining", trades, lambda t: _ttl_bucket(t["time_remaining"]))
    # By hour
    _table("By hour (UTC)", trades,
           lambda t: f"{datetime.fromtimestamp(t['opened_at'], tz=timezone.utc).hour:02d}:00")
    # Daily
    _table("Daily", trades,
           lambda t: datetime.fromtimestamp(t['opened_at'], tz=timezone.utc).strftime("%Y-%m-%d"))

    # Edge decay
    if len(trades) >= 10:
        first_half = trades[:len(trades)//2]
        second_half = trades[len(trades)//2:]
        wr1 = sum(1 for t in first_half if t["pnl"] > 0) / len(first_half) * 100
        wr2 = sum(1 for t in second_half if t["pnl"] > 0) / len(second_half) * 100
        delta = wr2 - wr1
        trend = "IMPROVING" if delta > 0 else "DECAYING"
        color = "green" if delta > 0 else "red"
        console.print(Panel(
            f"Edge [{color}]{trend}[/]: {wr1:.1f}% -> {wr2:.1f}% ({delta:+.1f}pp)",
            title="Edge decay analysis"))

    console.print()


def _table(title, trades, key_fn):
    groups = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)

    tbl = Table(title=title, expand=True)
    tbl.add_column("Group"); tbl.add_column("Trades", justify="right")
    tbl.add_column("WR%", justify="right"); tbl.add_column("P&L", justify="right")
    tbl.add_column("Avg Entry", justify="right"); tbl.add_column("PF", justify="right")

    for group, ts in sorted(groups.items()):
        w = sum(1 for t in ts if t["pnl"] > 0)
        pnl = sum(t["pnl"] for t in ts)
        gross_w = sum(t["pnl"] for t in ts if t["pnl"] > 0)
        gross_l = abs(sum(t["pnl"] for t in ts if t["pnl"] <= 0))
        pf = f"{gross_w/gross_l:.2f}" if gross_l > 0 else "inf"
        avg_e = sum(t["entry_price"] for t in ts) / len(ts)
        pc = "green" if pnl >= 0 else "red"
        tbl.add_row(group, str(len(ts)), f"{w/len(ts)*100:.1f}%",
                   f"[{pc}]${pnl:+,.4f}[/]", f"${avg_e:.3f}", pf)
    console.print(tbl)


def _delta_bucket(d):
    d = abs(d)
    if d < 0.02: return "<0.02%"
    if d < 0.03: return "0.02-0.03%"
    if d < 0.05: return "0.03-0.05%"
    if d < 0.10: return "0.05-0.10%"
    return ">0.10%"


def _ttl_bucket(t):
    if t < 10: return "<10s"
    if t < 20: return "10-20s"
    if t < 30: return "20-30s"
    if t < 45: return "30-45s"
    return "45-60s"


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Capital verification CLI.

Usage:
    python3 verify_capital.py             # Full audit report
    python3 verify_capital.py --fix       # Re-verify all EXPIRED trades and patch DB
    python3 verify_capital.py --export    # Export audit trail as CSV
    python3 verify_capital.py --snapshots # Show portfolio snapshots

The audit compares each trade's recorded P&L against the expected formula:
  WIN  : shares × (1 − fee_rate) × $1.00 − stake
  LOSS : −stake
  CLOSED (reversal exit): actual sell proceeds (already exact)
  CANCELLED: skipped (no capital moved)
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

# ── Project root on path ─────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from core.config import CFG
from core.database import Database
from core.capital_verifier import CapitalVerifier


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _severity_icon(s: str) -> str:
    return {"OK": "✓", "WARNING": "⚠", "CRITICAL": "✖"}.get(s, "?")


def audit_report(db: Database, cv: CapitalVerifier) -> None:
    """Print a human-readable audit report."""
    print("=" * 70)
    print("  CAPITAL VERIFICATION REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # ── Summary ───────────────────────────────────────────────────────
    summary = db.verification_summary()
    by_sev = summary["by_severity"]
    total = sum(by_sev.values())

    print(f"\n  Total verifications : {total}")
    for sev in ("OK", "WARNING", "CRITICAL"):
        n = by_sev.get(sev, 0)
        print(f"  {_severity_icon(sev)} {sev:<10} : {n}")

    print("\n  By outcome:")
    for outcome, info in summary["by_outcome"].items():
        print(
            f"    {outcome:<25} count={info['count']} avg_disc=${info['avg_disc']:.6f}"
        )

    # ── Recent verifications ──────────────────────────────────────────
    recent = db.recent_verifications(20)
    if recent:
        print("\n  Recent verifications (latest 20):")
        print(
            f"  {'Time':<20} {'Trade':<10} {'Outcome':<20} "
            f"{'Exp PnL':>9} {'Act PnL':>9} {'Disc':>9} Sev"
        )
        print("  " + "-" * 82)
        for v in recent:
            icon = _severity_icon(v["severity"])
            ts = _fmt_ts(v["timestamp"])
            print(
                f"  {ts:<20} {v['trade_id'][:8]:<10} {v['outcome']:<20} "
                f"${v['expected_pnl']:>8.4f} ${v['actual_pnl']:>8.4f} "
                f"${v['discrepancy']:>8.6f} {icon}{v['severity']}"
            )

    # ── Latest snapshot ───────────────────────────────────────────────
    snaps = db.recent_snapshots(1)
    if snaps:
        s = snaps[0]
        print(f"\n  Last portfolio snapshot ({_fmt_ts(s['timestamp'])}):")
        print(f"    Bot portfolio : ${s['portfolio']:.4f}")
        if s["clob_balance"] is not None:
            print(f"    CLOB balance  : ${s['clob_balance']:.4f}")
            print(f"    Discrepancy   : ${s['discrepancy']:.4f}")
        else:
            print("    CLOB balance  : (not available — run in live mode)")

    print()


def run_fix(db: Database, cv: CapitalVerifier) -> None:
    """Re-verify every EXPIRED/CLOSED live trade and patch verifications table."""
    print("Re-verifying all EXPIRED/CLOSED LIVE trades …")

    rows = db._rows(
        db.conn.execute(
            "SELECT id, asset, entry_price, size_usdc, pnl, status, mode "
            "FROM trades WHERE mode='LIVE' AND status IN ('EXPIRED','CLOSED')"
        )
    )

    ok = warn = crit = 0
    for r in rows:
        from core.models import Trade

        t = Trade(
            id=r["id"],
            asset=r["asset"],
            direction="UP",
            side="YES",
            entry_price=r["entry_price"],
            size_usdc=r["size_usdc"],
            oracle_delta=0,
            confidence=0,
            pnl=r["pnl"],
            status=r["status"],
            mode=r["mode"],
            opened_at=0,
            window_ts=0,
        )
        result = cv.verify_trade_close(t)
        sev = result["severity"]
        icon = _severity_icon(sev)
        if sev == "OK":
            ok += 1
        elif sev == "WARNING":
            warn += 1
            print(
                f"  {icon} {r['id'][:8]} {r['status']} "
                f"exp=${result['expected_pnl']:+.4f} "
                f"act=${result['actual_pnl']:+.4f} "
                f"disc=${result['discrepancy']:.6f}"
            )
        else:
            crit += 1
            print(
                f"  {icon} {r['id'][:8]} {r['status']} "
                f"exp=${result['expected_pnl']:+.4f} "
                f"act=${result['actual_pnl']:+.4f} "
                f"disc=${result['discrepancy']:.6f} ← CRITICAL"
            )

    print(f"\nDone. OK={ok} WARN={warn} CRIT={crit}")
    if crit:
        print("CRITICAL discrepancies require manual review.")
        sys.exit(1)


def export_csv(db: Database) -> None:
    """Export all verifications to CSV."""
    rows = db._rows(
        db.conn.execute("SELECT * FROM capital_verifications ORDER BY timestamp")
    )
    if not rows:
        print("No verifications recorded yet.")
        return

    path = Path("capital_audit.csv")
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} rows to {path}")


def show_snapshots(db: Database) -> None:
    """Print all portfolio snapshots."""
    snaps = db.recent_snapshots(50)
    print(f"{'Time':<22} {'Portfolio':>12} {'CLOB Bal':>12} {'Disc':>10}  Reason")
    print("-" * 72)
    for s in snaps:
        clob = f"${s['clob_balance']:.4f}" if s["clob_balance"] is not None else "N/A"
        disc = f"${s['discrepancy']:.4f}" if s["discrepancy"] is not None else "N/A"
        print(
            f"{_fmt_ts(s['timestamp']):<22} ${s['portfolio']:>11.4f} "
            f"{clob:>12} {disc:>10}  {s['reason']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Capital verification audit tool")
    parser.add_argument(
        "--fix", action="store_true", help="Re-verify all historical EXPIRED trades"
    )
    parser.add_argument(
        "--export", action="store_true", help="Export audit trail to capital_audit.csv"
    )
    parser.add_argument(
        "--snapshots", action="store_true", help="Show portfolio snapshot history"
    )
    args = parser.parse_args()

    db = Database(CFG.db_path)
    cv = CapitalVerifier(db)

    if args.export:
        export_csv(db)
    elif args.snapshots:
        show_snapshots(db)
    elif args.fix:
        run_fix(db, cv)
    else:
        audit_report(db, cv)
        if cv.trading_paused:
            print("⚠  CRITICAL discrepancies detected — trading is paused.")
            print("   Investigate and run: python verify_capital.py --fix")
            sys.exit(1)


if __name__ == "__main__":
    main()

"""
Manual Redemption
==================
Redeems all resolved winning positions back to USDC.e in your wallet.
Safe to run anytime — even while the bot is running.

Usage:
    python3 redeem_now.py
"""

import sys
from core.redeem import redeem_all, _fetch_redeemable_positions
from core.config import CFG

SEPARATOR = "─" * 48


def main():
    print()
    print(SEPARATOR)
    print("  Polymarket Manual Redemption")
    print(SEPARATOR)
    print(f"  Wallet : {CFG.funder_address or '(not set)'}")
    print()

    if not CFG.private_key or not CFG.funder_address:
        print("  ❌  POLY_PRIVATE_KEY / POLY_FUNDER_ADDRESS not found in .env")
        print("      Run python3 setup.py first.")
        print()
        sys.exit(1)

    # ── Show redeemable positions first ──────────────────────────────
    print("  Fetching redeemable positions...")
    positions = _fetch_redeemable_positions()

    if not positions:
        print("  ✅  Nothing to redeem — wallet is up to date.")
        print()
        sys.exit(0)

    print(f"  Found {len(positions)} position(s) to redeem:\n")
    total_usdc = 0.0
    for p in positions:
        market = p.get("title", p.get("conditionId", "")[:12])
        size   = float(p.get("size", 0))
        kind   = "neg-risk" if p.get("negativeRisk") else "standard"
        total_usdc += size
        print(f"    · {market[:45]:<45}  ${size:.2f}  [{kind}]")

    print()
    print(f"  Total : ${total_usdc:.2f} USDC.e")
    print(SEPARATOR)
    print()

    confirm = input("  Redeem all? [yes/no]: ").strip().lower()
    if confirm not in ("yes", "y"):
        print("\n  Cancelled. Nothing was redeemed.\n")
        sys.exit(0)

    # ── Execute ───────────────────────────────────────────────────────
    print()
    count, total_usdc = redeem_all()

    print()
    print(SEPARATOR)
    if count == len(positions):
        print(f"  ✅  All {count} position(s) redeemed — ${total_usdc:.2f} USDC.e back in wallet.")
    elif count > 0:
        print(f"  ⚠️   {count}/{len(positions)} redeemed (${total_usdc:.2f} USDC.e). Check logs for failures.")
    else:
        print("  ❌  No positions were redeemed. Check logs for errors.")
    print(SEPARATOR)
    print()


if __name__ == "__main__":
    main()

"""
Config Updates — Evidence-Based Parameter Changes (Revision 2)
=================================================================
All changes derived from analysis of 115 live EXPIRED trades (Apr 18-23, 2026).
See ANALYSIS_REPORT.md for full findings.

REVISION 2 CORRECTION: blackout_hours_utc was [7] (computed in local WIB time).
Correct UTC values are [0, 2, 6, 7, 17] — Asia, EU, and US midday market opens.
"""

CHANGES = {
    # ────────────────────────────────────────────────────────────────────────
    # CRITICAL: Structural Fixes
    # ────────────────────────────────────────────────────────────────────────

    "max_token_price": {
        "old": 0.95,
        "new": 0.67,
        "finding": "Finding #2",
        "rationale": (
            "Entry price determines payoff ratio b=(1-e)/e*(1-fee). "
            "At e=0.75 you need WR>77.2%; strategy produces 61.7%. "
            "UP+price≤0.67: PF=1.207, net=+$8.43/5.5d. "
            "UP+price>0.67: PF=0.71, net=-$44.28/5.5d. "
        ),
        "expected_impact": "+$44.28/5.5d = +$8.05/day",
    },

    "allow_down_direction": {
        "old": True,
        "new": False,
        "finding": "Finding #1",
        "rationale": (
            "DOWN oracle: 9 trades, WR=11.1% (z=-2.33, p<0.05 vs random). "
            "Net loss -$30.84/5.5d = 46% of all losses on 8% of trades. "
            "DOWN signals fire during trend dips that reverse before settlement in bull conditions."
        ),
        "expected_impact": "+$30.84/5.5d = +$5.61/day",
    },

    "size_mult_low": {
        "old": 0.5,
        "new": 1.0,
        "finding": "Finding #3",
        "rationale": (
            "Low-price tokens b=0.66+ (best payoff). Original 0.5× was wrong. "
            "1.0× = quarter-Kelly (3.0% of $140 portfolio = $4.20). "
            "Kill switch requires 5 consecutive losses — safe. "
            "(1.3× was rejected: 1.38× quarter-Kelly, kill switch at 3.8 losses)"
        ),
        "expected_impact": "2× bet sizing on profitable subset vs original",
    },

    "size_mult_high": {
        "old": 1.3,
        "new": 0.5,
        "finding": "Finding #3",
        "rationale": "High-price tokens b=0.124, need WR>89%. Inverted.",
        "expected_impact": "Prevented amplified losses on worst-EV bucket",
    },

    "min_edge_pct": {
        "old": 0.0,
        "new": 6.0,
        "finding": "Finding #4",
        "rationale": (
            "Previous fair_value returned 0.97 for delta≥0.20% (actual WR=47.6%). "
            "Recalibrated fair_value + 6% floor blocks HIGH-delta bad trades at entry=0.60-0.67."
        ),
        "expected_impact": "Blocks HIGH-delta false-edge trades",
    },

    "max_daily_trades": {
        "old": 288,
        "new": 50,
        "finding": "General",
        "rationale": "UP+price≤0.67+blackout generates ~4.5/day. 50 is a safety cap.",
        "expected_impact": "Safety cap only; no EV impact under normal conditions",
    },

    "max_concurrent_positions": {
        "old": 9,
        "new": 6,
        "finding": "Finding #6 (cluster losses)",
        "rationale": (
            "7 cluster events (2+ assets losing simultaneously) = 63% of total losses. "
            "Reducing to 6 (2 per asset max) limits cluster exposure."
        ),
        "expected_impact": "-$4-14 per cluster event prevented",
    },

    "consec_loss_limit": {
        "old": "N/A",
        "new": 3,
        "finding": "Finding #6",
        "rationale": "After 3 consecutive losses, conditions are adverse. 30-min lockout.",
        "expected_impact": "Prevents cluster loss escalation",
    },

    "snipe_exit_sec": {
        "old": 25.0,
        "new": 16.0,
        "finding": "Ghost prevention",
        "rationale": (
            "3 confirmed ghost redemptions: YES tokens resolved $0 on-chain at TTL≤15s. "
            "16.0s provides 1s buffer above confirmed ghost zone."
        ),
        "expected_impact": "Eliminates confirmed ghost categories",
    },

    # ────────────────────────────────────────────────────────────────────────
    # REVISION 2: Timezone-corrected blackout hours
    # ────────────────────────────────────────────────────────────────────────

    "blackout_hours_utc": {
        "old": "[7]  ← WRONG: computed in local WIB time, was blocking UTC 7 only",
        "new": "[0, 2, 6, 7, 17]",
        "finding": "Finding #6 REVISED",
        "rationale": (
            "CRITICAL CORRECTION: previous analysis computed hours in local time (WIB = UTC+7). "
            "UTC 0 = 07:00 WIB — the biggest bad window (n=13, WR=46%, net=-$18.27). "
            "\n"
            "Correct bad UTC hours are market-open volatility windows:\n"
            "  UTC 00: Asia equity open (08:00 SGT). n=13, WR=46%, net=-$18.27\n"
            "  UTC 02: Asia mid-morning (10:00 SGT). n=4, WR=25%, net=-$12.18\n"
            "  UTC 06: EU pre-market (07:00 CET). n=10, WR=50%, net=-$13.25\n"
            "  UTC 07: EU market open (08:00 CET). n=7, WR=43%, net=-$13.80\n"
            "  UTC 17: US midday algo peak (13:00 EST). n=4, WR=50%, net=-$6.61\n"
            "\n"
            "38 trades in bad hours: WR=44.7%, PF=0.266, net=-$64.11\n"
            "38 trades in good hours: WR=86.8%, PF=3.130, net=+$38.59\n"
            "Same trade count. Completely opposite outcomes.\n"
            "6/7 cluster loss events (2+ assets losing simultaneously) occurred in bad hours."
        ),
        "expected_impact": "+$64.11/5.5d = +$11.66/day (full blackout vs no blackout). "
                           "+$50.31/5.5d = +$9.15/day incremental vs old blackout=[7].",
    },

    # ────────────────────────────────────────────────────────────────────────
    # MODEL RECALIBRATIONS (implemented in engine/signal.py)
    # ────────────────────────────────────────────────────────────────────────

    "_fair_value_HIGH_delta": {
        "old": "base = 0.97 for delta >= 0.20%",
        "new": "base = 0.65-0.70 for delta 0.20-0.50%; 0.80 for delta >= 0.50%",
        "finding": "Finding #4",
        "rationale": (
            "HIGH delta (0.20-0.50%): actual WR=47.6% vs model assumption ~97%. "
            "At entry=0.60, new fair_value gives edge~3.3%, below min_edge_pct=6% → blocks most."
        ),
    },

    "_score_price_component": {
        "old": "price>=0.90: 20pts (rewards market 'agreement' = high price)",
        "new": "price<0.58: 20pts; <0.62: 15pts; <0.65: 10pts; <0.68: 5pts",
        "finding": "Finding #5",
        "rationale": (
            "Inverted to reward low-price (high-EV) tokens. "
            "Original gave 20pts to $0.90 token (needs 90.2% WR) and 2pts to $0.57 token (needs 56% WR)."
        ),
    },
}


def print_summary():
    print("=" * 65)
    print("CONFIG CHANGES SUMMARY (Revision 2)")
    print("=" * 65)
    for param, info in CHANGES.items():
        if param.startswith("_"):
            print(f"\n[MODEL] {param}")
        else:
            print(f"\n[CONFIG] {param}: {info['old']} → {info['new']}")
        print(f"  Finding: {info['finding']}")
        if "expected_impact" in info:
            print(f"  Impact:  {info['expected_impact']}")


if __name__ == "__main__":
    print_summary()

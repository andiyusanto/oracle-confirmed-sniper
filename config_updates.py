"""
Config Updates — Evidence-Based Parameter Changes
====================================================
All changes derived from analysis of 115 live EXPIRED trades (Apr 18-23, 2026).
See ANALYSIS_REPORT.md for full findings.

Usage: These are the recommended values. Apply by restarting the bot.
"""

CHANGES = {
    # ────────────────────────────────────────────────────────────────────────
    # CRITICAL: Structural Fixes (implement immediately)
    # ────────────────────────────────────────────────────────────────────────

    "max_token_price": {
        "old": 0.95,
        "new": 0.67,
        "finding": "Finding #2",
        "rationale": (
            "Entry price determines the payoff ratio b=(1-e)/e*(1-fee). "
            "At e=0.75 you need WR>77.2%; strategy produces only 58.6%. "
            "UP+price<=0.67 subset: PF=1.207, net=+$8.43/5d. "
            "UP+price>0.67 subset: PF=0.71, net=-$44.28/5d. "
            "The $0.67 breakpoint was identified via full price cap sweep."
        ),
        "expected_impact": "+$8.94/5d",
    },

    "only_up": {
        "old": "N/A (did not exist)",
        "new": True,
        "finding": "Finding #1",
        "rationale": (
            "DOWN oracle signal is anti-predictive: 9 trades, WR=11.1% "
            "(z=-2.33, p<0.05 vs random). Net loss -$30.84/5d = 46% of "
            "all losses on 8% of trades. The DOWN signal fires during "
            "trend dips that reverse before settlement in bull conditions."
        ),
        "expected_impact": "+$6.17/day",
    },

    "size_mult_low": {
        "old": 0.5,
        "new": 1.3,
        "finding": "Finding #3",
        "rationale": (
            "Low-price tokens have b=0.66+ (best payoff ratio). "
            "Original 0.5x was wrong — these should be the LARGEST bets. "
            "With max_token_price=0.67, all entries now fall in this bucket."
        ),
        "expected_impact": "2.6x bet sizing increase on the profitable subset",
    },

    "size_mult_high": {
        "old": 1.3,
        "new": 0.5,
        "finding": "Finding #3",
        "rationale": (
            "High-price tokens (>0.85) had b=0.124, need WR>89%. "
            "Giving 1.3x to these was the worst sizing decision. "
            "Now blocked by max_token_price=0.67 anyway."
        ),
        "expected_impact": "Historical: prevented $X.XX in amplified losses",
    },

    "min_edge_pct": {
        "old": 9.0,
        "new": 6.0,
        "finding": "Finding #4",
        "rationale": (
            "Old fair_value was over-optimistic (returned 0.95 for delta>=0.20%), "
            "making 9% easy to pass for bad trades. "
            "Recalibrated fair_value is conservative; 6% floor still maintains "
            ">4pp above fee breakeven (~1.9% at entry=0.60)."
        ),
        "expected_impact": "Blocks HIGH-delta bad trades while allowing STRONG-delta good ones",
    },

    "max_daily_trades": {
        "old": 288,
        "new": 50,
        "finding": "General",
        "rationale": (
            "UP+price<=0.67 generates ~7 trades/day. "
            "288 was calibrated for all-direction, all-price trading. "
            "50 provides headroom above the expected ~7/day without risk "
            "of runaway signaling from a configuration bug."
        ),
        "expected_impact": "Safety cap; no EV impact under normal conditions",
    },

    "max_concurrent_positions": {
        "old": 9,
        "new": 6,
        "finding": "Finding #6 (correlated losses)",
        "rationale": (
            "4 window-level cluster events accounted for 63% of total losses. "
            "All 3-asset concurrent losses (Apr 20 14:49: -$13.89; "
            "Apr 23 09:58: -$13.81) occurred when max_concurrent was reached. "
            "Reducing to 6 (2 per asset) limits cluster exposure."
        ),
        "expected_impact": "-$4.17/event prevented on avg",
    },

    "consec_loss_limit": {
        "old": "N/A (did not exist)",
        "new": 3,
        "finding": "Finding #6",
        "rationale": (
            "After 3 consecutive losses, market conditions are likely adverse "
            "(macro event, bad session). 30-min lockout prevents feeding a losing streak."
        ),
        "expected_impact": "Prevents cluster loss episodes from escalating",
    },

    "blackout_hours_utc": {
        "old": "N/A (did not exist)",
        "new": [7],
        "finding": "Finding #6 (THE DIAMOND)",
        "rationale": (
            "Hour 07:00 UTC (EU market open): n=13 UP trades, WR=46.2%, "
            "net=-$18.27. The EU open causes crypto volatility spikes that "
            "oracle feeds capture but CTF markets revert within 5m. "
            "Hour 08:00 UTC (post-open): WR=100%, n=7, net=+$10.67. "
            "Expand to [7, 9, 13, 14] after 10+ trades/hour are accumulated."
        ),
        "expected_impact": "+$3.65/day (saving 2.6 bad trades/day at 07:00)",
    },

    # ────────────────────────────────────────────────────────────────────────
    # MODEL RECALIBRATIONS (implemented in engine/signal.py)
    # ────────────────────────────────────────────────────────────────────────

    "_fair_value_HIGH_delta": {
        "old": "base = 0.95 for delta >= 0.20%",
        "new": "base = 0.65-0.70 for delta 0.20-0.50%; 0.80 for delta >= 0.50%",
        "finding": "Finding #4",
        "rationale": (
            "HIGH delta (0.20-0.50%): actual WR=47.6% vs model assumption ~95%. "
            "Recalibrated to actual win rate. At entry=0.60, new fair_value "
            "gives edge~3.3%, below min_edge_pct=6% → blocks most HIGH delta trades."
        ),
    },

    "_score_price_component": {
        "old": "price>=0.90: 20pts, price<0.60: 2pts",
        "new": "price<0.58: 20pts, price<0.62: 15pts, price>=0.65: 5pts",
        "finding": "Finding #5",
        "rationale": (
            "Inverted to reward low-price (high-EV) tokens. "
            "Original rewarded market 'agreement' (high price = likely winner) "
            "but ignored that high price also means catastrophically low payoff ratio."
        ),
    },
}


def print_summary():
    """Print a summary of all changes."""
    print("=" * 65)
    print("CONFIG CHANGES SUMMARY")
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

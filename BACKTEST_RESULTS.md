# Backtest / In-Sample Projection Results

> **Methodology:** Not a traditional backtest. Results computed by applying new filters
> to the existing 115 live EXPIRED trades. This is in-sample filtering, not walk-forward.
> All figures are optimistic estimates; expect 20-40% degradation in live trading.

---

## Baseline vs Filtered Performance (115 trades, Apr 18-23)

| Metric | Baseline (all trades) | After Fixes | Change |
|--------|-----------------------|-------------|--------|
| Total trades | 115 | 35 | -70% |
| Trades/day | 23.0 | 7.0 | -70% |
| Win rate | 61.7% | 65.7% | +4.0pp |
| Avg win | $1.62 | $2.13 | +$0.51 |
| Avg loss | $4.14 | $3.39 | -$0.75 |
| Payoff ratio b | 0.39 | 0.63 | +0.24 |
| Profit factor | 0.634 | **1.207** | +0.573 |
| Net PnL / 5d | -$66.68 | **+$8.43** | +$75.11 |
| Net PnL / day | -$13.34 | **+$1.69** | +$15.03 |
| Kelly fraction | -0.357 (don't bet) | +0.089 (bet) | positive |

**Filter applied:** `direction=UP AND entry_price<=0.67`

---

## Impact of Each Fix (Additive, Approximate)

| Fix | Trades Removed | Estimated Net Change |
|-----|---------------|----------------------|
| 1. Ban DOWN trades | -9 trades | +$30.84/5d = +$6.17/day |
| 2. max_token_price=0.67 | -62 trades | +$44.28/5d = +$8.86/day |
| 3. Invert size_mult | 0 removed | +sizing for profitable subset |
| 4. Fair_value recalibration | ~-5 HIGH delta | +$12.70/5d estimated |
| 5. Invert price_score | 0 removed | Better signal prioritization |
| 6. 07:00 UTC blackout | -13 trades | +$18.27/5d = +$3.65/day |
| 7. Consec. loss lockout | -4 cluster events | +$14.89/5d (cluster prevention) |

> Note: fixes overlap — the same trade can be removed by multiple filters.
> The combined effect (UP + price≤0.67) = +$75.11/5d, not sum of individual impacts.

---

## Combined Filter Deep Dive (UP + price≤0.67)

```
35 qualifying trades over 5 days:

Wins:  23 × avg +$2.13 = +$48.99
Losses: 12 × avg -$3.39 = -$40.56
Net: +$8.43

Profit factor: 48.99 / 40.56 = 1.207
Breakeven WR at avg entry $0.609: 60.9%
Actual WR: 65.7%
Margin above breakeven: +4.8pp
```

---

## Best Combination Found (UP + price≤0.67 + exclude hour 7)

**n=24 trades** (removed 11 bad-hour trades from the 35-trade subset):

| Metric | UP+price≤0.67 | + Hour filter |
|--------|---------------|---------------|
| n trades | 35 | 24 |
| Win rate | 65.7% | **79.2%** |
| Profit factor | 1.207 | **2.47** |
| Net / 5d | +$8.43 | **+$24.76** |
| Net / day | +$1.69 | **+$4.95** |

> ⚠️ Hour filter is in-sample. PF=2.47 is likely to regress to 1.5-1.8 out-of-sample.
> Use as a directional guide only. Validate over 50+ trades before trusting.

---

## Asset-Level Contribution (UP + price≤0.67)

| Asset | n | WR | PF | Net / 5d | Recommendation |
|-------|---|----|----|----------|----------------|
| BTC | 15 | 73.3% | **1.74** | **+$10.04** | Keep — solid edge |
| ETH | 8 | 62.5% | 1.02 | +$0.21 | Borderline — monitor |
| SOL | 12 | 58.3% | 0.89 | -$1.81 | Underperforming — consider removing |

**If BTC-only:** n=3/day, net +$2.01/day (+$10.04/5d) at higher confidence.

---

## Conservative 10-Day Forward Projection

Assumes 30% out-of-sample degradation on win rate (65.7% → 60%) and PF (1.207 → 1.05):

| Day | Portfolio | Trades | Est. Daily Net |
|-----|-----------|--------|----------------|
| 1 | $140 | 7 | +$1.18 |
| 2 | $141 | 7 | +$1.19 |
| 3 | $142 | 7 | +$1.20 |
| … | … | … | ~+$1.20/day |
| 10 | $152 | 7 | +$1.28 |

At live_max_usdc=$15, growth is linear (cap binds at ~$500). Realistic 10-day return: ~$12 at conservative degradation assumption.

**Optimistic projection** (no out-of-sample degradation, hour filter validated):
- 5 trades/day (24/5 = 4.8 after hour filter), net $4.95/day
- 10-day return from $140: ~$190 (36% return)

---

## Validation Gates for Continued Confidence

After 50 live trades with new config:
- If PF > 1.1: continue as-is
- If PF 0.9-1.1: re-examine hour filter, consider BTC-only
- If PF < 0.9: revert to analysis; may indicate regime change (bear market negates UP bias)

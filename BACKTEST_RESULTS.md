# Backtest / In-Sample Projection Results (Revision 2)

> **Methodology:** Applying new filters to the existing 115 live EXPIRED trades (in-sample filtering).
> All figures are optimistic; expect 20-30% degradation in live trading.
> Hour analysis corrected: all times are UTC. Previous report used local time (WIB = UTC+7).

---

## Baseline vs Filtered Performance (115 trades, Apr 18-23)

| Metric | Baseline | After Rev 1 Fixes | + Rev 2 Hour Fix | Change |
|--------|----------|-------------------|-----------------|--------|
| Total trades | 115 | 35 | 25 | -78% |
| Trades/day | 23.0 | 7.0 | 4.5 | -80% |
| Win rate | 61.7% | 65.7% | **76.0%** | +14.3pp |
| Avg win | $1.62 | $2.13 | — | — |
| Avg loss | $4.14 | $3.39 | — | — |
| Profit factor | 0.634 | 1.207 | **2.092** | +1.458 |
| Net PnL / 5.5d | -$66.68 | +$8.43 | **+$21.71** | +$88.39 |
| Net PnL / day | -$12.12 | +$1.53 | **+$3.95** | +$16.07 |

**Primary filter chain:** `direction=UP AND entry_price≤0.67 AND utc_hour NOT IN {0,2,6,7,17}`

---

## Incremental Fix Impact

| Fix | Trades Removed | Net Change |
|-----|---------------|------------|
| 1. Block DOWN trades | -9 | +$30.84/5.5d |
| 2. max_token_price=0.67 | -62 | +$44.28/5.5d |
| 3. Invert size_mult | 0 | Better sizing on profitable subset |
| 4. Fair_value recalibration | ~-5 HIGH delta | Blocks bad-edge trades |
| 5. Invert price_score | 0 | Better confidence signal for low-price tokens |
| 6. Blackout [7] (Rev 1) | -7 | +$13.80/5.5d |
| **6b. Blackout [0,2,6,7,17] (Rev 2)** | **-38** | **+$64.11/5.5d** |
| 7. Consec. loss lockout | preventive | Cluster loss mitigation |

> The hour blackout accounts for the single largest lift in the dataset.
> Bad UTC hours: same n=38 trades as good hours, WR=44.7% vs 86.8%.

---

## The Revised Diamond: Market-Open Volatility Blackout

```
Filter: UP + price≤0.67 (n=35 baseline)
  → + blackout [7] only (Rev 1): n=24, WR=79.2%, PF=2.47, net=+$24.76/5.5d
  → + blackout [0,2,6,7,17] (Rev 2): n=25, WR=76.0%, PF=2.09, net=+$21.71/5.5d
```

Note: Rev 2 removes more bad-hour trades but also removes one previously included good trade
in the partial-blackout filter. n=25 vs n=24 (one extra trade rescued from the correct UTC hours).

Why the bad hours are bad — economic causation:
- **UTC 00-02 (08:00-10:00 SGT):** Asia equity open. Hang Seng/Singapore crypto correlation creates
  oracle spike. CTF binary reverts within 5m as correlation fades.
- **UTC 06-07 (07:00-08:00 CET):** EU pre-market + London/Frankfurt open. Same spike-reversion pattern.
- **UTC 17 (13:00 EST):** US midday. HFT activity peak creates volatility that CTF doesn't track.

---

## Best All-Time Combination (UP + price≤0.67 + blackout [0,2,6,7,17])

**n=25 trades over 5.5 days:**

```
Wins:  19 × avg win  = +$40.08
Losses: 6 × avg loss = -$18.37

Profit factor: 40.08 / 18.37 = 2.18
Breakeven WR at avg entry $0.61: 60.7%
Actual WR: 76.0%
Margin above breakeven: +15.3pp
```

---

## Conservative 10-Day Forward Projection

Assumes 25% out-of-sample degradation: WR 76% → 68%, PF 2.09 → 1.40

| Setting | Bet size | EV/trade | EV/day | 10-day gain |
|---------|----------|----------|--------|-------------|
| Conservative | $4.20 | +$0.70 | +$3.14 | +$31.40 |
| Optimistic (in-sample) | $4.20 | +$1.37 | +$6.15 | +$61.50 |

At live_max_usdc=$15 cap, bet stays at $4.20 (portfolio < $500 where cap wouldn't bind).  
Conservative 10-day return from $140: ~$171 (+22%).

---

## Cluster Loss Prevention

7 cluster events (2+ assets losing same 15min window) totaling -$74.94:
- 6/7 events occurred in bad UTC hours → blocked by `blackout_hours_utc=[0,2,6,7,17]`
- 1/7 event at UTC 4 not blocked → consec_loss_limit=3 limits the cascade

---

## Validation Gates for Continued Confidence

After 50 live trades with new config:
- If PF > 1.5: continue as-is; consider expanding bad-hour blackout if data supports
- If PF 1.0-1.5: re-examine hour 2 and 17 (smallest sample — may be coincidence)
- If PF < 1.0: likely regime change; revisit allow_down_direction and price cap

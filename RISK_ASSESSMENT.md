# Risk Assessment: Failure Conditions and Recovery

---

## Structural Risks

### Risk 1: Regime Change (Bull → Bear Market)
**Condition:** BTC/ETH/SOL enter sustained downtrend.  
**Effect on strategy:**
- `only_up=True` means 0 trades during bear confirmation windows
- When oracle says DOWN (correct in bear), we skip → 0 wins, 0 losses
- When oracle still fires UP during dead-cat bounces → WR may drop to 40-50%
- Profit factor would degrade toward 0.60-0.80

**Detection:** Monitor 3-day rolling WR. If WR drops below 55% over 20+ trades, flag.  
**Recovery:** Set `only_up = False` and reduce `max_token_price` to 0.60 (tighter edge buffer). Re-examine DOWN signal performance.

### Risk 2: Liquidity Drying Up at Low Prices
**Condition:** Few markets have YES tokens priced ≤ $0.67.  
**Effect:** Trade count drops below 3/day → portfolio grows too slowly to compound.  
**Detection:** If `discovery_interval` scans find <5 qualifying tokens/hour.  
**Recovery:** Raise `max_token_price` to 0.70 and monitor whether WR holds above 63%.

### Risk 3: In-Sample Overfitting of Hour Filter
**Condition:** The 07:00 UTC blackout was derived from n=13 trades.  
**Effect:** If the pattern was regime-specific (EU volatility in Apr 2026), it may not persist.  
**Detection:** After 20+ new 07:00 UTC trades, if WR > 60%, remove from blackout_hours.  
**Recovery:** Set `blackout_hours_utc = []` to disable.

---

## Execution Risks

### Risk 4: Exit Reversal Never Fires
**Observation:** All 115 losses = exactly -size_usdc. Zero partial exits.  
**Cause candidates:**
1. Exit reversal conditions not met (reversal must persist 8s but TTL < 12s when detected)
2. CLOB has no buyers for YES tokens in last 15-30s of window
3. `exit_reversal_min_ttl=12.0` blocks exit attempts when TTL drops too fast

**Impact:** Avg loss remains $3.39 instead of potentially $2.00-2.50 with partial exits.  
**Investigation:** Add log counter: how many times does exit monitoring detect a reversal vs how many result in an attempted sell vs successful fill?  
**If confirmed broken:** The strategy is already profitable without partial exits. Fixing this would be upside (+$0.90-1.40 avg_loss improvement).

### Risk 5: Ghost Redemptions Persist
**Previous finding:** 3 confirmed ghost redemptions (YES tokens resolved $0 on-chain despite bot WIN).  
**Status:** `snipe_exit_sec=16.0` blocks all observed TTL≤15s ghost categories.  
**Residual risk:** New ghost patterns at TTL=17-25s possible but not observed.  
**Detection:** Any win where redemption queue runs for >5 minutes → ghost candidate.

---

## Statistical Risks

### Risk 6: Small Sample Size
**35 qualifying trades in UP+price≤0.67 subset.**  
95% confidence interval on 65.7% WR: approximately [48%, 80%].  
PF=1.207 could be as low as 0.85 at lower CI.

**Required validation period:** 50+ trades (~7-8 days at 7/day) before drawing strong conclusions.

**Don't change config again until:** 50+ trades accumulated at current settings.

### Risk 7: Correlated Multi-Asset Cluster Losses
**4 events where 2-3 assets all lost simultaneously accounted for 63% of total losses.**  
Cluster loss events: Apr 20 07:58 (-$6.50), Apr 20 14:49 (-$13.89), Apr 21 07:39 (-$8.03), Apr 23 09:58 (-$13.81).

**Trigger pattern:** All clusters occurred during EU session hours (07:00-09:00 UTC) or volatile midday (13:00-14:00 UTC). The hour blackout partially addresses this.

**Additional mitigation:** When `max_concurrent_positions=6` and 2+ positions in same time-block just resolved LOSS, consider temporary cooldown (already partially handled by `consec_loss_limit=3`).

---

## Recovery Procedures

### If Profit Factor < 0.9 over 50 trades:
1. Review UP+price≤0.67 breakdown by asset (if SOL is dragging, remove from `assets`)
2. Check if blackout_hours needs expansion (add 9, 13, 14 to the list)
3. Run `python3 analyze.py` to identify which delta tier is underperforming
4. Consider raising `min_delta_pct` to 0.05% to filter out WEAK signals

### If Portfolio drawdown > 15% in one day:
- Kill switch activates automatically via `kill_switch_drawdown_pct=15.0`
- Do NOT restart bot until root cause identified
- Run `python3 redeem_now.py` to recover any redeemable positions
- Review that day's log for abnormal signal patterns

### If 3 consecutive losses trigger lockout:
- Bot pauses 30 min automatically (`consec_loss_lockout_min=30`)
- This is working as intended; no intervention needed unless you want to shorten/extend
- After lockout clears, bot resumes normally

---

## Kill Conditions (Stop Trading Immediately)

| Condition | Trigger | Action |
|-----------|---------|--------|
| Daily loss > 15% | kill_switch_drawdown_pct | Auto: bot stops |
| Daily loss > 10% | max_daily_loss_pct | Auto: bot stops |
| 3 consecutive losses | consec_loss_limit | Auto: 30min pause |
| Ghost redemption detected | Manual review | Remove TTL windows |
| All assets enter bear trend | WR < 50% over 20 trades | Manual: set only_up=False |
| CLOB API error rate > 10% | Log monitoring | Manual: restart |

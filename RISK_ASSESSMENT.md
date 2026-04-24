# Risk Assessment: Failure Conditions and Recovery (Revision 2)

---

## Structural Risks

### Risk 1: Regime Change (Bull → Bear Market)
**Condition:** BTC/ETH/SOL enter sustained downtrend.  
**Effect on strategy:**
- `allow_down_direction=False` → 0 trades during bear confirmation windows
- When oracle fires UP during dead-cat bounces → WR may drop to 40-50%
- Profit factor would degrade toward 0.60-0.80

**Detection:** Monitor 3-day rolling WR. If WR drops below 55% over 20+ trades, flag.  
**Recovery:** Set `allow_down_direction=True` and reduce `max_token_price` to 0.60. Re-examine DOWN signal performance.

### Risk 2: Liquidity Drying Up at Low Prices
**Condition:** Few markets have YES tokens priced ≤ $0.67.  
**Effect:** Trade count drops below 2/day → portfolio grows too slowly.  
**Detection:** If discovery scans find <3 qualifying tokens/hour.  
**Recovery:** Raise `max_token_price` to 0.70 and monitor WR.

### Risk 3: Hour Blackout Overfitting
**Condition:** Bad-hour patterns were derived from 38 trades over 5.5 days.  
UTC 02 has only n=4 trades. UTC 17 has only n=4 trades.  
**Effect:** If these hours were regime-specific (April 2026 volatility), the blackout may block good trades.  
**Detection:** After 20+ new trades in UTC 2 and 17, if WR > 60%, remove from blackout_hours_utc.  
**Recovery:** Set `blackout_hours_utc=[0, 6, 7]` (keep only the most reliably bad with n≥10).

---

## Execution Risks

### Risk 4: Exit Reversal Never Fires
**Observation:** All 115 losses = exactly -size_usdc. Zero partial exits. Ever.  
**Cause candidates:**
1. Exit reversal conditions not met (reversal must persist 8s but TTL < 12s when detected)
2. CLOB has no buyers for YES tokens in last 15-30s of window
3. `exit_reversal_min_ttl=12.0` blocks exit attempts when TTL drops too fast

**Impact:** Avg loss remains $3.39 instead of potentially $2.00-2.50 with partial exits.  
**Investigation:** Add log counter: reversal detections vs attempted sells vs successful fills.  
**If confirmed broken:** Strategy is profitable without partial exits. Fixing this is upside.

### Risk 5: Ghost Redemptions
**Previous finding:** 3 confirmed ghost redemptions (YES tokens resolved $0 on-chain despite bot WIN).  
**Status:** `snipe_exit_sec=16.0` blocks all observed TTL≤15s ghost categories.  
**Residual risk:** New ghost patterns at TTL=17-25s possible but not observed in UP+price≤0.67 subset.  
**Detection:** Any win where redemption queue runs >5 minutes → ghost candidate.

---

## Statistical Risks

### Risk 6: Small Sample Size Per Hour Bucket
UTC 02: n=4 trades. UTC 17: n=4 trades.  
95% CI on WR=25% (n=4): approximately [1%, 81%]. Statistically worthless alone.  
**Mitigation:** UTC 00 (n=13) and UTC 06/07 (n=17 combined) are the reliable anchors.  
The cluster event data (6/7 events in bad hours) provides independent corroborating evidence.

### Risk 7: The Good-Hours Stat Is Also Volatile
38 good-hours trades: WR=86.8%, PF=3.13.  
95% CI on WR=86.8%: approximately [71%, 95%].  
Conservative out-of-sample estimate: 65-70%.  
**Don't change config again until:** 50+ trades accumulated at current settings.

---

## Recovery Procedures

### If Profit Factor < 1.0 over 30 trades:
1. Check WR in newly unblocked hours — did any blocked hour move to good?
2. Run `python3 analyze.py` to identify which delta tier is underperforming
3. Check if `max_token_price=0.67` is blocking too many qualifying markets (raise to 0.70 if <3/day)

### If Portfolio drawdown > 15% in one day:
- Kill switch activates automatically via `kill_switch_drawdown_pct=15.0`
- Do NOT restart bot until root cause identified
- Run `python3 redeem_now.py` to recover any redeemable positions
- Check that day's log for abnormal signal patterns (are bad hours getting through?)

### If 3 consecutive losses trigger lockout:
- Bot pauses 30 min automatically (`consec_loss_lockout_min=30`)
- Working as intended; no intervention needed
- After lockout clears, bot resumes normally

---

## Kill Conditions

| Condition | Trigger | Action |
|-----------|---------|--------|
| Daily loss > 15% | kill_switch_drawdown_pct | Auto: bot stops |
| Daily loss > 10% | max_daily_loss_pct | Auto: pauses trading |
| 3 consecutive losses | consec_loss_limit | Auto: 30min pause |
| Bad UTC hour | blackout_hours_utc=[0,2,6,7,17] | Auto: skip signal |
| Ghost redemption detected | Manual review | Remove TTL windows |
| All assets enter bear trend | WR < 50% over 20 trades | Manual: set allow_down_direction=True |
| CLOB API error rate > 10% | Log monitoring | Manual: restart |

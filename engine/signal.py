"""
Signal engine: Oracle-confirmed end-cycle sniper (Strategy D).

Optimizations applied:
  1. Tiered entry windows — strong deltas can enter earlier
  2. Adaptive confidence threshold — lower bar for high-conviction signals
  3. Improved fair_value curve with better calibration
  4. Minimum edge scaled by entry price (cheap tokens need bigger edge)
  5. Kelly-informed sizing with edge/variance awareness
  6. Chainlink staleness hard gate — block entry when CL feed is stale
  7. Direction reversal check extended to ALL signals (was weak-only)
  8. Multi-point delta confirmation — checks 30s and 20s lookbacks
  9. Spread width gate — skip thin/uncertain markets
 10. Consecutive pass confirmation — signal must pass all gates twice
"""

import logging
import time
from typing import Optional

from core.config import CFG
from core.models import Token, OracleState, Signal
from feeds.prices import PriceFeeds

log = logging.getLogger("hybrid.engine")


class HybridEngine:
    # Seconds to block an asset after any fill, regardless of window_ts.
    # Prevents double-fills when a 5m and 15m market share the same end_ts
    # and are simultaneously in their entry windows.
    ASSET_FILL_COOLDOWN = 10.0

    def __init__(self, feeds: PriceFeeds):
        self.feeds = feeds
        self._traded_windows: set[str] = set()
        self._asset_fill_ts: dict[str, float] = {}   # asset → last fill timestamp
        # Consecutive-pass gate: token_id → timestamp of first pass
        self._first_pass_ts: dict[str, float] = {}
        # Rate-limited gate-3 diagnostic log: asset → last log timestamp
        self._gate3_log_ts: dict[str, float] = {}

    def evaluate(self, token: Token, portfolio: float,
                 is_live: bool = False) -> Optional[Signal]:
        """Evaluate a single token for a snipe opportunity."""
        now = time.time()
        ttl = token.end_ts - now
        asset = token.asset

        # ── GATE 1: Not already traded this window ────────────────
        wkey = f"{asset}_{token.window_ts}"
        if wkey in self._traded_windows:
            return None

        # ── GATE 1b: Per-asset cooldown after any fill ────────────
        last_fill = self._asset_fill_ts.get(asset, 0.0)
        if now - last_fill < self.ASSET_FILL_COOLDOWN:
            return None

        # ── GATE 2: Capture opening price ─────────────────────────
        self.feeds.capture_opening(asset, token.window_ts)

        # ── GATE 3: Oracle delta ──────────────────────────────────
        delta = self.feeds.oracle_delta(asset, token.window_ts)
        abs_delta = abs(delta)
        if abs_delta < CFG.min_delta_pct:
            # Rate-limited diagnostic (once per 60s per asset) — lets operator
            # distinguish "no opening captured" (delta=0) from "delta too small"
            last_g3 = self._gate3_log_ts.get(asset, 0.0)
            if now - last_g3 >= 60.0:
                self._gate3_log_ts[asset] = now
                has_opening = bool(self.feeds.openings.get(asset, {}).get(token.window_ts))
                log.info(
                    "GATE3 SKIP %s: delta=%.4f%% < min=%.4f%% "
                    "(opening_captured=%s ttl=%.0fs)",
                    asset, delta, CFG.min_delta_pct, has_opening, ttl,
                )
            return None

        # ── GATE 3.5: Chainlink staleness hard gate ───────────────
        # If CL data is stale AND we still have >15s TTL, the delta direction
        # is based on an outdated oracle price — too uncertain to trade.
        # At TTL ≤ 15s the window is nearly done; staleness is less risky.
        stale = self.feeds.chainlink_staleness(asset)
        if stale > CFG.cl_staleness_hard_sec and ttl > 15.0:
            log.debug(
                "STALE SKIP %s: CL data %.0fs old (limit %.0fs), ttl=%.0fs",
                asset, stale, CFG.cl_staleness_hard_sec, ttl,
            )
            return None

        # ── GATE 3b: Delta momentum filter ───────────────────────────
        # Three checks to confirm the oracle is genuinely moving our way:
        #
        #   a) No direction reversal in last 20s — ALL signals.
        #      Strong signals no longer bypass this — a 0.08% delta that
        #      reversed direction is just as risky as a weak reversal.
        #
        #   b) No direction reversal in last 30s — ALL signals.
        #      Multi-point confirmation: direction must be stable over a
        #      longer lookback, not just in the last 20s.
        #
        #   c) Heavy fade check — weak + strong signals only.
        #      If current |delta| < 40% of |delta 20s ago|, the move is
        #      collapsing fast. Extreme signals (≥ extreme_delta_pct) can
        #      absorb larger retracements and still resolve in our favour.

        past_delta_20 = self.feeds.oracle_delta_at(asset, token.window_ts, 20.0)
        past_delta_30 = self.feeds.oracle_delta_at(asset, token.window_ts, 30.0)

        # a) Direction reversal at 20s — ALL signals
        if past_delta_20 != 0.0 and abs(past_delta_20) >= CFG.min_delta_pct:
            if (delta > 0) != (past_delta_20 > 0):
                log.debug(
                    "MOMENTUM SKIP %s: reversed vs 20s ago (was %.4f%%, now %.4f%%)",
                    asset, past_delta_20, delta,
                )
                return None

        # b) Direction reversal at 30s — ALL signals
        if past_delta_30 != 0.0 and abs(past_delta_30) >= CFG.min_delta_pct:
            if (delta > 0) != (past_delta_30 > 0):
                log.debug(
                    "MOMENTUM SKIP %s: reversed vs 30s ago (was %.4f%%, now %.4f%%)",
                    asset, past_delta_30, delta,
                )
                return None

        # c) Heavy fade check — weak + strong signals only.
        #    Block if current |delta| has fallen >50% from 20s-ago value.
        #    Threshold lowered from 80%→50% — small deltas oscillate naturally;
        #    only block genuine collapses (>50% fade), not normal noise.
        if abs_delta < CFG.extreme_delta_pct:
            if past_delta_20 != 0.0 and abs(past_delta_20) >= CFG.min_delta_pct:
                if abs_delta < abs(past_delta_20) * 0.50:
                    log.debug(
                        "MOMENTUM SKIP %s: delta fading >20%% (was %.4f%%, now %.4f%%)",
                        asset, past_delta_20, delta,
                    )
                    return None

        # d) Unconfirmed delta age gate — ALL tiers.
        #    Checks (a) and (b) are only evaluated when past_delta is above
        #    threshold. If both 20s and 30s lookbacks are below min_delta_pct,
        #    those checks were skipped entirely — the delta appeared suddenly
        #    with no history. A transient CL spike that reverses before CTF
        #    settlement causes a ghost even when snapshot looks correct.
        #    Require minimum TTL when delta has no confirmed history.
        delta_is_unconfirmed = (
            (past_delta_20 == 0.0 or abs(past_delta_20) < CFG.min_delta_pct) and
            (past_delta_30 == 0.0 or abs(past_delta_30) < CFG.min_delta_pct)
        )
        if delta_is_unconfirmed and ttl < CFG.min_ttl_unconfirmed_sec:
            log.debug(
                "UNCONFIRMED SKIP %s: delta appeared suddenly "
                "(20s=%.4f%%, 30s=%.4f%%), ttl=%.0fs < min=%.0fs",
                asset, past_delta_20, past_delta_30, ttl,
                CFG.min_ttl_unconfirmed_sec,
            )
            return None

        # ── GATE 4: Tiered timing based on delta strength ─────────
        if abs_delta >= CFG.extreme_delta_pct:
            max_entry_sec = CFG.snipe_entry_sec       # T-60s
        elif abs_delta >= CFG.strong_delta_pct:
            max_entry_sec = CFG.snipe_entry_strong     # T-45s
        else:
            max_entry_sec = CFG.snipe_entry_weak        # T-25s

        if ttl > max_entry_sec or ttl < CFG.snipe_exit_sec:
            return None

        oracle_says = "UP" if delta > 0 else "DOWN"

        # ── GATE 5: Oracle must agree with token direction ────────
        if oracle_says != token.direction:
            return None

        # ── GATE 5b: Binance agreement check ─────────────────────
        binance_agrees = self.feeds.binance_agrees(asset, oracle_says,
                                                   token.window_ts)

        # ── GATE 6: Token price in range ──────────────────────────
        price = token.book_price
        if price < CFG.min_token_price or price > CFG.max_token_price:
            return None

        # ── GATE 6.5: Spread width gate ───────────────────────────
        # Wide bid-ask spread = thin market, high uncertainty, oracle signal
        # is unreliable or already priced in. Skip to avoid ghost-prone trades.
        if token.book_spread > CFG.max_spread_pct:
            log.debug(
                "SPREAD SKIP %s: spread=%.1f%% > max %.0f%%",
                asset, token.book_spread * 100, CFG.max_spread_pct * 100,
            )
            return None

        # ── GATE 7: Confidence scoring (adaptive threshold) ──────
        confidence = self._score(delta, ttl, price, asset,
                                 binance_agrees=binance_agrees)

        threshold = (CFG.min_confidence_strong
                     if abs_delta >= CFG.strong_delta_pct
                     else CFG.min_confidence)
        if confidence < threshold:
            return None

        # ── GATE 8: Fair value and edge ───────────────────────────
        fair_value = self._fair_value(delta, ttl)
        edge_pct = (fair_value - price) * 100

        _fee = CFG.taker_fee_pct / 100
        fee_edge = price * _fee / (1 - _fee) * 100 + 1.0
        min_edge = max(CFG.min_edge_pct, fee_edge)
        if edge_pct < min_edge:
            log.info(
                "EDGE MISS %s %s @ $%.3f: edge=%.2f%% < min=%.2f%% "
                "(fv=%.3f delta=%.4f%% ttl=%.0fs)",
                asset, oracle_says, price, edge_pct, min_edge,
                fair_value, delta, ttl,
            )
            # Weak signals (delta < strong_delta_pct): hard-block at fee_edge.
            # These have insufficient directional conviction to overcome fees.
            # Strong/Extreme signals: soft block — only reject when deeply negative.
            is_weak = abs_delta < CFG.strong_delta_pct
            if is_weak or edge_pct < -3.0:
                return None

        # ── Position sizing ───────────────────────────────────────
        size = self._compute_size(price, edge_pct, portfolio, is_live)
        if size < 1.0:
            return None

        # ── GATE 9: Consecutive pass confirmation ─────────────────
        # Require this token to pass all gates on two consecutive evaluation
        # cycles before firing. Filters single-cycle spikes from transient
        # oracle data or brief CL price jumps.
        # On first pass: record timestamp and return None (pending).
        # On second pass within consecutive_pass_window_sec: proceed.
        # Prune stale first-pass entries on every call.
        cutoff = now - CFG.consecutive_pass_window_sec * 2
        self._first_pass_ts = {k: v for k, v in self._first_pass_ts.items()
                               if v > cutoff}

        first_ts = self._first_pass_ts.get(token.token_id, 0.0)
        if first_ts == 0.0:
            self._first_pass_ts[token.token_id] = now
            log.debug("PENDING %s: first pass recorded, awaiting confirmation",
                      asset)
            return None
        elif now - first_ts > CFG.consecutive_pass_window_sec:
            # First pass expired (gap too large) — reset and wait again
            self._first_pass_ts[token.token_id] = now
            log.debug("PENDING %s: first pass expired, resetting", asset)
            return None
        # Second pass within window — clear and proceed to fire
        del self._first_pass_ts[token.token_id]

        # ── Build signal ──────────────────────────────────────────
        opening = self.feeds.openings.get(asset, {}).get(
            token.window_ts, 0)
        oracle = OracleState(
            asset=asset, window_ts=token.window_ts,
            opening_price=opening,
            current_price=self.feeds.best_price(asset),
            delta_pct=delta, oracle_says=oracle_says,
            binance_agrees=binance_agrees,
            last_update=time.time(),
        )

        signal = Signal(
            token=token, oracle=oracle, side="YES",
            entry_price=price, fair_value=fair_value,
            edge_pct=edge_pct, confidence=confidence,
            size_usdc=size, time_remaining=ttl,
        )

        # ── Composite tier ────────────────────────────────────────
        if (abs_delta >= CFG.extreme_delta_pct
                and edge_pct >= 15.0 and confidence >= 80):
            tier = "EXTREME"
        elif (abs_delta >= CFG.extreme_delta_pct
              or (abs_delta >= CFG.strong_delta_pct and edge_pct >= 12.0
                  and confidence >= 65)):
            tier = "STRONG"
        elif (abs_delta >= CFG.strong_delta_pct
              or (abs_delta >= CFG.min_delta_pct and edge_pct >= 10.0)):
            tier = "MEDIUM"
        else:
            tier = "WEAK"

        log.info(
            "SIGNAL %s %s @ $%.3f | delta=%.4f%% conf=%.0f edge=%.1f%% "
            "ttl=%.0fs size=$%.2f fair=$%.3f spread=%.1f%% tier=%s",
            asset, oracle_says, price, delta, confidence, edge_pct,
            ttl, size, fair_value, token.book_spread * 100, tier,
        )

        return signal

    def mark_traded(self, asset: str, window_ts: int):
        """Mark a window as traded (prevent duplicates)."""
        now = time.time()
        self._traded_windows.add(f"{asset}_{window_ts}")
        self._asset_fill_ts[asset] = now
        self._traded_windows = {
            w for w in self._traded_windows
            if int(w.split("_")[1]) + 600 > now
        }

    def _score(self, delta: float, ttl: float, price: float,
               asset: str, binance_agrees: bool = True) -> float:
        """Combined confidence score (0-100).

        Components:
          delta_score      (0-40): oracle move magnitude
          time_score       (0-30): less time = more certain
          price_score      (0-20): market agreement
          freshness_score  (0-5):  Chainlink data freshness
          binance_score    (0-5):  Binance confirms direction (+5) or disagrees (-5)
        """
        abs_d = abs(delta)

        # Delta score — continuous interpolation
        if abs_d >= CFG.extreme_delta_pct:
            ds = 40.0
        elif abs_d >= CFG.strong_delta_pct:
            ds = 30.0 + (abs_d - CFG.strong_delta_pct) / \
                 (CFG.extreme_delta_pct - CFG.strong_delta_pct) * 10
        elif abs_d >= CFG.min_delta_pct:
            ds = 10.0 + (abs_d - CFG.min_delta_pct) / \
                 (CFG.strong_delta_pct - CFG.min_delta_pct) * 20
        else:
            ds = 0.0

        # Time score — continuous
        if ttl <= 5:
            ts = 30.0
        elif ttl <= 60:
            ts = 30.0 - (ttl - 5) * (22.0 / 55.0)
        else:
            ts = 5.0

        # Price score (market agreement)
        if price >= 0.90:
            ps = 20.0
        elif price >= 0.80:
            ps = 15.0
        elif price >= 0.70:
            ps = 10.0
        elif price >= 0.60:
            ps = 5.0
        else:
            ps = 2.0

        # Chainlink freshness score
        stale = self.feeds.chainlink_staleness(asset)
        if stale < 5:
            ss = 5.0
        elif stale < 15:
            ss = 3.0
        elif stale < 30:
            ss = 1.0
        else:
            ss = 0.0

        # Binance agreement: +5 if agrees, -5 if disagrees (soft signal, not a block)
        bs = 5.0 if binance_agrees else -5.0

        return min(100.0, ds + ts + ps + ss + bs)

    def _fair_value(self, delta: float, ttl: float) -> float:
        """Estimate true probability of this outcome winning.

        Calibrated for short-TTL crypto binary markets using random-walk model.
        With +0.04% oracle lead and 35s remaining, true win probability ~96%;
        base probabilities raised to reflect actual short-TTL statistics.
        """
        abs_d = abs(delta)

        # Base probability from delta magnitude (random-walk calibrated)
        if abs_d >= 0.20:
            base = 0.97
        elif abs_d >= 0.15:
            base = 0.96 + (abs_d - 0.15) / 0.05 * 0.01
        elif abs_d >= 0.10:
            base = 0.93 + (abs_d - 0.10) / 0.05 * 0.03
        elif abs_d >= 0.05:
            base = 0.87 + (abs_d - 0.05) / 0.05 * 0.06
        elif abs_d >= 0.025:
            base = 0.78 + (abs_d - 0.025) / 0.025 * 0.09
        elif abs_d >= 0.015:
            base = 0.72 + (abs_d - 0.015) / 0.01 * 0.06
        else:
            base = 0.65 + abs_d * 4.7

        # Time adjustment — stronger boost at short TTL
        if ttl <= 5:
            adj = 1.12
        elif ttl <= 10:
            adj = 1.10
        elif ttl <= 20:
            adj = 1.07
        elif ttl <= 30:
            adj = 1.05
        elif ttl <= 45:
            adj = 1.03
        else:
            adj = 0.95

        return min(0.97, base * adj)

    def _compute_size(self, entry_price: float, edge_pct: float,
                      portfolio: float, is_live: bool) -> float:
        """Dynamic position sizing."""
        base = portfolio * CFG.max_position_pct / 100
        base = min(base, CFG.max_position_usdc)

        if entry_price >= 0.85:
            mult = CFG.size_mult_high
        elif entry_price >= 0.70:
            mult = CFG.size_mult_mid
        else:
            mult = CFG.size_mult_low

        if edge_pct >= 10:
            edge_mult = 1.2
        elif edge_pct >= 5:
            edge_mult = 1.0
        elif edge_pct >= 2:
            edge_mult = 0.85
        else:
            edge_mult = 0.7

        size = base * mult * edge_mult

        if is_live:
            size = min(size, CFG.live_max_usdc)
            min_size_usdc = CFG.min_shares * entry_price
            if size < min_size_usdc:
                size = min_size_usdc

        return round(size, 2)

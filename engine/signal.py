"""
Signal engine: Oracle-confirmed end-cycle sniper (Strategy D).

Optimizations applied:
  1. Tiered entry windows — strong deltas can enter earlier
  2. Adaptive confidence threshold — lower bar for high-conviction signals
  3. Improved fair_value curve with better calibration
  4. Minimum edge scaled by entry price (cheap tokens need bigger edge)
  5. Kelly-informed sizing with edge/variance awareness
"""

import logging
import time
from typing import Optional

from core.config import CFG
from core.models import Token, OracleState, Signal
from feeds.prices import PriceFeeds

log = logging.getLogger("hybrid.engine")


class HybridEngine:
    def __init__(self, feeds: PriceFeeds):
        self.feeds = feeds
        self._traded_windows: set[str] = set()

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

        # ── GATE 2: Capture opening price ─────────────────────────
        self.feeds.capture_opening(asset, token.window_ts)

        # ── GATE 3: Oracle delta ──────────────────────────────────
        delta = self.feeds.oracle_delta(asset, token.window_ts)
        abs_delta = abs(delta)
        if abs_delta < CFG.min_delta_pct:
            return None

        # ── GATE 4: Tiered timing based on delta strength ─────────
        # Stronger delta = can enter earlier with confidence
        # Weak delta = wait longer for time confirmation
        if abs_delta >= CFG.extreme_delta_pct:
            max_entry_sec = CFG.snipe_entry_sec       # T-55s
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

        # ── GATE 7: Confidence scoring (adaptive threshold) ──────
        confidence = self._score(delta, ttl, price, asset,
                                 binance_agrees=binance_agrees)

        # Strong deltas get a lower confidence threshold
        threshold = (CFG.min_confidence_strong
                     if abs_delta >= CFG.strong_delta_pct
                     else CFG.min_confidence)
        if confidence < threshold:
            return None

        # ── GATE 8: Fair value and edge ───────────────────────────
        fair_value = self._fair_value(delta, ttl)
        edge_pct = (fair_value - price) * 100

        # Minimum edge must exceed break-even given the 10% taker fee.
        # Break-even: fair_value = price / (1 - fee), so
        #   min_edge = price * fee / (1 - fee) * 100 + 1.0 (safety buffer)
        # Floor of fee*50 (5.0%) covers very cheap tokens.
        _fee = CFG.taker_fee_pct / 100
        min_edge = max(_fee * 50, price * _fee / (1 - _fee) * 100 + 1.0)
        if edge_pct < min_edge:
            return None

        # ── Position sizing ───────────────────────────────────────
        size = self._compute_size(price, edge_pct, portfolio, is_live)
        if size < 1.0:
            return None

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

        # ── Composite tier: delta + edge + confidence ─────────────
        # Delta alone is misleading — a 0.36% delta with 0.6% edge and
        # conf=79 is not EXTREME. Tier reflects actual trade quality.
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
            "ttl=%.0fs size=$%.2f fair=$%.3f tier=%s",
            asset, oracle_says, price, delta, confidence, edge_pct,
            ttl, size, fair_value, tier,
        )

        return signal

    def mark_traded(self, asset: str, window_ts: int):
        """Mark a window as traded (prevent duplicates)."""
        self._traded_windows.add(f"{asset}_{window_ts}")
        now = time.time()
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
            # Linear from 30 (at 5s) to 8 (at 60s)
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

        Calibrated from observed market data. The key insight is that
        fair value depends on BOTH delta magnitude AND time remaining.
        A 0.05% delta at T-50s is much less certain than at T-10s.
        """
        abs_d = abs(delta)

        # Base probability from delta magnitude
        if abs_d >= 0.20:
            base = 0.95
        elif abs_d >= 0.15:
            base = 0.90 + (abs_d - 0.15) / 0.05 * 0.05
        elif abs_d >= 0.10:
            base = 0.78 + (abs_d - 0.10) / 0.05 * 0.12
        elif abs_d >= 0.05:
            base = 0.63 + (abs_d - 0.05) / 0.05 * 0.15
        elif abs_d >= 0.03:
            base = 0.57 + (abs_d - 0.03) / 0.02 * 0.06
        elif abs_d >= 0.02:
            base = 0.54 + (abs_d - 0.02) / 0.01 * 0.03
        else:
            base = 0.50 + abs_d * 2.0

        # Time adjustment — continuous multiplier
        # Less time = delta more likely to hold = higher probability
        if ttl <= 5:
            adj = 1.06
        elif ttl <= 10:
            adj = 1.04
        elif ttl <= 20:
            adj = 1.02
        elif ttl <= 30:
            adj = 1.00
        elif ttl <= 45:
            adj = 0.98
        else:
            adj = 0.95  # early entries get discounted more

        return min(0.97, base * adj)

    def _compute_size(self, entry_price: float, edge_pct: float,
                      portfolio: float, is_live: bool) -> float:
        """Dynamic position sizing.

        Uses a simplified Kelly-informed approach:
        - Base size from portfolio percentage
        - Scaled by entry price tier
        - Further scaled by edge magnitude (bigger edge = bigger bet)
        """
        # Base size
        base = portfolio * CFG.max_position_pct / 100
        base = min(base, CFG.max_position_usdc)

        # Tier multiplier
        if entry_price >= 0.85:
            mult = CFG.size_mult_high
        elif entry_price >= 0.70:
            mult = CFG.size_mult_mid
        else:
            mult = CFG.size_mult_low

        # Edge scaling: scale size by edge confidence
        # Edge of 1% → 0.7x, edge of 5% → 1.0x, edge of 10%+ → 1.2x
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
            # Ensure size produces at least min_shares at this entry price
            # (Polymarket rejects orders below 5 shares)
            min_size_usdc = CFG.min_shares * entry_price
            if size < min_size_usdc:
                size = min_size_usdc

        return round(size, 2)

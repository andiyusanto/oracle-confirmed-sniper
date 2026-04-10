"""
Signal engine: Oracle-confirmed end-cycle sniper (Strategy D).

Combines oracle-lead detection with end-cycle sniping.
Only trades when ALL of these are true simultaneously:
  1. Time remaining is within the snipe window (T-60s to T-3s)
  2. Oracle (Chainlink) delta from opening confirms direction
  3. Token price is in the profitable range ($0.55-$0.95)
  4. Combined confidence score exceeds threshold
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
        self._traded_windows: set[str] = set()  # "BTC_1775139300" etc

    def evaluate(self, token: Token, portfolio: float,
                 is_live: bool = False) -> Optional[Signal]:
        """
        Evaluate a single token for a snipe opportunity.
        Returns Signal if all conditions met, None otherwise.
        """
        now = time.time()
        ttl = token.end_ts - now
        asset = token.asset

        # ── GATE 1: Timing ──────────────────────────────────────────
        if ttl > CFG.snipe_entry_sec or ttl < CFG.snipe_exit_sec:
            return None

        # ── GATE 2: Not already traded this window ──────────────────
        wkey = f"{asset}_{token.window_ts}"
        if wkey in self._traded_windows:
            return None

        # ── GATE 3: Capture opening price ───────────────────────────
        self.feeds.capture_opening(asset, token.window_ts)

        # ── GATE 4: Oracle delta ────────────────────────────────────
        delta = self.feeds.oracle_delta(asset, token.window_ts)
        if abs(delta) < CFG.min_delta_pct:
            return None

        oracle_says = "UP" if delta > 0 else "DOWN"

        # ── GATE 5: Oracle must agree with token direction ──────────
        # If oracle says UP, we want to buy the UP token (YES side)
        # If oracle says DOWN, we want to buy the DOWN token (YES side)
        # OR buy NO on the opposite token
        if oracle_says != token.direction:
            # This token is on the losing side — skip
            # (We'll catch the winning-side token separately)
            return None

        # ── GATE 6: Token price in range ────────────────────────────
        price = token.book_price
        if price < CFG.min_token_price or price > CFG.max_token_price:
            return None

        # ── GATE 7: Confidence scoring ──────────────────────────────
        confidence = self._score(delta, ttl, price, asset)
        if confidence < CFG.min_confidence:
            return None

        # ── Compute fair value and edge ─────────────────────────────
        fair_value = self._fair_value(delta, ttl)
        edge_pct = (fair_value - price) * 100

        if edge_pct < 0.5:  # minimum 0.5% edge after pricing
            return None

        # ── Position sizing ─────────────────────────────────────────
        size = self._compute_size(price, portfolio, is_live)
        if size < 1.0:
            return None

        # ── Build oracle state ──────────────────────────────────────
        opening = self.feeds.openings.get(asset, {}).get(token.window_ts, 0)
        oracle = OracleState(
            asset=asset, window_ts=token.window_ts,
            opening_price=opening,
            current_price=self.feeds.best_price(asset),
            delta_pct=delta, oracle_says=oracle_says,
            binance_agrees=self.feeds.binance_agrees(asset, oracle_says),
            last_update=time.time(),
        )

        signal = Signal(
            token=token, oracle=oracle, side="YES",
            entry_price=price, fair_value=fair_value,
            edge_pct=edge_pct, confidence=confidence,
            size_usdc=size, time_remaining=ttl,
        )

        log.info(
            "SIGNAL %s %s @ $%.3f | delta=%.4f%% conf=%.0f edge=%.1f%% "
            "ttl=%.0fs size=$%.2f fair=$%.3f open=$%.2f",
            asset, oracle_says, price, delta, confidence, edge_pct,
            ttl, size, fair_value, opening,
        )

        return signal

    def mark_traded(self, asset: str, window_ts: int):
        """Mark a window as traded (prevent duplicates)."""
        self._traded_windows.add(f"{asset}_{window_ts}")
        # Prune old entries
        now = time.time()
        self._traded_windows = {
            w for w in self._traded_windows
            if int(w.split("_")[1]) + 600 > now  # keep 10 min
        }

    def _score(self, delta: float, ttl: float, price: float, asset: str) -> float:
        """
        Combined confidence score (0-100).
        Components:
          delta_score  (0-40): oracle move magnitude
          time_score   (0-30): less time = more certain
          price_score  (0-20): market agreement (higher price = more certain)
          source_score (0-10): Chainlink freshness
        """
        abs_d = abs(delta)

        # Delta score
        if abs_d >= CFG.extreme_delta_pct:
            ds = 40.0
        elif abs_d >= CFG.strong_delta_pct:
            ds = 30.0 + (abs_d - CFG.strong_delta_pct) / (CFG.extreme_delta_pct - CFG.strong_delta_pct) * 10
        elif abs_d >= CFG.min_delta_pct:
            ds = 10.0 + (abs_d - CFG.min_delta_pct) / (CFG.strong_delta_pct - CFG.min_delta_pct) * 20
        else:
            ds = 0.0

        # Time score
        if ttl <= 10:
            ts = 30.0
        elif ttl <= 20:
            ts = 25.0
        elif ttl <= 30:
            ts = 20.0
        elif ttl <= 45:
            ts = 15.0
        elif ttl <= 60:
            ts = 10.0
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

        # Source freshness score
        stale = self.feeds.chainlink_staleness(asset)
        if stale < 5:
            ss = 10.0
        elif stale < 15:
            ss = 7.0
        elif stale < 30:
            ss = 3.0
        else:
            ss = 0.0

        return min(100.0, ds + ts + ps + ss)

    def _fair_value(self, delta: float, ttl: float) -> float:
        """
        Estimate true probability of this outcome winning.
        Based on oracle delta magnitude and time remaining.

        Observed from market data:
          delta 0.02% → ~55% probability
          delta 0.05% → ~65% probability
          delta 0.10% → ~80% probability
          delta 0.15%+ → ~90% probability

        Time decay: less time = delta more likely to hold
        """
        abs_d = abs(delta)

        # Base probability from delta
        if abs_d >= 0.15:
            base = 0.92
        elif abs_d >= 0.10:
            base = 0.80 + (abs_d - 0.10) / 0.05 * 0.12
        elif abs_d >= 0.05:
            base = 0.65 + (abs_d - 0.05) / 0.05 * 0.15
        elif abs_d >= 0.03:
            base = 0.58 + (abs_d - 0.03) / 0.02 * 0.07
        elif abs_d >= 0.02:
            base = 0.55 + (abs_d - 0.02) / 0.01 * 0.03
        else:
            base = 0.50 + abs_d * 2.5

        # Time adjustment: less time = delta holds better
        if ttl <= 10:
            adj = 1.05  # 5% boost — almost no time to reverse
        elif ttl <= 20:
            adj = 1.03
        elif ttl <= 30:
            adj = 1.01
        elif ttl <= 45:
            adj = 1.00
        else:
            adj = 0.97  # slight discount — more time to reverse

        return min(0.97, base * adj)

    def _compute_size(self, entry_price: float, portfolio: float, is_live: bool) -> float:
        """Dynamic position sizing based on entry price tier."""
        # Base size from config
        base = portfolio * CFG.max_position_pct / 100
        base = min(base, CFG.max_position_usdc)

        # Tier multiplier: higher entry = more confident = bigger size
        if entry_price >= 0.85:
            mult = CFG.size_mult_high
        elif entry_price >= 0.70:
            mult = CFG.size_mult_mid
        else:
            mult = CFG.size_mult_low

        size = base * mult

        if is_live:
            size = min(size, CFG.live_max_usdc)

        return round(size, 2)

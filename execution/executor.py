"""Trade execution and position management."""

import logging
import random
import time
import uuid

from core.config import CFG
from core.database import Database
from core.models import Signal, Trade
from feeds.prices import PriceFeeds

log = logging.getLogger("hybrid.executor")


class Executor:
    def __init__(self, db: Database, feeds: PriceFeeds, is_live: bool):
        self.db = db
        self.feeds = feeds
        self.is_live = is_live
        self.open_positions: dict[str, Trade] = {}
        self._last_order_ts = 0.0

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    def execute(self, signal: Signal) -> Trade:
        """Execute a signal — paper or live."""
        now = time.time()
        if now - self._last_order_ts < CFG.cooldown_sec:
            return None

        trade = Trade(
            id=f"H-{uuid.uuid4().hex[:10]}",
            asset=signal.token.asset,
            direction=signal.token.direction,
            side=signal.side,
            entry_price=signal.entry_price,
            size_usdc=signal.size_usdc,
            oracle_delta=signal.oracle.delta_pct,
            confidence=signal.confidence,
            status="OPEN",
            mode="LIVE" if self.is_live else "PAPER",
            opened_at=now,
            window_ts=signal.token.window_ts,
            time_remaining=signal.time_remaining,
            fair_value=signal.fair_value,
            binance_price=self.feeds.binance.get(signal.token.asset, 0),
            chainlink_price=self.feeds.chainlink.get(signal.token.asset, 0),
            opening_price=signal.oracle.opening_price,
        )

        if self.is_live:
            # TODO: implement live execution via py_clob_client
            log.info("LIVE ORDER: %s %s %s $%.2f (NOT YET IMPLEMENTED)",
                     trade.asset, trade.direction, trade.side, trade.size_usdc)
        else:
            log.info("PAPER: %s %s %s @ $%.4f size=$%.2f delta=%.4f%%",
                     trade.asset, trade.direction, trade.side,
                     trade.entry_price, trade.size_usdc, trade.oracle_delta)

        self.db.save_trade(trade)
        wkey = f"{trade.asset}_{trade.window_ts}"
        self.open_positions[wkey] = trade
        self._last_order_ts = now
        return trade

    def close_expired(self):
        """Close positions whose windows have expired."""
        now = time.time()
        to_close = []

        for wkey, trade in list(self.open_positions.items()):
            dur_sec = 300  # 5min default
            window_end = trade.window_ts + dur_sec
            if now < window_end + 3:
                continue
            to_close.append((wkey, trade))

        for wkey, trade in to_close:
            pnl = self._compute_pnl(trade)
            trade.pnl = pnl
            trade.status = "EXPIRED"
            trade.closed_at = now
            self.db.close_trade(trade.id, pnl)
            del self.open_positions[wkey]

            tag = "WIN" if pnl > 0 else "LOSS"
            log.info("[%s] %s %s pnl=$%+.4f (delta=%.4f%% entry=$%.3f)",
                     tag, trade.asset, trade.direction, pnl,
                     trade.oracle_delta, trade.entry_price)

        return to_close

    def _compute_pnl(self, trade: Trade) -> float:
        """
        Compute paper P&L.
        The outcome depends on whether the oracle delta at close
        matches the trade's direction.
        """
        # Get final delta
        final_delta = self.feeds.oracle_delta(trade.asset, trade.window_ts)

        # Determine if the trade's direction won
        if trade.direction == "UP":
            won = final_delta >= 0
        else:
            won = final_delta < 0

        # If we bought YES on the winning side
        if trade.side == "YES":
            outcome_won = won
        else:
            outcome_won = not won

        shares = trade.size_usdc / trade.entry_price

        if outcome_won:
            pnl = shares * (1.0 - trade.entry_price)
            if CFG.use_maker:
                pnl += trade.size_usdc * CFG.maker_rebate_pct / 100
            else:
                pnl -= trade.size_usdc * CFG.taker_fee_pct / 100
        else:
            pnl = -trade.size_usdc

        return round(pnl, 6)

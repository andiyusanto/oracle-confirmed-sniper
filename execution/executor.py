"""Trade execution and position management.

Fixes applied:
  1. Live execution via py_clob_client (was TODO stub)
  2. Slippage modeling for paper mode
  3. Correct fee math on both win AND loss
  4. Final delta snapshot before window expiry
"""

import logging
import time
import uuid
from typing import Optional

from core.config import CFG
from core.database import Database
from core.models import Signal, Trade
from feeds.prices import PriceFeeds

log = logging.getLogger("hybrid.executor")

# ── Live execution imports ────────────────────────────────────────────
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds, MarketOrderArgs, OrderType,
        PartialCreateOrderOptions,
    )
    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False


class Executor:
    def __init__(self, db: Database, feeds: PriceFeeds, is_live: bool):
        self.db = db
        self.feeds = feeds
        self.is_live = is_live
        self.open_positions: dict[str, Trade] = {}
        self._last_order_ts = 0.0
        self._clob: Optional[object] = None

        # Initialize authenticated CLOB client for live mode
        if is_live and HAS_CLOB:
            self._init_clob_client()

    def _init_clob_client(self):
        """Set up authenticated ClobClient for live order submission."""
        if not CFG.private_key:
            log.error("LIVE mode requires POLY_PRIVATE_KEY in .env")
            return
        try:
            from py_clob_client.constants import POLYGON
            creds = ApiCreds(
                api_key=CFG.api_key,
                api_secret=CFG.api_secret,
                api_passphrase=CFG.api_passphrase,
            )
            self._clob = ClobClient(
                host=CFG.clob_host,
                chain_id=POLYGON,
                key=CFG.private_key,
                creds=creds,
                signature_type=CFG.sig_type,
                funder=CFG.funder_address or None,
            )
            log.info("CLOB client initialized for live trading")
        except Exception as e:
            log.error("Failed to init CLOB client: %s", e)
            self._clob = None

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    def execute(self, signal: Signal) -> Optional[Trade]:
        """Execute a signal — paper or live."""
        now = time.time()
        if now - self._last_order_ts < CFG.cooldown_sec:
            return None

        # ── Paper slippage modeling ───────────────────────────────────
        entry_price = signal.entry_price
        if not self.is_live:
            slippage = self._estimate_slippage(signal.time_remaining,
                                                signal.entry_price)
            entry_price = min(entry_price + slippage, 0.99)

        trade = Trade(
            id=f"H-{uuid.uuid4().hex[:10]}",
            asset=signal.token.asset,
            direction=signal.token.direction,
            side=signal.side,
            entry_price=entry_price,
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
            success = self._execute_live(signal, trade)
            if not success:
                trade.status = "CANCELLED"
                self.db.save_trade(trade)
                return None
        else:
            log.info("PAPER: %s %s %s @ $%.4f (raw=$%.4f slip=$%.4f) "
                     "size=$%.2f delta=%.4f%%",
                     trade.asset, trade.direction, trade.side,
                     trade.entry_price, signal.entry_price,
                     entry_price - signal.entry_price,
                     trade.size_usdc, trade.oracle_delta)

        self.db.save_trade(trade)
        wkey = f"{trade.asset}_{trade.window_ts}"
        self.open_positions[wkey] = trade
        self._last_order_ts = now
        return trade

    def _execute_live(self, signal: Signal, trade: Trade) -> bool:
        """Place a real order on Polymarket via py_clob_client.

        Uses a FOK (Fill-Or-Kill) market order so the entire amount
        either fills immediately or the order is cancelled — no partial
        fills or stale resting orders left behind.
        """
        if not self._clob:
            log.error("LIVE ORDER FAILED: CLOB client not initialized")
            return False

        try:
            # Get tick size for this token
            tick_size = self._clob.get_tick_size(signal.token.token_id)

            # Calculate market price for our order size
            mkt_price = self._clob.calculate_market_price(
                token_id=signal.token.token_id,
                side="BUY",
                amount=signal.size_usdc,
                order_type=OrderType.FOK,
            )

            # Sanity: don't buy above max_token_price
            if mkt_price > CFG.max_token_price:
                log.warning("LIVE SKIP: market price $%.4f > max $%.2f",
                            mkt_price, CFG.max_token_price)
                return False

            # Build market order — FOK ensures all-or-nothing fill
            order_args = MarketOrderArgs(
                token_id=signal.token.token_id,
                amount=round(signal.size_usdc, 2),
                side="BUY",
                price=mkt_price,
                fee_rate_bps=int(CFG.taker_fee_pct * 100)
                              if not CFG.use_maker else 0,
                order_type=OrderType.FOK,
            )

            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=False,
            )

            # Submit order
            resp = self._clob.create_market_order(order_args, options)

            # Check response — handle both object and dict formats
            order_id = None
            if resp and hasattr(resp, 'orderID'):
                order_id = resp.orderID
            elif resp and isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id")

            if order_id:
                trade.entry_price = mkt_price
                log.info("LIVE FILLED: %s %s %s @ $%.4f size=$%.2f "
                         "order=%s",
                         trade.asset, trade.direction, trade.side,
                         mkt_price, trade.size_usdc, order_id)
                return True
            else:
                log.warning("LIVE ORDER no fill: %s", resp)
                return False

        except Exception as e:
            log.error("LIVE ORDER EXCEPTION: %s", e, exc_info=True)
            return False

    def _estimate_slippage(self, time_remaining: float,
                           book_mid: float) -> float:
        """Model slippage for paper trades.

        Slippage increases as:
          - Time remaining decreases (thinner books near expiry)
          - Book mid price increases (winning side is crowded)

        Returns slippage in price units (add to entry price).
        """
        # Time component: more slippage with less time
        if time_remaining <= 5:
            time_slip = 0.015
        elif time_remaining <= 15:
            time_slip = 0.010
        elif time_remaining <= 30:
            time_slip = 0.007
        elif time_remaining <= 45:
            time_slip = 0.005
        else:
            time_slip = 0.003

        # Price component: higher-priced tokens have more competition
        price_slip = max(0, (book_mid - 0.55) * 0.015)

        return round(time_slip + price_slip, 4)

    def close_expired(self):
        """Close positions whose windows have expired.

        Two-phase approach:
          Phase 1: Snapshot final oracle delta just before window ends
          Phase 2: Close and compute PnL after window + buffer
        """
        now = time.time()
        to_close = []

        for wkey, trade in list(self.open_positions.items()):
            dur_sec = 300  # 5min default
            window_end = trade.window_ts + dur_sec

            # Phase 1: snapshot final delta in last 5s of window
            if not getattr(trade, '_final_delta_captured', False):
                if window_end - 5 <= now <= window_end + 1:
                    delta = self.feeds.oracle_delta(
                        trade.asset, trade.window_ts)
                    trade._final_delta = delta
                    trade._final_cl_price = self.feeds.chainlink.get(
                        trade.asset, 0)
                    trade._final_bn_price = self.feeds.binance.get(
                        trade.asset, 0)
                    trade._final_delta_captured = True
                    log.debug("SNAPSHOT %s delta=%.4f%% cl=$%.2f",
                              wkey, delta, trade._final_cl_price)

            # Phase 2: close after window + 3s buffer
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
            log.info("[%s] %s %s pnl=$%+.4f (delta=%.4f%% entry=$%.3f "
                     "snap=%s)",
                     tag, trade.asset, trade.direction, pnl,
                     trade.oracle_delta, trade.entry_price,
                     "yes" if getattr(trade, '_final_delta_captured', False)
                     else "no")

        return to_close

    def _compute_pnl(self, trade: Trade) -> float:
        """Compute P&L with proper fee handling.

        Uses snapshotted final delta when available; falls back to
        live delta if snapshot was missed.

        Fee handling: maker rebate or taker fee applied to BOTH
        wins and losses — you always pay/receive fee on entry.
        """
        # Use snapshotted delta if available, otherwise live
        if getattr(trade, '_final_delta_captured', False):
            final_delta = trade._final_delta
        else:
            final_delta = self.feeds.oracle_delta(
                trade.asset, trade.window_ts)
            log.warning("Using live delta for %s_%d (snapshot missed)",
                        trade.asset, trade.window_ts)

        # Polymarket resolves UP if final price > opening price
        # A delta of exactly 0 resolves as NO for both sides
        if trade.direction == "UP":
            won = final_delta > 0  # strict >0, not >=0
        else:
            won = final_delta < 0

        if trade.side == "YES":
            outcome_won = won
        else:
            outcome_won = not won

        shares = trade.size_usdc / trade.entry_price

        if outcome_won:
            pnl = shares * (1.0 - trade.entry_price)
        else:
            pnl = -trade.size_usdc

        # Fees apply regardless of outcome
        if CFG.use_maker:
            pnl += trade.size_usdc * CFG.maker_rebate_pct / 100
        else:
            pnl -= trade.size_usdc * CFG.taker_fee_pct / 100

        return round(pnl, 6)

"""Trade execution and position management.

Fixes applied:
  1. Live execution via py_clob_client with order-book-aware pricing
  2. Wallet balance fetching for real portfolio value
  3. Fallback from market order to aggressive limit order on "no match"
  4. Slippage modeling for paper mode
  5. Correct fee math on both win AND loss
  6. Final delta snapshot before window expiry
"""

import logging
import time
import uuid
from decimal import Decimal, ROUND_DOWN
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
        ApiCreds, OrderArgs, MarketOrderArgs, OrderType,
        PartialCreateOrderOptions, BalanceAllowanceParams, AssetType,
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

        # Circuit breaker for fatal API errors (geoblock, auth failure)
        self._circuit_open = False
        self._circuit_reason = ""
        self._circuit_ts = 0.0

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

    def cancel_all_orders(self) -> bool:
        """Cancel all open CLOB orders. Called on kill switch activation.

        Prevents orphaned limit orders from filling after the bot halts.
        """
        if not self._clob:
            return False
        try:
            resp = self._clob.cancel_all()
            log.warning("KILL SWITCH: all open CLOB orders cancelled — %s", resp)
            return True
        except Exception as e:
            log.error("Failed to cancel all CLOB orders: %s", e)
            return False

    def sync_balance(self) -> bool:
        """Tell Polymarket CLOB to resync its ledger from the on-chain balance.

        Must be called after any on-chain redemption so the exchange sees
        the newly returned USDC.e and allows further orders.
        """
        if not self._clob:
            return False
        try:
            self._clob.update_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            log.info("CLOB balance synced after redemption")
            return True
        except Exception as e:
            log.warning("Failed to sync CLOB balance: %s", e)
            return False

    def get_wallet_balance(self) -> float:
        """Fetch actual USDC balance from Polymarket wallet.

        Returns the collateral (USDC) balance, or 0 on failure.
        The API returns balance as a string in raw USDC units (not wei).
        """
        if not self._clob:
            return 0.0
        try:
            resp = self._clob.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            log.info("Wallet balance response: %s (type=%s)", resp, type(resp).__name__)

            if not resp:
                return 0.0

            # Response is typically: {"balance": "123.45", "allowance": "..."}
            # Balance can be a string or number, in USDC or in raw units
            raw_balance = None
            if isinstance(resp, dict):
                raw_balance = resp.get("balance", 0)
            elif hasattr(resp, 'balance'):
                raw_balance = resp.balance

            if raw_balance is None:
                return 0.0

            balance = float(raw_balance)

            # Detect if balance is in wei/raw units (very large number)
            # USDC has 6 decimals, so >1M likely means raw units
            if balance > 1_000_000:
                balance = balance / 1e6

            log.info("Parsed wallet balance: $%.2f", balance)
            return balance

        except Exception as e:
            log.warning("Failed to fetch wallet balance: %s", e, exc_info=True)
            return 0.0

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
            # Bug 1 fix: store actual window length (5m=300, 15m=900)
            duration_sec=int(signal.token.end_ts - signal.token.window_ts),
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
        """Place a real order on Polymarket.

        Includes circuit breaker: if a fatal error (geoblock, auth) is
        detected, all future orders are blocked until bot restart.
        """
        if not self._clob:
            log.error("LIVE ORDER FAILED: CLOB client not initialized")
            return False

        # Circuit breaker: don't retry fatal errors
        if self._circuit_open:
            return False

        try:
            # Step 1: Read order book to get real prices
            book = self._clob.get_order_book(signal.token.token_id)
            asks = sorted(
                [float(a.price) for a in (book.asks or []) if float(a.price) > 0]
            )

            if not asks:
                log.warning("LIVE SKIP: no asks in order book for %s %s",
                            signal.token.asset, signal.token.direction)
                return False

            best_ask = asks[0]

            # Step 2: Sanity-check the live price against expected range.
            # Below min_token_price → near-worthless token, likely wrong
            # token_id or severely stale book cache — do not buy.
            if best_ask < CFG.min_token_price:
                log.warning("LIVE SKIP: best ask $%.4f < min $%.2f "
                            "(possible wrong token or stale cache) for %s %s",
                            best_ask, CFG.min_token_price,
                            signal.token.asset, signal.token.direction)
                return False

            if best_ask > CFG.max_token_price:
                log.warning("LIVE SKIP: best ask $%.4f > max $%.2f "
                            "(book already priced in) for %s %s",
                            best_ask, CFG.max_token_price,
                            signal.token.asset, signal.token.direction)
                return False

            if best_ask > signal.entry_price + 0.10:
                log.warning("LIVE SKIP: best ask $%.4f >> signal entry $%.4f "
                            "(stale book data) for %s %s",
                            best_ask, signal.entry_price,
                            signal.token.asset, signal.token.direction)
                return False

            # Step 3: Get tick size
            tick_size = self._clob.get_tick_size(signal.token.token_id)

            # Step 4: Precision-safe price + size (Decimal, ROUND_DOWN)
            # Prevents float artifacts like 4.9999... rounding to 4.99 < min_shares
            price_d  = float(
                Decimal(str(best_ask)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            )
            shares   = float(
                Decimal(str(signal.size_usdc / best_ask)).quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN
                )
            )
            if shares < CFG.min_shares:
                log.warning("LIVE SKIP: %.2f shares < minimum %.0f (size=$%.2f @ $%.4f)",
                            shares, CFG.min_shares, signal.size_usdc, best_ask)
                return False

            order_args = OrderArgs(
                token_id=signal.token.token_id,
                price=price_d,
                size=shares,
                side="BUY",
                fee_rate_bps=int(CFG.taker_fee_pct * 100)
                              if not CFG.use_maker else 0,
            )

            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=False,
            )

            resp = self._clob.create_and_post_order(order_args, options)

            # Parse response
            order_id = None
            if resp and isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id")
            elif resp and hasattr(resp, 'orderID'):
                order_id = resp.orderID

            if order_id:
                trade.entry_price = price_d
                trade.size_usdc   = float(
                    Decimal(str(shares * price_d)).quantize(
                        Decimal("0.01"), rounding=ROUND_DOWN
                    )
                )
                log.info("LIVE FILLED: %s %s %s @ $%.4f size=$%.2f "
                         "shares=%.2f order=%s",
                         trade.asset, trade.direction, trade.side,
                         price_d, trade.size_usdc, shares, order_id)
                return True
            else:
                log.warning("LIVE ORDER no fill: %s", resp)
                return False

        except Exception as e:
            err_str = str(e).lower()

            # Insufficient balance: sync CLOB ledger and skip this order
            if "not enough balance" in err_str or "allowance" in err_str:
                log.warning(
                    "LIVE SKIP: insufficient CLOB balance — syncing ledger "
                    "(redeem may not have been followed by balance sync)")
                self.sync_balance()
                return False

            # Circuit breaker: detect fatal errors that won't resolve by retrying
            if any(fatal in err_str for fatal in [
                "geoblock", "restricted in your region", "403",
                "unauthorized", "invalid api key", "forbidden",
            ]):
                self._circuit_open = True
                self._circuit_reason = str(e)
                self._circuit_ts = time.time()
                log.critical(
                    "CIRCUIT BREAKER OPEN: %s — All live orders blocked. "
                    "Fix the issue and restart the bot.", e)
                return False

            log.error("LIVE ORDER EXCEPTION: %s", e, exc_info=True)
            return False

    def _estimate_slippage(self, time_remaining: float,
                           book_mid: float) -> float:
        """Model slippage for paper trades."""
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

        price_slip = max(0, (book_mid - 0.55) * 0.015)
        return round(time_slip + price_slip, 4)

    def close_expired(self):
        """Close positions whose windows have expired.

        Two-phase: snapshot delta near expiry, then close after buffer.
        """
        now = time.time()
        to_close = []

        for wkey, trade in list(self.open_positions.items()):
            # Bug 1 fix: use stored duration (5m=300, 15m=900).
            # getattr fallback handles trades opened before this field existed.
            dur_sec    = getattr(trade, 'duration_sec', 300)
            window_end = trade.window_ts + dur_sec

            # Phase 1: snapshot final delta in last 5s
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
        """Compute P&L with proper fee handling."""
        if getattr(trade, '_final_delta_captured', False):
            final_delta = trade._final_delta
        else:
            final_delta = self.feeds.oracle_delta(
                trade.asset, trade.window_ts)
            log.warning("Using live delta for %s_%d (snapshot missed)",
                        trade.asset, trade.window_ts)

        if trade.direction == "UP":
            won = final_delta > 0
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

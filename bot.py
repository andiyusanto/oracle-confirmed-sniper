#!/usr/bin/env python3
"""
Polymarket Hybrid Oracle Sniper (Strategy D)
=============================================

Combines oracle-lead detection (A) + end-cycle sniping (B).

Only trades when:
  1. Chainlink oracle shows clear direction vs opening price
  2. Token is priced $0.55-$0.95 (market partially agrees)
  3. Last 60 seconds of the 5-minute window
  4. Combined confidence exceeds threshold

Usage:
    python bot.py                                          # paper mode
    python bot.py --portfolio 500                          # custom portfolio
    python bot.py --live --confirm-live --accept-risk      # live mode
"""

import asyncio
import argparse
import logging
import logging.handlers
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.live import Live

from core.config import CFG
from core.database import Database
from core import telegram
from core import redeem
from feeds.prices import PriceFeeds
from feeds.markets import MarketDiscovery
from engine.signal import HybridEngine
from engine.risk import RiskManager
from execution.executor import Executor
from ui.dashboard import Dashboard

# ── Logging ─────────────────────────────────────────────────────────


def _setup_logging():
    """Configure logging with daily rotation into logs/ folder.

    Active log  : logs/YYYY-MM-DD_hybrid.log  (today)
    On midnight : rotates to logs/YYYY-MM-DD_hybrid.log (new date)
    Retention   : 90 days
    """
    logs_dir = Path(CFG.log_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    log_path = logs_dir / f"{today}_hybrid.log"

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        backupCount=90,
        encoding="utf-8",
        utc=False,
    )

    # Rename rotated files from logs/YYYY-MM-DD_hybrid.log.YYYY-MM-DD
    # to   logs/YYYY-MM-DD_hybrid.log  (next day's date as prefix)
    def _namer(default_name: str) -> str:
        base, date_suffix = default_name.rsplit(".", 1)
        return str(logs_dir / f"{date_suffix}_hybrid.log")

    file_handler.namer = _namer

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    for noisy in ("httpx", "httpcore", "websockets", "asyncio", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return str(log_path)


_active_log = _setup_logging()

log = logging.getLogger("hybrid.main")


async def run(is_live: bool, portfolio: float):
    log.info("=" * 60)
    log.info("  HYBRID ORACLE SNIPER — Strategy D")
    log.info("  Mode: %s  Portfolio: $%.2f", "LIVE" if is_live else "PAPER", portfolio)
    log.info(
        "  Entry window: T-%.0fs to T-%.0fs", CFG.snipe_entry_sec, CFG.snipe_exit_sec
    )
    log.info("  Price range: $%.2f - $%.2f", CFG.min_token_price, CFG.max_token_price)
    log.info(
        "  Min delta: %.3f%%  Min confidence: %.0f",
        CFG.min_delta_pct,
        CFG.min_confidence,
    )
    log.info("=" * 60)
    if telegram.is_configured():
        log.info("Telegram notifications: ENABLED")
    else:
        log.warning(
            "Telegram notifications: DISABLED — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
        )

    # Initialize components
    db = Database(CFG.db_path)
    feeds = PriceFeeds()
    markets = MarketDiscovery(price_feeds=feeds)
    engine = HybridEngine(feeds)
    executor = Executor(db, feeds, is_live)

    # In live mode, fetch actual wallet balance as portfolio value.
    # sync_balance() must run first — it tells the CLOB to refresh its ledger
    # from on-chain state so get_wallet_balance() returns the current value.
    if is_live:
        executor.sync_balance()
        wallet_balance = executor.get_wallet_balance()
        if wallet_balance > 0:
            portfolio = wallet_balance
            log.info("Wallet balance: $%.2f (using as portfolio)", portfolio)
        else:
            log.warning(
                "Could not fetch wallet balance, using --portfolio $%.2f", portfolio
            )

    risk = RiskManager(db, portfolio)
    dash = Dashboard(db, feeds, markets, risk, executor, is_live)
    verifier = executor.verifier  # shared CapitalVerifier instance

    # Opening portfolio snapshot
    clob_balance = executor.get_wallet_balance() if is_live else None
    verifier.snapshot(portfolio, "bot_start", clob_balance)

    # Start feed tasks
    feeds._running = True
    tasks = [
        asyncio.create_task(feeds.run_rtds()),
        asyncio.create_task(feeds.run_binance()),
    ]

    # Wait for price data
    log.info("Waiting for price feeds...")
    for _ in range(30):
        if feeds.is_ready:
            break
        await asyncio.sleep(1)

    if not feeds.is_ready:
        log.error("No price data after 30s. Check network.")
        return

    for a in CFG.assets:
        log.info("%s: CL=$%.2f BN=$%.2f", a, feeds.chainlink[a], feeds.binance[a])

    # Initial market discovery
    await markets.discover()

    # Bug 2 fix: startup redemption scan — catches any positions that won
    # while the bot was offline (crash, restart, manual stop).
    if is_live:
        log.info("Startup: scanning for unredeemed winning positions...")
        _s_count, _s_usdc, _s_losses, _s_cancelled = await redeem.redeem_all_async()
        for _cid in _s_cancelled:
            _pnl_delta = db.correct_trade_to_cancelled(_cid)
            if _pnl_delta is not None:
                risk.update_portfolio(_pnl_delta)
                log.warning(
                    "Startup: market CANCELLED for conditionId=%s "
                    "(portfolio adjusted $%+.4f → $%.2f)",
                    _cid[:18],
                    _pnl_delta,
                    risk.portfolio,
                )
        for _cid in _s_losses:
            # Startup: wallet balance already reflects these losses — no portfolio adjustment.
            if db.correct_trade_to_loss(_cid) is not None:
                log.info(
                    "Startup: corrected false-WIN to LOSS for conditionId=%s", _cid[:18]
                )
        if _s_usdc > 0:
            executor.sync_balance()
            await telegram.notify_redeemed(_s_count, _s_usdc)
            log.info(
                "Startup redemption: %d position(s) $%.4f USDC.e", _s_count, _s_usdc
            )
        elif _s_count > 0:
            executor.sync_balance()

    # Telegram: bot started
    mode_str = "LIVE" if is_live else "PAPER"
    await telegram.notify_bot_start(mode_str, portfolio)

    # Pending redemption queue: timestamp when a WIN was detected.
    # Polymarket Data API takes 1–15 min after resolution to list positions
    # as redeemable, so we retry every 45s until actual USDC.e is received.
    # Note: on-chain oracle (payoutNumerators) can legitimately take 1-2+ hours
    # to settle — the queue must survive the full oracle dispute window.
    _redeem_pending_ts: float = 0.0  # 0 = no pending redemption
    _last_redeem_attempt_ts: float = 0.0  # last time we called redeem_all
    _REDEEM_RETRY_INTERVAL = 45.0  # seconds between retry attempts (was 90s)
    _REDEEM_MAX_WAIT = 14400.0  # stop queue after 4 hours (was 20 min)
    _REDEEM_SLOW_ALERT_SEC = 3600.0  # Telegram alert if still waiting at 1 hour
    _redeem_slow_alerted: bool = False  # ensures 1-hour alert fires only once

    # Periodic scan catches orphaned wins regardless of bot-computed PnL
    # or whether a new win triggers the queue (bot restart, oracle delay, etc).
    # Initialized to now so it doesn't fire immediately on the first loop
    # iteration — startup scan above already covered any pending positions.
    _PERIODIC_REDEEM_INTERVAL = 900.0  # scan every 15 min unconditionally
    _last_periodic_redeem_ts: float = time.time()
    _last_status_log_ts: float = 0.0
    _STATUS_LOG_INTERVAL = 60.0  # log oracle status every 60s
    _kill_switch_actioned: bool = False  # cancel + notify fires only once

    try:
        with Live(dash.render(), refresh_per_second=2, console=dash.console) as live:
            while True:
                now = time.time()

                # Periodic oracle/market status log — diagnose why trades aren't firing
                if now - _last_status_log_ts >= _STATUS_LOG_INTERVAL:
                    _last_status_log_ts = now
                    for a in CFG.assets:
                        cl_stale = feeds.chainlink_staleness(a)
                        delta_vals = []
                        for wts, op in feeds.openings.get(a, {}).items():
                            if op > 0:
                                cur = feeds.best_price(a)
                                if cur > 0:
                                    d = (cur - op) / op * 100
                                    delta_vals.append(f"{d:+.4f}%")
                        delta_str = (
                            ", ".join(delta_vals) if delta_vals else "no openings"
                        )
                        log.info(
                            "STATUS %s: CL=$%.2f stale=%.0fs markets=%d deltas=[%s] signals=%d/%d",
                            a,
                            feeds.chainlink[a],
                            cl_stale,
                            len(markets.tokens),
                            delta_str,
                            dash.signals_fired,
                            dash.signals_seen,
                        )

                # Rediscover markets periodically
                if markets.needs_refresh():
                    await markets.discover()

                # Close expired positions + auto-redeem wins
                closed = executor.close_expired()
                for _, trade in closed:
                    risk.on_trade_closed(trade.pnl)
                    await telegram.notify_trade_closed(trade)
                    if trade.pnl > 0 and is_live:
                        # Queue redemption — positions may not be redeemable
                        # immediately; the retry loop below handles the delay.
                        if _redeem_pending_ts == 0.0:
                            _redeem_pending_ts = now
                            log.info("WIN detected — redemption queued")

                # Retry redemption while pending — attempt immediately on first
                # detection, then every 45s. Clears only when USDC.e is confirmed.
                if is_live and _redeem_pending_ts > 0:
                    waited = now - _redeem_pending_ts
                    since_last = now - _last_redeem_attempt_ts

                    # 1-hour alert: oracle is taking unusually long (ghost redemption guard
                    # blocking — payoutNumerators still 0). Position is safe — not burned.
                    # Periodic scan will redeem automatically once oracle settles.
                    if not _redeem_slow_alerted and waited >= _REDEEM_SLOW_ALERT_SEC:
                        _redeem_slow_alerted = True
                        log.warning(
                            "Redemption still pending after %.0fm — oracle has not settled yet. "
                            "Position is safe. Periodic scan will retry every 15 min.",
                            waited / 60,
                        )
                        await telegram.notify_oracle_slow(waited)

                    if waited >= _REDEEM_MAX_WAIT:
                        # Queue has been active for 4 hours — hand off to periodic scan.
                        # The periodic scan will continue retrying every 15 min indefinitely.
                        log.warning(
                            "Redemption queue cleared after %.0fh — "
                            "periodic scan will continue retrying every 15 min. "
                            "Run redeem_now.py to force immediately.",
                            waited / 3600,
                        )
                        _redeem_pending_ts = 0.0
                        _last_redeem_attempt_ts = 0.0
                        _redeem_slow_alerted = False
                    elif since_last >= _REDEEM_RETRY_INTERVAL:
                        _last_redeem_attempt_ts = now
                        # Sync periodic timer so the two callers don't double-fire
                        # when their intervals happen to align on the same cycle.
                        _last_periodic_redeem_ts = now
                        (
                            count,
                            total_usdc,
                            lost_cids,
                            cancelled_cids,
                        ) = await redeem.redeem_all_async()
                        for _cid in cancelled_cids:
                            _pnl_delta = db.correct_trade_to_cancelled(_cid)
                            if _pnl_delta is not None:
                                outcome = (
                                    "WIN_CANCEL" if _pnl_delta < 0 else "LOSS_CANCEL"
                                )
                                verifier.verify_win_cancel(
                                    _cid, -_pnl_delta, 0
                                ) if _pnl_delta < 0 else verifier.verify_loss_cancel(
                                    _cid, -_pnl_delta, 0
                                )
                                risk.update_portfolio(_pnl_delta)
                                log.warning(
                                    "%s: market voided conditionId=%s "
                                    "(portfolio adjusted $%+.4f → $%.2f)",
                                    outcome,
                                    _cid[:18],
                                    _pnl_delta,
                                    risk.portfolio,
                                )
                        for _cid in lost_cids:
                            _pnl_delta = db.correct_trade_to_loss(_cid)
                            if _pnl_delta is not None:
                                risk.update_portfolio(_pnl_delta)
                                log.info(
                                    "Corrected false-WIN to LOSS: conditionId=%s "
                                    "(portfolio adjusted $%+.4f → $%.2f)",
                                    _cid[:18],
                                    _pnl_delta,
                                    risk.portfolio,
                                )
                        if count > 0:
                            loop = asyncio.get_running_loop()
                            await loop.run_in_executor(None, executor.sync_balance)
                        if total_usdc > 0:
                            log.info(
                                "Auto-redeemed %d position(s) ($%.4f USDC.e) to wallet",
                                count,
                                total_usdc,
                            )
                            await telegram.notify_redeemed(count, total_usdc)
                            _redeem_pending_ts = 0.0
                            _last_redeem_attempt_ts = 0.0
                            _redeem_slow_alerted = False
                            clob_post = executor.get_wallet_balance()
                            verifier.snapshot(risk.portfolio, "after_redeem", clob_post)
                        elif count > 0:
                            log.info(
                                "Redeem txs sent (%d) but no USDC.e Transfer yet — retrying in %.0fs",
                                count,
                                _REDEEM_RETRY_INTERVAL,
                            )
                        else:
                            log.info(
                                "Waiting for Data API to index position "
                                "(waited %.0fs, retry in %.0fs)",
                                waited,
                                _REDEEM_RETRY_INTERVAL,
                            )

                # Periodic scan every 15 min — catches orphaned wins regardless
                # of bot-computed PnL or queue state (bot restart, oracle delay,
                # second win that became redeemable after the queue cleared).
                if (
                    is_live
                    and (now - _last_periodic_redeem_ts) >= _PERIODIC_REDEEM_INTERVAL
                ):
                    _last_periodic_redeem_ts = now
                    # Sync retry timer so pending queue doesn't also fire this cycle
                    _last_redeem_attempt_ts = now
                    (
                        p_count,
                        p_usdc,
                        p_losses,
                        p_cancelled,
                    ) = await redeem.redeem_all_async()
                    for _cid in p_cancelled:
                        _pnl_delta = db.correct_trade_to_cancelled(_cid)
                        if _pnl_delta is not None:
                            outcome = "WIN_CANCEL" if _pnl_delta < 0 else "LOSS_CANCEL"
                            verifier.verify_win_cancel(
                                _cid, -_pnl_delta, 0
                            ) if _pnl_delta < 0 else verifier.verify_loss_cancel(
                                _cid, -_pnl_delta, 0
                            )
                            risk.update_portfolio(_pnl_delta)
                            log.warning(
                                "Periodic %s: market voided conditionId=%s "
                                "(portfolio adjusted $%+.4f → $%.2f)",
                                outcome,
                                _cid[:18],
                                _pnl_delta,
                                risk.portfolio,
                            )
                    for _cid in p_losses:
                        _pnl_delta = db.correct_trade_to_loss(_cid)
                        if _pnl_delta is not None:
                            risk.update_portfolio(_pnl_delta)
                            log.info(
                                "Periodic: corrected false-WIN to LOSS: conditionId=%s "
                                "(portfolio adjusted $%+.4f → $%.2f)",
                                _cid[:18],
                                _pnl_delta,
                                risk.portfolio,
                            )
                    if p_usdc > 0:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, executor.sync_balance)
                        log.info(
                            "Periodic redeem: %d position(s) $%.4f USDC.e",
                            p_count,
                            p_usdc,
                        )
                        await telegram.notify_redeemed(p_count, p_usdc)
                        _redeem_pending_ts = 0.0
                        _last_redeem_attempt_ts = 0.0
                    elif p_count > 0:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, executor.sync_balance)
                    else:
                        log.info("Periodic check: no redeemable positions")

                # Capital verifier pause gate
                if verifier.trading_paused:
                    log.critical(
                        "CAPITAL VERIFIER: trading paused — "
                        "run `python verify_capital.py --fix` to investigate"
                    )
                    live.update(dash.render())
                    await asyncio.sleep(CFG.poll_interval)
                    continue

                # Risk check
                can_trade, reason = risk.can_trade()
                # Reset actioned flag when kill switch clears at UTC midnight
                if _kill_switch_actioned and not risk.kill_switch:
                    _kill_switch_actioned = False
                if not can_trade:
                    if risk.kill_switch and reason.startswith("kill switch"):
                        if not _kill_switch_actioned:
                            _kill_switch_actioned = True
                            executor.cancel_all_orders()
                            await telegram.notify_kill_switch(
                                reason, db.daily_pnl(), risk.portfolio
                            )
                    live.update(dash.render())
                    await asyncio.sleep(CFG.poll_interval)
                    continue

                # Concurrent position limit
                if not risk.check_concurrent(executor.open_count):
                    live.update(dash.render())
                    await asyncio.sleep(CFG.poll_interval)
                    continue

                # Scan all tokens for snipe opportunities
                for _, token in list(markets.tokens.items()):
                    ttl = token.end_ts - now

                    # Quick pre-filter: only tokens in the time window
                    if ttl > CFG.snipe_entry_sec or ttl < CFG.snipe_exit_sec:
                        continue

                    # Refresh order book price
                    await markets.refresh_book(token)
                    dash.signals_seen += 1

                    # Evaluate signal
                    signal = engine.evaluate(token, risk.portfolio, is_live)
                    if signal is None:
                        continue

                    # Execute
                    trade = executor.execute(signal)
                    # Always mark window as traded — prevents the same window from
                    # re-firing on every loop iteration when live execution fails
                    # (CANCELLED trade). Without this, one failed window generates
                    # 100+ CANCELLED records before it expires.
                    engine.mark_traded(token.asset, token.window_ts)
                    if trade:
                        risk.on_trade()
                        dash.signals_fired += 1
                        await telegram.notify_trade_opened(trade)

                # ── Live exit: monitor open positions for oracle reversal ──────
                # If oracle delta reverses and holds for exit_reversal_hold_sec,
                # sell the position back to the CLOB to limit the loss.
                # Only active in live mode — paper positions are handled by
                # close_expired() using the final oracle delta snapshot.
                if is_live:
                    for wkey, trade in list(executor.open_positions.items()):
                        if trade.status != "OPEN":
                            continue
                        dur_sec = getattr(trade, "duration_sec", 300)
                        ttl_pos = trade.window_ts + dur_sec - now
                        if ttl_pos < CFG.exit_reversal_min_ttl:
                            # Too close to expiry — let close_expired handle it
                            executor._reversal_first_ts.pop(wkey, None)
                            continue

                        delta = feeds.oracle_delta(trade.asset, trade.window_ts)
                        expected_up = trade.direction == "UP"
                        delta_reversed = (expected_up and delta < 0) or (
                            not expected_up and delta > 0
                        )
                        # Strategy 2: delta collapse — oracle move faded to
                        # noise level without reversing direction. The edge
                        # that justified entry no longer exists.
                        delta_collapsed = (
                            not delta_reversed and abs(delta) < CFG.min_delta_pct
                        )

                        should_exit = delta_reversed or delta_collapsed
                        if should_exit:
                            token_id = executor._open_token_ids.get(wkey)
                            if not token_id:
                                continue
                            # Strategy 1: adaptive hold — scale hold time by
                            # remaining TTL so we exit faster when little time
                            # remains.  20% of TTL, floored at 2s, capped at
                            # exit_reversal_hold_sec.
                            effective_hold = max(
                                2.0,
                                min(CFG.exit_reversal_hold_sec, ttl_pos * 0.20),
                            )
                            first_rev = executor._reversal_first_ts.get(wkey)
                            reason = "reversal" if delta_reversed else "collapse"
                            if first_rev is None:
                                executor._reversal_first_ts[wkey] = now
                                log.debug(
                                    "EXIT WATCH %s [%s] delta=%.4f%% "
                                    "— holding %.1fs (ttl=%.0fs)",
                                    wkey,
                                    reason,
                                    delta,
                                    effective_hold,
                                    ttl_pos,
                                )
                            elif now - first_rev >= effective_hold:
                                log.warning(
                                    "EXIT CONFIRMED %s [%s]: delta=%.4f%% "
                                    "held %.1fs — attempting exit",
                                    wkey,
                                    reason,
                                    delta,
                                    now - first_rev,
                                )
                                sold = executor.sell_position(wkey, token_id, trade)
                                if sold:
                                    risk.on_trade_closed(trade.pnl)
                                    await telegram.notify_trade_closed(trade)
                        else:
                            # Delta healthy — clear exit watch timer
                            if wkey in executor._reversal_first_ts:
                                log.debug(
                                    "EXIT WATCH CLEARED %s: delta=%.4f%%", wkey, delta
                                )
                                executor._reversal_first_ts.pop(wkey, None)

                live.update(dash.render())
                await asyncio.sleep(CFG.poll_interval)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        feeds.stop()
        for t in tasks:
            t.cancel()

        # Closing portfolio snapshot
        clob_stop = executor.get_wallet_balance() if is_live else None
        verifier.snapshot(risk.portfolio, "bot_stop", clob_stop)

        # Final stats
        st = db.lifetime_stats()
        log.info(
            "FINAL: P&L=$%+.4f WR=%.1f%% (%d/%d) Exp=$%+.4f",
            st["pnl"],
            st["wr"],
            st["wins"],
            st["total"],
            st["expectancy"],
        )
        try:
            await telegram.notify_bot_stop(st, risk.portfolio)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Hybrid Oracle Sniper")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--accept-risk", action="store_true")
    parser.add_argument("--portfolio", type=float, default=1000.0)
    args = parser.parse_args()

    is_live = False
    if args.live:
        if not (args.confirm_live and args.accept_risk):
            print("\nLive mode requires: --live --confirm-live --accept-risk\n")
            sys.exit(1)
        is_live = True

    asyncio.run(run(is_live, args.portfolio))


if __name__ == "__main__":
    main()

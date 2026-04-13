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
import os
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

    today    = datetime.now().strftime("%Y-%m-%d")
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
    log.info("  Entry window: T-%.0fs to T-%.0fs", CFG.snipe_entry_sec, CFG.snipe_exit_sec)
    log.info("  Price range: $%.2f - $%.2f", CFG.min_token_price, CFG.max_token_price)
    log.info("  Min delta: %.3f%%  Min confidence: %.0f", CFG.min_delta_pct, CFG.min_confidence)
    log.info("=" * 60)

    # Initialize components
    db = Database(CFG.db_path)
    feeds = PriceFeeds()
    markets = MarketDiscovery(price_feeds=feeds)
    engine = HybridEngine(feeds)
    executor = Executor(db, feeds, is_live)

    # In live mode, fetch actual wallet balance as portfolio value
    if is_live:
        wallet_balance = executor.get_wallet_balance()
        if wallet_balance > 0:
            portfolio = wallet_balance
            log.info("Wallet balance: $%.2f (using as portfolio)", portfolio)
        else:
            log.warning("Could not fetch wallet balance, using --portfolio $%.2f", portfolio)

    risk = RiskManager(db, portfolio)
    dash = Dashboard(db, feeds, markets, risk, executor, is_live)

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
        _s_count, _s_usdc = await redeem.redeem_all_async()
        if _s_usdc > 0:
            executor.sync_balance()
            await telegram.notify_redeemed(_s_count, _s_usdc)
            log.info("Startup redemption: %d position(s) $%.4f USDC.e", _s_count, _s_usdc)
        elif _s_count > 0:
            executor.sync_balance()

    # Telegram: bot started
    mode_str = "LIVE" if is_live else "PAPER"
    await telegram.notify_bot_start(mode_str, portfolio)

    # Pending redemption queue: timestamp when a WIN was detected.
    # Polymarket Data API takes 1–15 min after resolution to list positions
    # as redeemable, so we retry every 90s until actual USDC.e is received.
    _redeem_pending_ts: float = 0.0       # 0 = no pending redemption
    _last_redeem_attempt_ts: float = 0.0  # last time we called redeem_all
    _REDEEM_RETRY_INTERVAL = 90.0         # seconds between retry attempts
    _REDEEM_MAX_WAIT = 1200.0             # Bug 5 fix: give up after 20 min

    # Bug 2+3+4 fix: periodic scan catches orphaned wins regardless of
    # bot-computed PnL or whether a new win triggers the queue.
    _PERIODIC_REDEEM_INTERVAL = 900.0     # scan every 15 min unconditionally
    _last_periodic_redeem_ts: float = 0.0

    try:
        with Live(dash.render(), refresh_per_second=2, console=dash.console) as live:
            while True:
                now = time.time()

                # Rediscover markets periodically
                if markets.needs_refresh():
                    await markets.discover()

                # Close expired positions + auto-redeem wins
                closed = executor.close_expired()
                for _, trade in closed:
                    risk.update_portfolio(trade.pnl)
                    await telegram.notify_trade_closed(trade)
                    if trade.pnl > 0 and is_live:
                        # Queue redemption — positions may not be redeemable
                        # immediately; the retry loop below handles the delay.
                        if _redeem_pending_ts == 0.0:
                            _redeem_pending_ts = now
                            log.info("WIN detected — redemption queued")

                # Retry redemption while pending — attempt immediately, then
                # every 90s. Only clears when real USDC.e is confirmed received.
                if is_live and _redeem_pending_ts > 0:
                    waited = now - _redeem_pending_ts
                    since_last = now - _last_redeem_attempt_ts
                    if waited >= _REDEEM_MAX_WAIT:
                        log.warning("Redemption gave up after %.0fs — run redeem_now.py manually", waited)
                        _redeem_pending_ts = 0.0
                        _last_redeem_attempt_ts = 0.0
                    elif since_last >= _REDEEM_RETRY_INTERVAL:
                        _last_redeem_attempt_ts = now
                        count, total_usdc = await redeem.redeem_all_async()
                        if count > 0:
                            # Bug 6 fix: sync_balance is blocking HTTP — run in executor
                            loop = asyncio.get_running_loop()
                            await loop.run_in_executor(None, executor.sync_balance)
                        if total_usdc > 0:
                            log.info("Auto-redeemed %d position(s) ($%.4f USDC.e) to wallet",
                                     count, total_usdc)
                            await telegram.notify_redeemed(count, total_usdc)
                            _redeem_pending_ts = 0.0       # confirmed — done
                            _last_redeem_attempt_ts = 0.0
                        elif count > 0:
                            log.info("Redeem txs sent (%d) but no USDC.e Transfer yet — retrying in %.0fs",
                                     count, _REDEEM_RETRY_INTERVAL)
                        else:
                            log.info("No redeemable positions yet (waited %.0fs) — retrying in %.0fs",
                                     waited, _REDEEM_RETRY_INTERVAL)

                # Bug 2+3+4 fix: periodic scan every 15 min — catches orphaned
                # wins regardless of bot-computed PnL or queue state.
                # Covers: bot restart after win, oracle miscalculation, second
                # win that became redeemable after the queue already cleared.
                if is_live and (now - _last_periodic_redeem_ts) >= _PERIODIC_REDEEM_INTERVAL:
                    _last_periodic_redeem_ts = now
                    p_count, p_usdc = await redeem.redeem_all_async()
                    if p_usdc > 0:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, executor.sync_balance)
                        log.info("Periodic redeem: %d position(s) $%.4f USDC.e",
                                 p_count, p_usdc)
                        await telegram.notify_redeemed(p_count, p_usdc)
                        # Also clear the win queue if it was pending
                        _redeem_pending_ts = 0.0
                        _last_redeem_attempt_ts = 0.0
                    elif p_count > 0:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, executor.sync_balance)

                # Risk check
                can_trade, reason = risk.can_trade()
                if not can_trade:
                    if risk.kill_switch and reason.startswith("kill switch"):
                        # Priority 2: cancel open CLOB orders before halting
                        executor.cancel_all_orders()
                        await telegram.notify_kill_switch(
                            reason, db.daily_pnl(), risk.portfolio)
                    live.update(dash.render())
                    await asyncio.sleep(CFG.poll_interval)
                    continue

                # Concurrent position limit
                if not risk.check_concurrent(executor.open_count):
                    live.update(dash.render())
                    await asyncio.sleep(CFG.poll_interval)
                    continue

                # Scan all tokens for snipe opportunities
                for tid, token in list(markets.tokens.items()):
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
                    if trade:
                        engine.mark_traded(token.asset, token.window_ts)
                        risk.on_trade()
                        dash.signals_fired += 1
                        await telegram.notify_trade_opened(trade)

                live.update(dash.render())
                await asyncio.sleep(CFG.poll_interval)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        feeds.stop()
        for t in tasks:
            t.cancel()

        # Final stats
        st = db.lifetime_stats()
        log.info("FINAL: P&L=$%+.4f WR=%.1f%% (%d/%d) Exp=$%+.4f",
                 st["pnl"], st["wr"], st["wins"], st["total"], st["expectancy"])
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

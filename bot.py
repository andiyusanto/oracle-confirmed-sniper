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
import sys
import time

from rich.live import Live

from core.config import CFG
from core.database import Database
from feeds.prices import PriceFeeds
from feeds.markets import MarketDiscovery
from engine.signal import HybridEngine
from engine.risk import RiskManager
from execution.executor import Executor
from ui.dashboard import Dashboard

# ── Logging ─────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(CFG.log_file),
        logging.StreamHandler(),
    ],
)
for noisy in ("httpx", "httpcore", "websockets", "asyncio", "hpack", "h2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

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
    risk = RiskManager(db, portfolio)
    executor = Executor(db, feeds, is_live)
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

    try:
        with Live(dash.render(), refresh_per_second=2, console=dash.console) as live:
            while True:
                now = time.time()

                # Rediscover markets periodically
                if markets.needs_refresh():
                    await markets.discover()

                # Close expired positions
                closed = executor.close_expired()
                for _, trade in closed:
                    risk.update_portfolio(trade.pnl)

                # Risk check
                can_trade, reason = risk.can_trade()
                if not can_trade:
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

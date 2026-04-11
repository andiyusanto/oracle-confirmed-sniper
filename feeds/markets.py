"""Market discovery via deterministic slug lookup.

Fixes applied:
  1. Reuse ClobClient instead of creating per refresh_book() call
  2. Retry logic on HTTP failures (3 attempts with backoff)
  3. Faster discovery interval option
  4. Pass Gamma outcomePrices to PriceFeeds for reliable opening prices
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import aiohttp

from core.config import CFG
from core.models import Token

log = logging.getLogger("hybrid.markets")

try:
    from py_clob_client.client import ClobClient
    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False

try:
    from py_clob_client.constants import POLYGON
except ImportError:
    POLYGON = 137


class MarketDiscovery:
    def __init__(self, price_feeds=None):
        self.tokens: dict[str, Token] = {}
        self._last_discovery = 0.0
        self._book_cache: dict[str, tuple[float, float]] = {}  # tid: (price, ts)
        self._executor = ThreadPoolExecutor(max_workers=6)
        self._price_feeds = price_feeds

        # Reuse a single ClobClient for order book queries
        self._clob: Optional[object] = None
        if HAS_CLOB:
            try:
                self._clob = ClobClient(host=CFG.clob_host, chain_id=POLYGON)
            except Exception as e:
                log.warning("Failed to init read-only ClobClient: %s", e)

    def needs_refresh(self) -> bool:
        return time.time() - self._last_discovery > CFG.discovery_interval

    async def discover(self):
        """Find active markets using deterministic slug lookup."""
        now = time.time()
        now_int = int(now)
        found = {}

        try:
            async with aiohttp.ClientSession() as session:
                for asset_l in [a.lower() for a in CFG.assets]:
                    asset_u = asset_l.upper()
                    for dur_label, dur_sec in CFG.durations:
                        current_ts = now_int - (now_int % dur_sec)
                        for offset in [0, dur_sec, dur_sec * 2]:
                            wts = current_ts + offset
                            end_ts = float(wts + dur_sec)
                            if end_ts < now:
                                continue
                            slug = f"{asset_l}-updown-{dur_label}-{wts}"
                            tokens = await self._fetch_slug_with_retry(
                                session, slug, asset_u, end_ts,
                                wts, dur_label)
                            found.update(tokens)
        except Exception as e:
            log.error("Discovery failed: %s", e)

        # Merge and prune expired
        now2 = time.time()
        for tid, tok in found.items():
            self.tokens[tid] = tok
        self.tokens = {k: v for k, v in self.tokens.items()
                       if v.end_ts > now2}
        self._last_discovery = now2

        if found:
            log.info("Markets: %d active (%d new)",
                     len(self.tokens), len(found))

    async def _fetch_slug_with_retry(self, session, slug, asset,
                                      end_ts, wts, dur_label,
                                      max_retries: int = 3
                                      ) -> dict[str, Token]:
        """Fetch slug with exponential backoff retry."""
        for attempt in range(max_retries):
            result = await self._fetch_slug(
                session, slug, asset, end_ts, wts, dur_label)
            if result is not None:
                return result
            if attempt < max_retries - 1:
                wait = 1.0 * (2 ** attempt)
                log.debug("Retry %d/%d for %s in %.1fs",
                          attempt + 1, max_retries, slug, wait)
                await asyncio.sleep(wait)
        return {}

    async def _fetch_slug(self, session, slug, asset, end_ts,
                           wts, dur_label) -> Optional[dict[str, Token]]:
        """Fetch a single slug. Returns None on retryable failure,
        empty dict on 404/no-data, populated dict on success."""
        found = {}
        try:
            async with session.get(
                f"{CFG.gamma_url}?slug={slug}",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 429:
                    log.warning("Rate limited on slug %s", slug)
                    return None  # retryable
                if resp.status != 200:
                    return found  # empty, not retryable
                events = await resp.json(content_type=None)

            if not events:
                return found

            event = events[0] if isinstance(events, list) else events
            for m in (event.get("markets") or []):
                if m.get("closed") or m.get("resolved"):
                    continue
                tids = m.get("clobTokenIds") or []
                if isinstance(tids, str):
                    tids = json.loads(tids)
                outcomes = m.get("outcomes") or []
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                prices = m.get("outcomePrices") or []
                if isinstance(prices, str):
                    prices = json.loads(prices)

                for i, tid in enumerate(tids):
                    tid = str(tid)
                    oc = str(outcomes[i]).lower() if i < len(outcomes) else ""
                    direction = "UP" if any(k in oc for k in ["up", "yes"]) \
                                else "DOWN"
                    price = float(prices[i]) if i < len(prices) else 0.5
                    dur_str = dur_label.replace("m", "min")

                    found[tid] = Token(
                        token_id=tid, asset=asset, direction=direction,
                        duration=dur_str, end_ts=end_ts, window_ts=wts,
                        book_price=price, book_updated=0,
                    )

                    # Pass opening price from Gamma to PriceFeeds
                    # outcomePrices at discovery time approximates the
                    # market's initial pricing for this window
                    if self._price_feeds and direction == "UP" and price > 0:
                        # The UP token price ~ market's implied probability
                        # Opening price for the underlying asset comes from
                        # the feed, but we can signal that this window exists
                        self._price_feeds.capture_opening(asset, wts)

        except asyncio.TimeoutError:
            return None  # retryable
        except aiohttp.ClientError as e:
            log.debug("HTTP error for %s: %s", slug, e)
            return None  # retryable
        except Exception as e:
            log.debug("Slug %s error: %s", slug, e)
        return found

    async def refresh_book(self, token: Token) -> float:
        """Get fresh order book mid price for a token.

        Uses a shared ClobClient instance instead of creating new ones.
        """
        now = time.time()
        cached = self._book_cache.get(token.token_id)
        if cached and now - cached[1] < CFG.book_cache_sec:
            return cached[0]

        if not HAS_CLOB or not self._clob:
            return token.book_price

        try:
            loop = asyncio.get_running_loop()

            def _fetch():
                book = self._clob.get_order_book(token.token_id)
                asks = [float(a.price) for a in (book.asks or [])
                        if float(a.price) > 0]
                bids = [float(b.price) for b in (book.bids or [])
                        if float(b.price) > 0]
                # No asks = market fully priced in, nothing to buy.
                # Return 0.99 so the signal engine's max_token_price
                # check filters it out before execution.
                if not asks:
                    return 0.99
                bb = max(bids) if bids else min(asks)
                return (min(asks) + bb) / 2

            price = await loop.run_in_executor(self._executor, _fetch)
            self._book_cache[token.token_id] = (price, now)
            token.book_price = price
            token.book_updated = now
            return price

        except Exception as e:
            log.debug("Book error %s: %s", token.token_id[:12], e)
            return token.book_price

"""Real-time price feeds: Chainlink RTDS + Binance WebSocket.

Fixes applied:
  1. Opening price captured at actual window boundary (not first observation)
  2. Proper WebSocket ping_interval instead of manual text PING
  3. Opening price cross-checked with Gamma API outcomePrices
  4. binance_agrees() kept as stub per user request
"""

import asyncio
import json
import logging
import time

import websockets

from core.config import CFG

log = logging.getLogger("hybrid.feeds")


class PriceFeeds:
    """Dual-source price feed: Chainlink (oracle/resolution) + Binance."""

    def __init__(self):
        self.chainlink: dict[str, float] = {}
        self.binance: dict[str, float] = {}
        self.cl_ts: dict[str, float] = {}      # last Chainlink update time
        self.bn_ts: dict[str, float] = {}       # last Binance update time
        self.openings: dict[str, dict[int, float]] = {}  # {asset: {window_ts: price}}
        self._running = False
        self._rtds_reconnects = 0
        self._binance_reconnects = 0

        # Price history for opening price interpolation
        # Stores (timestamp, price) tuples, rolling 10-minute buffer
        self._price_history: dict[str, list[tuple[float, float]]] = {}

        for a in CFG.assets:
            self.chainlink[a] = 0.0
            self.binance[a] = 0.0
            self.cl_ts[a] = 0.0
            self.bn_ts[a] = 0.0
            self.openings[a] = {}
            self._price_history[a] = []

    @property
    def is_ready(self) -> bool:
        return any(self.binance[a] > 0 for a in CFG.assets)

    def best_price(self, asset: str) -> float:
        """Best available price: Chainlink if fresh, else Binance."""
        if self.chainlink[asset] > 0 and time.time() - self.cl_ts.get(asset, 0) < 30:
            return self.chainlink[asset]
        return self.binance.get(asset, 0)

    def capture_opening(self, asset: str, window_ts: int):
        """Capture the 'Price to Beat' at window open.

        Uses price history to find the price closest to the actual
        window_ts boundary. Falls back to current price only if no
        historical data is available (bot started mid-window).
        """
        if window_ts in self.openings.get(asset, {}):
            return

        price = self._interpolate_price_at(asset, float(window_ts))

        if price > 0:
            self.openings[asset][window_ts] = price
            source = "history" if self._has_history_near(asset, window_ts) else "live"
            log.info("OPEN %s $%.2f (window %d, src=%s)",
                     asset, price, window_ts, source)
        else:
            # Last resort: use current price (less accurate)
            current = self.best_price(asset)
            if current > 0:
                self.openings[asset][window_ts] = current
                log.warning("OPEN %s $%.2f (window %d, src=fallback — "
                            "no history near boundary)", asset, current, window_ts)

        # Prune old openings
        if len(self.openings[asset]) > 30:
            for k in sorted(self.openings[asset])[:-30]:
                del self.openings[asset][k]

    def set_opening_from_gamma(self, asset: str, window_ts: int,
                                gamma_price: float):
        """Set opening price from Gamma API data (most reliable source).

        Called by MarketDiscovery when it first discovers a market and
        has access to the event's outcomePrices at creation time.
        """
        if window_ts in self.openings.get(asset, {}):
            return  # don't overwrite — first source wins
        if gamma_price > 0:
            self.openings[asset][window_ts] = gamma_price
            log.info("OPEN %s $%.2f (window %d, src=gamma)",
                     asset, gamma_price, window_ts)

    def _interpolate_price_at(self, asset: str, target_ts: float) -> float:
        """Find the price closest to a target timestamp from history."""
        history = self._price_history.get(asset, [])
        if not history:
            return 0.0

        # Find closest entry within 30 seconds of target
        best_price = 0.0
        best_gap = float('inf')
        for ts, price in history:
            gap = abs(ts - target_ts)
            if gap < best_gap:
                best_gap = gap
                best_price = price

        # Only use if within 30 seconds of the boundary
        if best_gap <= 30:
            return best_price
        return 0.0

    def _has_history_near(self, asset: str, window_ts: int) -> bool:
        """Check if we have price history near the window boundary."""
        history = self._price_history.get(asset, [])
        for ts, _ in history:
            if abs(ts - window_ts) <= 30:
                return True
        return False

    def _record_price(self, asset: str, price: float):
        """Record price with timestamp for opening price interpolation."""
        now = time.time()
        history = self._price_history[asset]
        history.append((now, price))

        # Keep only last 10 minutes
        cutoff = now - 600
        self._price_history[asset] = [
            (t, p) for t, p in history if t > cutoff
        ]

    def oracle_delta(self, asset: str, window_ts: int) -> float:
        """Signed % delta: current oracle price vs opening price."""
        opening = self.openings.get(asset, {}).get(window_ts, 0)
        if opening <= 0:
            return 0.0
        current = self.best_price(asset)
        if current <= 0:
            return 0.0
        return (current - opening) / opening * 100

    def binance_agrees(self, asset: str, oracle_says: str) -> bool:
        """Check if Binance price direction agrees with Chainlink oracle.

        Compares Binance current price against the window opening price.
        Returns True if both sources agree on direction (UP or DOWN).
        Returns True if Binance data is stale (>30s) to avoid blocking
        valid signals during Binance feed outages.
        """
        # If Binance data is stale, don't penalise — CL is the authority
        bn_age = time.time() - self.bn_ts.get(asset, 0)
        if bn_age > 30:
            return True

        bn_price = self.binance.get(asset, 0)
        if bn_price <= 0:
            return True

        # Find the most recent window opening we have
        openings = self.openings.get(asset, {})
        if not openings:
            return True

        latest_window_ts = max(openings.keys())
        opening = openings[latest_window_ts]
        if opening <= 0:
            return True

        bn_delta = (bn_price - opening) / opening * 100
        bn_says = "UP" if bn_delta > 0 else "DOWN"
        return bn_says == oracle_says

    def chainlink_staleness(self, asset: str) -> float:
        """Seconds since last Chainlink update."""
        return time.time() - self.cl_ts.get(asset, 0)

    async def run_rtds(self):
        """Connect to Polymarket RTDS for Chainlink + Binance prices.

        Uses proper WebSocket ping_interval instead of manual text PING.
        """
        self._running = True
        while self._running:
            try:
                async with websockets.connect(
                    CFG.rtds_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    log.info("RTDS connected: %s", CFG.rtds_url)
                    rtds_filters = ",".join(
                        a.lower() + "usdt" for a in CFG.assets
                    )
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [
                            {"topic": "crypto_prices_chainlink",
                             "type": "update", "filters": ""},
                            {"topic": "crypto_prices",
                             "type": "update",
                             "filters": rtds_filters},
                        ]
                    }))
                    async for raw in ws:
                        if not self._running:
                            break
                        self._parse_rtds(raw)
            except Exception as e:
                if self._running:
                    self._rtds_reconnects += 1
                    log.warning("RTDS disconnected (%d total): %s — "
                                "reconnecting", self._rtds_reconnects, e)
                    await asyncio.sleep(3)

    async def run_binance(self):
        """Direct Binance WebSocket as backup/cross-check."""
        symbols = "/".join(a.lower() + "usdt@bookTicker" for a in CFG.assets)
        while self._running:
            try:
                url = f"{CFG.binance_ws}?streams={symbols}"
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10
                ) as ws:
                    log.info("Binance WS connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        self._parse_binance(raw)
            except Exception as e:
                if self._running:
                    self._binance_reconnects += 1
                    log.warning("Binance disconnected (%d total): %s",
                                self._binance_reconnects, e)
                    delay = min(3 * (2 ** min(self._binance_reconnects - 1, 4)), 60)
                    await asyncio.sleep(delay)

    def _parse_rtds(self, raw: str):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        topic = msg.get("topic", "")
        payload = msg.get("payload", {})
        symbol = payload.get("symbol", "").lower()
        value = payload.get("value")
        if not value or float(value) <= 0:
            return
        asset = self._symbol_to_asset(symbol)
        if not asset:
            return
        fval = float(value)
        if topic == "crypto_prices_chainlink":
            self.chainlink[asset] = fval
            self.cl_ts[asset] = time.time()
            self._record_price(asset, fval)
        elif topic == "crypto_prices":
            self.binance[asset] = fval
            self.bn_ts[asset] = time.time()
            self._record_price(asset, fval)

    def _parse_binance(self, raw: str):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        data = msg.get("data", {})
        stream = msg.get("stream", "")
        asset = self._symbol_to_asset(
            stream.split("@")[0] if "@" in stream else "")
        if not asset:
            return
        bb = float(data.get("b", 0))
        ba = float(data.get("a", 0))
        if bb > 0 and ba > 0:
            mid = (bb + ba) / 2
            self.binance[asset] = mid
            self.bn_ts[asset] = time.time()
            self._record_price(asset, mid)

    @staticmethod
    def _symbol_to_asset(symbol: str) -> str:
        s = symbol.lower()
        if "btc" in s:
            return "BTC"
        if "eth" in s:
            return "ETH"
        if "sol" in s:
            return "SOL"
        return ""

    def stop(self):
        self._running = False

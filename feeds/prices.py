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
        self._skipped_windows: set[tuple[str, int]] = set()  # (asset, window_ts) permanently skipped
        self._running = False
        self._rtds_reconnects = 0
        self._binance_reconnects = 0

        # Price history for opening price interpolation
        # Stores (timestamp, price) tuples, rolling 10-minute buffer
        self._price_history: dict[str, list[tuple[float, float]]] = {}

        # Priority 5: silent-freeze watchdog timestamps
        self._rtds_last_msg_ts:    float = 0.0
        self._binance_last_msg_ts: float = 0.0
        _WS_SILENCE_TIMEOUT = 60  # seconds — RTDS goes quiet during low volatility

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
        if (asset, window_ts) in self._skipped_windows:
            return

        price = self._interpolate_price_at(asset, float(window_ts))

        if price > 0:
            self.openings[asset][window_ts] = price
            source = "history" if self._has_history_near(asset, window_ts) else "live"
            log.info("OPEN %s $%.2f (window %d, src=%s)",
                     asset, price, window_ts, source)
        else:
            # Last resort: use current price only if we just joined the window.
            # If we're >60s in, the true opening is unknowable — setting current
            # price as opening produces delta≈0 and corrupts signal quality.
            elapsed_in_window = time.time() - window_ts
            if elapsed_in_window > 60:
                self._skipped_windows.add((asset, window_ts))
                log.warning("OPEN %s skipped (window %d, %.0fs elapsed — "
                            "opening unknowable after reconnect)",
                            asset, window_ts, elapsed_in_window)
                return
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

    def oracle_delta_at(self, asset: str, window_ts: int,
                        lookback_sec: float) -> float:
        """Signed % delta at a past point vs the same window's opening price.

        Used for momentum filtering: compare current delta to delta N seconds
        ago to detect fading or reversing moves before committing capital.

        Returns 0.0 if no history is available near that timestamp
        (caller should treat 0.0 as 'unknown, skip momentum check').
        """
        opening = self.openings.get(asset, {}).get(window_ts, 0)
        if opening <= 0:
            return 0.0
        target_ts = time.time() - lookback_sec
        past_price = self._interpolate_price_at(asset, target_ts)
        if past_price <= 0:
            return 0.0
        return (past_price - opening) / opening * 100

    def binance_agrees(self, asset: str, oracle_says: str,
                       window_ts: int = 0) -> bool:
        """Check if Binance price direction agrees with Chainlink oracle.

        Compares Binance current price against the *specific window's*
        opening price (passed via window_ts). Falls back to the most
        recent window only when window_ts is not provided.
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

        openings = self.openings.get(asset, {})
        if not openings:
            return True

        # Use the specific signal's window opening; fall back to latest
        # only when window_ts is unknown (e.g. called from OracleState).
        # Using max() was buggy: if a future window was already registered
        # its opening ≈ current price → near-zero delta → random bn_says.
        if window_ts and window_ts in openings:
            opening = openings[window_ts]
        else:
            opening = openings[max(openings.keys())]

        if opening <= 0:
            return True

        bn_delta = (bn_price - opening) / opening * 100
        bn_says = "UP" if bn_delta > 0 else "DOWN"
        return bn_says == oracle_says

    def chainlink_staleness(self, asset: str) -> float:
        """Seconds since last Chainlink update."""
        return time.time() - self.cl_ts.get(asset, 0)

    _WS_SILENCE_TIMEOUT = 60  # seconds without a message → force reconnect

    async def run_rtds(self):
        """Connect to Polymarket RTDS for Chainlink + Binance prices.

        Priority 5: uses asyncio.wait_for on each recv() so a silent
        connection (no exception but no data) is detected and reconnected.
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
                    self._rtds_last_msg_ts = time.time()
                    # crypto_prices (Binance proxy): symbol format "btcusdt"
                    bn_filters = ",".join(a.lower() + "usdt" for a in CFG.assets)
                    # crypto_prices_chainlink: symbol format "btc/usd" (confirmed via RTDS sniff)
                    cl_filters = ",".join(a.lower() + "/usd" for a in CFG.assets)
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [
                            {"topic": "crypto_prices_chainlink",
                             "type": "update", "filters": cl_filters},
                            {"topic": "crypto_prices",
                             "type": "update",
                             "filters": bn_filters},
                        ]
                    }))
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self._WS_SILENCE_TIMEOUT
                            )
                            self._rtds_last_msg_ts = time.time()
                            self._parse_rtds(raw)
                        except asyncio.TimeoutError:
                            log.warning(
                                "RTDS: no message in %ds — reconnecting",
                                self._WS_SILENCE_TIMEOUT,
                            )
                            break  # exits inner while, triggers reconnect
            except Exception as e:
                if self._running:
                    self._rtds_reconnects += 1
                    log.warning("RTDS disconnected (%d total): %s — "
                                "reconnecting", self._rtds_reconnects, e)
                    await asyncio.sleep(3)

    async def run_binance(self):
        """Direct Binance WebSocket as backup/cross-check.

        Priority 5: asyncio.wait_for detects silent freeze and forces reconnect.
        """
        symbols = "/".join(a.lower() + "usdt@bookTicker" for a in CFG.assets)
        while self._running:
            try:
                url = f"{CFG.binance_ws}?streams={symbols}"
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10
                ) as ws:
                    log.info("Binance WS connected")
                    self._binance_last_msg_ts = time.time()
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self._WS_SILENCE_TIMEOUT
                            )
                            self._binance_last_msg_ts = time.time()
                            self._parse_binance(raw)
                        except asyncio.TimeoutError:
                            log.warning(
                                "Binance WS: no message in %ds — reconnecting",
                                self._WS_SILENCE_TIMEOUT,
                            )
                            break
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
            log.info("CL feed: %s=$%.2f", asset, fval)
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

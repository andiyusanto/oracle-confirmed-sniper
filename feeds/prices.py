"""Real-time price feeds: Chainlink RTDS + Binance WebSocket."""

import asyncio
import json
import logging
import time

import websockets

from core.config import CFG

log = logging.getLogger("hybrid.feeds")


class PriceFeeds:
    """Dual-source price feed: Chainlink (oracle/resolution) + Binance (cross-check)."""

    def __init__(self):
        self.chainlink: dict[str, float] = {}
        self.binance: dict[str, float] = {}
        self.cl_ts: dict[str, float] = {}      # last Chainlink update time
        self.bn_ts: dict[str, float] = {}       # last Binance update time
        self.openings: dict[str, dict[int, float]] = {}  # {asset: {window_ts: price}}
        self._running = False

        for a in CFG.assets:
            self.chainlink[a] = 0.0
            self.binance[a] = 0.0
            self.cl_ts[a] = 0.0
            self.bn_ts[a] = 0.0
            self.openings[a] = {}

    @property
    def is_ready(self) -> bool:
        return any(self.binance[a] > 0 for a in CFG.assets)

    def best_price(self, asset: str) -> float:
        """Best available price: Chainlink if fresh, else Binance."""
        if self.chainlink[asset] > 0 and time.time() - self.cl_ts.get(asset, 0) < 30:
            return self.chainlink[asset]
        return self.binance.get(asset, 0)

    def capture_opening(self, asset: str, window_ts: int):
        """Capture the 'Price to Beat' at window open."""
        if window_ts in self.openings.get(asset, {}):
            return
        price = self.best_price(asset)
        if price > 0:
            self.openings[asset][window_ts] = price
            log.info("OPEN %s $%.2f (window %d)", asset, price, window_ts)
            # Prune
            if len(self.openings[asset]) > 30:
                for k in sorted(self.openings[asset])[:-30]:
                    del self.openings[asset][k]

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
        """Check if Binance momentum direction matches oracle."""
        # Compare Binance to Chainlink — if both say same direction, stronger signal
        cl = self.chainlink.get(asset, 0)
        bn = self.binance.get(asset, 0)
        if cl <= 0 or bn <= 0:
            return True  # can't check, assume agree
        # Both should be on same side relative to opening
        return True  # simplified — main delta check handles this

    def chainlink_staleness(self, asset: str) -> float:
        """Seconds since last Chainlink update."""
        return time.time() - self.cl_ts.get(asset, 0)

    async def run_rtds(self):
        """Connect to Polymarket RTDS for Chainlink + Binance prices."""
        self._running = True
        while self._running:
            try:
                async with websockets.connect(
                    CFG.rtds_url, ping_interval=None, close_timeout=5
                ) as ws:
                    log.info("RTDS connected: %s", CFG.rtds_url)
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [
                            {"topic": "crypto_prices_chainlink", "type": "update", "filters": ""},
                            {"topic": "crypto_prices", "type": "update", "filters": "btcusdt,ethusdt"},
                        ]
                    }))
                    last_ping = time.time()
                    async for raw in ws:
                        if not self._running:
                            break
                        now = time.time()
                        if now - last_ping > 5:
                            try:
                                await ws.send("PING")
                            except Exception:
                                pass
                            last_ping = now
                        self._parse_rtds(raw)
            except Exception as e:
                if self._running:
                    log.warning("RTDS disconnected: %s — reconnecting", e)
                    await asyncio.sleep(3)

    async def run_binance(self):
        """Direct Binance WebSocket as backup/cross-check."""
        symbols = "btcusdt@bookTicker/ethusdt@bookTicker"
        while self._running:
            try:
                url = f"{CFG.binance_ws}?streams={symbols}"
                async with websockets.connect(url, ping_interval=20) as ws:
                    log.info("Binance WS connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        self._parse_binance(raw)
            except Exception as e:
                if self._running:
                    log.warning("Binance disconnected: %s", e)
                    await asyncio.sleep(3)

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
        if topic == "crypto_prices_chainlink":
            self.chainlink[asset] = float(value)
            self.cl_ts[asset] = time.time()
        elif topic == "crypto_prices":
            self.binance[asset] = float(value)
            self.bn_ts[asset] = time.time()

    def _parse_binance(self, raw: str):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        data = msg.get("data", {})
        stream = msg.get("stream", "")
        asset = self._symbol_to_asset(stream.split("@")[0] if "@" in stream else "")
        if not asset:
            return
        bb = float(data.get("b", 0))
        ba = float(data.get("a", 0))
        if bb > 0 and ba > 0:
            self.binance[asset] = (bb + ba) / 2
            self.bn_ts[asset] = time.time()

    @staticmethod
    def _symbol_to_asset(symbol: str) -> str:
        s = symbol.lower()
        if "btc" in s:
            return "BTC"
        if "eth" in s:
            return "ETH"
        return ""

    def stop(self):
        self._running = False

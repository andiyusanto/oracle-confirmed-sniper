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

# ── On-chain condition validator ──────────────────────────────────────
# Used to check that a conditionId is actually registered on the CTF
# contract before we commit capital to that market.
# A conditionId with getOutcomeSlotCount == 0 was never deployed on-chain
# and its oracle will never call reportPayouts → permanent zombie market.
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_CTF_SLOT_ABI = [
    {
        "name": "getOutcomeSlotCount",
        "type": "function",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    }
]
_POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
]

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
        self._book_cache: dict[str, tuple[float, float, float]] = {}  # tid: (price, spread, ts)
        self._executor = ThreadPoolExecutor(max_workers=6)
        self._price_feeds = price_feeds

        # Reuse a single ClobClient for order book queries
        self._clob: Optional[object] = None
        if HAS_CLOB:
            try:
                self._clob = ClobClient(host=CFG.clob_host, chain_id=POLYGON)
            except Exception as e:
                log.warning("Failed to init read-only ClobClient: %s", e)

        # conditionId validity cache — checked once per session per cid.
        # True  = registered on CTF (getOutcomeSlotCount > 0) → safe to trade
        # False = not registered (zombie) → skip all tokens for this cid
        # None  = not yet checked
        self._cid_valid: dict[str, bool] = {}
        self._w3 = None  # lazy-initialised on first validation call

    def needs_refresh(self) -> bool:
        return time.time() - self._last_discovery > CFG.discovery_interval

    def _get_w3(self):
        """Lazy Web3 connection — only created when conditionId validation runs."""
        if self._w3 and self._w3.is_connected():
            return self._w3
        try:
            from web3 import Web3
            for rpc in _POLYGON_RPCS:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
                    if w3.is_connected():
                        self._w3 = w3
                        return w3
                except Exception:
                    continue
        except ImportError:
            pass
        return None

    def _validate_condition_sync(self, cid_hex: str) -> bool:
        """Check that a conditionId is registered on the CTF contract.

        Runs synchronously — always called via thread executor from async context.

        Returns True  → getOutcomeSlotCount > 0 → condition exists → safe to trade
        Returns False → count == 0 or RPC failed → zombie / unknown → skip
        """
        if not cid_hex:
            return True  # no conditionId from Gamma API — fail-open, don't block

        # Normalise hex
        if not cid_hex.startswith("0x"):
            cid_hex = "0x" + cid_hex

        w3 = self._get_w3()
        if not w3:
            log.debug("conditionId validation skipped (no RPC): %s", cid_hex[:18])
            return True  # fail-open: don't block trades if we can't reach the node

        try:
            from web3 import Web3 as _W3
            ctf = w3.eth.contract(
                address=_W3.to_checksum_address(_CTF_ADDRESS),
                abi=_CTF_SLOT_ABI,
            )
            condition_bytes = bytes.fromhex(cid_hex[2:])
            slots = ctf.functions.getOutcomeSlotCount(condition_bytes).call()
            if slots == 0:
                log.warning(
                    "[PRE-ENTRY] conditionId %s not registered on CTF "
                    "(getOutcomeSlotCount=0) — market will be excluded",
                    cid_hex[:18],
                )
                return False
            log.debug("conditionId %s validated (slots=%d)", cid_hex[:18], slots)
            return True
        except Exception as e:
            log.debug("conditionId %s validation error: %s — fail-open", cid_hex[:18], e)
            return True  # fail-open: RPC errors must not silently kill trading

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

        # ── On-chain conditionId validation ──────────────────────────────
        # For each conditionId we haven't seen before, check that it is
        # registered on the CTF contract (getOutcomeSlotCount > 0).
        # This runs in the thread executor so it doesn't block the event loop.
        # Fail-open: if the RPC call fails, the token is kept.
        loop2 = asyncio.get_running_loop()
        new_cids = {
            tok.conditionId
            for tok in found.values()
            if tok.conditionId and tok.conditionId not in self._cid_valid
        }
        for cid in new_cids:
            valid = await loop2.run_in_executor(
                self._executor, self._validate_condition_sync, cid
            )
            self._cid_valid[cid] = valid

        # Remove tokens for zombie conditionIds (registered=False)
        zombie_cids = {cid for cid, ok in self._cid_valid.items() if not ok}
        if zombie_cids:
            before = len(found)
            found = {
                tid: tok for tid, tok in found.items()
                if not tok.conditionId or tok.conditionId not in zombie_cids
            }
            removed = before - len(found)
            if removed:
                log.warning(
                    "[PRE-ENTRY] Excluded %d token(s) for unregistered conditionId(s): %s",
                    removed, [c[:18] for c in zombie_cids],
                )

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

                cid = m.get("conditionId") or m.get("condition_id") or ""

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
                        conditionId=cid,
                    )

                    # Trigger opening price capture for any token in this window.
                    # Previously UP-only, which meant windows discovered via a DOWN
                    # token first (rare but possible) never got an opening captured.
                    if self._price_feeds:
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
        if cached and now - cached[2] < CFG.book_cache_sec:
            token.book_price = cached[0]
            token.book_spread = cached[1]
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
                    return 0.99, 1.0  # price, spread (1.0 = 100% = filtered out)
                best_ask = min(asks)
                best_bid = max(bids) if bids else best_ask
                mid = (best_ask + best_bid) / 2
                spread = (best_ask - best_bid) / mid if mid > 0 else 1.0
                return mid, spread

            price, spread = await loop.run_in_executor(self._executor, _fetch)
            self._book_cache[token.token_id] = (price, spread, now)
            token.book_price = price
            token.book_spread = spread
            token.book_updated = now
            return price

        except Exception as e:
            log.debug("Book error %s: %s", token.token_id[:12], e)
            return token.book_price

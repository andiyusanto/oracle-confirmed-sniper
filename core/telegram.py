"""Telegram notifications for trade events.

Sends async alerts for:
  - Trade opened (signal fired)
  - Trade closed (win/loss with PnL)
  - Kill switch triggered
  - Daily summary
  - Bot start/stop

Uses httpx for async HTTP to avoid blocking the event loop.
Falls back silently if credentials are missing or sends fail.
"""

import asyncio
import logging
import time
import httpx

from core.config import CFG

log = logging.getLogger("hybrid.telegram")

# Rate limit: max 1 message per second (Telegram API limit is 30/sec
# but we keep it conservative to avoid issues)
_MIN_INTERVAL = 1.0
_last_send_ts = 0.0


def is_configured() -> bool:
    """Check if Telegram credentials are set."""
    return bool(CFG.telegram_token and CFG.telegram_chat_id)


async def send(text: str, parse_mode: str = "HTML",
               disable_preview: bool = True,
               _retries: int = 3) -> bool:
    """Send a Telegram message. Returns True on success.

    Retries up to _retries times on transient failures (network error,
    5xx, timeout). Respects Retry-After on 429 rate-limit responses.
    """
    global _last_send_ts

    if not is_configured():
        return False

    url = f"https://api.telegram.org/bot{CFG.telegram_token}/sendMessage"
    payload = {
        "chat_id": CFG.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }

    for attempt in range(_retries):
        # Rate limiting — honour minimum interval between sends
        now = time.time()
        elapsed = now - _last_send_ts
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
            _last_send_ts = time.time()

            if resp.status_code == 200:
                return True

            if resp.status_code == 429:
                # Telegram rate limit — back off by Retry-After header
                retry_after = int(resp.headers.get("Retry-After", 5))
                log.warning("Telegram rate limited — waiting %ds (attempt %d/%d)",
                            retry_after, attempt + 1, _retries)
                await asyncio.sleep(retry_after)
                continue

            log.warning("Telegram send failed: %d %s (attempt %d/%d)",
                        resp.status_code, resp.text[:200], attempt + 1, _retries)

        except Exception as e:
            log.warning("Telegram send error (attempt %d/%d): %s",
                        attempt + 1, _retries, e)

        # Backoff before retry (skip delay on final attempt)
        if attempt < _retries - 1:
            await asyncio.sleep(2.0 * (attempt + 1))

    return False


def send_sync(text: str, parse_mode: str = "HTML",
              disable_preview: bool = True) -> bool:
    """Synchronous wrapper around send() for non-async callers (e.g. redeem_now.py)."""
    try:
        return asyncio.run(send(text, parse_mode, disable_preview))
    except Exception as e:
        log.warning("Telegram sync send error: %s", e)
        return False


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


# ── Pre-built message formatters ──────────────────────────────────────

async def notify_trade_opened(trade) -> bool:
    """Send alert when a trade is opened."""
    emoji = "🟢" if trade.mode == "LIVE" else "📝"
    msg = (
        f"{emoji} <b>TRADE OPENED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Asset: <b>{trade.asset} {trade.direction}</b>\n"
        f"Entry: <b>${trade.entry_price:.4f}</b>\n"
        f"Size: <b>${trade.size_usdc:.2f}</b>\n"
        f"Oracle Δ: <b>{trade.oracle_delta:+.4f}%</b>\n"
        f"Confidence: <b>{trade.confidence:.0f}</b>\n"
        f"TTL: <b>{trade.time_remaining:.0f}s</b>\n"
        f"Mode: {trade.mode}\n"
        f"ID: <code>{trade.id}</code>"
    )
    return await send(msg)


async def notify_trade_closed(trade) -> bool:
    """Send alert when a trade is closed with result."""
    won = trade.pnl > 0
    emoji = "✅" if won else "❌"
    tag = "WIN" if won else "LOSS"
    fair = getattr(trade, "fair_value", None)
    fair_line = f"Fair value: ${fair:.4f}\n" if fair else ""
    msg = (
        f"{emoji} <b>TRADE {tag}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Asset: <b>{trade.asset} {trade.direction}</b>\n"
        f"Entry: ${trade.entry_price:.4f}  Size: ${trade.size_usdc:.2f}\n"
        f"{fair_line}"
        f"P&L: <b>${trade.pnl:+.4f}</b>\n"
        f"Oracle Δ: {trade.oracle_delta:+.4f}%\n"
        f"Mode: {trade.mode}\n"
        f"ID: <code>{trade.id}</code>"
    )
    return await send(msg)


async def notify_kill_switch(reason: str, daily_pnl: float,
                              portfolio: float) -> bool:
    """Send alert when kill switch is triggered."""
    msg = (
        f"🚨 <b>KILL SWITCH ACTIVATED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Reason: {_escape_html(reason)}\n"
        f"Daily P&L: <b>${daily_pnl:+.4f}</b>\n"
        f"Portfolio: ${portfolio:.2f}\n"
        f"⚠️ Trading paused until next UTC day"
    )
    return await send(msg)


async def notify_daily_summary(stats: dict, portfolio: float) -> bool:
    """Send daily performance summary."""
    pnl = stats.get("pnl", 0)
    emoji = "📈" if pnl >= 0 else "📉"
    msg = (
        f"{emoji} <b>DAILY SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades: <b>{stats.get('total', 0)}</b>\n"
        f"Win Rate: <b>{stats.get('wr', 0):.1f}%</b> "
        f"({stats.get('wins', 0)}/{stats.get('total', 0)})\n"
        f"P&L: <b>${pnl:+.4f}</b>\n"
        f"Expectancy: ${stats.get('expectancy', 0):+.4f}/trade\n"
        f"Avg Win: ${stats.get('avg_win', 0):.4f}\n"
        f"Avg Loss: ${stats.get('avg_loss', 0):.4f}\n"
        f"Portfolio: <b>${portfolio:.2f}</b>"
    )
    return await send(msg)


async def notify_redeemed(count: int, total_usdc: float) -> bool:
    """Send alert when winning positions are redeemed on-chain."""
    msg = (
        f"💰 <b>REDEEMED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Positions: <b>{count}</b>\n"
        f"Returned: <b>${total_usdc:.2f} USDC.e</b>\n"
        f"✅ Back in wallet"
    )
    return await send(msg)


async def notify_manual_redeem_start(n_positions: int,
                                     estimated_usdc: float) -> bool:
    """Send alert when manual redemption is kicked off via redeem_now.py."""
    msg = (
        f"🔄 <b>MANUAL REDEMPTION STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Positions: <b>{n_positions}</b>\n"
        f"Estimated: <b>~${estimated_usdc:.2f} USDC.e</b>\n"
        f"⏳ Submitting on-chain transactions..."
    )
    return await send(msg)


async def notify_redeem_result(attempted: int, redeemed: int,
                               total_usdc: float) -> bool:
    """Send the final outcome of a manual redemption run.

    Covers three cases:
      - All redeemed       → use notify_redeemed
      - Partial (some blocked by oracle guard)
      - Zero redeemed      (oracle not settled yet or RPC error)
    """
    if redeemed == attempted and total_usdc > 0:
        return await notify_redeemed(redeemed, total_usdc)

    if redeemed > 0 and total_usdc > 0:
        msg = (
            f"⚠️ <b>PARTIAL REDEMPTION</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Redeemed: <b>{redeemed}/{attempted}</b>\n"
            f"Returned: <b>${total_usdc:.2f} USDC.e</b>\n"
            f"Some positions blocked — oracle may not have settled yet.\n"
            f"Run <code>python3 redeem_now.py</code> again in a few minutes."
        )
    else:
        msg = (
            f"⏸ <b>REDEMPTION BLOCKED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Attempted: <b>{attempted}</b> position(s)\n"
            f"Oracle has not settled yet (payoutNumerators=0).\n"
            f"Run <code>python3 redeem_now.py</code> again in ~3 minutes."
        )
    return await send(msg)


async def notify_bot_start(mode: str, portfolio: float) -> bool:
    """Send alert when bot starts."""
    msg = (
        f"🤖 <b>BOT STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode: <b>{mode}</b>\n"
        f"Portfolio: <b>${portfolio:.2f}</b>\n"
        f"Entry window: T-{CFG.snipe_entry_sec:.0f}s to T-{CFG.snipe_exit_sec:.0f}s\n"
        f"Min delta: {CFG.min_delta_pct:.3f}%\n"
        f"Price range: ${CFG.min_token_price:.2f} - ${CFG.max_token_price:.2f}\n"
        f"Assets: {', '.join(CFG.assets)}"
    )
    return await send(msg)


async def notify_bot_stop(stats: dict, portfolio: float) -> bool:
    """Send alert when bot stops."""
    msg = (
        f"🛑 <b>BOT STOPPED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Lifetime P&L: <b>${stats.get('pnl', 0):+.4f}</b>\n"
        f"Win Rate: {stats.get('wr', 0):.1f}%\n"
        f"Total Trades: {stats.get('total', 0)}\n"
        f"Final Portfolio: <b>${portfolio:.2f}</b>"
    )
    return await send(msg)

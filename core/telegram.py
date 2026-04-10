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
from datetime import datetime, timezone
from typing import Optional

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
               disable_preview: bool = True) -> bool:
    """Send a Telegram message. Returns True on success."""
    global _last_send_ts

    if not is_configured():
        return False

    # Rate limiting
    now = time.time()
    elapsed = now - _last_send_ts
    if elapsed < _MIN_INTERVAL:
        await asyncio.sleep(_MIN_INTERVAL - elapsed)

    url = f"https://api.telegram.org/bot{CFG.telegram_token}/sendMessage"
    payload = {
        "chat_id": CFG.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            _last_send_ts = time.time()
            if resp.status_code == 200:
                return True
            else:
                log.warning("Telegram send failed: %d %s",
                            resp.status_code, resp.text[:200])
                return False
    except Exception as e:
        log.warning("Telegram send error: %s", e)
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
    msg = (
        f"{emoji} <b>TRADE {tag}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Asset: <b>{trade.asset} {trade.direction}</b>\n"
        f"Entry: ${trade.entry_price:.4f}\n"
        f"P&L: <b>${trade.pnl:+.4f}</b>\n"
        f"Oracle Δ: {trade.oracle_delta:+.4f}%\n"
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

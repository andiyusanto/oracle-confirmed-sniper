"""Risk manager: kill switches, position limits, daily caps."""

import logging
import time
from datetime import datetime, timezone

from core.config import CFG
from core.database import Database

log = logging.getLogger("hybrid.risk")


class RiskManager:
    def __init__(self, db: Database, portfolio: float):
        self.db = db
        self.portfolio = portfolio
        self.kill_switch = False
        self._daily_count = 0
        self._last_day = ""
        self._consecutive_losses = 0
        self._lockout_until = 0.0
        self._check_day()

    def _check_day(self):
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_day:
            self._last_day = today
            self._daily_count = self.db.daily_count()
            self.kill_switch = False

    def can_trade(self) -> tuple[bool, str]:
        """Check all risk conditions. Returns (allowed, reason)."""
        self._check_day()

        if self.kill_switch:
            return False, "kill switch active"

        # Consecutive loss lockout
        now = time.time()
        if now < self._lockout_until:
            remaining = int(self._lockout_until - now)
            return False, f"consecutive loss lockout ({remaining}s remaining)"

        daily_pnl = self.db.daily_pnl()

        # Max daily loss
        if daily_pnl < 0:
            loss_pct = abs(daily_pnl) / self.portfolio * 100
            if loss_pct > CFG.kill_switch_drawdown_pct:
                self.kill_switch = True
                log.critical("KILL SWITCH: daily loss %.1f%%", loss_pct)
                return False, f"kill switch: -{loss_pct:.1f}%"
            if loss_pct > CFG.max_daily_loss_pct:
                return False, f"daily loss cap: -{loss_pct:.1f}%"

        # Max daily trades
        if self._daily_count >= CFG.max_daily_trades:
            return False, f"daily trade cap: {self._daily_count}"

        return True, "ok"

    def check_concurrent(self, open_count: int) -> bool:
        return open_count < CFG.max_concurrent_positions

    def on_trade(self):
        """Record a new trade opening (count only; P&L unknown until close)."""
        self._daily_count += 1

    def on_trade_closed(self, pnl: float):
        """Update consecutive-loss streak and portfolio after a position closes."""
        self.portfolio = max(1.0, self.portfolio + pnl)
        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= CFG.consec_loss_limit:
                lockout_sec = CFG.consec_loss_lockout_min * 60
                self._lockout_until = time.time() + lockout_sec
                log.warning(
                    "LOCKOUT: %d consecutive losses — pausing %d min",
                    self._consecutive_losses,
                    CFG.consec_loss_lockout_min,
                )
        else:
            self._consecutive_losses = 0

    def update_portfolio(self, pnl: float):
        """Adjust portfolio without touching the consecutive-loss streak.

        Used for external corrections (startup redemption scan, periodic
        redeem) where the trade was already counted at close time.
        """
        self.portfolio = max(1.0, self.portfolio + pnl)

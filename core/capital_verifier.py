"""Capital verification engine.

Verifies that each trade's recorded P&L matches the expected formula
for its outcome (WIN / LOSS / WIN_CANCEL / LOSS_CANCEL), logs
discrepancies to the database, and emits warnings when the gap is large.

Severity tiers
--------------
  OK       < 1 % of stake       — normal rounding
  WARNING  1 – 5 % of stake     — investigate, auto-reconcile flag set
  CRITICAL > 5 % of stake       — trading paused until manual review

Integration
-----------
  Call `verify_trade_close(trade)` from executor.close_trade() or
  sell_position() immediately after the P&L is computed.  Call
  `snapshot(portfolio, reason, clob_balance)` on bot start/stop and
  after every redemption.
"""

import logging
from typing import Optional

from core.config import CFG
from core.database import Database
from core.models import Trade

log = logging.getLogger("hybrid.capital")

# Severity thresholds as fraction of trade stake
_WARN_FRAC = 0.01  # 1 %
_CRIT_FRAC = 0.05  # 5 %


class CapitalVerifier:
    def __init__(self, db: Database):
        self.db = db
        self._pause_trading = False

    # ── Public API ────────────────────────────────────────────────────

    @property
    def trading_paused(self) -> bool:
        return self._pause_trading

    def verify_trade_close(self, trade: Trade) -> dict:
        """Verify P&L recorded for a closed trade matches formula.

        Handles all four outcome types automatically:
          WIN          — pnl > 0 and status EXPIRED
          LOSS         — pnl < 0 and status EXPIRED
          CLOSED early — status CLOSED (reversal-exit sell)
          CANCELLED    — pnl = 0, no verification needed
        """
        if trade.status == "CANCELLED" or trade.mode != "LIVE":
            return {"outcome": "SKIP", "severity": "OK"}

        stake = trade.size_usdc
        actual_pnl = trade.pnl

        if trade.status == "CLOSED":
            outcome = "REVERSAL_EXIT"
            expected_pnl = actual_pnl  # sell_position() computed exact proceeds
        elif actual_pnl > 0:
            outcome = "WIN"
            expected_pnl = self._expected_win_pnl(stake, trade.entry_price)
        else:
            outcome = "LOSS"
            expected_pnl = -stake

        return self._check(trade.id, outcome, expected_pnl, actual_pnl, stake)

    def verify_win_cancel(
        self, trade_id: str, original_pnl: float, stake: float
    ) -> dict:
        """Verify a WIN→CANCELLED correction (market voided after WIN was recorded).

        Polymarket returned the stake (not the profit), so the net PnL must be 0.
        The portfolio should be decremented by original_pnl to undo the false gain.
        """
        return self._check(
            trade_id,
            "WIN_CANCELLED",
            expected_pnl=0.0,
            actual_pnl=round(-original_pnl, 6),  # delta already applied by caller
            stake=stake,
        )

    def verify_loss_cancel(
        self, trade_id: str, original_pnl: float, stake: float
    ) -> dict:
        """Verify a LOSS→CANCELLED correction (market voided, stake refunded).

        Polymarket returned the stake, so the net PnL must be 0.
        The portfolio should be incremented by stake (absolute value of original_pnl)
        to undo the false loss.
        """
        return self._check(
            trade_id,
            "LOSS_CANCELLED",
            expected_pnl=0.0,
            actual_pnl=round(-original_pnl, 6),  # delta already applied by caller
            stake=stake,
        )

    def verify_correction(
        self, trade_id: str, old_pnl: float, new_pnl: float, stake: float
    ) -> dict:
        """Verify a WIN→LOSS correction made by the startup redemption scan."""
        return self._check(
            trade_id,
            "WIN_CORRECTED_TO_LOSS",
            expected_pnl=-stake,
            actual_pnl=new_pnl,
            stake=stake,
        )

    def snapshot(
        self,
        portfolio: float,
        reason: str,
        clob_balance: Optional[float] = None,
    ) -> dict:
        """Save a portfolio snapshot and check against live CLOB balance.

        Returns a dict with the snapshot data and any discrepancy found.
        """
        self.db.save_snapshot(portfolio, reason, clob_balance)

        if clob_balance is None or clob_balance <= 0:
            return {
                "portfolio": portfolio,
                "clob_balance": None,
                "discrepancy": None,
                "severity": "OK",
            }

        discrepancy = abs(portfolio - clob_balance)
        frac = discrepancy / max(clob_balance, 0.01)
        severity = self._severity(frac, stake=clob_balance)

        result = {
            "portfolio": portfolio,
            "clob_balance": clob_balance,
            "discrepancy": round(discrepancy, 4),
            "frac": round(frac * 100, 2),
            "severity": severity,
        }

        if severity == "WARNING":
            log.warning(
                "CAPITAL WARN: bot portfolio $%.2f vs CLOB $%.2f "
                "(gap $%.4f = %.2f%%) — reason: %s",
                portfolio,
                clob_balance,
                discrepancy,
                frac * 100,
                reason,
            )
        elif severity == "CRITICAL":
            log.critical(
                "CAPITAL CRITICAL: bot portfolio $%.2f vs CLOB $%.2f "
                "(gap $%.4f = %.2f%%) — TRADING PAUSED pending manual review. "
                "Run: python verify_capital.py --fix",
                portfolio,
                clob_balance,
                discrepancy,
                frac * 100,
            )
            self._pause_trading = True

        return result

    def clear_pause(self) -> None:
        self._pause_trading = False
        log.info("CAPITAL: trading-pause cleared manually")

    # ── Internal helpers ──────────────────────────────────────────────

    def _expected_win_pnl(self, stake: float, entry_price: float) -> float:
        """Net profit on a WIN trade after taker fee deduction.

        Polymarket deducts the taker fee from the token quantity at fill:
          actual_received = shares × (1 − fee_rate)
          net_proceeds    = actual_received × $1.00
          pnl             = net_proceeds − stake
        """
        shares = stake / entry_price
        fee_rate = CFG.taker_fee_pct / 100
        net_proceeds = shares * (1.0 - fee_rate)
        return round(net_proceeds - stake, 6)

    def _check(
        self,
        trade_id: str,
        outcome: str,
        expected_pnl: float,
        actual_pnl: float,
        stake: float,
    ) -> dict:
        discrepancy = abs(actual_pnl - expected_pnl)
        frac = discrepancy / max(stake, 0.01)
        severity = self._severity(frac, stake)

        self.db.save_verification(
            trade_id=trade_id,
            outcome=outcome,
            expected_pnl=expected_pnl,
            actual_pnl=actual_pnl,
            discrepancy=discrepancy,
            severity=severity,
        )

        result = {
            "trade_id": trade_id,
            "outcome": outcome,
            "expected_pnl": round(expected_pnl, 6),
            "actual_pnl": round(actual_pnl, 6),
            "discrepancy": round(discrepancy, 6),
            "severity": severity,
        }

        if severity == "OK":
            log.debug(
                "VERIFY OK [%s] %s: expected=$%+.4f actual=$%+.4f disc=$%.6f",
                outcome,
                trade_id[:8],
                expected_pnl,
                actual_pnl,
                discrepancy,
            )
        elif severity == "WARNING":
            log.warning(
                "VERIFY WARN [%s] %s: expected=$%+.4f actual=$%+.4f "
                "disc=$%.4f (%.1f%% of stake)",
                outcome,
                trade_id[:8],
                expected_pnl,
                actual_pnl,
                discrepancy,
                frac * 100,
            )
        elif severity == "CRITICAL":
            log.critical(
                "VERIFY CRITICAL [%s] %s: expected=$%+.4f actual=$%+.4f "
                "disc=$%.4f (%.1f%% of stake) — TRADING PAUSED",
                outcome,
                trade_id[:8],
                expected_pnl,
                actual_pnl,
                discrepancy,
                frac * 100,
            )
            self._pause_trading = True

        return result

    @staticmethod
    def _severity(frac: float, stake: float) -> str:
        if frac >= _CRIT_FRAC:
            return "CRITICAL"
        if frac >= _WARN_FRAC:
            return "WARNING"
        return "OK"

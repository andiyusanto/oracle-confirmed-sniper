"""Thread-safe SQLite database for trade storage."""

import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from core.models import Trade


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                direction TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size_usdc REAL NOT NULL,
                oracle_delta REAL DEFAULT 0,
                confidence REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                status TEXT DEFAULT 'OPEN',
                mode TEXT DEFAULT 'PAPER',
                opened_at REAL NOT NULL,
                closed_at REAL,
                window_ts INTEGER NOT NULL,
                time_remaining REAL DEFAULT 0,
                fair_value REAL DEFAULT 0,
                binance_price REAL DEFAULT 0,
                chainlink_price REAL DEFAULT 0,
                opening_price REAL DEFAULT 0,
                condition_id TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
            CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window_ts);

            CREATE TABLE IF NOT EXISTS capital_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                expected_pnl REAL NOT NULL,
                actual_pnl REAL NOT NULL,
                discrepancy REAL NOT NULL,
                severity TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cv_trade ON capital_verifications(trade_id);
            CREATE INDEX IF NOT EXISTS idx_cv_ts ON capital_verifications(timestamp);

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                portfolio REAL NOT NULL,
                clob_balance REAL,
                reason TEXT NOT NULL,
                discrepancy REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ps_ts ON portfolio_snapshots(timestamp);
        """)
        # Migration: add condition_id to existing DBs that predate this column
        try:
            self.conn.execute(
                "ALTER TABLE trades ADD COLUMN condition_id TEXT DEFAULT ''"
            )
            self.conn.commit()
        except Exception as _e:
            if "duplicate column" not in str(_e).lower():
                import logging as _log

                _log.getLogger("hybrid.db").warning(
                    "condition_id migration failed: %s", _e
                )

    def save_trade(self, t: Trade):
        with self._lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO trades VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""",
                (
                    t.id,
                    t.asset,
                    t.direction,
                    t.side,
                    t.entry_price,
                    t.size_usdc,
                    t.oracle_delta,
                    t.confidence,
                    t.pnl,
                    t.status,
                    t.mode,
                    t.opened_at,
                    t.closed_at,
                    t.window_ts,
                    t.time_remaining,
                    t.fair_value,
                    t.binance_price,
                    t.chainlink_price,
                    t.opening_price,
                    getattr(t, "condition_id", ""),
                ),
            )
            self.conn.commit()

    def correct_trade_to_cancelled(self, condition_id: str) -> Optional[float]:
        """Reverse PnL for an EXPIRED trade when Polymarket cancels the market.

        On cancellation both YES and NO outcomes pay out equally — the stake is
        returned, not any profit. Whatever the bot recorded as PnL (positive WIN
        profit or negative LOSS stake) must be zeroed out so the portfolio reflects
        reality (net change = 0 — stake back, no gain or loss).

        Returns the pnl delta the caller must apply to the in-memory portfolio:
          WIN_CANCEL : delta = -(recorded_profit)   (negative — removes false gain)
          LOSS_CANCEL: delta = +size_usdc            (positive — restores debited stake)
        Returns None if no matching trade found.
        """
        if not condition_id:
            return None
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, size_usdc, pnl FROM trades "
                "WHERE condition_id=? AND status='EXPIRED'",
                (condition_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            trade_id, _size_usdc, original_pnl = row
            delta = round(0.0 - original_pnl, 6)  # reverse whatever PnL was recorded
            self.conn.execute(
                "UPDATE trades SET pnl=0, status='CANCELLED' WHERE id=?",
                (trade_id,),
            )
            self.conn.commit()
            return delta

    def correct_trade_to_loss(self, condition_id: str) -> Optional[float]:
        """Correct a false-WIN trade to LOSS when oracle confirms we lost.

        Returns the pnl delta (negative float) if a correction was made,
        or None if no matching trade was found. The caller should apply
        this delta to the in-memory portfolio tracker for in-session corrections.
        """
        if not condition_id:
            return None
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, size_usdc, pnl FROM trades "
                "WHERE condition_id=? AND status='EXPIRED' AND pnl > 0",
                (condition_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            trade_id, size_usdc, original_pnl = row
            true_loss = round(-size_usdc, 6)
            self.conn.execute(
                "UPDATE trades SET pnl=?, status='EXPIRED' WHERE id=?",
                (true_loss, trade_id),
            )
            self.conn.commit()
            return round(true_loss - original_pnl, 6)

    def close_trade(self, tid: str, pnl: float, status: str = "EXPIRED"):
        with self._lock:
            self.conn.execute(
                "UPDATE trades SET pnl=?, status=?, closed_at=? WHERE id=?",
                (round(pnl, 6), status, time.time(), tid),
            )
            self.conn.commit()

    def open_trades(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY opened_at"
        )
        return self._rows(cur)

    def recent(self, n: int = 15) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (n,)
        )
        return self._rows(cur)

    def daily_pnl(self) -> float:
        ts = (
            datetime.now(tz=timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE opened_at >= ?", (ts,)
        )
        return cur.fetchone()[0]

    def daily_count(self) -> int:
        ts = (
            datetime.now(tz=timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE opened_at >= ?", (ts,)
        )
        return cur.fetchone()[0]

    def lifetime_stats(self) -> dict:
        cur = self.conn.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(pnl), 0),
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0),
                   COALESCE(AVG(CASE WHEN pnl <= 0 THEN pnl END), 0),
                   COALESCE(MAX(pnl), 0),
                   COALESCE(MIN(pnl), 0)
            FROM trades WHERE status IN ('EXPIRED', 'CLOSED')
        """)
        total, wins, pnl, avg_w, avg_l, max_w, max_l = cur.fetchone()
        return {
            "total": total,
            "wins": wins,
            "pnl": round(pnl, 4),
            "wr": round(wins / total * 100, 1) if total else 0,
            "avg_win": round(avg_w, 4),
            "avg_loss": round(avg_l, 4),
            "max_win": round(max_w, 4),
            "max_loss": round(max_l, 4),
            "expectancy": round(pnl / total, 4) if total else 0,
        }

    def save_verification(
        self,
        trade_id: str,
        outcome: str,
        expected_pnl: float,
        actual_pnl: float,
        discrepancy: float,
        severity: str,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO capital_verifications "
                "(trade_id, outcome, expected_pnl, actual_pnl, discrepancy, severity, timestamp) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    trade_id,
                    outcome,
                    round(expected_pnl, 6),
                    round(actual_pnl, 6),
                    round(discrepancy, 6),
                    severity,
                    time.time(),
                ),
            )
            self.conn.commit()

    def save_snapshot(
        self,
        portfolio: float,
        reason: str,
        clob_balance: Optional[float] = None,
    ) -> None:
        discrepancy = (
            round(abs(portfolio - clob_balance), 4)
            if clob_balance is not None
            else None
        )
        with self._lock:
            self.conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(timestamp, portfolio, clob_balance, reason, discrepancy) "
                "VALUES (?,?,?,?,?)",
                (time.time(), round(portfolio, 4), clob_balance, reason, discrepancy),
            )
            self.conn.commit()

    def verification_summary(self) -> dict:
        cur = self.conn.execute(
            "SELECT severity, COUNT(*) FROM capital_verifications GROUP BY severity"
        )
        counts: dict[str, int] = {r[0]: r[1] for r in cur.fetchall()}
        cur2 = self.conn.execute(
            "SELECT outcome, COUNT(*), ROUND(AVG(ABS(discrepancy)),6) "
            "FROM capital_verifications GROUP BY outcome"
        )
        by_outcome = {r[0]: {"count": r[1], "avg_disc": r[2]} for r in cur2.fetchall()}
        return {"by_severity": counts, "by_outcome": by_outcome}

    def recent_verifications(self, n: int = 20) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM capital_verifications ORDER BY timestamp DESC LIMIT ?", (n,)
        )
        return self._rows(cur)

    def recent_snapshots(self, n: int = 20) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT ?", (n,)
        )
        return self._rows(cur)

    def _rows(self, cur) -> list[dict]:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

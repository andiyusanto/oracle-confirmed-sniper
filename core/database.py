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
        """)
        # Migration: add condition_id to existing DBs that predate this column
        try:
            self.conn.execute("ALTER TABLE trades ADD COLUMN condition_id TEXT DEFAULT ''")
            self.conn.commit()
        except Exception as _e:
            if "duplicate column" not in str(_e).lower():
                import logging as _log
                _log.getLogger("hybrid.db").warning("condition_id migration failed: %s", _e)

    def save_trade(self, t: Trade):
        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO trades VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""", (
                t.id, t.asset, t.direction, t.side, t.entry_price,
                t.size_usdc, t.oracle_delta, t.confidence, t.pnl,
                t.status, t.mode, t.opened_at, t.closed_at,
                t.window_ts, t.time_remaining, t.fair_value,
                t.binance_price, t.chainlink_price, t.opening_price,
                getattr(t, 'condition_id', ''),
            ))
            self.conn.commit()

    def correct_trade_to_loss(self, condition_id: str) -> bool:
        """Correct a false-WIN trade to LOSS when oracle confirms we lost.

        Returns True if a matching EXPIRED trade was found and updated.
        """
        if not condition_id:
            return False
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, size_usdc FROM trades "
                "WHERE condition_id=? AND status='EXPIRED' AND pnl > 0",
                (condition_id,)
            )
            row = cur.fetchone()
            if not row:
                return False
            trade_id, size_usdc = row
            true_loss = round(-size_usdc, 6)
            self.conn.execute(
                "UPDATE trades SET pnl=?, status='EXPIRED' WHERE id=?",
                (true_loss, trade_id)
            )
            self.conn.commit()
            return True

    def close_trade(self, tid: str, pnl: float, status: str = "EXPIRED"):
        with self._lock:
            self.conn.execute(
                "UPDATE trades SET pnl=?, status=?, closed_at=? WHERE id=?",
                (round(pnl, 6), status, time.time(), tid))
            self.conn.commit()

    def open_trades(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY opened_at")
        return self._rows(cur)

    def recent(self, n: int = 15) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (n,))
        return self._rows(cur)

    def daily_pnl(self) -> float:
        ts = datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE opened_at >= ?", (ts,))
        return cur.fetchone()[0]

    def daily_count(self) -> int:
        ts = datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE opened_at >= ?", (ts,))
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
            "total": total, "wins": wins, "pnl": round(pnl, 4),
            "wr": round(wins / total * 100, 1) if total else 0,
            "avg_win": round(avg_w, 4), "avg_loss": round(avg_l, 4),
            "max_win": round(max_w, 4), "max_loss": round(max_l, 4),
            "expectancy": round(pnl / total, 4) if total else 0,
        }

    def _rows(self, cur) -> list[dict]:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

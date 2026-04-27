"""Data models for the hybrid sniper."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Token:
    token_id: str
    asset: str  # BTC, ETH
    direction: str  # UP, DOWN
    duration: str  # 5min, 15min
    end_ts: float
    window_ts: int
    book_price: float = 0.5
    book_updated: float = 0.0
    book_spread: float = (
        0.0  # bid-ask spread as fraction of mid price (e.g. 0.15 = 15%)
    )
    conditionId: str = ""  # CTF conditionId — empty if Gamma API didn't return it


@dataclass
class OracleState:
    asset: str
    window_ts: int
    opening_price: float
    current_price: float = 0.0
    delta_pct: float = 0.0
    oracle_says: str = ""  # UP or DOWN
    binance_agrees: bool = False
    last_update: float = 0.0


@dataclass
class Signal:
    token: Token
    oracle: OracleState
    side: str  # YES or NO
    entry_price: float
    fair_value: float
    edge_pct: float
    confidence: float
    size_usdc: float
    time_remaining: float


@dataclass
class Trade:
    id: str
    asset: str
    direction: str
    side: str
    entry_price: float
    size_usdc: float
    oracle_delta: float
    confidence: float
    pnl: float = 0.0
    status: str = "OPEN"  # OPEN, EXPIRED, CLOSED, CANCELLED
    mode: str = "PAPER"
    opened_at: float = 0.0
    closed_at: Optional[float] = None
    window_ts: int = 0
    time_remaining: float = 0.0
    fair_value: float = 0.0
    binance_price: float = 0.0
    chainlink_price: float = 0.0
    opening_price: float = 0.0
    duration_sec: int = 300  # market window length (5m=300, 15m=900)
    condition_id: str = ""  # CTF conditionId — used to correct false-WIN records

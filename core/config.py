"""
Configuration for Oracle-Confirmed Sniper (Strategy D).

Combines oracle-lead detection (A) with end-cycle sniping (B).
Tuned for maximum trade count while maintaining edge.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Credentials ─────────────────────────────────────────────────
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    api_key: str = os.getenv("POLY_API_KEY", "")
    api_secret: str = os.getenv("POLY_API_SECRET", "")
    api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")
    funder_address: str = os.getenv("POLY_FUNDER_ADDRESS", "")
    sig_type: int = int(os.getenv("POLY_SIG_TYPE", "0"))
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Endpoints ───────────────────────────────────────────────────
    clob_host: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com/events"
    rtds_url: str = "wss://ws-live-data.polymarket.com"
    binance_ws: str = "wss://data-stream.binance.com/stream"

    # ── Strategy D: Oracle-Confirmed Sniper ─────────────────────────
    #
    # PHASE 1 (T-120s to T-30s): Oracle-lead detection
    #   Monitor Chainlink oracle delta from opening "Price to Beat"
    #   Build conviction as delta grows and time shrinks
    #
    # PHASE 2 (T-30s to T-5s): Snipe execution
    #   If oracle confirms AND token price is in snipe range → execute
    #   Buy the winning-side token at $0.60-0.95 (market priced it in)
    #   Oracle confirmation pushes actual WR above market-implied WR
    #
    # The edge: you know the resolution source's answer AND you enter
    # late enough that reversal is unlikely, but early enough that
    # the price hasn't fully converged to $1.00

    # ── Window timing ───────────────────────────────────────────────
    # Phase 1: start watching oracle delta
    oracle_watch_sec: float = 120.0     # start checking at T-120s
    # Phase 2: execution window
    snipe_entry_sec: float = 60.0       # can enter from T-60s (aggressive)
    snipe_exit_sec: float = 3.0         # stop at T-3s (need fill time)

    # ── Oracle thresholds ───────────────────────────────────────────
    # Minimum delta from opening price to consider (filters noise)
    min_delta_pct: float = 0.015        # 0.015% = ~$10 at $67k BTC
    # Strong delta — higher confidence, can enter earlier
    strong_delta_pct: float = 0.05      # 0.05% = ~$33 at $67k BTC
    # Very strong delta — near-certain outcome
    extreme_delta_pct: float = 0.10     # 0.10% = ~$67 at $67k BTC

    # ── Token price range ───────────────────────────────────────────
    # How cheap the winning token must still be to have edge
    min_token_price: float = 0.55       # aggressive: include 55%+ tokens
    max_token_price: float = 0.95       # don't buy above 95%

    # Price tiers for dynamic sizing:
    # $0.55-0.70: risky but high reward, small size
    # $0.70-0.85: moderate confidence, normal size
    # $0.85-0.95: high confidence, larger size

    # ── Confidence scoring ──────────────────────────────────────────
    min_confidence: float = 35.0        # minimum combined score to trade
    # Score components:
    #   delta_score:  0-40 (how far oracle moved from open)
    #   time_score:   0-30 (less time = more certain)
    #   price_score:  0-20 (market agreement)
    #   binance_score: 0-10 (cross-validation)

    # ── Position sizing ─────────────────────────────────────────────
    max_position_pct: float = 3.0       # max 3% of portfolio
    max_position_usdc: float = 30.0     # hard cap
    kelly_fraction: float = 0.25        # quarter-Kelly
    live_max_usdc: float = 10.0         # live safety cap

    # ── Dynamic sizing by entry price ───────────────────────────────
    # Higher entry = more confident = larger size multiplier
    size_mult_low: float = 0.5          # $0.55-0.70: half size
    size_mult_mid: float = 1.0          # $0.70-0.85: full size
    size_mult_high: float = 1.3         # $0.85-0.95: 130% size

    # ── Risk management ─────────────────────────────────────────────
    kill_switch_drawdown_pct: float = 15.0
    max_daily_trades: int = 100         # high limit for data collection
    max_concurrent_positions: int = 4   # max simultaneous open
    cooldown_sec: float = 0.5           # between orders
    max_daily_loss_pct: float = 10.0    # pause after 10% daily loss

    # ── Fee structure ───────────────────────────────────────────────
    use_maker: bool = True
    maker_rebate_pct: float = 0.20
    taker_fee_pct: float = 1.80

    # ── Market selection ────────────────────────────────────────────
    assets: list = field(default_factory=lambda: ["BTC", "ETH"])
    durations: list = field(default_factory=lambda: [("5m", 300)])
    # Enable 15m markets too for more opportunities:
    # durations: list = field(default_factory=lambda: [("5m", 300), ("15m", 900)])

    # ── Infrastructure ──────────────────────────────────────────────
    db_path: str = "hybrid_trades.db"
    log_file: str = "hybrid.log"
    poll_interval: float = 0.8         # fast poll in snipe window
    discovery_interval: float = 45.0   # rediscover markets
    book_cache_sec: float = 2.0        # cache order book for N seconds


CFG = Config()

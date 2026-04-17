"""
Configuration for Oracle-Confirmed Sniper (Strategy D).

Optimized for maximum PnL × trade count product.
Changes from original:
  - Tiered entry windows: aggressive (T-45s) for strong deltas, 
    conservative (T-25s) for weak deltas
  - Delta thresholds raised slightly to filter noise
  - Confidence threshold lowered for high-delta signals
  - Discovery interval shortened to avoid missing windows
  - Added 15min markets (commented) for more opportunities
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

    # ── Strategy D: Oracle-Confirmed Sniper (Optimized) ─────────────
    #
    # Key optimization: TIERED entry windows based on delta strength
    # Strong delta (>0.05%) → enter from T-45s (more time, but confident)
    # Weak delta (0.02-0.05%) → enter from T-25s (need time confirmation)
    # Extreme delta (>0.10%) → enter from T-55s (very high conviction)

    # ── Window timing ───────────────────────────────────────────────
    oracle_watch_sec: float = 120.0     # start watching at T-120s
    snipe_entry_sec: float = 75.0       # max entry window (extreme delta)
    snipe_entry_strong: float = 55.0    # strong delta entry
    snipe_entry_weak: float = 25.0      # weak delta entry — tighter window
    snipe_exit_sec: float = 3.0         # stop at T-3s (need fill time)

    # ── Oracle thresholds (slightly tightened) ──────────────────────
    min_delta_pct: float = 0.025        # raised: 0.015 admitted too many marginal signals
    strong_delta_pct: float = 0.050     # unchanged
    extreme_delta_pct: float = 0.100    # unchanged

    # ── Token price range ───────────────────────────────────────────
    min_token_price: float = 0.55       # keep aggressive for volume
    max_token_price: float = 0.95

    # ── Oracle source ────────────────────────────────────────────────
    # Require Binance to agree with Chainlink direction before trading.
    # Both sources must show the same direction (UP/DOWN) vs window open.
    # If Binance feed is stale (>30s), this check is skipped automatically.
    require_binance_agrees: bool = True

    # ── Confidence scoring ──────────────────────────────────────────
    min_confidence: float = 20.0        # floor only — real trades score 55+; was 35.0
    min_confidence_strong: float = 15.0 # lower bar for strong deltas; was 30.0
    # Score components:
    #   delta_score:    0-40 (how far oracle moved from open)
    #   time_score:     0-30 (less time = more certain)
    #   price_score:    0-20 (market agreement)
    #   freshness_score: 0-10 (Chainlink data staleness)

    # ── Position sizing ─────────────────────────────────────────────
    max_position_pct: float = 3.0       # max 3% of portfolio
    max_position_usdc: float = 30.0     # hard cap
    kelly_fraction: float = 0.25        # quarter-Kelly
    live_max_usdc: float = 15.0         # live safety cap (≥$4.75 needed for 5-share minimum)
    min_shares: float = 5.0             # Polymarket minimum order size (shares)

    # ── Dynamic sizing by entry price ───────────────────────────────
    size_mult_low: float = 0.5          # $0.55-0.70: half size
    size_mult_mid: float = 1.0          # $0.70-0.85: full size
    size_mult_high: float = 1.3         # $0.85-0.95: 130% size

    # ── Risk management ─────────────────────────────────────────────
    kill_switch_drawdown_pct: float = 15.0
    max_daily_trades: int = 288          # ~3 assets × 2 durations × ~48 windows/day
    max_concurrent_positions: int = 9   # 3 assets × 3 max concurrent per asset
    cooldown_sec: float = 0.5
    max_daily_loss_pct: float = 10.0

    # ── Fee structure ───────────────────────────────────────────────
    # Orders are placed at best_ask → immediate match → taker in practice.
    # Polymarket deducts the taker fee from token quantity at fill time.
    # Market requires fee_rate_bps=1000 (10%). Previous assumption of 2% was
    # wrong — CLOB rejected orders with fee_rate_bps=200.
    use_maker: bool = False
    maker_rebate_pct: float = 0.20   # unused while use_maker=False
    taker_fee_pct: float = 10.0      # actual Polymarket taker fee = 10% (1000 bps)

    # ── Edge filter floor ───────────────────────────────────────────
    # Minimum edge % required before a signal is traded, independent of the
    # fee-based break-even calc. At 10% fee the fee_edge formula already
    # produces a ~11% floor at $0.75; min_edge_pct acts as a sanity floor only.
    min_edge_pct: float = 9.0

    # ── Market selection ────────────────────────────────────────────
    assets: list = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    durations: list = field(default_factory=lambda: [("5m", 300), ("15m", 900)])

    # ── Signal quality gates (ghost-redemption prevention) ──────────────
    # 1. Staleness hard gate: block entry when CL data is stale AND TTL > 15s
    cl_staleness_hard_sec: float = 15.0  # CL seconds-old threshold for hard block; was 10.0

    # 2. Spread gate: skip tokens with wide bid-ask spread (thin/uncertain market)
    max_spread_pct: float = 0.20         # max spread as fraction of mid (0.20 = 20%)

    # 3. Consecutive pass: signal must pass all gates twice before firing
    consecutive_pass_window_sec: float = 2.0  # max gap between two consecutive passes; was 1.0

    # ── Live exit on oracle reversal ─────────────────────────────────────
    # After a fill, if oracle delta reverses and holds for exit_reversal_hold_sec,
    # attempt to sell the position back to the CLOB to limit the loss.
    exit_reversal_min_ttl: float = 12.0  # skip exit if < 12s left in window
    exit_reversal_hold_sec: float = 8.0  # reversal must persist this long before exiting

    # ── Infrastructure ──────────────────────────────────────────────
    db_path: str = "hybrid_trades.db"
    log_dir: str = "logs"             # daily logs saved as logs/YYYY-MM-DD_hybrid.log
    poll_interval: float = 0.1
    discovery_interval: float = 30.0    # reduced from 45s — catch more windows
    book_cache_sec: float = 2.0


CFG = Config()

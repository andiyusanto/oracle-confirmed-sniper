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
    snipe_entry_weak: float = 40.0      # weak delta entry — widened from 35s; was 25.0
    snipe_exit_sec: float = 16.0        # minimum TTL at entry — TTL≤15s is ghost zone (3 confirmed cases)

    # ── Oracle thresholds (slightly tightened) ──────────────────────
    min_delta_pct: float = 0.012        # lowered: captures 0.013-0.014% near-misses seen in flat markets
    strong_delta_pct: float = 0.050     # unchanged
    extreme_delta_pct: float = 0.100    # unchanged

    # ── Token price range ───────────────────────────────────────────
    min_token_price: float = 0.55
    max_token_price: float = 0.67       # UP+price≤0.67: PF=1.207; UP+price>0.67: PF=0.71

    # ── Direction filter ─────────────────────────────────────────────
    # DOWN oracle signal is anti-predictive in bull conditions: 9 trades, WR=11.1%
    # (z=-2.33, p<0.05). Net loss -$30.84/5d = 46% of all losses on 8% of trades.
    allow_down_direction: bool = False  # was True — UP-only eliminates worst loss source

    # DOWN condition thresholds
    down_min_delta_pct: float = 0.10     # minimum |delta| for DOWN (extreme tier)
    down_snipe_entry_sec: float = 35.0   # DOWN: only enter when TTL ≤ 35s
    down_snipe_exit_sec: float = 10.0    # DOWN: minimum TTL at entry

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
    # Inverted from original — low-price tokens have best payoff ratio (b≥0.66).
    # With max_token_price=0.67, all entries fall in the low bucket.
    # 1.0× = quarter-Kelly (3.0% of portfolio at $140) — safe, 5 losses to kill switch.
    size_mult_low: float = 1.0          # $0.55-0.70: full size (best EV)
    size_mult_mid: float = 0.9          # $0.70-0.85: slightly reduced
    size_mult_high: float = 0.5         # $0.85-0.95: half size (worst payoff ratio)

    # ── Risk management ─────────────────────────────────────────────
    kill_switch_drawdown_pct: float = 15.0
    max_daily_trades: int = 50           # UP+price≤0.67 generates ~7/day; 50 is a safety cap
    max_concurrent_positions: int = 6    # was 9 — cluster losses: 4 events = 63% of all losses
    cooldown_sec: float = 0.5
    max_daily_loss_pct: float = 10.0
    consec_loss_limit: int = 3           # trigger lockout after 3 consecutive losses
    consec_loss_lockout_min: int = 30    # lockout duration in minutes
    # Market-open volatility windows — oracle fires on spike but CTF reverts before settlement.
    # UTC 00-02: Asia equity open (HK/SGX 08:00-10:00 SGT = 00:00-02:00 UTC)
    # UTC 06-07: EU pre-market + London/Frankfurt open (07:00-08:00 CET = 06:00-07:00 UTC)
    # UTC 17: US midday algo/HFT peak
    # Evidence: 38 UP trades in these hours → WR=44.7%, PF=0.266, net=-$64.11
    #           38 UP trades outside these hours → WR=86.8%, PF=3.130, net=+$38.59
    # NOTE: previous config had [7] derived from local time (WIB=UTC+7). UTC 0 = 07:00 WIB.
    blackout_hours_utc: list = field(default_factory=lambda: [0, 2, 6, 7, 17])

    # ── Fee structure ───────────────────────────────────────────────
    # Orders are placed at best_ask → immediate match → taker in practice.
    # Polymarket deducts the taker fee from token quantity at fill time.
    # Market requires fee_rate_bps=1000 (10%). Previous assumption of 2% was
    # wrong — CLOB rejected orders with fee_rate_bps=200.
    use_maker: bool = False
    maker_rebate_pct: float = 0.20   # unused while use_maker=False
    taker_fee_pct: float = 1.5        # confirmed ~1.31% on-chain; 1.5% is a safe ceiling

    # ── Edge filter floor ───────────────────────────────────────────
    # Minimum edge % required before a signal is traded. With 1.5% taker fee,
    # fee_edge ≈ 2% at entry=0.60; 6.0% floor = ~3× the fee, well above breakeven.
    # Previous fair_value returned 0.95 for delta≥0.20% → fake 25% "edge" at $0.70.
    # Recalibrated fair_value + 6% floor together block HIGH-delta bad trades.
    min_edge_pct: float = 6.0

    # ── Market selection ────────────────────────────────────────────
    assets: list = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    durations: list = field(default_factory=lambda: [("5m", 300), ("15m", 900)])

    # ── Signal quality gates (ghost-redemption prevention) ──────────────
    # 1. Staleness hard gate: block entry when CL data is stale AND TTL > 15s
    cl_staleness_hard_sec: float = 30.0  # raised: aligns with best_price() 30s threshold; was 15.0

    # 2. Spread gate: skip tokens with wide bid-ask spread (thin/uncertain market)
    max_spread_pct: float = 0.12         # tightened from 0.20 — wide spread = signal already priced in or thin book

    # 3. Consecutive pass: signal must pass all gates twice before firing
    consecutive_pass_window_sec: float = 3.0  # widened for async I/O latency; was 2.0

    # 4. Unconfirmed delta TTL gate: when delta has no history above min_delta_pct
    #    in the last 20s or 30s (appeared suddenly), require this minimum TTL.
    #    Prevents late-entry ghost: transient CL spike resolves before CTF settlement.
    min_ttl_unconfirmed_sec: float = 10.0

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

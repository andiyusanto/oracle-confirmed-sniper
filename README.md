# Oracle-Confirmed Sniper (Strategy D)

A Polymarket trading bot that combines **oracle-lead detection** with **end-cycle sniping** on BTC and ETH 5-minute prediction markets.

## How It Works

The bot exploits a structural edge: Chainlink oracle prices resolve Polymarket's 5-minute crypto markets, but the oracle answer is publicly readable seconds before resolution. When the oracle has moved significantly from the window's opening price, the outcome is largely determined — yet tokens may still be priced below $1.00.

**Two-phase strategy:**

| Phase | Window | Action |
|---|---|---|
| 1 — Oracle watch | T-120s to T-60s | Monitor Chainlink delta vs. opening price; build conviction |
| 2 — Snipe execution | T-60s to T-3s | If oracle confirms AND token is in range → execute |

**All four conditions must be true to trade:**
1. Time remaining is within the snipe window (T-60s to T-3s)
2. Chainlink oracle has moved at least `min_delta_pct` from the window's opening price
3. Token price is in the range $0.55–$0.95 (market partially agrees, room for profit)
4. Combined confidence score exceeds threshold (default: 35/100)

## Confidence Scoring

Scores combine four components (max 100):

| Component | Max | Description |
|---|---|---|
| Delta score | 40 | How far oracle moved from window open |
| Time score | 30 | Less time remaining = outcome more certain |
| Price score | 20 | Higher token price = stronger market agreement |
| Freshness score | 10 | Chainlink data staleness |

## Project Structure

```
oracle-confirmed-sniper/
├── bot.py          # Main entry point and event loop
├── config.py       # All tunable parameters (CFG)
├── models.py       # Data classes: Token, OracleState, Signal, Trade
├── signal.py       # HybridEngine: signal evaluation and sizing
├── risk.py         # RiskManager: kill switches, daily caps
├── executor.py     # Trade execution (paper and live)
├── markets.py      # Market discovery via Gamma API
├── prices.py       # Price feeds: Chainlink (RTDS) + Binance WebSocket
├── database.py     # SQLite persistence
├── dashboard.py    # Rich terminal UI
├── analyze.py      # Post-session trade analysis
└── .env.example    # Environment variable template
```

## Setup

**Requirements:** Python 3.10+

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

```env
POLY_PRIVATE_KEY=0x...
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=
POLY_FUNDER_ADDRESS=
POLY_SIG_TYPE=0

# Optional: Telegram alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## Usage

### Paper mode (default — no real money)

```bash
python bot.py
```

```bash
python bot.py --portfolio 500   # custom starting portfolio size
```

### Live mode

Live mode requires three explicit flags as a safety gate:

```bash
python bot.py --live --confirm-live --accept-risk
```

### Analyze past trades

```bash
python analyze.py                  # all history
python analyze.py --days 7         # last 7 days
python analyze.py --db path/to.db  # custom database
```

Analysis output includes: overall P&L, win rate, expectancy, and breakdowns by asset/direction, entry price tier, oracle delta magnitude, time remaining, hour of day, and daily.

## Key Parameters (`config.py`)

### Timing
| Parameter | Default | Description |
|---|---|---|
| `snipe_entry_sec` | 60s | Start of execution window (T-60s) |
| `snipe_exit_sec` | 3s | Stop entering at T-3s (fill time) |
| `oracle_watch_sec` | 120s | Start watching oracle from T-120s |

### Oracle thresholds
| Parameter | Default | Description |
|---|---|---|
| `min_delta_pct` | 0.015% | Minimum delta to consider (~$10 at $67k BTC) |
| `strong_delta_pct` | 0.05% | Strong signal threshold |
| `extreme_delta_pct` | 0.10% | Near-certain outcome threshold |

### Token price range
| Parameter | Default | Description |
|---|---|---|
| `min_token_price` | $0.55 | Don't buy below 55c (too risky) |
| `max_token_price` | $0.95 | Don't buy above 95c (no room for profit) |

### Position sizing
| Parameter | Default | Description |
|---|---|---|
| `max_position_pct` | 3% | Max position as % of portfolio |
| `max_position_usdc` | $30 | Hard cap per trade |
| `live_max_usdc` | $10 | Safety cap in live mode |
| `kelly_fraction` | 0.25 | Quarter-Kelly sizing |

Size is also scaled by entry price tier:
- **$0.55–0.70** → 0.5× (higher risk, lower reward)
- **$0.70–0.85** → 1.0× (standard)
- **$0.85–0.95** → 1.3× (high confidence)

### Risk management
| Parameter | Default | Description |
|---|---|---|
| `kill_switch_drawdown_pct` | 15% | Hard stop for the day |
| `max_daily_loss_pct` | 10% | Pause after 10% daily loss |
| `max_daily_trades` | 100 | Cap for data collection |
| `max_concurrent_positions` | 4 | Max simultaneous open positions |

## Data

Trades are stored in `hybrid_trades.db` (SQLite). Logs are written to `hybrid.log`.

## Markets Supported

BTC and ETH 5-minute Polymarket prediction markets (configurable in `config.py` via `assets` and `durations`). 15-minute markets can be enabled by uncommenting the `durations` line in config.

## Risk Disclaimer

This bot trades real money in live mode. Prediction markets are inherently risky. Past paper performance does not guarantee live results. Use a dedicated wallet with only funds you can afford to lose.

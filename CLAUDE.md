# Claude Working Instructions — Oracle-Confirmed Sniper

## Anti-Hallucination Protocol
- **Read Before Action**: NEVER suggest a change to a file you have not read in the current session.
- **Strict Verification**: Check `requirements.txt` before assuming a library version supports a feature.
- **Reference Code**: When explaining logic, quote a snippet from the actual source file. No placeholder code.
- **Web Search**: Use `web_search` for any library released or updated after 2024 (Polymarket APIs, py-clob-client-v2, web3.py).

## Python Standards
- **Environment**: Always assume we are in a virtual environment at `venv/`. Run `pip list` to verify installed packages.
- **Linter**: Run `ruff check .` and `ruff format .` after every edit.
- **Type hints**: Required on all functions and class fields. Never suggest code without `typing` annotations.
- **Pathlib**: Use `pathlib.Path` for file operations. Never hardcode OS paths.
- **Style**: PEP 8 strictly.

---

# Project: Oracle-Confirmed Sniper

## What This Bot Does

Trades Polymarket 5-minute and 15-minute BTC/ETH/SOL prediction markets by reading the Chainlink oracle price feed seconds before market resolution. When the oracle has moved significantly from a window's opening price, the outcome is largely determined — but YES tokens may still be priced below $1.00. The bot buys that mispriced token as a taker (instant fill at best_ask).

**Only trades UP direction** — DOWN oracle signals are anti-predictive (WR=11.1% historically).

**Structural edge**: Chainlink settles Polymarket CTF markets, so its current value IS the resolution answer, readable in real-time via RTDS WebSocket.

## Architecture at a Glance

| File | Role |
|------|------|
| `bot.py` | Main async event loop |
| `core/config.py` | All parameters — `CFG` singleton |
| `core/models.py` | Data classes: `Token`, `OracleState`, `Signal`, `Trade` |
| `core/database.py` | SQLite persistence (`hybrid_trades.db`) |
| `core/redeem.py` | On-chain CTF redemption via web3 |
| `feeds/prices.py` | `PriceFeeds`: Chainlink RTDS + Binance WebSocket |
| `feeds/markets.py` | `MarketDiscovery`: Gamma API polling every 30s |
| `engine/signal.py` | `HybridEngine`: 7-gate signal evaluation |
| `engine/risk.py` | `RiskManager`: kill switch, daily cap, concurrent limit |
| `execution/executor.py` | Trade execution — paper and live |
| `ui/dashboard.py` | Rich terminal UI |
| `setup.py` | Derive API creds from private key → write `.env` |
| `wrap_pusd.py` | One-time: convert USDC.e → pUSD via Collateral Onramp |
| `approve_usdc.py` | On-chain pUSD approve() for V2 CLOB contracts |
| `withdraw.py` | Interactive pUSD withdrawal to any Polygon address |
| `redeem_now.py` | Manual CTF redemption with position list + confirmation |
| `analysis/analyze.py` | Trade analysis with `--watch` mode |

## Signal Gate Order (all must pass)

1. UTC hour NOT in blackout set `{0, 2, 6, 7, 17}`
2. Oracle direction == UP
3. Time remaining within snipe window (T-75s / T-55s / T-40s by delta tier)
4. Chainlink delta ≥ `min_delta_pct` (0.012%) from window opening
5. Binance agrees with Chainlink direction
6. Token YES price in `$0.55–$0.67` (above $0.67 → negative EV at taker fee)
7. Confidence score ≥ threshold AND edge ≥ 6%

## Breakeven Math (hardcoded context)

At avg entry $0.61, taker fee 1.5%:
- Win: `+$2.68` per share-unit
- Loss: `−$4.20` (full stake)
- Breakeven WR: **62.2%**

Any live analysis showing WR < 62.2% means negative expectancy. Surface this immediately when reviewing trade data.

---

# Polymarket V2 Infrastructure (April 28, 2026)

Polymarket launched Exchange V2 on April 28, 2026. **This changed the collateral token from USDC.e to pUSD.** Any code or advice referencing USDC.e as the active collateral is outdated.

## Key Addresses (Polygon mainnet)

| Contract | Address |
|----------|---------|
| USDC.e (legacy, read-only) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| pUSD (active collateral) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| Collateral Onramp | `0x93070a847efEf7F70739046A929D47a521F5B8ee` |
| CTF Exchange V2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| NegRisk CTF Exchange V2 | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| USDC Transfer Helper V2 | `0xe2222d279d744050d28e00520010520000310F59` |

## CLOB Client Library

- **Package**: `py-clob-client-v2` (replaces `py-clob-client`)
- **Import module name**: `py_clob_client` (unchanged — no import changes needed in bot code)
- **Install**: `pip install py-clob-client-v2`

## Known RPC Quirk — Stale State

Public Polygon RPCs (especially publicnode) frequently return stale on-chain state immediately after a confirmed transaction. **A confirmed receipt with `status=1` is the source of truth — not the balance read immediately after.** Do not treat a post-tx balance of $0 as a failure if the receipt was successful.

When writing on-chain tx scripts:
- Use `gas_price = int(w3.eth.gas_price * 1.3)` (30% buffer) to avoid "replacement transaction underpriced"
- Use `w3.eth.get_transaction_count(wallet, "pending")` for nonce if a prior tx may still be pending
- Always wait for receipt with `w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)` before reporting success or failure

## CLOB API `get_balance_allowance()` Warning

`setup.py` calls `get_balance_allowance()` and may show `Allowance: $0.00 ❌`. This is a **cosmetic false alarm** — the CLOB backend's allowance view is unreliable. What matters is the on-chain `approve()` executed by `approve_usdc.py`. Do not attempt to fix this warning by changing setup.py logic.

---

# Current Live Performance Context

- **Live since**: April 18, 2026
- **Portfolio**: ~$59.93 pUSD
- **Observed WR**: 58.1% (25/43 trades as of Apr 29, 2026) — below breakeven of 62.2%
- **Expectancy**: −$0.20/trade (negative)
- **Known risk**: −$3 losses may indicate ghost-zone entries (TTL ≤ 15s slipping through `snipe_exit_sec=16` guard)

When reviewing live trade data, always compare WR against 62.2% breakeven first. If WR < 60% over 20+ trades, escalate — that is a regime change, not noise.

---

# Development Rules for This Project

## On-Chain Scripts (`wrap_pusd.py`, `approve_usdc.py`, `withdraw.py`, `redeem_now.py`)

- Always print the wallet address and token balance before any transaction
- Always print the tx hash immediately after `send_raw_transaction()`
- Always wait for receipt and check `receipt.status == 1` before marking success
- Never send a tx without asking for confirmation if the script is interactive
- Always try multiple RPCs from `POLYGON_RPCS` list before failing

## Bot Logic (`engine/`, `feeds/`, `execution/`)

- Do not change signal gate thresholds without a data-backed justification (`analyze.py` output)
- Do not add new gates without checking they don't reduce trade count below ~3/day
- `CFG` is the single source of truth for all parameters — never hardcode thresholds in signal or executor logic
- `PriceFeeds.best_price()` prefers Chainlink if fresh (<30s), falls back to Binance — do not bypass this

## Config Changes

Before changing any parameter in `core/config.py`, state:
1. What problem it solves
2. What the current value is and what it will become
3. What the expected effect on trade count and WR is

## Testing

- `pytest` for unit tests
- On-chain scripts: test with a small amount first if balance allows
- No mocking of web3 or CLOB client in integration paths — use real RPC/API calls

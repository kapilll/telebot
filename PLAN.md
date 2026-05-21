# Telegram Signal Trading Bot — Plan

## Context
User receives gold trading signals in a Telegram channel via their personal account. They need a bot that:
- Reads signals from the channel using their personal Telegram account (not a bot added to the channel)
- Parses signals using an LLM (Claude) since format is flexible/unknown
- Sends a confirmation message to the user via a Telegram bot with ✅/❌ inline buttons
- Auto-executes the trade if no response within 30 seconds
- Places trades on one or more MetaTrader 5 accounts simultaneously
- Sizes positions using a configurable % of account balance + the SL from the signal
- **Records every signal and trade outcome in a local database for performance tracking**
- **Analyzes historical Telegram messages to backfill past signals and build a complete track record**
- **Provides strategy analysis to help user learn and eventually understand the signal provider's approach**

---

## Architecture Overview

```
Telegram Channel
      │ (signal message, also historical scrape)
      ▼
[Telethon Listener]  ← user's personal Telegram account
      │
      ▼
[Signal Parser]  ← Claude Haiku (fast, cheap LLM)
      │  extracts: symbol, direction, entry, SL, TP1, TP2...
      │
      ├──▶ [Trade Journal DB]  ← SQLite: stores every signal + outcome
      │
      ▼
[Confirmation Bot]  ← separate Telegram bot token (user creates via BotFather)
      │  sends inline keyboard to user's Telegram
      │  waits 30 seconds
      │  if no response → auto-execute
      ▼
[Trade Executor]
      │  places trade on ALL configured MT5 accounts
      │  reports fill prices back to Trade Journal DB
      ├──▶ MT5 Account 1 (FundingPips #1)
      └──▶ MT5 Account 2 (FundingPips #2)

[History Scraper]  ← one-time / on-demand tool
      │  fetches all past messages from the channel via Telethon
      └──▶ runs each through Signal Parser → populates Trade Journal DB

[Analytics Engine]  ← on-demand reports
      │  queries Trade Journal DB
      └──▶ win rate, avg R:R, drawdown, best/worst hours, strategy patterns
```

---

## Project Structure

```
tradebot/
├── main.py                  # entry point, wires everything together
├── config.yaml              # all user settings (editable, never committed with secrets)
├── .env                     # secrets: API keys, tokens, passwords
├── requirements.txt
│
├── telegram/
│   ├── listener.py          # Telethon user-client: watches signal channel
│   ├── confirmation_bot.py  # python-telegram-bot: sends confirm/skip buttons
│   └── history_scraper.py   # one-time scrape of all past channel messages
│
├── parser/
│   └── signal_parser.py     # Claude API call to parse raw message text
│
├── trading/
│   ├── account_manager.py   # loads MT5 accounts from config, manages connections
│   ├── executor.py          # places/closes trades on a single MT5 account
│   └── position_sizer.py    # calculates lot size from risk % + SL distance
│
├── journal/
│   ├── database.py          # SQLite schema + read/write helpers
│   ├── trade_recorder.py    # writes signals + outcomes to DB
│   └── analytics.py         # win rate, R:R, drawdown, pattern analysis reports
│
└── utils/
    └── logger.py            # structured logging to console + file
```

---

## Component Details

### 1. Config (`config.yaml`) — Single source of truth, nothing hardcoded
Every tunable value lives here. Code reads from config at startup; changing a value and restarting the bot is all that's needed.

```yaml
trading:
  risk_percent: 1.0          # % of account balance to risk per trade
  max_lot_size: 5.0          # safety cap — never exceed this lot size
  min_lot_size: 0.01         # broker minimum
  lot_step: 0.01             # rounding step for lot size calculation
  slippage: 10               # max slippage in points for market orders
  magic_number: 20240101     # unique ID to tag bot-placed orders

confirmation:
  timeout_seconds: 30        # auto-execute after this if no response
  # change to "skip" to make timeout skip instead of execute

signal_channel: "ChannelNameOrID"   # Telegram channel username or numeric ID

accounts:
  - name: "FundingPips #1"
    login: 123456
    server: "FundingPips-Live"
    path: "C:/MT5_Account1/terminal64.exe"   # path to this MT5 terminal
    enabled: true
  - name: "FundingPips #2"
    login: 789012
    server: "FundingPips-Live"
    path: "C:/MT5_Account2/terminal64.exe"
    enabled: true

logging:
  level: "INFO"              # DEBUG for verbose output
  log_to_file: true
  log_file: "tradebot.log"
```

`.env` holds only secrets (never committed to git):
```
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_USER_ID=...         # your Telegram user ID (for confirmation messages)
ANTHROPIC_API_KEY=...
MT5_PASSWORD_ACCOUNT1=...
MT5_PASSWORD_ACCOUNT2=...
```

### 2. Telegram Listener (`telegram/listener.py`)
- Uses **Telethon** (user-account client, not bot API)
- Connects with user's `api_id` + `api_hash` from my.telegram.org
- Session stored locally so only needs login once
- Watches the configured channel for new messages
- On new message → passes raw text to signal parser

### 3. Signal Parser (`parser/signal_parser.py`)
- Calls **Claude claude-haiku-4-5** (fast + cheap) via Anthropic SDK
- System prompt instructs it to extract structured fields from any signal format:
  - `symbol` (XAUUSD / GOLD)
  - `direction` (BUY / SELL)
  - `entry` (price or "market")
  - `stop_loss`
  - `take_profits` (list, e.g. [TP1, TP2, TP3])
- Returns a typed Python dataclass `ParsedSignal`
- If message is not a trade signal → returns `None` (ignored)
- Uses prompt caching on the system prompt for speed/cost

### 4. Confirmation Bot (`telegram/confirmation_bot.py`)
- Separate **Telegram bot** created by user via BotFather (simple, 1 minute setup)
- When a valid signal is parsed:
  1. Sends a formatted message to the user's personal Telegram with inline buttons:
     ```
     🟡 NEW SIGNAL — XAUUSD BUY
     Entry: 2345.00 | SL: 2330.00 | TP: 2370.00
     Risk: 1% → 0.05 lots (est.)
     
     ⏱ Auto-executes in 30s
     [✅ Execute Now]  [❌ Skip]
     ```
  2. Starts a 30-second countdown asyncio task
  3. If button pressed → cancels countdown, executes or skips
  4. If countdown expires → auto-executes

### 5. Position Sizer (`trading/position_sizer.py`)
- Formula: `lot_size = (balance × risk_pct) / (sl_distance_pips × pip_value_per_lot)`
- For XAUUSD: pip value = $1 per 0.01 lot per pip ($10/lot/10pip standard)
- Rounds to nearest 0.01 lot, respects broker min/max lot limits
- Calculated per-account (each account may have different balance)

### 6. Trade Executor (`trading/executor.py`)
- Uses **MetaTrader5** Python library (`pip install MetaTrader5`)
- Supports: market orders, limit orders, multiple TPs (partial positions)
- For multiple TPs: opens N equal sub-positions, each with its own TP
- Places trades on ALL accounts in parallel using `asyncio` + thread pool
- Sets SL and TP on order placement
- Logs every order result (ticket, fill price, etc.)

### 7. Account Manager (`trading/account_manager.py`)
- Initializes MT5 connections for each configured account
- MT5 only allows one active account at a time per MT5 terminal instance
- **Solution**: requires one MT5 terminal per account (standard prop firm setup)
  - Or use multiple MT5 installs / portable versions
- Health-checks connections before each trade

### 8. Trade Journal Database (`journal/database.py`)
- **SQLite** — zero-setup, single file, works locally and on VPS
- Tables:
  - `signals` — every parsed signal: timestamp, raw_text, symbol, direction, entry, sl, tp list, source_message_id
  - `trades` — every executed trade: signal_id, account, lot_size, fill_price, status (open/closed/skipped)
  - `outcomes` — closed trade results: exit_price, pnl_pips, pnl_usd, exit_reason (TP1/TP2/SL/manual), duration
- Automatically updated by `trade_recorder.py` at each lifecycle event (signal received → trade opened → trade closed)

### 9. History Scraper (`telegram/history_scraper.py`)
- Run once (or on demand) via CLI: `python -m tradebot.scrape`
- Uses Telethon to fetch **all historical messages** from the signal channel
- Passes each through `signal_parser.py`
- Saves parsed signals to `signals` table with `source: "historical"` flag
- Skips duplicates (idempotent — safe to re-run)
- For historical outcomes: if entry/SL/TP are known, can simulate outcome against XAUUSD historical price data (optional enhancement)

### 10. Analytics Engine (`journal/analytics.py`)
- Run on demand: `python -m tradebot.analytics`
- Queries the SQLite DB and prints/exports reports:
  - **Win rate**: % of signals that hit TP vs SL
  - **Average R:R**: actual reward vs risk per trade
  - **Drawdown**: max consecutive losses, max equity dip
  - **Time-of-day analysis**: which hours/sessions produce best signals
  - **Pattern detection**: Claude Sonnet analyzes all signals to identify recurring setups (e.g., "signals after NY open tend to be longs at round numbers")
  - **Provider track record**: overall PnL if you had traded every signal at stated lots
- Output: console report + optional CSV export for Excel/Google Sheets

---

## Setup Requirements (One-Time)

1. **Telegram API credentials**: Go to my.telegram.org → create app → get `api_id` and `api_hash`
2. **Telegram bot**: Message @BotFather → `/newbot` → get bot token; start a chat with the bot
3. **Anthropic API key**: For Claude signal parsing
4. **MT5 terminals**: One per account, each logged in and running
5. **Python 3.10+** on Windows (MT5 library is Windows-only)

### VPS consideration
If hosting on a VPS: must be a **Windows VPS** (MT5 is Windows-only). A Linux VPS would require Wine + MT5, which is complex. A Windows VPS ($15–20/month) is the clean path.

---

## Key Dependencies
```
telethon          # personal Telegram account listener
python-telegram-bot>=20  # async bot for confirmations
anthropic         # Claude API for signal parsing + analytics
MetaTrader5       # MT5 trade execution (Windows only)
pyyaml            # config loading
python-dotenv     # .env secrets
aiosqlite         # async SQLite for trade journal
pandas            # analytics data processing
tabulate          # pretty-print analytics tables in terminal
```

---

## Build Order (Implementation Phases)

1. **Phase 1 — Foundation**: `config.yaml`, `.env`, `logger.py`, `requirements.txt`
2. **Phase 2 — Signal parsing**: `signal_parser.py` with Claude API + test with sample messages
3. **Phase 3 — Trade Journal**: `database.py`, `trade_recorder.py` — SQLite schema, write helpers
4. **Phase 4 — MT5 trading**: `position_sizer.py`, `executor.py`, `account_manager.py` + test with demo account
5. **Phase 5 — Telegram listener**: `listener.py` with Telethon, connect to channel, test message capture
6. **Phase 6 — History scraper**: `history_scraper.py` — scrape all past signals into DB
7. **Phase 7 — Confirmation bot**: `confirmation_bot.py` with inline buttons + 30s timeout logic
8. **Phase 8 — Integration**: `main.py` wiring all components, end-to-end test
9. **Phase 9 — Analytics**: `analytics.py` reports — win rate, R:R, time analysis, Claude pattern detection
10. **Phase 10 — Hardening**: error handling, reconnection logic, graceful shutdown

---

## Verification / Testing Plan
- **Parser**: Feed 10 different signal formats, verify Claude extracts correct fields
- **Position sizer**: Unit test with known balance + SL → verify lot size math
- **MT5 executor**: Test on a MT5 demo account first before live
- **Confirmation flow**: Send a dummy signal, verify Telegram message arrives, test both button-press and timeout paths
- **History scraper**: Scrape channel history, verify signals are parsed and stored correctly in DB
- **Analytics**: Run reports against seeded DB data, verify win rate / R:R math
- **End-to-end**: Full flow from fake signal message → MT5 demo trade placed → outcome recorded → analytics updated

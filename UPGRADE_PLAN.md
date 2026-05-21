# Full Autonomous AI Trading System — Upgrade Plan

## Context

The existing bot can parse signals, ask for manual confirmation, and execute basic trades. The user wants to:
- Remove all manual steps — everything must be fully automatic
- Add a second Telegram channel to monitor (`@Pips_Profit_67`)
- Add a Claude Sonnet AI brain that evaluates every signal, manages risk dynamically, and monitors open trades
- Handle TP1/TP2/TP3 with automatic SL progression (BE after TP1, TP1 after TP2, etc.)
- Scale lot sizes dynamically based on account P&L state and risk budget remaining
- Monitor all open trades every 10 seconds so Claude can modify or close them

Account context: FundingPips $5k account, 6% daily loss limit, currently at 4.5% loss → ~$75 of daily budget remaining.

---

## Architecture Overview (what changes)

```
Channel 1 (-1002760637066)  Channel 2 (@Pips_Profit_67)
              │                        │
              └──────────┬─────────────┘
                         ▼
               [Multi-Channel Listener]   ← instant detection via Telethon
                         │
                         ▼
               [Signal Parser]            ← Claude Haiku (unchanged)
                         │
                         ▼
               [Risk Engine]              ← reads MT5 live: balance, equity, daily P&L
                         │
                         ▼
               [AI Trade Intelligence]    ← Claude Sonnet: analyze signal + indicators
                         │  decides: TRADE / SKIP + lot size + reasoning
                         │
                         ▼
               [Multi-TP Executor]        ← splits position into N sub-lots per TP
                         │
                    ┌────┴────┐
                    ▼         ▼
              Account 1   Account 2
                    │
                    ▼
               [Notifier]                 ← Telegram notification (info only, no buttons)
                    │
                    ▼
         [Trade Monitor — 10s loop]       ← polls open positions
                    │
             every 60s: Claude Sonnet reassesses
                    │
             issues: HOLD / MODIFY_SL / CLOSE_PARTIAL / CLOSE_ALL
```

---

## Files to Create

### `trading/risk_engine.py`
Reads live MT5 account state and calculates risk budget.

```python
class RiskEngine:
    def get_account_state(account) -> dict:
        # Connects to MT5, reads account_info() and positions_get()
        # Returns:
        {
            "balance": 5000,
            "equity": 4775,           # live including open P&L
            "daily_pnl": -225,        # today's P&L (negative = loss)
            "daily_loss_pct": 4.5,    # % lost today
            "daily_budget_remaining": 75,   # USD left before limit hit
            "drawdown_pct": 4.5,
            "open_positions": [...],
            "lot_multiplier": 0.3,    # calculated: conservative when near limit
        }

    def calculate_lot_multiplier(state) -> float:
        # 0.0 → SKIP (limit hit)
        # 0.3 → near limit (>80% consumed)
        # 0.5 → in loss but room remaining
        # 1.0 → normal
        # 1.5 → in profit >2% today
```

Key config values to read:
- `risk.daily_loss_limit_pct: 6.0`
- `risk.initial_balance: 5000`
- `risk.max_drawdown_pct: 10.0`

### `trading/indicators.py`
Calculates ADX and ATR from MT5 OHLCV data (no external lib needed).

```python
def get_indicators(symbol, timeframe="H1") -> dict:
    # calls mt5.copy_rates_from_pos() to get last 50 bars
    # calculates ADX(14), ATR(14) in pure Python/numpy
    return {
        "adx": 28.4,         # trend strength — <20 = ranging
        "atr": 8.5,          # avg true range in price units
        "trend": "UP",       # direction of ADX trend
        "session": "LONDON", # current session based on UTC time
    }
```

### `trading/ai_trader.py`
Claude Sonnet brain — called once per signal, once per monitor cycle.

```python
class AITrader:
    def evaluate_signal(signal, risk_state, indicators) -> TradeDecision:
        # Calls claude-sonnet-4-6 with extended thinking
        # Provides: signal details, risk state, ADX/ATR, session, recent trade history
        # Returns:
        TradeDecision(
            action="TRADE",       # or "SKIP" or "WAIT"
            lot_size=0.02,
            reasoning="ADX 28 confirms trend, budget $75, risking $20 (27%)",
            confidence=0.88,
        )

    def reassess_trade(position, current_price, risk_state) -> TradeAction:
        # Called every 60s during monitor loop
        # Returns: HOLD / MODIFY_SL(new_sl) / CLOSE_PARTIAL / CLOSE_ALL
```

System prompt includes: signal channel track record stats from DB, account rules, current risk state, technical context.

### `trading/trade_monitor.py`
Asyncio loop, runs every 10 seconds.

```python
class TradeMonitor:
    async def run():
        while True:
            await asyncio.sleep(10)
            positions = get_open_bot_positions()  # by magic number
            for pos in positions:
                check_tp_hits(pos)         # move SL automatically
            if time_for_claude_reassess:   # every 60s
                await claude_reassess(positions)

    def check_tp_hits(pos, signal_record):
        # Compares current price to TP levels stored in DB
        # If TP1 crossed and SL not yet moved to BE:
        #   → modify_position_sl(tickets_above_tp1, entry_price)
        # If TP2 crossed and SL not yet at TP1:
        #   → modify_position_sl(tickets_above_tp2, tp1_price)
```

### `tg/notifier.py`
Replaces `confirmation_bot.py` — notification only, no buttons.

```python
class Notifier:
    async def send_trade_executed(signal, lot_size, accounts, reasoning)
    async def send_trade_skipped(signal, reason)
    async def send_sl_moved(symbol, new_sl, reason)
    async def send_trade_closed(symbol, pnl_usd, reason)
    async def send_risk_alert(message)
```

---

## Files to Modify

### `main.py`
- Remove `ConfirmationBot` entirely
- Add `RiskEngine`, `AITrader`, `TradeMonitor`, `Notifier`
- Change `handle_new_message()`:
  1. Parse signal (Haiku)
  2. Get risk state (RiskEngine)
  3. If risk state allows → get indicators → call AITrader
  4. If AITrader says TRADE → execute
  5. Send notification
  6. Register open tickets in TradeMonitor
- Start `TradeMonitor` as a background task

### `tg/listener.py`
Add support for multiple channels:
```python
# Replace single channel string with list
signal_channels = ["-1002760637066", "@Pips_Profit_67"]

# Register handler on all channels simultaneously
@client.on(NewMessage(chats=channel_entities))
async def message_handler(event):
    ...
```

### `trading/executor.py`
Add:
- `modify_position_sl(ticket, new_sl)` — uses `TRADE_ACTION_SLTP`
- `close_position(ticket, lot_size)` — market close for partial or full
- Multi-TP split logic moved from account_manager: given N TPs, open N equal sub-orders each with one TP

### `trading/account_manager.py`
Add:
- `get_open_positions(magic)` — returns all positions tagged with bot's magic number
- `get_account_state()` — returns balance, equity, daily P&L (history query)
- `modify_sl_for_tickets(tickets, new_sl)` — bulk SL modification
- `close_positions(tickets)` — bulk close

### `trading/position_sizer.py`
Add dynamic multiplier input:
```python
def calculate_lot_size(..., lot_multiplier: float = 1.0) -> float:
    # Apply multiplier after base calculation, before clamping
    lot_size = base_lot_size * lot_multiplier
```

### `journal/database.py`
Add columns / new table:

```sql
-- add to signals table:
ALTER TABLE signals ADD COLUMN channel TEXT;  -- which channel it came from
ALTER TABLE signals ADD COLUMN ai_reasoning TEXT;
ALTER TABLE signals ADD COLUMN ai_action TEXT;  -- TRADE/SKIP/WAIT

-- new table: active_positions (for monitor to track TP states)
CREATE TABLE active_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    ticket INTEGER,           -- MT5 ticket
    account_name TEXT,
    tp_level INTEGER,         -- 1, 2, 3, 4 (which TP this sub-lot targets)
    entry_price REAL,
    sl_current REAL,
    tp_price REAL,
    sl_state TEXT DEFAULT 'original',  -- original / breakeven / tp1 / tp2
    status TEXT DEFAULT 'open'         -- open / closed
);
```

### `config.yaml`
```yaml
signal_channels:
  - "-1002760637066"          # VIP PREMIUM SIGNAL SERVICE
  - "@Pips_Profit_67"         # Pips Profit

risk:
  daily_loss_limit_pct: 6.0
  max_drawdown_pct: 10.0
  initial_balance: 5000       # starting account balance for drawdown calc
  max_risk_per_trade_pct: 30  # % of remaining daily budget per trade

ai:
  model: "claude-sonnet-4-6"
  monitor_interval_seconds: 10
  reassess_interval_seconds: 60

trading:
  dry_run: true               # keep true until MT5 tested
  magic_number: 20240101
  ...
```

---

## Dynamic Lot Sizing Logic

```
Base calculation (unchanged):
  lot = (balance × risk_pct%) / (sl_distance × 100)

Risk budget calculation:
  daily_budget_remaining = initial_balance × daily_loss_limit_pct% - |daily_loss_so_far|
  max_risk_this_trade = daily_budget_remaining × max_risk_per_trade_pct%
  risk_usd = min(balance × risk_pct%, max_risk_this_trade)

Lot multiplier (from risk state):
  - daily_loss_pct > 5.5% → multiplier = 0.0  (SKIP, too close to limit)
  - daily_loss_pct > 4.0% → multiplier = 0.3  (very conservative)
  - daily_loss_pct > 2.0% → multiplier = 0.6
  - daily_pnl == 0         → multiplier = 1.0  (normal)
  - daily_profit_pct > 1%  → multiplier = 1.2
  - daily_profit_pct > 2%  → multiplier = 1.5  (in profit, scale up)

AI confidence modifier:
  lot = lot × ai_confidence  (0.7 confidence → 70% of calculated lot)
```

---

## TP / SL Progression

**Core rule: follow the signal's SL strictly. Do NOT move SL prematurely. Premature SL moves cause small losses on trades that would have been full winners.**

Signal with 4 TPs → 4 equal sub-lots placed simultaneously:
```
Lot 1: SL=original, TP=TP1    → closes at TP1 naturally. NO SL change yet — let trade breathe.
Lot 2: SL=original, TP=TP2    → closes at TP2. NOW move SL of lots 3,4 to breakeven (entry).
Lot 3: SL=original→BE, TP=TP3 → closes at TP3. Move SL of lot 4 to TP1.
Lot 4: SL→TP1, TP=TP4         → run to final target.
```

**SL movement rules:**
- SL moves ONLY when a TP level is hit — never based on time, momentum, or Claude's "feeling"
- TP1 hit → partial profit booked, original SL stays on remaining lots (no SL change)
- TP2 hit → move remaining SL to breakeven (entry price) — first protection point
- TP3 hit → move remaining SL to TP1 — locks real profit
- Claude's reassess loop may only recommend CLOSE_ALL (e.g. news risk, reversal signal from provider) — never arbitrary SL tightening

**What Claude's monitor loop is allowed to do:**
- HOLD — do nothing (default)
- CLOSE_ALL — emergency exit only (strong counter-signal, news event, provider reversal message)
- MODIFY_SL — only to implement the TP-triggered progression above, not based on price action feel

Monitor detects TP hits by comparing current MT5 position list against DB `active_positions`. When a ticket disappears from MT5 positions, it closed — check if TP or SL.

---

## Claude Sonnet AI Prompt (signal evaluation)

Context Claude receives:
- Signal: symbol, direction, entry range, SL, TP list, source channel
- Risk state: balance, equity, daily P&L, daily budget remaining, lot multiplier
- Indicators: ADX, ATR, current price, session (London/NY/Asia)
- History: win rate last 10 signals, current open positions count
- Rules: daily limit %, drawdown limit %, initial balance

**Hard constraint baked into system prompt:** "You must follow the signal provider's SL and TP levels exactly as given. You are NOT allowed to move the SL or tighten it based on price action or momentum. The only SL modifications allowed are the TP-triggered progressions defined in the system rules (BE after TP2, TP1 after TP3). Your job at signal evaluation is: TRADE or SKIP and what lot size. Your job during monitoring is: HOLD or CLOSE_ALL (emergency only)."

Claude signal evaluation outputs JSON:
```json
{
  "action": "TRADE",
  "lot_size": 0.02,
  "reasoning": "ADX 28 confirms trend strength, NY session opening, budget $55 remaining, risking $18 (33% of budget). High confidence.",
  "confidence": 0.88,
  "risk_flags": []
}
```

Claude monitor reassessment outputs JSON:
```json
{
  "action": "HOLD",
  "reasoning": "Trade progressing normally toward TP2. No counter-signals. Hold."
}
```
or:
```json
{
  "action": "CLOSE_ALL",
  "reasoning": "Provider posted reversal signal on same pair. Closing to avoid full SL."
}
```

---

## Build Order

1. **Phase A** — Config + DB schema updates (add `active_positions` table, new config keys)
2. **Phase B** — `tg/notifier.py` + `tg/listener.py` multi-channel support
3. **Phase C** — `trading/risk_engine.py` (MT5 account state reader)
4. **Phase D** — `trading/indicators.py` (ADX, ATR)
5. **Phase E** — `trading/ai_trader.py` (Claude Sonnet layer)
6. **Phase F** — `trading/executor.py` additions (modify_sl, close, multi-TP split)
7. **Phase G** — `trading/account_manager.py` additions (positions, bulk modify)
8. **Phase H** — `trading/trade_monitor.py` (10s loop + SL progression)
9. **Phase I** — `main.py` rewire (remove confirmation, add AI + monitor)
10. **Phase J** — Integration test in dry_run mode

---

## Verification

1. `! python test_parser.py` — verify signal parser still works on both channel formats
2. Start bot with `dry_run: true`, send a test message to the group → verify:
   - Signal detected instantly
   - Risk engine reads account state
   - Claude Sonnet produces a TRADE/SKIP decision with reasoning
   - Telegram notification received (no buttons)
   - `active_positions` DB table populated (dry_run = simulated)
3. Simulate TP1 hit by manually updating DB → verify monitor moves SL to BE in dry_run
4. Check daily limit enforcement: set `initial_balance` low so limit is already hit → verify SKIP
5. Once dry_run verified → set MT5 passwords correctly, `dry_run: false`, test on demo

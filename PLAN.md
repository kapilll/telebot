# Final Plan: Fully Autonomous AI Gold Signal Trading Bot

## Context

The existing bot can parse signals and place trades, but has 9 critical bugs and is not ready for live money. The upgrade: full autonomy, second signal channel, Haiku parsing every channel message (handles human typos gracefully), automatic execution of **all** signals (they are pre-vetted high-confidence signals — we always take them), smart entry timing, real-time follow-up message handling, and Claude Haiku monitoring open trades every 30 seconds.

Account: FundingPips $5k, 6% daily loss limit.

---

## Core Philosophy (drives every design decision)

1. **All signals are taken.** The provider sends very few, high-quality signals. We don't second-guess them. The only reason to skip is hitting the daily risk budget.
2. **Follow-up messages are gold.** Provider posts TP updates, SL moves, "close now" reversals. React to these immediately.
3. **Haiku parses everything.** Channel messages are written by humans — typos, varied formats, mixed language. Haiku handles this cheaply.
4. **Monitor open trades every 30 seconds with Claude.** Between signal arrivals, the bot's main job is managing what's already open.
5. **Entry timing matters.** When a signal gives a range (e.g., "2344–2350"), enter at the better end via limit order for tighter SL distance and better R:R.

---

## Architecture

```
Channel 1 (-1002760637066)     Channel 2 (@Pips_Profit_67)
              │                          │
              └────────────┬─────────────┘
                           ▼
              [Multi-Channel Listener]     Telethon — every message
                           │
                           ▼
              [Message Parser — Claude Haiku]
                  handles typos, varied formats, human language
                           │
              ┌────────────┼────────────────┐
              │            │                │
         NEW_SIGNAL   FOLLOW_UP           NOISE
              │       (tp_hit /             │
              │        close_all /         skip
              │        modify_sl /
              │        extend_tp)
              │            │
              │            ▼
              │    [Follow-Up Handler]
              │      → modify SL / close positions / record TP
              │      → Notifier: "TP2 hit → SL moved to BE"
              │
              ▼
        Risk Engine check
          lot_multiplier = 0.0 → SKIP + alert (only skip condition)
              │
              ▼
        Entry Optimizer
          range entry → place LIMIT order at better end
          single entry → MARKET order immediately
              │
              ▼
        [Multi-TP Executor]      N sub-lots per TP level
              │         ┌─────────────┐
              │         ▼             ▼
           Account 1  Account 2   (sequential)
              │
              ▼
        [Notifier]              info-only, no buttons
              │
              ▼
        [Trade Monitor — every 10s mechanical + 30s Claude Haiku]
              │
          10s: MT5 poll — detect TP/SL hits, apply SL progression
          30s: Claude Haiku — review all open positions, recommend
               HOLD or CLOSE_ALL (emergency only)
```

---

## Part 1 — Bug Fixes (Must Do First)

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `main.py:83` | `account.get('index',1)` always 1 — account 2 password never loaded | `enumerate(accounts)` → `f"MT5_PASSWORD_ACCOUNT{i+1}"` |
| 2 | `account_manager.py:89,150` | `account["mt5_path"]` KeyError — config uses `"path"` | `account.get("path", account.get("mt5_path", ""))` |
| 3 | `trade_recorder.py:25` | `take_profits` list stored via `str()` — breaks readback | `json.dumps(signal.take_profits)` |
| 4 | `position_sizer.py` | `entry or sl` → `sl-sl=0` → ZeroDivisionError on market orders | Guard: `if sl_distance == 0: raise ValueError` |
| 5 | `executor.py:_check_connection` | `mt5.initialize()` without `mt5.login()` — no account after reconnect | Store creds at `connect()`; re-login in `_check_connection` |
| 6 | `account_manager.py:execute_on_all` | Parallel thread execution — MT5 not thread-safe | Sequential: `for account: await run_in_executor(...)` |
| 7 | `analytics.py` (all methods) | SQL columns don't match schema; wrong JOIN; `"trades.db"` → `"tradebot.db"` | Fix all queries (detail in analytics section) |
| 8 | `history_scraper.py` | `parse()` / `record_signal()` without `await`; `ParsedSignal` treated as dict; wrong imports | Add `await`; pass dataclass; fix import paths |

`confirmation_bot.py` bugs are moot — file is replaced by `notifier.py`.

---

## Part 2 — Message Parser (Extended)

### `parser/signal_parser.py` — Unified Haiku classifier

Every channel message — new signal, follow-up, or noise — goes through one Haiku call. The model handles typos, abbreviations, and varied human language naturally.

**Extended system prompt:**

```
You are a trading message classifier for a gold signal Telegram channel.
Messages are written by humans and may contain typos, mixed language, or informal phrasing.

Classify every message as one of three types:

1. NEW_SIGNAL — a new trade with entry, SL, and at least one TP.
   Return:
   {"type": "new_signal", "symbol": "XAUUSD", "direction": "BUY",
    "entry": 2345.0,        ← if range "2344-2348" use average 2346; also store entry_range
    "entry_high": 2348.0,   ← upper end of range (null if single price)
    "entry_low": 2344.0,    ← lower end of range (null if single price)
    "stop_loss": 2330.0,
    "take_profits": [2360.0, 2375.0, 2390.0],
    "confidence": 0.92}

2. FOLLOW_UP — an update about an existing open trade.
   Variants:
   {"type": "follow_up", "action": "tp_hit", "tp_level": 1}
   {"type": "follow_up", "action": "close_all", "reason": "reversal"}
   {"type": "follow_up", "action": "modify_sl", "new_sl": 2341.0}
   {"type": "follow_up", "action": "modify_sl", "new_sl": "breakeven"}
   {"type": "follow_up", "action": "extend_tp", "new_tp": 2405.0}

3. NOISE — greetings, commentary, pip updates without new signals.
   {"type": "noise"}

Typo handling examples:
  "TP 1 hitted" → follow_up tp_hit tp_level=1
  "clse all" → follow_up close_all
  "gld by 2344" with SL and TP → new_signal XAUUSD BUY
  "GOLD SELLS" without SL → noise (incomplete)
  "mve sl to entry" → follow_up modify_sl breakeven
```

New `ParsedMessage` dataclass:

```python
@dataclass
class ParsedMessage:
    type: str                     # "new_signal" | "follow_up" | "noise"
    signal: Optional[ParsedSignal] = None
    # follow_up fields:
    follow_up_action: Optional[str] = None  # "tp_hit"|"close_all"|"modify_sl"|"extend_tp"
    tp_level: Optional[int] = None
    new_sl: Optional[Union[float, str]] = None   # float or "breakeven"
    new_tp: Optional[float] = None
    reason: Optional[str] = None

@dataclass
class ParsedSignal:
    symbol: str
    direction: str
    entry: float          # midpoint if range; current ask/bid if "market"
    entry_low: Optional[float]   # lower end of range (for limit entry optimization)
    entry_high: Optional[float]  # upper end of range
    stop_loss: float
    take_profits: list[float]
    raw_text: str
    confidence: float
```

Keep `cache_control: ephemeral` on system prompt.

---

## Part 3 — Entry Optimizer (New Logic in `main.py`)

When a signal has an entry range, entering at the better price gives:
- Smaller SL distance → better R:R ratio
- Or same SL distance → smaller lot for same risk (more budget preserved)

**Logic:**

```python
def determine_entry_order_type(signal, current_price):
    if signal.entry_low is None:
        # Single price or "market" — market order now
        return "MARKET", signal.entry

    # Range signal — try to get the better end
    if signal.direction == "BUY":
        # Better entry = lower price (closer to SL means less risk per pip, but
        # actually for BUY, lower entry = smaller SL distance = better)
        # If current price is already AT or BELOW the low end → market order
        if current_price <= signal.entry_low * 1.0005:  # within 0.05% of low
            return "MARKET", current_price
        elif current_price <= signal.entry_high:
            # Price is in range — limit at low end for better fill
            return "LIMIT", signal.entry_low
        else:
            # Price hasn't entered range yet — limit at entry_high (range top)
            return "LIMIT", signal.entry_high
    else:  # SELL
        # Better entry = higher price
        if current_price >= signal.entry_high * 0.9995:
            return "MARKET", current_price
        elif current_price >= signal.entry_low:
            return "LIMIT", signal.entry_high
        else:
            return "LIMIT", signal.entry_low
```

For limit orders: place and wait up to 15 minutes. If not filled → cancel (signal stale).

---

## Part 4 — New Files

### `trading/risk_engine.py`

Reads live MT5 state. Provides `lot_multiplier` used by position sizer.

```python
@dataclass
class RiskState:
    balance: float
    equity: float
    daily_pnl: float              # USD, negative = loss
    daily_loss_pct: float
    daily_budget_remaining: float
    open_position_count: int
    lot_multiplier: float         # 0.0 = skip, 0.3–1.5 based on P&L state

class RiskEngine:
    def get_state(self, account) -> RiskState:
        # mt5.account_info() → balance, equity
        # mt5.history_deals_get(midnight_utc, now) → sum deal profits

    def lot_multiplier(self, state) -> float:
        # The chances system in position_sizer handles scaling naturally.
        # This method only provides the hard stop at the daily limit edge.
        if state.daily_loss_pct >= 5.5: return 0.0   # 0.5% buffer before 6% limit
        return 1.0
```

### `trading/trade_monitor.py`

Two loops: 10-second mechanical MT5 check, and 30-second Claude Haiku review.

```python
class TradeMonitor:
    async def run(self):
        mechanical_counter = 0
        ai_counter = 0
        while True:
            await asyncio.sleep(10)
            mechanical_counter += 1

            # Every 10s: mechanical MT5 check
            await self._check_mt5_closures()

            # Every 30s: Claude Haiku reviews open trades
            if mechanical_counter % 3 == 0:
                await self._claude_review_open_trades()

    async def _check_mt5_closures(self):
        """
        Compare DB active_positions (status=open) against live MT5 positions.
        Any ticket in DB but gone from MT5 = position closed.
        Determine if TP or SL from exit price.
        Apply TP-triggered SL progression on remaining lots.
        Record outcome in DB.
        Notify user.
        """
        db_open = await self.db.get_active_positions(status='open')
        mt5_tickets = {p.ticket for p in self.executor.get_open_positions(magic=MAGIC)}
        for pos in db_open:
            if pos.ticket not in mt5_tickets:
                await self._handle_closure(pos)

    async def _claude_review_open_trades(self):
        """
        Every 30s: send open positions snapshot to Claude Haiku.
        Haiku outputs HOLD or CLOSE_ALL (emergency only).
        Cost: ~$0.001 per call. Keeps the bot aware of drift.
        """
        open_positions = await self.db.get_active_positions(status='open')
        if not open_positions:
            return

        risk_state = self.risk_engine.get_state(primary_account)
        snapshot = self._format_positions_for_claude(open_positions, risk_state)

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=[{"type": "text", "text": MONITOR_SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": snapshot}]
        )
        action = json.loads(response.content[0].text)

        if action["action"] == "CLOSE_ALL":
            await self.account_manager.close_all_bot_positions()
            await self.notifier.send_emergency_close(action["reasoning"])
            await self.trade_recorder.record_emergency_close(open_positions, action["reasoning"])
```

**Monitor system prompt (cached):**
```
You are monitoring open gold trades on a FundingPips prop firm account.
Daily loss limit: 6%. You see the current open positions.

Output ONLY JSON:
{"action": "HOLD", "reasoning": "Trade progressing normally."}
OR
{"action": "CLOSE_ALL", "reasoning": "Specific emergency reason."}

CLOSE_ALL only for genuine emergencies:
- Daily loss limit is about to be breached by these positions' unrealized loss
- You see a message from the provider reversing this signal
- A clear technical catastrophe (e.g., flash crash, SL blown through)
Default is always HOLD. Never close based on "feeling" or price drift.
```

### `trading/follow_up_handler.py`

Acts on all follow-up message types. This is the most safety-critical path.

```python
class FollowUpHandler:
    async def handle(self, msg: ParsedMessage, symbol: str):
        open_positions = await self.db.get_active_positions(symbol=symbol, status='open')
        if not open_positions:
            return   # no open positions — follow-up is informational only

        if msg.follow_up_action == "tp_hit":
            await self._on_tp_hit(msg.tp_level, open_positions)

        elif msg.follow_up_action == "close_all":
            await self.account_manager.close_all_bot_positions(symbol=symbol)
            await self.trade_recorder.record_provider_close(open_positions, msg.reason)
            await self.notifier.send_provider_close(symbol, msg.reason)

        elif msg.follow_up_action == "modify_sl":
            new_sl = open_positions[0].entry_price if msg.new_sl == "breakeven" else msg.new_sl
            remaining = [p for p in open_positions if p.status == 'open']
            for pos in remaining:
                await self.account_manager.modify_sl_for_tickets([pos.ticket], new_sl, pos.account_name)
            await self.db.update_sl_state_bulk([p.ticket for p in remaining], new_sl, 'provider')
            await self.notifier.send_sl_modified(symbol, new_sl, "provider instruction")

        elif msg.follow_up_action == "extend_tp":
            await self.db.extend_tp_for_symbol(symbol, msg.new_tp)
            await self.notifier.send_tp_extended(symbol, msg.new_tp)

    async def _on_tp_hit(self, tp_level, open_positions):
        # TP-triggered SL rules (immutable):
        # TP1 hit → NO SL change (let remaining lots breathe)
        # TP2 hit → move remaining SL to entry (breakeven)
        # TP3 hit → move remaining SL to TP1 price
        remaining = [p for p in open_positions if p.tp_level > tp_level]
        sl_moved_to = None
        if tp_level == 2 and remaining:
            sl_moved_to = remaining[0].entry_price
        elif tp_level >= 3 and remaining:
            tp1 = next((p.tp_price for p in open_positions if p.tp_level == 1), None)
            sl_moved_to = tp1
        if sl_moved_to:
            for pos in remaining:
                await self.account_manager.modify_sl_for_tickets(
                    [pos.ticket], sl_moved_to, pos.account_name
                )
        pnl = await self.trade_recorder.record_tp_hit(tp_level, open_positions)
        await self.notifier.send_tp_hit(open_positions[0].symbol, tp_level, pnl, sl_moved_to)
```

### `tg/notifier.py`

Pure Telegram notification. No buttons. Replaces `confirmation_bot.py`.

```python
class Notifier:
    async def send_trade_executed(self, signal, fill_results, tp_split, order_type): ...
    async def send_trade_skipped(self, signal, reason): ...   # only: daily limit hit
    async def send_tp_hit(self, symbol, tp_level, pnl_usd, sl_moved_to): ...
    async def send_sl_hit(self, symbol, pnl_usd): ...
    async def send_sl_modified(self, symbol, new_sl, reason): ...
    async def send_provider_close(self, symbol, reason): ...
    async def send_tp_extended(self, symbol, new_tp): ...
    async def send_risk_alert(self, message): ...
    async def send_emergency_close(self, reasoning): ...
    async def send_limit_order_cancelled(self, symbol, reason): ...  # stale limit
```

---

## Part 5 — Modified Files

### `main.py` — Full rewire

Remove: `ConfirmationBot`, manual flow, `signal_in_progress`.

Add: `RiskEngine`, `FollowUpHandler`, `TradeMonitor`, `Notifier`.

New `handle_new_message()`:

```python
async def handle_new_message(self, text, message_id, channel_id):
    msg = await self.signal_parser.parse_message(text)

    if msg.type == "noise":
        return

    if msg.type == "follow_up":
        # Most critical path — react to provider instruction immediately
        await self.follow_up_handler.handle(msg, symbol="XAUUSD")
        return

    # New signal — always trade (provider pre-vetted)
    signal = msg.signal
    risk_state = self.risk_engine.get_state(self.config["accounts"][0])

    if risk_state.lot_multiplier == 0.0:
        await self.notifier.send_risk_alert("⛔ Daily loss limit reached — signal skipped.")
        return

    signal_id = await self.trade_recorder.record_signal(
        signal, source='live', source_message_id=message_id, channel=str(channel_id)
    )

    # Entry optimization: range → limit at better end; single → market
    tick = mt5.symbol_info_tick(signal.symbol)
    current_price = tick.ask if signal.direction == "BUY" else tick.bid
    order_type, entry_price = determine_entry_order_type(signal, current_price)

    results = await self.account_manager.execute_multi_tp(
        signal, entry_price, order_type,
        risk_state.lot_multiplier,
        self.config["trading"],
        self.config["tp_split"]
    )

    await self.db.register_active_positions(signal_id, results)
    await self.notifier.send_trade_executed(signal, results, self.config["tp_split"], order_type)

    # If limit order, start a cancellation watchdog (15 min timeout)
    if order_type == "LIMIT":
        asyncio.create_task(self._cancel_unfilled_limits(results, timeout_minutes=15))
```

Fix password enumeration (bug #1):
```python
for i, account in enumerate(self.config.get("accounts", [])):
    env_var = f"MT5_PASSWORD_ACCOUNT{i + 1}"
```

Start `TradeMonitor` as background task in `start()`.

### `tg/listener.py` — Multi-channel

```python
channel_entities = []
for ch in self.config["signal_channels"]:
    try:
        entity = await client.get_entity(int(ch) if ch.lstrip("-").isdigit() else ch)
        channel_entities.append(entity)
    except Exception as e:
        logger.error(f"Could not resolve channel {ch}: {e}")

@client.on(NewMessage(chats=channel_entities))
async def message_handler(event):
    await on_message_callback(event.message.text, event.message.id, event.chat_id)
```

### `trading/executor.py` — Add modify + close + fix reconnect

```python
def connect(self, login, password, server):
    self._login, self._password, self._server = login, password, server
    ...

def _check_connection(self):
    if not mt5.initialize(path=self.mt5_path):
        raise RuntimeError("MT5 init failed")
    info = mt5.account_info()
    if info is None or info.login != self._login:
        if not mt5.login(self._login, self._password, self._server):
            raise RuntimeError(f"MT5 re-login failed: {mt5.last_error()}")

def place_limit_order(self, symbol, direction, lot_size, entry, sl, tp, magic, slippage) -> Optional[dict]:
    # ORDER_TYPE_BUY_LIMIT / ORDER_TYPE_SELL_LIMIT via TRADE_ACTION_PENDING

def modify_position_sl(self, ticket: int, new_sl: float) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos: return False
    p = pos[0]
    result = mt5.order_send({
        "action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol,
        "position": ticket, "sl": new_sl, "tp": p.tp,
    })
    return result.retcode == mt5.TRADE_RETCODE_DONE

def close_position(self, ticket: int) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos: return False
    p = pos[0]
    order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(p.symbol)
    price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
    result = mt5.order_send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
        "volume": p.volume, "type": order_type, "position": ticket,
        "price": price, "deviation": 20, "magic": p.magic,
    })
    return result.retcode == mt5.TRADE_RETCODE_DONE

def cancel_pending_order(self, ticket: int) -> bool:
    result = mt5.order_send({
        "action": mt5.TRADE_ACTION_REMOVE, "order": ticket,
    })
    return result.retcode == mt5.TRADE_RETCODE_DONE

def get_open_positions(self, magic: int = None) -> list:
    positions = mt5.positions_get() or []
    return [p for p in positions if magic is None or p.magic == magic]

def get_pending_orders(self, magic: int = None) -> list:
    orders = mt5.orders_get() or []
    return [o for o in orders if magic is None or o.magic == magic]

def get_deals_since(self, from_dt: datetime) -> list:
    return list(mt5.history_deals_get(from_dt, datetime.utcnow()) or [])
```

### `trading/account_manager.py` — Multi-TP + serial + path fix

```python
async def execute_multi_tp(self, signal, entry_price, order_type, lot_multiplier, trading_cfg, tp_split_cfg):
    tps = signal.take_profits
    n = len(tps)
    weights = tp_split_cfg["weights"].get(f"{n}_tp", [1.0/n]*n)

    # Calculate base lot: sized to leave min_remaining_chances more SL hits in daily budget
    base_lot = calculate_lot_size(
        balance=risk_state.balance,
        daily_loss_limit_pct=risk_cfg["daily_loss_limit_pct"],
        daily_pnl=risk_state.daily_pnl,
        entry=entry_price, stop_loss=signal.stop_loss,
        min_lot=trading_cfg["min_lot_size"],
        max_lot=trading_cfg["max_lot_size"],   # 0.05
        lot_step=trading_cfg["lot_step"],
        min_remaining_chances=risk_cfg["min_remaining_chances"],
    )

    results = []
    for account in self.accounts_config:
        if not account.get("enabled"): continue
        mt5_path = account.get("path", account.get("mt5_path", ""))
        executor = TradeExecutor(mt5_path)
        login = account["login"]
        if not executor.connect(login, self.passwords.get(login), account["server"]):
            results.append({"login": login, "success": False, "error": "connection failed"})
            continue
        try:
            orders = []
            active_tps = adjust_lots_for_tp_split(
                base_lot, weights,
                trading_cfg["min_lot_size"], trading_cfg["lot_step"]
            )
            for tp_idx, sub_lot in active_tps:
                tp = tps[tp_idx]
                if order_type == "MARKET":
                    order = executor.place_market_order(
                        signal.symbol, signal.direction, sub_lot,
                        signal.stop_loss, tp,
                        trading_cfg["magic_number"], trading_cfg["slippage"]
                    )
                else:
                    order = executor.place_limit_order(
                        signal.symbol, signal.direction, sub_lot,
                        entry_price, signal.stop_loss, tp,
                        trading_cfg["magic_number"], trading_cfg["slippage"]
                    )
                if order:
                    orders.append({"tp_level": i+1, "ticket": order["ticket"],
                                   "fill_price": order.get("fill_price", entry_price),
                                   "lot": sub_lot, "tp_price": tp})
            results.append({"login": login, "account_name": account["name"],
                           "success": True, "orders": orders})
        finally:
            executor.disconnect()
    return results

def close_all_bot_positions(self, symbol=None):
    """Close all open positions with bot's magic number, optionally filtered by symbol."""

def modify_sl_for_tickets(self, tickets, new_sl, account_name):
    """Modify SL on specific tickets for a named account."""

def get_account_balance(self, account_name):
    mt5_path = account.get("path", account.get("mt5_path", ""))  # fix key
    ...
```

### `trading/position_sizer.py`

**Core idea:** Always preserve `min_remaining_chances` more SL hits within the day's remaining budget. This guarantees that even with consecutive losses the account survives to the next signal. As the day becomes profitable the remaining budget grows, naturally allowing larger lots.

```python
def calculate_lot_size(
    balance: float,
    daily_loss_limit_pct: float,   # e.g. 6.0
    daily_pnl: float,              # positive=profit, negative=loss so far today
    entry: float,
    stop_loss: float,
    min_lot: float,
    max_lot: float,                # hard cap — default 0.05
    lot_step: float,
    min_remaining_chances: int = 3,  # configurable
) -> float:
    sl_distance = abs(entry - stop_loss)
    if sl_distance == 0:
        raise ValueError(f"SL distance is zero: entry={entry} stop_loss={stop_loss}")

    # How many dollars can still be lost before hitting the daily limit
    # daily_pnl positive (profit) → more room; negative (loss) → less room
    daily_budget_total = balance * daily_loss_limit_pct / 100
    daily_budget_remaining = daily_budget_total + daily_pnl

    if daily_budget_remaining <= 0:
        return 0.0  # safety: should have been blocked by risk engine already

    # Risk per trade = fair share of remaining budget over desired chances
    # More profit today → larger budget → larger lots automatically
    risk_per_trade = daily_budget_remaining / min_remaining_chances

    # XAUUSD: 1.0 lot = $100 per 1-unit price move
    lot_size = risk_per_trade / (sl_distance * 100)
    lot_size = round(lot_size / lot_step) * lot_step
    return max(min_lot, min(lot_size, max_lot))


def adjust_lots_for_tp_split(base_lot, weights, min_lot, lot_step):
    """
    Split base_lot into sub-lots per TP level.
    If a sub-lot would be below min_lot, drop the lowest-weight TP levels
    until all sub-lots are >= min_lot (avoids invalid tiny orders).
    Returns list of (tp_index, sub_lot) pairs.
    """
    active = list(enumerate(weights))
    while active:
        sub_lots = [
            (i, max(min_lot, round(base_lot * w / lot_step) * lot_step))
            for i, w in active
        ]
        if all(sl >= min_lot for _, sl in sub_lots):
            return sub_lots
        # Drop the lowest-weight TP
        active = active[:-1]
    return [(0, min_lot)]  # fallback: single lot at TP1
```

**Example with a fresh $5 000 account (6% limit, 15-point SL, 3 chances):**
| Day's P&L | Budget remaining | Risk/trade | Lot calc | Final lot |
|-----------|-----------------|------------|----------|-----------|
| $0 (start)| $300 | $100 | 0.067 | **0.05** (capped) |
| −$100 (1 loss) | $200 | $67 | 0.045 | 0.04 |
| −$200 (2 losses) | $100 | $33 | 0.022 | 0.02 |
| +$200 (profitable) | $500 | $167 | 0.111 | **0.05** (capped) |

After 2 consecutive SL hits there is still 1 chance at 0.02 lot ($30 max loss) within budget — the daily limit is protected.

### `journal/database.py`

```sql
ALTER TABLE signals ADD COLUMN channel TEXT;
ALTER TABLE signals ADD COLUMN ai_action TEXT;      -- reserved, currently unused
ALTER TABLE signals ADD COLUMN ai_reasoning TEXT;   -- reserved

CREATE TABLE IF NOT EXISTS active_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    ticket INTEGER UNIQUE,
    account_name TEXT,
    tp_level INTEGER,
    entry_price REAL,
    sl_original REAL,
    sl_current REAL,
    tp_price REAL,
    sl_state TEXT DEFAULT 'original',  -- original / breakeven / tp1 / tp2 / provider
    order_type TEXT DEFAULT 'market',  -- market / limit
    status TEXT DEFAULT 'open'
);
```

New methods: `register_active_positions`, `get_active_positions`, `update_active_position_sl`, `close_active_position`, `extend_tp_for_symbol`, `get_recent_signal_stats`, `get_daily_pnl_from_outcomes`.

### `journal/trade_recorder.py`

```python
import json

async def record_signal(self, signal, source='live', source_message_id=None,
                        channel=None) -> int:
    ...
    json.dumps(signal.take_profits),   # was str() — fix
    ...
```

### `journal/analytics.py` — Fix all SQL

```python
# Correct JOIN chain: signals → trades (signal_id) → outcomes (trade_id)

# win_rate():
SELECT s.symbol, s.direction, o.exit_reason, o.pnl_pips
FROM signals s JOIN trades t ON t.signal_id = s.id JOIN outcomes o ON o.trade_id = t.id
WHERE o.exit_reason IS NOT NULL
# exit_reason LIKE 'TP%' → win; = 'SL' → loss

# avg_rr(): no rr column — compute in Python
SELECT s.entry, s.stop_loss, s.direction, o.pnl_pips
FROM signals s JOIN trades t ON t.signal_id = s.id JOIN outcomes o ON o.trade_id = t.id

# max_drawdown(): t.closed_at (outcomes has no timestamp column)
# time_of_day(): s.timestamp (not o.created_at)
# pattern_analysis(): s.entry, s.stop_loss, s.take_profits, s.timestamp
# export_csv(): same column fixes
# main(): db_path = "tradebot.db"
```

### `tg/history_scraper.py`

```python
msg = await signal_parser.parse_message(message.text or "")   # await + unified method
if msg.type == "new_signal":
    await trade_recorder.record_signal(
        msg.signal, source='historical', source_message_id=message.id
    )

# main() imports:
from parser.signal_parser import SignalParser
from journal.trade_recorder import TradeRecorder
from journal.database import Database
```

### `config.yaml`

```yaml
trading:
  dry_run: true
  magic_number: 20240101
  slippage: 10
  min_lot_size: 0.01
  max_lot_size: 0.05               # hard cap — configurable; update as account grows
  lot_step: 0.01
  limit_order_timeout_minutes: 15   # cancel unfilled limits after this

signal_channels:
  - "-1002760637066"
  - "@Pips_Profit_67"

accounts:
  - name: "FundingPips #1"
    login: 12226465
    server: "FundingPips2-Live"     # CHANGE from SIM when going live
    path: "C:/MT5_Account1/terminal64.exe"
    enabled: true
  - name: "FundingPips #2"
    login: 11937135
    server: "FundingPips2-Live"
    path: "C:/MT5_Account2/terminal64.exe"
    enabled: true

risk:
  daily_loss_limit_pct: 6.0        # FundingPips rule — do not exceed
  min_remaining_chances: 3         # always keep budget for this many more SL hits
  initial_balance: 5000

ai:
  model_monitor: "claude-haiku-4-5-20251001"
  monitor_interval_seconds: 10      # MT5 mechanical check
  ai_review_interval_seconds: 30    # Claude Haiku open-trade review

tp_split:
  weights:
    1_tp: [1.0]
    2_tp: [0.50, 0.50]
    3_tp: [0.40, 0.35, 0.25]
    4_tp: [0.30, 0.25, 0.25, 0.20]

logging:
  level: "INFO"
  log_to_file: true
  log_file: "tradebot.log"
```

---

## TP / SL Progression Rules (immutable)

```
Signal with N TPs → N sub-lots placed simultaneously.

TP1 hit → partial profit booked. NO SL change on remaining lots.
          Let them breathe — the signal is working.

TP2 hit → move remaining SL to entry (breakeven).
          First protection: worst case now = breakeven.

TP3 hit → move remaining SL to TP1 price.
          Locked in real profit on the runner.

Provider posts "close" / "exit" / "cancel" → close ALL immediately.

Provider posts "move SL to X" → move all remaining SL to that price.

Claude Haiku (30s review) may ONLY: HOLD or CLOSE_ALL (emergency).
Claude may NOT move SL or tighten based on price action.
```

---

## Dynamic Lot Sizing

```
1. daily_budget_remaining = (balance × daily_loss_limit_pct/100) + daily_pnl_today
   ↑ negative daily_pnl shrinks this; positive daily_pnl grows it (bigger lots allowed)

2. risk_per_trade = daily_budget_remaining / min_remaining_chances
   ↑ e.g. $300 remaining / 3 chances = $100 max risk this trade

3. base_lot = risk_per_trade / (sl_distance × 100)     [XAUUSD: $100/lot/1-unit move]
   e.g. $100 / (15 × $100) = 0.067

4. base_lot = min(base_lot, max_lot_size)              [hard cap: 0.05]
   → 0.05

5. split by tp_split.weights, each sub_lot >= min_lot_size
   Drop lowest TP levels if a sub_lot would fall below min_lot (small-account safety)

6. Risk engine: if daily_loss_pct >= 5.5% → lot_multiplier=0.0 → skip signal entirely

Adjusting as account grows:
  Just update max_lot_size in config.yaml (discuss with Claude daily).
  The formula scales naturally — no other changes needed.
```

---

## Build Order

```
Phase A — Foundation (no deps)
  1. trade_recorder.py      json.dumps fix + source/channel params
  2. position_sizer.py      zero-div guard + lot_multiplier param
  3. executor.py            _check_connection re-login + new methods
                            (modify_position_sl, close_position,
                             cancel_pending_order, get_open_positions,
                             get_pending_orders, get_deals_since,
                             place_limit_order)

Phase B — Config + Schema
  4. config.yaml            updated structure
  5. database.py            active_positions table + signal columns + new methods

Phase C — Parsing (extends signal_parser.py)
  6. signal_parser.py       unified parse_message() → ParsedMessage
                            extended system prompt (handles typos, follow-ups)
                            ParsedSignal gains entry_low/entry_high fields

Phase D — New modules
  7. tg/notifier.py         pure Telegram notification
  8. trading/risk_engine.py MT5 account state + lot_multiplier
  9. trading/follow_up_handler.py  tp_hit/close_all/modify_sl/extend_tp
 10. trading/trade_monitor.py  10s MT5 loop + 30s Claude Haiku review

Phase E — Execution layer
 11. account_manager.py     path fix + serial + execute_multi_tp (market + limit)
                            + close_all_bot_positions + modify_sl_for_tickets
 12. main.py                full rewire + password fix + entry optimizer
                            + multi-channel callback + limit watchdog

Phase F — Listener
 13. tg/listener.py         multi-channel, passes channel_id to callback

Phase G — Analytics + Scraper
 14. analytics.py           all SQL fixed
 15. history_scraper.py     await + imports + ParsedMessage

Phase H — Integration test (dry_run=true)
 16. End-to-end on live channels
```

---

## Pre-Live Checklist

- [ ] Change both account `server:` to live server name (not `FundingPips2-SIM`)
- [ ] MT5 terminals running, logged in, XAUUSD showing live quotes
- [ ] `python test_parser.py` — signals parse correctly
- [ ] Run `dry_run: true` — send a test signal in the group, verify:
  - Message detected on both channels
  - Haiku classifies as new_signal, correct fields extracted
  - Telegram notification received
  - `signals` and `active_positions` tables populated (simulated)
  - Trade Monitor starts, 30s Claude Haiku review fires (check logs)
- [ ] Post a fake "TP2 hit" in your test channel → verify follow-up handler moves SL to entry in logs
- [ ] Post a fake "close all" → verify close logic triggered in logs
- [ ] Simulate daily limit: set `initial_balance: 100, daily_loss_limit_pct: 1` → verify skip alert
- [ ] Run `python -m journal.analytics` — no SQL errors
- [ ] Set `risk_percent: 0.5` for first live week
- [ ] Set `dry_run: false` → watch next real signal → confirm MT5 shows the sub-orders

## Key Safety Notes

- **Follow-up messages are the primary trade management mechanism** — the 30s Claude review is a safety net, not the main loop
- **Limit orders have a 15-minute timeout** — if the entry range is never touched within 15 minutes, orders are cancelled (signal stale)
- **`lot_multiplier = 0.0` is the hard stop** — never removed by anything except the clock ticking past midnight UTC
- **Prop firm rule**: add to Haiku monitor prompt — if daily equity drawdown is within 1% of the limit and there are open losing positions, recommend CLOSE_ALL
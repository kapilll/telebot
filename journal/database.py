import aiosqlite
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from utils.logger import get_logger


@dataclass
class ActivePosition:
    id: int
    signal_id: int
    ticket: int
    account_name: str
    tp_level: int
    entry_price: float
    sl_original: float
    sl_current: float
    tp_price: float
    sl_state: str
    order_type: str
    status: str
    lot_size: float = 0.0
    symbol: str = "XAUUSD"


class Database:
    def __init__(self, db_path: str = "tradebot.db"):
        self.db_path = Path(db_path)
        self.connection = None
        self.logger = get_logger("database")

    async def init(self):
        self.connection = await aiosqlite.connect(str(self.db_path))
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")

        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                raw_text TEXT,
                symbol TEXT,
                direction TEXT,
                entry REAL,
                stop_loss REAL,
                take_profits TEXT,
                source TEXT DEFAULT 'live',
                source_message_id INTEGER,
                confidence REAL,
                channel TEXT,
                ai_action TEXT,
                ai_reasoning TEXT
            )
        """)

        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER REFERENCES signals(id),
                account_name TEXT,
                lot_size REAL,
                fill_price REAL,
                ticket INTEGER,
                status TEXT DEFAULT 'open',
                opened_at TEXT,
                closed_at TEXT
            )
        """)

        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER REFERENCES trades(id),
                exit_price REAL,
                pnl_pips REAL,
                pnl_usd REAL,
                exit_reason TEXT,
                duration_seconds INTEGER
            )
        """)

        await self.connection.execute("""
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
                sl_state TEXT DEFAULT 'original',
                order_type TEXT DEFAULT 'market',
                status TEXT DEFAULT 'open',
                lot_size REAL DEFAULT 0.0,
                symbol TEXT DEFAULT 'XAUUSD'
            )
        """)

        # Migrate existing signals table if columns missing
        for col, coldef in [
            ("channel", "TEXT"),
            ("ai_action", "TEXT"),
            ("ai_reasoning", "TEXT"),
        ]:
            try:
                await self.connection.execute(f"ALTER TABLE signals ADD COLUMN {col} {coldef}")
            except Exception:
                pass  # column already exists

        await self.connection.commit()
        self.logger.info("Database initialized")

    async def close(self):
        if self.connection:
            await self.connection.close()

    # ── signals ──────────────────────────────────────────────────────────────

    async def insert_signal(self, **kwargs):
        try:
            cursor = await self.connection.execute(
                """INSERT INTO signals (timestamp, raw_text, symbol, direction, entry,
                   stop_loss, take_profits, source, source_message_id, confidence, channel)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(kwargs.get(k) for k in [
                    "timestamp", "raw_text", "symbol", "direction", "entry",
                    "stop_loss", "take_profits", "source", "source_message_id",
                    "confidence", "channel"
                ])
            )
            await self.connection.commit()
            return cursor.lastrowid
        except Exception as e:
            self.logger.error(f"insert_signal failed: {e}")
            return None

    async def get_recent_signal_stats(self, n: int = 10) -> dict:
        """Return win rate and count for the last n closed signals."""
        try:
            cursor = await self.connection.execute(
                """SELECT o.exit_reason FROM signals s
                   JOIN trades t ON t.signal_id = s.id
                   JOIN outcomes o ON o.trade_id = t.id
                   WHERE o.exit_reason IS NOT NULL
                   ORDER BY s.timestamp DESC LIMIT ?""",
                (n,)
            )
            rows = await cursor.fetchall()
            wins = sum(1 for r in rows if str(r[0]).startswith('TP'))
            total = len(rows)
            return {"total": total, "wins": wins,
                    "win_rate": wins / total if total else 0}
        except Exception:
            return {"total": 0, "wins": 0, "win_rate": 0}

    # ── trades ────────────────────────────────────────────────────────────────

    async def insert_trade(self, **kwargs):
        try:
            cursor = await self.connection.execute(
                """INSERT INTO trades (signal_id, account_name, lot_size, fill_price,
                   ticket, status, opened_at, closed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(kwargs.get(k) for k in [
                    "signal_id", "account_name", "lot_size", "fill_price",
                    "ticket", "status", "opened_at", "closed_at"
                ])
            )
            await self.connection.commit()
            return cursor.lastrowid
        except Exception as e:
            self.logger.error(f"insert_trade failed: {e}")
            return None

    async def insert_outcome(self, **kwargs):
        try:
            cursor = await self.connection.execute(
                """INSERT INTO outcomes (trade_id, exit_price, pnl_pips, pnl_usd,
                   exit_reason, duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                tuple(kwargs.get(k) for k in [
                    "trade_id", "exit_price", "pnl_pips", "pnl_usd",
                    "exit_reason", "duration_seconds"
                ])
            )
            await self.connection.commit()
            return cursor.lastrowid
        except Exception as e:
            self.logger.error(f"insert_outcome failed: {e}")
            return None

    # ── active_positions ──────────────────────────────────────────────────────

    async def register_active_positions(self, signal_id: int, execute_results: list):
        """Insert active_position rows from multi-account execute results."""
        for account_result in execute_results:
            if not account_result.get("success"):
                continue
            account_name = account_result.get("account_name", "")
            for order in account_result.get("orders", []):
                try:
                    await self.connection.execute(
                        """INSERT OR IGNORE INTO active_positions
                           (signal_id, ticket, account_name, tp_level, entry_price,
                            sl_original, sl_current, tp_price, sl_state, order_type,
                            status, lot_size, symbol)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'original', ?, 'open', ?, ?)""",
                        (
                            signal_id,
                            order["ticket"],
                            account_name,
                            order["tp_level"],
                            order.get("fill_price", order.get("entry_price", 0)),
                            order.get("sl", 0),
                            order.get("sl", 0),
                            order["tp_price"],
                            order.get("order_type", "market"),
                            order.get("lot", 0),
                            order.get("symbol", "XAUUSD"),
                        )
                    )
                except Exception as e:
                    self.logger.error(f"register_active_position error: {e}")
        await self.connection.commit()

    async def get_active_positions(self, status: str = 'open',
                                   symbol: str = None) -> list:
        try:
            if symbol:
                cursor = await self.connection.execute(
                    "SELECT * FROM active_positions WHERE status=? AND symbol=?",
                    (status, symbol)
                )
            else:
                cursor = await self.connection.execute(
                    "SELECT * FROM active_positions WHERE status=?", (status,)
                )
            rows = await cursor.fetchall()
            return [_row_to_active_position(r) for r in rows]
        except Exception as e:
            self.logger.error(f"get_active_positions error: {e}")
            return []

    async def close_active_position(self, ticket: int, exit_reason: str = ''):
        try:
            await self.connection.execute(
                "UPDATE active_positions SET status='closed' WHERE ticket=?", (ticket,)
            )
            await self.connection.commit()
        except Exception as e:
            self.logger.error(f"close_active_position error: {e}")

    async def update_active_position_sl(self, ticket: int, new_sl: float, sl_state: str):
        try:
            await self.connection.execute(
                "UPDATE active_positions SET sl_current=?, sl_state=? WHERE ticket=?",
                (new_sl, sl_state, ticket)
            )
            await self.connection.commit()
        except Exception as e:
            self.logger.error(f"update_active_position_sl error: {e}")

    async def update_sl_state_bulk(self, tickets: list, new_sl: float, sl_state: str):
        for ticket in tickets:
            await self.update_active_position_sl(ticket, new_sl, sl_state)

    async def extend_tp_for_symbol(self, symbol: str, new_tp: float):
        """Extend the highest TP level for open positions on a symbol."""
        try:
            positions = await self.get_active_positions(symbol=symbol)
            if not positions:
                return
            max_level = max(p.tp_level for p in positions)
            await self.connection.execute(
                """UPDATE active_positions SET tp_price=?
                   WHERE symbol=? AND status='open' AND tp_level=?""",
                (new_tp, symbol, max_level)
            )
            await self.connection.commit()
        except Exception as e:
            self.logger.error(f"extend_tp_for_symbol error: {e}")

    async def get_daily_pnl(self) -> float:
        """Sum of pnl_usd from outcomes closed today (UTC)."""
        try:
            today = datetime.utcnow().date().isoformat()
            cursor = await self.connection.execute(
                """SELECT COALESCE(SUM(o.pnl_usd), 0)
                   FROM outcomes o
                   JOIN trades t ON t.id = o.trade_id
                   WHERE t.closed_at >= ?""",
                (today,)
            )
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0
        except Exception:
            return 0.0


def _row_to_active_position(row) -> ActivePosition:
    d = dict(row)
    return ActivePosition(
        id=d.get("id", 0),
        signal_id=d.get("signal_id", 0),
        ticket=d.get("ticket", 0),
        account_name=d.get("account_name", ""),
        tp_level=d.get("tp_level", 1),
        entry_price=d.get("entry_price", 0.0),
        sl_original=d.get("sl_original", 0.0),
        sl_current=d.get("sl_current", 0.0),
        tp_price=d.get("tp_price", 0.0),
        sl_state=d.get("sl_state", "original"),
        order_type=d.get("order_type", "market"),
        status=d.get("status", "open"),
        lot_size=d.get("lot_size", 0.0),
        symbol=d.get("symbol", "XAUUSD"),
    )

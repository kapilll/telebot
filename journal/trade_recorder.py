import json
from typing import Optional
from datetime import datetime
from journal.database import Database
from utils.logger import get_logger

logger = get_logger("trade_recorder")


class TradeRecorder:
    def __init__(self, db: Database):
        self.db = db

    async def record_signal(self, signal, source: str = 'live',
                            source_message_id: int = None, channel: str = None) -> int:
        cursor = await self.db.connection.execute(
            """
            INSERT INTO signals
            (timestamp, raw_text, symbol, direction, entry, stop_loss, take_profits,
             source, source_message_id, confidence, channel)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                getattr(signal, 'raw_text', None),
                getattr(signal, 'symbol', None),
                getattr(signal, 'direction', None),
                getattr(signal, 'entry', None),
                getattr(signal, 'stop_loss', None),
                json.dumps(getattr(signal, 'take_profits', [])),
                source,
                source_message_id,
                getattr(signal, 'confidence', None),
                channel,
            ),
        )
        await self.db.connection.commit()
        logger.debug(f"Signal recorded id={cursor.lastrowid}")
        return cursor.lastrowid

    async def record_trade(self, signal_id: int, account_name: str,
                           lot_size: float, fill_price: float, ticket: int) -> int:
        cursor = await self.db.connection.execute(
            """
            INSERT INTO trades
            (signal_id, account_name, lot_size, fill_price, ticket, status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (signal_id, account_name, lot_size, fill_price, ticket,
             'open', datetime.utcnow().isoformat()),
        )
        await self.db.connection.commit()
        return cursor.lastrowid

    async def record_tp_hit(self, tp_level: int, open_positions: list) -> float:
        """Mark positions at this TP level as closed, return estimated PnL USD."""
        pnl = 0.0
        for pos in open_positions:
            if getattr(pos, 'tp_level', None) == tp_level:
                await self.db.close_active_position(pos.ticket, exit_reason=f'TP{tp_level}')
                sl_dist = abs(pos.entry_price - pos.sl_original)
                tp_dist = abs(pos.tp_price - pos.entry_price)
                pnl += tp_dist * 100 * pos.lot_size if hasattr(pos, 'lot_size') else 0
        return pnl

    async def record_provider_close(self, open_positions: list, reason: str):
        for pos in open_positions:
            await self.db.close_active_position(pos.ticket, exit_reason=f'provider:{reason}')

    async def record_emergency_close(self, open_positions: list, reasoning: str):
        for pos in open_positions:
            await self.db.close_active_position(pos.ticket, exit_reason=f'emergency:{reasoning[:50]}')

    async def update_trade_closed(self, trade_id: int, exit_price: float,
                                  pnl_pips: float, pnl_usd: float,
                                  exit_reason: str, duration_seconds: int):
        await self.db.connection.execute(
            "UPDATE trades SET status=?, closed_at=? WHERE id=?",
            ('closed', datetime.utcnow().isoformat(), trade_id),
        )
        await self.db.connection.execute(
            """INSERT INTO outcomes (trade_id, exit_price, pnl_pips, pnl_usd, exit_reason, duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (trade_id, exit_price, pnl_pips, pnl_usd, exit_reason, duration_seconds),
        )
        await self.db.connection.commit()

    async def skip_signal(self, signal_id: int):
        await self.db.connection.execute(
            "UPDATE trades SET status=? WHERE signal_id=?", ('skipped', signal_id)
        )
        await self.db.connection.commit()

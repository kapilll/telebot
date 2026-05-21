from typing import Optional
from datetime import datetime
from parser.signal_parser import ParsedSignal
from journal.database import Database


class TradeRecorder:
    def __init__(self, db: Database):
        self.db = db

    async def record_signal(self, signal: ParsedSignal) -> int:
        """Insert a signal into the signals table and return the id."""
        cursor = await self.db.connection.execute(
            """
            INSERT INTO signals
            (timestamp, raw_text, symbol, direction, entry, stop_loss, take_profits, source, source_message_id, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                signal.raw_text if hasattr(signal, 'raw_text') else None,
                signal.symbol if hasattr(signal, 'symbol') else None,
                signal.direction if hasattr(signal, 'direction') else None,
                signal.entry if hasattr(signal, 'entry') else None,
                signal.stop_loss if hasattr(signal, 'stop_loss') else None,
                signal.take_profits if hasattr(signal, 'take_profits') else None,
                'live',
                signal.source_message_id if hasattr(signal, 'source_message_id') else None,
                signal.confidence if hasattr(signal, 'confidence') else None,
            ),
        )
        await self.db.connection.commit()
        return cursor.lastrowid

    async def record_trade(
        self,
        signal_id: int,
        account_name: str,
        lot_size: float,
        fill_price: float,
        ticket: int,
    ) -> int:
        """Insert a trade into the trades table and return the id."""
        cursor = await self.db.connection.execute(
            """
            INSERT INTO trades
            (signal_id, account_name, lot_size, fill_price, ticket, status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                account_name,
                lot_size,
                fill_price,
                ticket,
                'open',
                datetime.utcnow().isoformat(),
            ),
        )
        await self.db.connection.commit()
        return cursor.lastrowid

    async def update_trade_closed(
        self,
        trade_id: int,
        exit_price: float,
        pnl_pips: float,
        pnl_usd: float,
        exit_reason: str,
        duration_seconds: int,
    ):
        """Update a trade as closed and insert the outcome."""
        await self.db.connection.execute(
            """
            UPDATE trades
            SET status = ?, closed_at = ?
            WHERE id = ?
            """,
            ('closed', datetime.utcnow().isoformat(), trade_id),
        )

        await self.db.connection.execute(
            """
            INSERT INTO outcomes
            (trade_id, exit_price, pnl_pips, pnl_usd, exit_reason, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trade_id, exit_price, pnl_pips, pnl_usd, exit_reason, duration_seconds),
        )

        await self.db.connection.commit()

    async def skip_signal(self, signal_id: int):
        """Mark all trades for a signal as 'skipped'."""
        await self.db.connection.execute(
            """
            UPDATE trades
            SET status = ?
            WHERE signal_id = ?
            """,
            ('skipped', signal_id),
        )
        await self.db.connection.commit()

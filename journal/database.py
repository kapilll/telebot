import aiosqlite
from pathlib import Path
from utils.logger import get_logger


class Database:
    def __init__(self, db_path: str = "tradebot.db"):
        self.db_path = Path(db_path)
        self.connection = None
        self.logger = get_logger("database")

    async def init(self):
        """Initialize database and create tables if they don't exist."""
        try:
            self.connection = await aiosqlite.connect(str(self.db_path))
            self.logger.info(f"Database connection opened: {self.db_path}")

            # Enable WAL mode for better concurrent access
            try:
                await self.connection.execute("PRAGMA journal_mode=WAL")
                self.logger.debug("WAL mode enabled for database")
            except Exception as e:
                self.logger.warning(f"Failed to enable WAL mode: {e}")

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
                    confidence REAL
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

            await self.connection.commit()
            self.logger.info("Database tables initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize database: {e}", exc_info=True)
            raise

    async def close(self):
        """Close the database connection."""
        try:
            if self.connection:
                await self.connection.close()
                self.logger.info("Database connection closed")
        except Exception as e:
            self.logger.error(f"Error closing database connection: {e}", exc_info=True)

    async def insert_signal(self, **kwargs):
        """
        Insert a signal record with error handling.

        Returns:
            Row ID on success, None on failure
        """
        try:
            cursor = await self.connection.execute(
                """INSERT INTO signals (timestamp, raw_text, symbol, direction, entry,
                   stop_loss, take_profits, source, source_message_id, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(kwargs.get(k) for k in ["timestamp", "raw_text", "symbol", "direction",
                                               "entry", "stop_loss", "take_profits", "source",
                                               "source_message_id", "confidence"])
            )
            await self.connection.commit()
            self.logger.debug(f"Signal inserted with ID {cursor.lastrowid}")
            return cursor.lastrowid
        except Exception as e:
            self.logger.error(f"Failed to insert signal: {e}", exc_info=True)
            return None

    async def insert_trade(self, **kwargs):
        """
        Insert a trade record with error handling.

        Returns:
            Row ID on success, None on failure
        """
        try:
            cursor = await self.connection.execute(
                """INSERT INTO trades (signal_id, account_name, lot_size, fill_price,
                   ticket, status, opened_at, closed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(kwargs.get(k) for k in ["signal_id", "account_name", "lot_size",
                                               "fill_price", "ticket", "status",
                                               "opened_at", "closed_at"])
            )
            await self.connection.commit()
            self.logger.debug(f"Trade inserted with ID {cursor.lastrowid}")
            return cursor.lastrowid
        except Exception as e:
            self.logger.error(f"Failed to insert trade: {e}", exc_info=True)
            return None

    async def insert_outcome(self, **kwargs):
        """
        Insert an outcome record with error handling.

        Returns:
            Row ID on success, None on failure
        """
        try:
            cursor = await self.connection.execute(
                """INSERT INTO outcomes (trade_id, exit_price, pnl_pips, pnl_usd,
                   exit_reason, duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                tuple(kwargs.get(k) for k in ["trade_id", "exit_price", "pnl_pips",
                                               "pnl_usd", "exit_reason", "duration_seconds"])
            )
            await self.connection.commit()
            self.logger.debug(f"Outcome inserted with ID {cursor.lastrowid}")
            return cursor.lastrowid
        except Exception as e:
            self.logger.error(f"Failed to insert outcome: {e}", exc_info=True)
            return None

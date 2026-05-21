import asyncio
import os
import sys
from typing import Optional, Dict
from datetime import datetime

import yaml
from dotenv import load_dotenv

from tg.listener import ChannelListener
from tg.confirmation_bot import ConfirmationBot
from parser.signal_parser import SignalParser, ParsedSignal
from trading.position_sizer import calculate_lot_size
from trading.account_manager import AccountManager
from journal.database import Database
from journal.trade_recorder import TradeRecorder
from utils.logger import get_logger

logger = get_logger("main")


class TradingBot:
    """Main orchestrator for the Telegram gold signal trading bot."""

    def __init__(self):
        self.db: Optional[Database] = None
        self.trade_recorder: Optional[TradeRecorder] = None
        self.signal_parser: Optional[SignalParser] = None
        self.account_manager: Optional[AccountManager] = None
        self.confirmation_bot: Optional[ConfirmationBot] = None
        self.channel_listener: Optional[ChannelListener] = None
        self.config: Dict = {}
        self.signal_in_progress: Dict[int, dict] = {}

    async def initialize(self):
        """Initialize all components."""
        try:
            logger.info("Initializing TradingBot...")

            # Load environment variables
            load_dotenv()

            # Load configuration
            with open("config.yaml", "r") as f:
                self.config = yaml.safe_load(f)
            logger.info("Configuration loaded")

            # Extract API credentials
            telegram_api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
            telegram_api_hash = os.getenv("TELEGRAM_API_HASH", "")
            telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            telegram_user_id = int(os.getenv("TELEGRAM_USER_ID", "0"))
            anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")

            if not all(
                [
                    telegram_api_id,
                    telegram_api_hash,
                    telegram_bot_token,
                    telegram_user_id,
                    anthropic_api_key,
                ]
            ):
                raise ValueError("Missing required environment variables")

            logger.info("Environment variables loaded")

            # Initialize database
            self.db = Database()
            await self.db.init()
            logger.info("Database initialized")

            # Initialize components
            self.trade_recorder = TradeRecorder(self.db)
            logger.info("TradeRecorder initialized")

            self.signal_parser = SignalParser(anthropic_api_key)
            logger.info("SignalParser initialized")

            # Build passwords dictionary from environment variables
            passwords = {}
            for account in self.config.get("accounts", []):
                login = account["login"]
                env_var = f"MT5_PASSWORD_ACCOUNT{account.get('index', 1)}"
                password = os.getenv(env_var)
                if password:
                    passwords[login] = password
                else:
                    logger.warning(
                        f"Password not found for account {login} (env var: {env_var})"
                    )

            self.account_manager = AccountManager(
                self.config.get("accounts", []), passwords
            )
            logger.info("AccountManager initialized")

            # Initialize confirmation bot
            timeout_seconds = self.config.get("confirmation", {}).get("timeout_seconds", 30)
            self.confirmation_bot = ConfirmationBot(
                telegram_bot_token, telegram_user_id, timeout_seconds
            )
            logger.info("ConfirmationBot initialized")

            # Initialize channel listener
            self.channel_listener = ChannelListener(telegram_api_id, telegram_api_hash)
            logger.info("ChannelListener initialized")

            logger.info("TradingBot initialization complete")

        except Exception as e:
            logger.error(f"Failed to initialize TradingBot: {e}")
            raise

    async def handle_new_message(self, text: str, message_id: int):
        """
        Handle new message from the signal channel.

        Args:
            text: Message text
            message_id: Telegram message ID
        """
        try:
            logger.info(f"New message received: {message_id}")

            # Parse signal
            signal = await self.signal_parser.parse(text)
            if signal is None:
                logger.debug(f"Message {message_id} is not a trading signal")
                return

            logger.info(
                f"Signal parsed: {signal.symbol} {signal.direction} @ {signal.entry}"
            )

            # Record signal to database
            signal_id = await self.trade_recorder.record_signal(signal)
            logger.info(f"Signal {signal_id} recorded to database")

            # Calculate estimated lot size for preview
            # In dry_run mode skip MT5 balance lookup and use a placeholder balance
            dry_run = self.config.get("trading", {}).get("dry_run", True)
            try:
                if dry_run:
                    preview_balance = 10000.0
                else:
                    first_account = self.config.get("accounts", [{}])[0]
                    preview_balance = self.account_manager.get_account_balance(
                        str(first_account["login"])
                    ) or 10000.0
                estimated_lots = calculate_lot_size(
                    balance=preview_balance,
                    risk_percent=self.config.get("trading", {}).get("risk_percent", 1.0),
                    entry=signal.entry or signal.stop_loss,
                    stop_loss=signal.stop_loss,
                    symbol=signal.symbol,
                    min_lot=self.config.get("trading", {}).get("min_lot_size", 0.01),
                    max_lot=self.config.get("trading", {}).get("max_lot_size", 5.0),
                    lot_step=self.config.get("trading", {}).get("lot_step", 0.01),
                )
                logger.info(f"Estimated lot size: {estimated_lots} (dry_run={dry_run})")
            except Exception as e:
                logger.error(f"Error calculating lot size: {e}")
                estimated_lots = 0.0

            # Store signal data for callbacks
            signal_dict = {
                "id": signal_id,
                "symbol": signal.symbol,
                "direction": signal.direction,
                "entry": signal.entry,
                "sl": signal.stop_loss,
                "tp": signal.take_profits,
                "risk_percent": self.config.get("trading", {}).get("risk_percent", 1.0),
                "message_id": message_id,
            }
            self.signal_in_progress[signal_id] = signal_dict

            # Ask for confirmation
            await self.confirmation_bot.ask_confirmation(
                signal_dict,
                estimated_lots,
                lambda: self.on_execute(signal_id),
                lambda: self.on_skip(signal_id),
            )

        except Exception as e:
            logger.error(f"Error handling message {message_id}: {e}")

    async def on_execute(self, signal_id: int):
        """Execute the signal on all enabled accounts (skipped in dry_run mode)."""
        try:
            if signal_id not in self.signal_in_progress:
                logger.warning(f"Signal {signal_id} not found in progress")
                return

            signal_dict = self.signal_in_progress[signal_id]
            dry_run = self.config.get("trading", {}).get("dry_run", True)

            if dry_run:
                logger.info(
                    f"[DRY RUN] Would execute signal {signal_id}: "
                    f"{signal_dict['symbol']} {signal_dict['direction']} "
                    f"entry={signal_dict['entry']} SL={signal_dict['sl']} "
                    f"TP={signal_dict['tp']}"
                )
                logger.info("[DRY RUN] No trades placed. Set dry_run: false in config.yaml to go live.")
                return

            logger.info(f"Executing signal {signal_id}: {signal_dict['symbol']}")

            # Calculate lot sizes for all enabled accounts
            lot_sizes = {}
            for account in self.config.get("accounts", []):
                if not account.get("enabled", True):
                    continue
                login = account["login"]
                balance = self.account_manager.get_account_balance(str(login))
                if balance is None:
                    logger.warning(f"Could not fetch balance for account {login}")
                    continue
                try:
                    lot_size = calculate_lot_size(
                        balance=balance,
                        risk_percent=signal_dict.get("risk_percent", 1.0),
                        entry=signal_dict["entry"] or signal_dict["sl"],
                        stop_loss=signal_dict["sl"],
                        symbol=signal_dict["symbol"],
                        min_lot=self.config.get("trading", {}).get("min_lot_size", 0.01),
                        max_lot=self.config.get("trading", {}).get("max_lot_size", 5.0),
                        lot_step=self.config.get("trading", {}).get("lot_step", 0.01),
                    )
                    lot_sizes[login] = lot_size
                    logger.info(f"Calculated lot size for {login}: {lot_size}")
                except Exception as e:
                    logger.error(f"Error calculating lot size for {login}: {e}")

            if not lot_sizes:
                logger.error(f"No valid lot sizes calculated for signal {signal_id}")
                return

            trade_signal = {
                "symbol": signal_dict["symbol"],
                "direction": signal_dict["direction"],
                "entry": signal_dict["entry"],
                "stop_loss": signal_dict["sl"],
                "take_profit": signal_dict["tp"][0] if signal_dict["tp"] else None,
                "magic": self.config.get("trading", {}).get("magic_number", 20240101),
                "slippage": self.config.get("trading", {}).get("slippage", 10),
            }

            results = await self.account_manager.execute_on_all(trade_signal, lot_sizes)

            for result in results:
                try:
                    if result.get("success"):
                        account_name = next(
                            (a.get("name") for a in self.config.get("accounts", []) if a["login"] == result["login"]),
                            str(result["login"])
                        )
                        trade_id = await self.trade_recorder.record_trade(
                            signal_id=signal_id,
                            account_name=account_name,
                            lot_size=lot_sizes.get(result["login"], 0),
                            fill_price=result.get("fill_price", 0),
                            ticket=result.get("ticket", 0),
                        )
                        logger.info(f"Trade {trade_id} recorded for signal {signal_id} on {account_name}")
                    else:
                        logger.error(f"Trade failed for {result['login']}: {result.get('error')}")
                except Exception as e:
                    logger.error(f"Error recording trade for {result['login']}: {e}")

            logger.info(f"Signal {signal_id} execution complete")

        except Exception as e:
            logger.error(f"Error executing signal {signal_id}: {e}")
        finally:
            if signal_id in self.signal_in_progress:
                del self.signal_in_progress[signal_id]

    async def on_skip(self, signal_id: int):
        """
        Skip the signal.

        Args:
            signal_id: Signal database ID
        """
        try:
            logger.info(f"Skipping signal {signal_id}")
            await self.trade_recorder.skip_signal(signal_id)
            logger.info(f"Signal {signal_id} marked as skipped")

        except Exception as e:
            logger.error(f"Error skipping signal {signal_id}: {e}")
        finally:
            if signal_id in self.signal_in_progress:
                del self.signal_in_progress[signal_id]

    async def start(self):
        """Start the trading bot."""
        try:
            logger.info("Starting TradingBot...")

            # Start confirmation bot
            await self.confirmation_bot.start()
            logger.info("ConfirmationBot started")

            # Start channel listener
            signal_channel = self.config.get("signal_channel", "")
            if not signal_channel:
                raise ValueError("signal_channel not configured")

            logger.info(f"Starting channel listener on: {signal_channel}")

            # Run listener as a task
            listener_task = asyncio.create_task(
                self.channel_listener.start(
                    signal_channel, self.handle_new_message
                )
            )

            # Wait for listener or keyboard interrupt
            await listener_task

        except KeyboardInterrupt:
            logger.info("Received KeyboardInterrupt, shutting down...")
        except Exception as e:
            logger.error(f"Error during bot execution: {e}")
            raise
        finally:
            await self.stop()

    async def stop(self):
        """Stop the trading bot and clean up resources."""
        try:
            logger.info("Stopping TradingBot...")

            if self.channel_listener:
                await self.channel_listener.stop()
                logger.info("ChannelListener stopped")

            if self.confirmation_bot:
                await self.confirmation_bot.stop()
                logger.info("ConfirmationBot stopped")

            if self.db:
                await self.db.close()
                logger.info("Database connection closed")

            logger.info("TradingBot stopped successfully")

        except Exception as e:
            logger.error(f"Error during shutdown: {e}")


async def main():
    """Main entry point."""
    bot = TradingBot()

    try:
        await bot.initialize()
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

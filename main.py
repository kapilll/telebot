import asyncio
import os
import sys
from datetime import datetime
from typing import Optional

import yaml
from dotenv import load_dotenv
import MetaTrader5 as mt5

from tg.listener import ChannelListener
from tg.notifier import Notifier
from parser.signal_parser import SignalParser, ParsedMessage
from trading.account_manager import AccountManager
from trading.risk_engine import RiskEngine
from trading.follow_up_handler import FollowUpHandler
from trading.trade_monitor import TradeMonitor
from journal.database import Database
from journal.trade_recorder import TradeRecorder
from utils.logger import get_logger

logger = get_logger("main")

MAGIC = 20240101  # referenced by account_manager close_all


def determine_entry_order(signal, trading_cfg: dict):
    """
    Decide MARKET vs LIMIT and the entry price.
    Range entry → LIMIT at the better end for tighter SL distance.
    Single / market entry → MARKET now.
    """
    if signal.entry_low is None:
        return "MARKET", signal.entry or 0.0

    try:
        tick = mt5.symbol_info_tick(signal.symbol)
        if tick is None:
            return "MARKET", signal.entry
        current = tick.ask if signal.direction == "BUY" else tick.bid
    except Exception:
        return "MARKET", signal.entry

    if signal.direction == "BUY":
        if current <= signal.entry_low * 1.0005:
            return "MARKET", current
        elif current <= signal.entry_high:
            return "LIMIT", signal.entry_low
        else:
            return "LIMIT", signal.entry_high
    else:  # SELL
        if current >= signal.entry_high * 0.9995:
            return "MARKET", current
        elif current >= signal.entry_low:
            return "LIMIT", signal.entry_high
        else:
            return "LIMIT", signal.entry_low


class TradingBot:
    def __init__(self):
        self.config = {}
        self.db: Optional[Database] = None
        self.trade_recorder: Optional[TradeRecorder] = None
        self.signal_parser: Optional[SignalParser] = None
        self.account_manager: Optional[AccountManager] = None
        self.risk_engine: Optional[RiskEngine] = None
        self.follow_up_handler: Optional[FollowUpHandler] = None
        self.trade_monitor: Optional[TradeMonitor] = None
        self.notifier: Optional[Notifier] = None
        self.channel_listener: Optional[ChannelListener] = None

    async def initialize(self):
        load_dotenv()

        with open("config.yaml", "r") as f:
            self.config = yaml.safe_load(f)
        logger.info("Config loaded")

        telegram_api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
        telegram_api_hash = os.getenv("TELEGRAM_API_HASH", "")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        telegram_user_id = int(os.getenv("TELEGRAM_USER_ID", "0"))
        anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")

        if not all([telegram_api_id, telegram_api_hash, telegram_bot_token,
                    telegram_user_id, anthropic_api_key]):
            raise ValueError("Missing required environment variables")

        self.db = Database()
        await self.db.init()

        self.trade_recorder = TradeRecorder(self.db)
        self.signal_parser = SignalParser(anthropic_api_key)

        # Build passwords dict — fix: use enumerate so account2 gets correct env var
        passwords = {}
        for i, account in enumerate(self.config.get("accounts", [])):
            env_var = f"MT5_PASSWORD_ACCOUNT{i + 1}"
            pwd = os.getenv(env_var)
            if pwd:
                passwords[account["login"]] = pwd
            else:
                logger.warning(f"No password for account {account['login']} ({env_var})")

        self.account_manager = AccountManager(
            self.config.get("accounts", []), passwords
        )
        self.risk_engine = RiskEngine(self.config)
        self.notifier = Notifier(telegram_bot_token, telegram_user_id)

        self.follow_up_handler = FollowUpHandler(
            self.db, self.account_manager, self.notifier, self.trade_recorder
        )

        self.trade_monitor = TradeMonitor(
            self.db, self.account_manager, self.risk_engine,
            self.notifier, self.trade_recorder,
            self.config, anthropic_api_key
        )

        self.channel_listener = ChannelListener(telegram_api_id, telegram_api_hash)
        logger.info("TradingBot initialized")

    async def handle_new_message(self, text: str, message_id: int, channel_id: int):
        if not text or not text.strip():
            return

        logger.info(f"Message received id={message_id} channel={channel_id}")

        msg: ParsedMessage = await self.signal_parser.parse_message(text)

        if msg.type == "noise":
            return

        if msg.type == "follow_up":
            await self.follow_up_handler.handle(msg, symbol="XAUUSD")
            return

        # New signal
        signal = msg.signal
        if not signal or not signal.take_profits:
            logger.warning("Parsed as new_signal but missing TPs — skipping")
            return

        dry_run = self.config.get("trading", {}).get("dry_run", True)

        # Get risk state
        if dry_run:
            daily_pnl = await self.db.get_daily_pnl()
            risk_state = self.risk_engine.get_state_dry_run(daily_pnl)
        else:
            first_account = self.config["accounts"][0]
            executor_tmp = __import__('trading.executor', fromlist=['TradeExecutor']).TradeExecutor(
                first_account.get("path", "")
            )
            executor_tmp.connect(
                first_account["login"],
                list(self.account_manager.passwords.values())[0] if self.account_manager.passwords else "",
                first_account["server"]
            )
            risk_state = self.risk_engine.get_state(executor_tmp)
            executor_tmp.disconnect()

        if risk_state.lot_multiplier == 0.0:
            await self.notifier.send_trade_skipped(
                signal, "Daily loss limit reached — no more trades today"
            )
            return

        # Record signal
        signal_id = await self.trade_recorder.record_signal(
            signal, source='live',
            source_message_id=message_id,
            channel=str(channel_id)
        )

        # Entry optimization
        order_type, entry_price = determine_entry_order(
            signal, self.config.get("trading", {})
        )
        if entry_price == 0.0:
            entry_price = signal.stop_loss  # fallback if tick unavailable

        logger.info(
            f"Signal {signal_id}: {signal.direction} {signal.symbol} "
            f"entry={entry_price} ({order_type}) SL={signal.stop_loss} "
            f"TPs={signal.take_profits}"
        )

        if dry_run:
            logger.info(
                f"[DRY RUN] Would trade: {signal.direction} {signal.symbol} "
                f"{order_type} @{entry_price} SL={signal.stop_loss} TPs={signal.take_profits} "
                f"budget_remaining=${risk_state.daily_budget_remaining:.2f}"
            )
            await self.notifier.send_trade_executed(signal, [], [], order_type)
            return

        results = await self.account_manager.execute_multi_tp(
            signal, entry_price, order_type,
            risk_state,
            self.config.get("trading", {}),
            self.config.get("tp_split", {})
        )

        await self.db.register_active_positions(signal_id, results)
        await self.notifier.send_trade_executed(
            signal, results, signal.take_profits, order_type
        )

        if order_type == "LIMIT":
            timeout = self.config.get("trading", {}).get("limit_order_timeout_minutes", 15)
            asyncio.create_task(
                self._cancel_unfilled_limits(results, signal, timeout)
            )

    async def _cancel_unfilled_limits(self, results: list, signal, timeout_minutes: int):
        await asyncio.sleep(timeout_minutes * 60)
        # Cancel any remaining pending orders from this batch
        for r in results:
            if not r.get("success"):
                continue
            account = self.account_manager._find_account(r.get("account_name", ""))
            if not account:
                continue
            login = account["login"]
            from trading.executor import TradeExecutor
            executor = TradeExecutor(account.get("path", account.get("mt5_path", "")))
            if executor.connect(login, self.account_manager.passwords.get(login), account["server"]):
                try:
                    for order in r.get("orders", []):
                        executor.cancel_pending_order(order["ticket"])
                        await self.db.close_active_position(
                            order["ticket"], exit_reason='limit_timeout'
                        )
                finally:
                    executor.disconnect()
        await self.notifier.send_limit_order_cancelled(signal.symbol, "15-min timeout — signal stale")

    async def start(self):
        logger.info("Starting TradingBot...")

        channels = self.config.get("signal_channels", [])
        if not channels:
            # backward compat
            ch = self.config.get("signal_channel", "")
            channels = [ch] if ch else []

        if not channels:
            raise ValueError("No signal_channels configured")

        # Start trade monitor as background task
        asyncio.create_task(self.trade_monitor.run())
        logger.info("TradeMonitor started")

        logger.info(f"Listening to channels: {channels}")
        await self.channel_listener.start(channels, self.handle_new_message)

    async def stop(self):
        logger.info("Stopping TradingBot...")
        self.trade_monitor.stop()
        if self.channel_listener:
            await self.channel_listener.stop()
        if self.db:
            await self.db.close()
        logger.info("TradingBot stopped")


async def main():
    bot = TradingBot()
    try:
        await bot.initialize()
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())

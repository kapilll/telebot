import asyncio
import logging
from typing import Callable, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from utils.logger import get_logger

logger = get_logger("confirmation_bot")


class ConfirmationBot:
    """Telegram bot for confirming gold signal trading decisions."""

    def __init__(self, bot_token: str, user_id: int, timeout_seconds: int = 30):
        """
        Initialize the confirmation bot.

        Args:
            bot_token: Telegram bot token
            user_id: Target user ID to send confirmations to
            timeout_seconds: Auto-execute timeout in seconds (default 30)
        """
        self.bot_token = bot_token
        self.user_id = user_id
        self.timeout_seconds = timeout_seconds
        self.application: Optional[Application] = None
        self._timeout_tasks: dict = {}
        self._message_ids: dict = {}

    async def start(self):
        """Initialize and start the bot application."""
        try:
            self.application = Application.builder().token(self.bot_token).build()
            self.application.add_handler(
                CallbackQueryHandler(self._handle_button_press)
            )
            await self.application.initialize()
            await self.application.start()
            logger.info("ConfirmationBot started successfully")
        except Exception as e:
            logger.error(f"Failed to start ConfirmationBot: {e}")
            raise

    async def stop(self):
        """Stop the bot application."""
        try:
            if self.application:
                await self.application.stop()
                await self.application.shutdown()
                logger.info("ConfirmationBot stopped successfully")
        except Exception as e:
            logger.error(f"Failed to stop ConfirmationBot: {e}")
            raise

    async def ask_confirmation(
        self,
        signal: dict,
        estimated_lots: float,
        on_execute: Callable,
        on_skip: Callable,
    ):
        """
        Send a confirmation message for a trading signal.

        Args:
            signal: Signal dict with keys: symbol, action, entry, sl, tp (list), risk_percent
            estimated_lots: Estimated lot size
            on_execute: Async callback to execute when user confirms or timeout fires
            on_skip: Async callback to execute when user skips
        """
        if not self.application:
            logger.error("Bot not initialized. Call start() first.")
            return

        signal_id = id(signal)
        signal_key = f"signal_{signal_id}"

        try:
            # Format message
            symbol = signal.get("symbol", "UNKNOWN")
            action = signal.get("action", "").upper()
            entry = signal.get("entry", 0)
            sl = signal.get("sl", 0)
            tp = signal.get("tp", [])
            risk_percent = signal.get("risk_percent", 0)

            tp_str = ", ".join(f"{t:.2f}" for t in tp) if tp else "N/A"

            message_text = (
                f"🟡 NEW SIGNAL — {symbol} {action}\n"
                f"Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp_str}\n"
                f"Risk: {risk_percent}% → {estimated_lots:.2f} lots (est.)\n\n"
                f"⏱ Auto-executes in {self.timeout_seconds}s"
            )

            # Create inline keyboard
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Execute Now", callback_data=f"exec_{signal_id}"
                    ),
                    InlineKeyboardButton("❌ Skip", callback_data=f"skip_{signal_id}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Send message
            message = await self.application.bot.send_message(
                chat_id=self.user_id,
                text=message_text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )

            self._message_ids[signal_key] = message.message_id

            # Store callbacks and handlers
            self._timeout_tasks[signal_key] = {
                "on_execute": on_execute,
                "on_skip": on_skip,
                "message_id": message.message_id,
                "timeout_task": None,
            }

            # Start timeout task
            timeout_task = asyncio.create_task(
                self._handle_timeout(signal_key, signal_id)
            )
            self._timeout_tasks[signal_key]["timeout_task"] = timeout_task

            logger.info(f"Confirmation sent for signal {signal_id}")

        except Exception as e:
            logger.error(f"Failed to send confirmation for signal: {e}")
            raise

    async def _handle_button_press(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button presses."""
        query = update.callback_query
        callback_data = query.data

        if not callback_data:
            return

        try:
            if callback_data.startswith("exec_"):
                signal_id = int(callback_data.split("_")[1])
                await self._execute_signal(signal_id)
            elif callback_data.startswith("skip_"):
                signal_id = int(callback_data.split("_")[1])
                await self._skip_signal(signal_id)
        except Exception as e:
            logger.error(f"Error handling button press: {e}")
            try:
                await query.answer(text="Error processing request", show_alert=True)
            except Exception:
                pass

    async def _execute_signal(self, signal_id: int):
        """Execute the signal and update message."""
        signal_key = f"signal_{signal_id}"

        if signal_key not in self._timeout_tasks:
            logger.warning(f"Signal {signal_id} not found in timeout tasks")
            return

        task_data = self._timeout_tasks[signal_key]

        try:
            # Cancel timeout task
            if task_data["timeout_task"]:
                task_data["timeout_task"].cancel()
                try:
                    await task_data["timeout_task"]
                except asyncio.CancelledError:
                    pass

            # Execute callback
            on_execute = task_data["on_execute"]
            if asyncio.iscoroutinefunction(on_execute):
                await on_execute()
            else:
                on_execute()

            # Update message
            message_id = task_data["message_id"]
            try:
                await self.application.bot.edit_message_text(
                    chat_id=self.user_id,
                    message_id=message_id,
                    text="✅ Executing...",
                )
            except Exception as e:
                logger.warning(f"Failed to edit message: {e}")

            logger.info(f"Signal {signal_id} executed")

        except Exception as e:
            logger.error(f"Error executing signal {signal_id}: {e}")
        finally:
            # Cleanup
            if signal_key in self._timeout_tasks:
                del self._timeout_tasks[signal_key]
            if signal_key in self._message_ids:
                del self._message_ids[signal_key]

    async def _skip_signal(self, signal_id: int):
        """Skip the signal and update message."""
        signal_key = f"signal_{signal_id}"

        if signal_key not in self._timeout_tasks:
            logger.warning(f"Signal {signal_id} not found in timeout tasks")
            return

        task_data = self._timeout_tasks[signal_key]

        try:
            # Cancel timeout task
            if task_data["timeout_task"]:
                task_data["timeout_task"].cancel()
                try:
                    await task_data["timeout_task"]
                except asyncio.CancelledError:
                    pass

            # Execute callback
            on_skip = task_data["on_skip"]
            if asyncio.iscoroutinefunction(on_skip):
                await on_skip()
            else:
                on_skip()

            # Update message
            message_id = task_data["message_id"]
            try:
                await self.application.bot.edit_message_text(
                    chat_id=self.user_id,
                    message_id=message_id,
                    text="❌ Skipped",
                )
            except Exception as e:
                logger.warning(f"Failed to edit message: {e}")

            logger.info(f"Signal {signal_id} skipped")

        except Exception as e:
            logger.error(f"Error skipping signal {signal_id}: {e}")
        finally:
            # Cleanup
            if signal_key in self._timeout_tasks:
                del self._timeout_tasks[signal_key]
            if signal_key in self._message_ids:
                del self._message_ids[signal_key]

    async def _handle_timeout(self, signal_key: str, signal_id: int):
        """Handle timeout and auto-execute signal."""
        try:
            await asyncio.sleep(self.timeout_seconds)

            if signal_key not in self._timeout_tasks:
                return

            task_data = self._timeout_tasks[signal_key]
            on_execute = task_data["on_execute"]

            # Auto-execute
            if asyncio.iscoroutinefunction(on_execute):
                await on_execute()
            else:
                on_execute()

            # Update message
            message_id = task_data["message_id"]
            try:
                await self.application.bot.edit_message_text(
                    chat_id=self.user_id,
                    message_id=message_id,
                    text="⏱ Auto-executed (timeout)",
                )
            except Exception as e:
                logger.warning(f"Failed to edit message: {e}")

            logger.info(f"Signal {signal_id} auto-executed due to timeout")

        except asyncio.CancelledError:
            logger.debug(f"Timeout cancelled for signal {signal_id}")
        except Exception as e:
            logger.error(f"Error in timeout handler for signal {signal_id}: {e}")
        finally:
            # Cleanup
            if signal_key in self._timeout_tasks:
                del self._timeout_tasks[signal_key]
            if signal_key in self._message_ids:
                del self._message_ids[signal_key]

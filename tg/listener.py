import asyncio
import time
from typing import Callable, Optional

from telethon import TelegramClient
from telethon.events import NewMessage
from telethon.errors import FloodWaitError

from utils.logger import get_logger


class ChannelListener:
    def __init__(self, api_id: int, api_hash: str, session_name: str = "tradebot_session"):
        """
        Initialize the ChannelListener with Telegram API credentials.

        Args:
            api_id: Telegram API ID
            api_hash: Telegram API hash
            session_name: Session file name (stored in current working directory)
        """
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.client = TelegramClient(session_name, api_id, api_hash)
        self.logger = get_logger("listener")
        self._running = False

    async def start(self, channel: str, on_message_callback: Callable[[str, int], None]):
        """
        Start listening to a Telegram channel.

        Args:
            channel: Channel name or ID to listen to
            on_message_callback: Async or sync callable that receives (message_text, message_id)
        """
        self._running = True
        reconnect_count = 0

        while self._running:
            try:
                reconnect_count += 1
                self.logger.info(f"Connecting to Telegram (attempt {reconnect_count})...")
                await self.client.start()
                self.logger.info(f"Connected to Telegram")
                reconnect_count = 0  # Reset on successful connection

                # Get the channel entity
                # For numeric IDs Telethon needs an int, not a string.
                # If that still fails (entity not cached), scan dialogs to warm the cache.
                try:
                    channel_lookup = int(channel) if str(channel).lstrip("-").isdigit() else channel
                    channel_entity = await self.client.get_entity(channel_lookup)
                    self.logger.info(f"Found channel: {channel_entity.title}")
                except (ValueError, Exception):
                    self.logger.info("Entity not cached — scanning dialogs to find channel...")
                    channel_entity = None
                    async for dialog in self.client.iter_dialogs():
                        dialog_id = dialog.entity.id
                        # Match against bare ID or -100-prefixed supergroup ID
                        needle = int(str(channel).lstrip("-").replace("100", "", 1)) if str(channel).startswith("-100") else int(str(channel).lstrip("-"))
                        if dialog_id == needle or dialog_id == int(str(channel).lstrip("-")):
                            channel_entity = dialog.entity
                            self.logger.info(f"Found channel via dialogs: {dialog.name}")
                            break
                    if channel_entity is None:
                        raise ValueError(f"Channel {channel} not found in dialogs. Make sure you are a member of the group.")

                # Register message handler
                @self.client.on(NewMessage(chats=channel_entity))
                async def message_handler(event):
                    try:
                        message_text = event.message.text
                        message_id = event.message.id

                        # Call the callback
                        if asyncio.iscoroutinefunction(on_message_callback):
                            await on_message_callback(message_text, message_id)
                        else:
                            on_message_callback(message_text, message_id)
                    except Exception as e:
                        self.logger.error(f"Error in message handler: {e}")

                self.logger.info(f"Listening to channel: {channel}")

                # Run until disconnected
                await self.client.run_until_disconnected()

            except FloodWaitError as flood_err:
                wait_time = flood_err.seconds
                self.logger.warning(f"Flood wait triggered. Waiting {wait_time} seconds before retry...")
                if self._running:
                    await asyncio.sleep(wait_time)
                    self.logger.info(f"Flood wait complete. Retrying connection...")
                else:
                    break

            except Exception as e:
                self.logger.error(f"Error in listener: {e}", exc_info=True)

                # Clean up before retry
                try:
                    await self.client.disconnect()
                except Exception as disconnect_err:
                    self.logger.warning(f"Error during disconnect: {disconnect_err}")

                if self._running:
                    self.logger.info(f"Retrying in 10 seconds (attempt {reconnect_count})...")
                    await asyncio.sleep(10)
                else:
                    break

    async def stop(self):
        """Stop listening and disconnect from Telegram."""
        self._running = False

        try:
            if self.client.is_connected():
                await self.client.disconnect()
                self.logger.info("Disconnected from Telegram")
        except Exception as e:
            self.logger.error(f"Error disconnecting: {e}")

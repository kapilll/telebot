import asyncio
from typing import Callable, List, Union

from telethon import TelegramClient
from telethon.events import NewMessage
from telethon.errors import FloodWaitError

from utils.logger import get_logger


class ChannelListener:
    def __init__(self, api_id: int, api_hash: str, session_name: str = "tradebot_session"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.client = TelegramClient(session_name, api_id, api_hash)
        self.logger = get_logger("listener")
        self._running = False

    async def start(self, channels: Union[str, List[str]],
                    on_message_callback: Callable):
        """
        Listen to one or more channels simultaneously.
        on_message_callback(text, message_id, chat_id)
        """
        if isinstance(channels, str):
            channels = [channels]

        self._running = True
        reconnect_count = 0

        while self._running:
            try:
                reconnect_count += 1
                self.logger.info(f"Connecting to Telegram (attempt {reconnect_count})...")
                await self.client.start()
                reconnect_count = 0

                # Resolve all channel entities
                channel_entities = []
                for ch in channels:
                    try:
                        lookup = int(ch) if str(ch).lstrip("-").isdigit() else ch
                        entity = await self.client.get_entity(lookup)
                        channel_entities.append(entity)
                        self.logger.info(f"Resolved channel: {ch} → {getattr(entity, 'title', ch)}")
                    except Exception:
                        # Entity not cached — scan dialogs
                        self.logger.info(f"Entity {ch} not cached — scanning dialogs...")
                        found = await self._find_in_dialogs(ch)
                        if found:
                            channel_entities.append(found)
                            self.logger.info(f"Found via dialogs: {ch}")
                        else:
                            self.logger.error(f"Could not resolve channel {ch} — skipping")

                if not channel_entities:
                    raise ValueError("No channels could be resolved")

                @self.client.on(NewMessage(chats=channel_entities))
                async def message_handler(event):
                    try:
                        text = event.message.text or ""
                        msg_id = event.message.id
                        chat_id = event.chat_id
                        if asyncio.iscoroutinefunction(on_message_callback):
                            await on_message_callback(text, msg_id, chat_id)
                        else:
                            on_message_callback(text, msg_id, chat_id)
                    except Exception as e:
                        self.logger.error(f"Message handler error: {e}")

                self.logger.info(f"Listening to {len(channel_entities)} channel(s)")
                await self.client.run_until_disconnected()

            except FloodWaitError as e:
                self.logger.warning(f"FloodWait {e.seconds}s")
                if self._running:
                    await asyncio.sleep(e.seconds)

            except Exception as e:
                self.logger.error(f"Listener error: {e}", exc_info=True)
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                if self._running:
                    self.logger.info("Retrying in 10s...")
                    await asyncio.sleep(10)

    async def _find_in_dialogs(self, channel: str):
        needle_str = str(channel).lstrip("-")
        if needle_str.startswith("100"):
            needle_str = needle_str[3:]
        try:
            needle = int(needle_str)
        except ValueError:
            needle = None

        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            eid = entity.id
            title = getattr(entity, 'username', '') or ''
            if needle and (eid == needle or eid == int(str(channel).lstrip("-"))):
                return entity
            if title and (title.lower() == str(channel).lstrip("@").lower()):
                return entity
        return None

    async def stop(self):
        self._running = False
        try:
            if self.client.is_connected():
                await self.client.disconnect()
        except Exception as e:
            self.logger.error(f"Disconnect error: {e}")

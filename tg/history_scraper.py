import asyncio
import os
from dotenv import load_dotenv
import yaml
from telethon import TelegramClient

from parser.signal_parser import SignalParser
from journal.database import Database
from journal.trade_recorder import TradeRecorder
from utils.logger import get_logger

logger = get_logger("history_scraper")


class HistoryScraper:
    def __init__(self, api_id: int, api_hash: str, session_name: str = "tradebot_session"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = TelegramClient(session_name, api_id, api_hash)

    async def scrape(self, channel: str, signal_parser: SignalParser,
                     trade_recorder: TradeRecorder, limit: int = 0) -> dict:
        await self.client.start()
        logger.info(f"Scraping channel '{channel}'")

        total = signals_found = signals_saved = 0
        seen_ids = set()

        try:
            try:
                lookup = int(channel) if str(channel).lstrip("-").isdigit() else channel
                entity = await self.client.get_entity(lookup)
            except Exception:
                entity = None
                async for dialog in self.client.iter_dialogs():
                    if str(dialog.entity.id) in str(channel) or \
                       getattr(dialog.entity, 'username', '') == channel.lstrip('@'):
                        entity = dialog.entity
                        break
                if entity is None:
                    raise ValueError(f"Channel {channel} not found")

            async for message in self.client.iter_messages(entity, limit=limit or None):
                total += 1
                if not message.text or message.id in seen_ids:
                    continue

                msg = await signal_parser.parse_message(message.text)
                if msg.type == "new_signal" and msg.signal:
                    signals_found += 1
                    try:
                        await trade_recorder.record_signal(
                            msg.signal,
                            source='historical',
                            source_message_id=message.id,
                            channel=channel,
                        )
                        seen_ids.add(message.id)
                        signals_saved += 1
                    except Exception as e:
                        logger.warning(f"Failed to save signal {message.id}: {e}")

                if total % 100 == 0:
                    logger.info(f"Processed {total} messages, {signals_found} signals found")

        except Exception as e:
            logger.error(f"Scrape error: {e}")
            raise
        finally:
            await self.client.disconnect()

        logger.info(f"Done: {total} messages, {signals_found} found, {signals_saved} saved")
        return {"total_messages": total, "signals_found": signals_found,
                "signals_saved": signals_saved}


async def main():
    load_dotenv()
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    db = Database()
    await db.init()
    recorder = TradeRecorder(db)
    parser = SignalParser(anthropic_key)
    scraper = HistoryScraper(api_id, api_hash)

    channels = config.get("signal_channels", [config.get("signal_channel", "")])
    for ch in channels:
        if ch:
            result = await scraper.scrape(ch, parser, recorder)
            logger.info(f"Channel {ch}: {result}")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())

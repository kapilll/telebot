import asyncio
import os
from typing import Dict, Any
from dotenv import load_dotenv
import yaml
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from utils.logger import get_logger

logger = get_logger("history_scraper")


class HistoryScraper:
    def __init__(self, api_id: int, api_hash: str, session_name: str = "tradebot_session"):
        """
        Initialize the HistoryScraper with Telegram credentials.

        Args:
            api_id: Telegram API ID
            api_hash: Telegram API hash
            session_name: Session file name for storing authentication
        """
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.client = TelegramClient(session_name, api_id, api_hash)

    async def scrape(
        self,
        channel: str,
        signal_parser,
        trade_recorder,
        limit: int = 0
    ) -> Dict[str, Any]:
        """
        Scrape historical messages from a Telegram channel and parse signals.

        Args:
            channel: Channel name or ID to scrape
            signal_parser: Signal parser with parse() method
            trade_recorder: Trade recorder with record_signal() method
            limit: Maximum number of messages to fetch (0 = all)

        Returns:
            Dictionary with keys:
            - total_messages: Total messages processed
            - signals_found: Total signals found by parser
            - signals_saved: Signals successfully saved (deduplicated)
        """
        await self.client.start()
        logger.info(f"Starting scrape of channel '{channel}'")

        total_messages = 0
        signals_found = 0
        signals_saved = 0
        processed_message_ids = set()

        try:
            # Get the entity (channel or user)
            entity = await self.client.get_entity(channel)

            # Fetch message history
            async for message in self.client.iter_messages(entity, limit=limit or None):
                total_messages += 1

                # Parse the message for signals
                parsed_signals = signal_parser.parse(message.text or "")

                if parsed_signals:
                    signals_found += len(parsed_signals) if isinstance(parsed_signals, list) else 1

                    # Handle both single signal and list of signals
                    signals_list = parsed_signals if isinstance(parsed_signals, list) else [parsed_signals]

                    for signal in signals_list:
                        # Check for duplicates by source_message_id
                        if message.id not in processed_message_ids:
                            # Add source_message_id and source to the signal
                            signal['source_message_id'] = message.id
                            signal['source'] = 'historical'
                            signal['channel'] = channel

                            # Record the signal
                            try:
                                trade_recorder.record_signal(signal)
                                signals_saved += 1
                                processed_message_ids.add(message.id)
                            except Exception as e:
                                logger.warning(f"Failed to record signal from message {message.id}: {e}")

                # Print progress every 100 messages
                if total_messages % 100 == 0:
                    logger.info(f"Processed {total_messages} messages, found {signals_found} signals")

            logger.info(
                f"Scrape complete: {total_messages} messages processed, "
                f"{signals_found} signals found, {signals_saved} signals saved"
            )

        except Exception as e:
            logger.error(f"Error during scraping: {e}")
            raise
        finally:
            await self.client.disconnect()

        return {
            "total_messages": total_messages,
            "signals_found": signals_found,
            "signals_saved": signals_saved
        }


async def main():
    """Main entry point for running history scraper as a module."""
    # Load environment variables
    load_dotenv()

    # Load configuration
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Get credentials from environment or config
    api_id = int(os.getenv('TELEGRAM_API_ID') or config.get('telegram', {}).get('api_id'))
    api_hash = os.getenv('TELEGRAM_API_HASH') or config.get('telegram', {}).get('api_hash')

    if not api_id or not api_hash:
        logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")
        return

    # Get channel and other config
    channel = config.get('scraper', {}).get('channel', '')
    limit = config.get('scraper', {}).get('limit', 0)

    if not channel:
        logger.error("Channel must be configured in config.yaml")
        return

    # Initialize scraper
    scraper = HistoryScraper(api_id, api_hash)

    # Import signal parser and trade recorder (assuming they exist)
    try:
        from telegram.signal_parser import SignalParser
        from database.trade_recorder import TradeRecorder

        signal_parser = SignalParser()
        trade_recorder = TradeRecorder()

        # Run scrape
        result = await scraper.scrape(channel, signal_parser, trade_recorder, limit=limit)
        logger.info(f"Final results: {result}")

    except ImportError as e:
        logger.error(f"Failed to import required modules: {e}")


if __name__ == "__main__":
    asyncio.run(main())

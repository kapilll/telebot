"""
Fetches the last 100 messages from the signal channel and runs each through
the signal parser. Prints what Claude extracts from each message.
Run AFTER completing Telethon login via main.py.

Usage: python test_parser.py
Output is printed to console AND saved to test_parser_output.txt
"""
import asyncio
import os
import sys
import yaml
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient
from parser.signal_parser import SignalParser

load_dotenv()

class Tee:
    """Write to both stdout and a file simultaneously."""
    def __init__(self, filepath):
        self.file = open(filepath, "w", encoding="utf-8")
        self.stdout = sys.stdout
    def write(self, data):
        self.stdout.write(data)
        self.file.write(data)
    def flush(self):
        self.stdout.flush()
        self.file.flush()
    def close(self):
        self.file.close()


async def main():
    output_file = f"test_parser_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    tee = Tee(output_file)
    sys.stdout = tee
    print(f"Output saving to: {output_file}\n")

    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    api_id = int(os.getenv("TELEGRAM_API_ID"))
    api_hash = os.getenv("TELEGRAM_API_HASH")
    channel = config["signal_channel"]
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    parser = SignalParser(anthropic_key)

    async with TelegramClient("tradebot_session", api_id, api_hash) as client:
        # Resolve channel — numeric IDs must be int; scan dialogs if not cached
        channel_lookup = int(channel) if str(channel).lstrip("-").isdigit() else channel
        try:
            entity = await client.get_entity(channel_lookup)
        except Exception:
            print("Entity not cached — scanning dialogs...")
            entity = None
            async for dialog in client.iter_dialogs():
                did = dialog.entity.id
                needle = abs(int(channel))
                if str(needle).endswith(str(did)) or did == needle:
                    entity = dialog.entity
                    print(f"Found: {dialog.name}")
                    break
            if entity is None:
                print(f"ERROR: Could not find channel {channel}. Are you a member?")
                return

        print(f"\nFetching last 100 messages from channel: {channel}\n{'='*60}")
        messages = await client.get_messages(entity, limit=100)
        print(f"Got {len(messages)} messages\n")

        signals_found = 0
        for msg in reversed(messages):  # oldest first
            if not msg.text or not msg.text.strip():
                continue

            print(f"\n--- Message {msg.id} ({msg.date.strftime('%Y-%m-%d %H:%M')}) ---")
            print(f"TEXT: {msg.text[:200]}")

            from parser.signal_parser import _clean_telegram_markdown
            cleaned = _clean_telegram_markdown(msg.text)
            if cleaned != msg.text.strip():
                print(f"  CLEANED: {cleaned[:300]}")

            signal = await parser.parse(msg.text)
            if signal:
                signals_found += 1
                print(f"  ✅ SIGNAL DETECTED:")
                print(f"     Symbol:    {signal.symbol}")
                print(f"     Direction: {signal.direction}")
                print(f"     Entry:     {signal.entry if signal.entry else 'MARKET'}")
                print(f"     SL:        {signal.stop_loss}")
                print(f"     TPs:       {signal.take_profits}")
                print(f"     Confidence:{signal.confidence:.2f}")
            else:
                print(f"  ⏭  Not a signal")

        print(f"\n{'='*60}")
        print(f"Done. Found {signals_found} signals out of {len(messages)} messages.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if isinstance(sys.stdout, Tee):
            sys.stdout.close()
            sys.stdout = sys.__stdout__

import re
import json
import anthropic
from dataclasses import dataclass


@dataclass
class ParsedSignal:
    symbol: str
    direction: str
    entry: float | None
    stop_loss: float
    take_profits: list[float]
    raw_text: str
    confidence: float


def _clean_telegram_markdown(text: str) -> str:
    """Strip Telegram markdown bold artifacts (**, ****)  before parsing."""
    text = re.sub(r'\*{2,}', ' ', text)
    text = re.sub(r'_{2,}', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


class SignalParser:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    async def parse(self, message_text: str) -> ParsedSignal | None:
        cleaned = _clean_telegram_markdown(message_text)

        system_prompt = """You are a trading signal parser for gold (XAUUSD) signals.

If the message contains a new trade signal with an entry price, stop loss, and take profit, respond with ONLY a JSON object:
{
  "is_signal": true,
  "symbol": "XAUUSD",
  "direction": "BUY",
  "entry": 2500.50,
  "stop_loss": 2495.00,
  "take_profits": [2510.00, 2520.00],
  "confidence": 0.95
}

Rules:
- Entry may be a range like "4560-4565" — use the average (e.g. 4562.5)
- Ignore take profits that say "OPEN" — only include numeric TPs
- Normalize symbol: "GOLD" → "XAUUSD"
- Normalize direction: "LONG" → "BUY", "SHORT" → "SELL"
- Messages about TP hits, pip updates, or greetings are NOT signals → {"is_signal": false}
- A signal MUST have a stop loss line to be valid
- confidence: 0.0–1.0 based on how clearly all fields are present"""

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"}
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": cleaned
                    }
                ]
            )

            response_text = response.content[0].text.strip()

            # Extract just the JSON object — ignore any text before/after it
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if not json_match:
                return None
            response_text = json_match.group(0)

            parsed = json.loads(response_text)

            if not parsed.get("is_signal", False):
                return None

            symbol = parsed.get("symbol", "").upper()
            if symbol == "GOLD":
                symbol = "XAUUSD"

            direction = parsed.get("direction", "").upper()
            if direction == "LONG":
                direction = "BUY"
            elif direction == "SHORT":
                direction = "SELL"

            entry = parsed.get("entry")
            if entry is not None:
                entry = float(entry)

            stop_loss = float(parsed.get("stop_loss", 0))
            take_profits = [float(tp) for tp in parsed.get("take_profits", [])]
            confidence = float(parsed.get("confidence", 0.5))

            return ParsedSignal(
                symbol=symbol,
                direction=direction,
                entry=entry,
                stop_loss=stop_loss,
                take_profits=take_profits,
                raw_text=message_text,
                confidence=confidence
            )

        except Exception as e:
            print(f"  [PARSER ERROR] {type(e).__name__}: {e}")
            print(f"  [RAW RESPONSE] {response_text if 'response_text' in dir() else 'no response'}")
            return None

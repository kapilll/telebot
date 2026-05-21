import re
import json
import anthropic
from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass
class ParsedSignal:
    symbol: str
    direction: str
    entry: float
    entry_low: Optional[float]
    entry_high: Optional[float]
    stop_loss: float
    take_profits: list
    raw_text: str
    confidence: float


@dataclass
class ParsedMessage:
    type: str                              # "new_signal" | "follow_up" | "noise"
    signal: Optional[ParsedSignal] = None
    follow_up_action: Optional[str] = None  # tp_hit | close_all | modify_sl | extend_tp
    tp_level: Optional[int] = None
    new_sl: Optional[Union[float, str]] = None   # float or "breakeven"
    new_tp: Optional[float] = None
    reason: Optional[str] = None


SYSTEM_PROMPT = """You are a trading message classifier for a gold signal Telegram channel.
Messages are written by humans — expect typos, mixed language, abbreviations, emojis, informal phrasing.

Classify every message as one of three types and return ONLY a JSON object:

1. NEW_SIGNAL — a new trade entry with SL and at least one TP.
{"type":"new_signal","symbol":"XAUUSD","direction":"BUY",
 "entry":2345.0,"entry_high":2348.0,"entry_low":2344.0,
 "stop_loss":2330.0,"take_profits":[2360.0,2375.0,2390.0],"confidence":0.92}

Rules for new_signal:
- entry range "2344-2348" → entry=average(2346), entry_low=2344, entry_high=2348
- single price → entry=that price, entry_low=null, entry_high=null
- "market" entry → entry=0, entry_low=null, entry_high=null
- ignore take_profits that say "OPEN" or "open" — numeric only
- MUST have a stop_loss line — otherwise it's noise
- Normalize: "GOLD"→"XAUUSD", "LONG"→"BUY", "SHORT"→"SELL"

2. FOLLOW_UP — an update about an existing open trade. Variants:
{"type":"follow_up","action":"tp_hit","tp_level":1}
{"type":"follow_up","action":"close_all","reason":"reversal"}
{"type":"follow_up","action":"modify_sl","new_sl":2341.0}
{"type":"follow_up","action":"modify_sl","new_sl":"breakeven"}
{"type":"follow_up","action":"extend_tp","new_tp":2405.0}

Typo/variant examples for follow_up:
  "TP 1 hitted" → tp_hit tp_level=1
  "TP1 done" → tp_hit tp_level=1
  "clse all" / "exit now" / "cancel trade" → close_all
  "mve sl to entry" / "sl to be" → modify_sl breakeven
  "move sl to 2341" → modify_sl 2341.0
  "new target 2405" / "extend tp" → extend_tp

3. NOISE — greetings, pip updates, commentary, incomplete signals (no SL).
{"type":"noise"}

IMPORTANT: Output ONLY the JSON object. No explanation, no markdown fences."""


def _clean_telegram_markdown(text: str) -> str:
    return _clean(text)


def _clean(text: str) -> str:
    text = re.sub(r'\*{2,}', ' ', text)
    text = re.sub(r'_{2,}', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


class SignalParser:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    async def parse_message(self, message_text: str) -> ParsedMessage:
        cleaned = _clean(message_text)
        if not cleaned:
            return ParsedMessage(type="noise")

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": cleaned}]
            )
            response_text = response.content[0].text.strip()

            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if not json_match:
                return ParsedMessage(type="noise")
            parsed = json.loads(json_match.group(0))

            msg_type = parsed.get("type", "noise")

            if msg_type == "new_signal":
                tps = [float(t) for t in parsed.get("take_profits", [])
                       if t not in (None, "OPEN", "open")]
                entry_raw = parsed.get("entry")
                entry = float(entry_raw) if entry_raw else 0.0
                entry_low = float(parsed["entry_low"]) if parsed.get("entry_low") else None
                entry_high = float(parsed["entry_high"]) if parsed.get("entry_high") else None

                symbol = parsed.get("symbol", "XAUUSD").upper()
                if symbol == "GOLD":
                    symbol = "XAUUSD"
                direction = parsed.get("direction", "").upper()
                if direction == "LONG":
                    direction = "BUY"
                elif direction == "SHORT":
                    direction = "SELL"

                signal = ParsedSignal(
                    symbol=symbol,
                    direction=direction,
                    entry=entry,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    stop_loss=float(parsed.get("stop_loss", 0)),
                    take_profits=tps,
                    raw_text=message_text,
                    confidence=float(parsed.get("confidence", 0.5)),
                )
                return ParsedMessage(type="new_signal", signal=signal)

            elif msg_type == "follow_up":
                action = parsed.get("action")
                new_sl_raw = parsed.get("new_sl")
                new_sl: Optional[Union[float, str]] = None
                if new_sl_raw == "breakeven":
                    new_sl = "breakeven"
                elif new_sl_raw is not None:
                    try:
                        new_sl = float(new_sl_raw)
                    except (ValueError, TypeError):
                        new_sl = "breakeven"

                return ParsedMessage(
                    type="follow_up",
                    follow_up_action=action,
                    tp_level=parsed.get("tp_level"),
                    new_sl=new_sl,
                    new_tp=float(parsed["new_tp"]) if parsed.get("new_tp") else None,
                    reason=parsed.get("reason"),
                )

            return ParsedMessage(type="noise")

        except Exception as e:
            print(f"[PARSER ERROR] {type(e).__name__}: {e}")
            return ParsedMessage(type="noise")

    # Backward-compat alias used by history_scraper / test_parser
    async def parse(self, message_text: str):
        msg = await self.parse_message(message_text)
        return msg.signal if msg.type == "new_signal" else None

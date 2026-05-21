import os
from telegram import Bot
from utils.logger import get_logger

logger = get_logger("notifier")


class Notifier:
    def __init__(self, bot_token: str, user_id: int):
        self.bot = Bot(token=bot_token)
        self.user_id = user_id

    async def _send(self, text: str):
        try:
            await self.bot.send_message(chat_id=self.user_id, text=text,
                                        parse_mode="HTML")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    async def send_trade_executed(self, signal, fill_results: list,
                                  tp_split: list, order_type: str):
        lots_info = ""
        for r in fill_results:
            if r.get("success"):
                orders = r.get("orders", [])
                lots_str = " | ".join(
                    f"TP{o['tp_level']} {o['lot']}lot @{o.get('fill_price', o.get('entry_price', '?'))}"
                    for o in orders
                )
                lots_info += f"\n  {r['account_name']}: {lots_str}"

        tps_str = " → ".join(str(t) for t in signal.take_profits)
        entry_str = f"{signal.entry_low}–{signal.entry_high}" if signal.entry_low else str(signal.entry)
        mode = "LIMIT" if order_type == "LIMIT" else "MARKET"

        await self._send(
            f"✅ <b>TRADED [{mode}]: {signal.direction} {signal.symbol}</b>\n"
            f"Entry: {entry_str} | SL: {signal.stop_loss}\n"
            f"TPs: {tps_str}{lots_info}"
        )

    async def send_trade_skipped(self, signal, reason: str):
        await self._send(
            f"⛔ <b>SKIPPED: {signal.direction} {signal.symbol}</b>\n"
            f"Reason: {reason}"
        )

    async def send_tp_hit(self, symbol: str, tp_level: int,
                          pnl_usd: float, sl_moved_to):
        sl_msg = f" | SL → {sl_moved_to}" if sl_moved_to else " | SL unchanged"
        await self._send(
            f"🎯 <b>TP{tp_level} HIT: {symbol}</b>\n"
            f"Est. PnL: +${pnl_usd:.2f}{sl_msg}"
        )

    async def send_sl_hit(self, symbol: str, pnl_usd: float):
        await self._send(
            f"❌ <b>SL HIT: {symbol}</b>\n"
            f"Loss: ${pnl_usd:.2f}"
        )

    async def send_sl_modified(self, symbol: str, new_sl, reason: str):
        await self._send(
            f"🔧 <b>SL MODIFIED: {symbol}</b>\n"
            f"New SL: {new_sl} | Reason: {reason}"
        )

    async def send_provider_close(self, symbol: str, reason: str):
        await self._send(
            f"🚨 <b>PROVIDER CLOSE: {symbol}</b>\n"
            f"Reason: {reason or 'provider instruction'}"
        )

    async def send_tp_extended(self, symbol: str, new_tp: float):
        await self._send(f"📈 <b>TP EXTENDED: {symbol}</b> → {new_tp}")

    async def send_risk_alert(self, message: str):
        await self._send(f"⚠️ <b>RISK ALERT</b>\n{message}")

    async def send_emergency_close(self, reasoning: str):
        await self._send(f"🆘 <b>EMERGENCY CLOSE</b>\n{reasoning}")

    async def send_limit_order_cancelled(self, symbol: str, reason: str):
        await self._send(
            f"⏰ <b>LIMIT CANCELLED: {symbol}</b>\n"
            f"Reason: {reason}"
        )

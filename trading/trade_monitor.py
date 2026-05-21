import asyncio
import json
import anthropic
from datetime import datetime
from journal.database import Database
from trading.risk_engine import RiskEngine
from tg.notifier import Notifier
from utils.logger import get_logger

logger = get_logger("trade_monitor")

MONITOR_SYSTEM_PROMPT = """You are monitoring open gold trades on a FundingPips prop firm account.
Daily loss limit: 6%. You receive a snapshot of current open positions and account state.

Output ONLY JSON — one of:
{"action": "HOLD", "reasoning": "Trade progressing normally."}
{"action": "CLOSE_ALL", "reasoning": "Specific emergency reason."}

CLOSE_ALL ONLY for genuine emergencies:
- Daily loss limit is about to be breached by unrealized losses on these positions
- A clear flash crash or extreme market event is occurring
- Provider has explicitly reversed this signal (you will see this noted in context)

Default is ALWAYS HOLD. Never close based on price drift, slow movement, or "feeling".
The SL placed by the bot already protects the account — trust it."""


class TradeMonitor:
    def __init__(self, db: Database, account_manager, risk_engine: RiskEngine,
                 notifier: Notifier, trade_recorder, config: dict,
                 anthropic_api_key: str):
        self.db = db
        self.account_manager = account_manager
        self.risk_engine = risk_engine
        self.notifier = notifier
        self.trade_recorder = trade_recorder
        self.magic = config.get("trading", {}).get("magic_number", 20240101)
        self.monitor_interval = config.get("ai", {}).get("monitor_interval_seconds", 10)
        self.ai_interval = config.get("ai", {}).get("ai_review_interval_seconds", 30)
        self.dry_run = config.get("trading", {}).get("dry_run", True)
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self._tick = 0
        self._running = False

    async def run(self):
        self._running = True
        logger.info("TradeMonitor started")
        while self._running:
            await asyncio.sleep(self.monitor_interval)
            self._tick += 1
            try:
                await self._check_mt5_closures()
                if self._tick % (self.ai_interval // self.monitor_interval) == 0:
                    await self._claude_review_open_trades()
            except Exception as e:
                logger.error(f"TradeMonitor loop error: {e}")

    def stop(self):
        self._running = False

    async def _check_mt5_closures(self):
        db_open = await self.db.get_active_positions(status='open')
        if not db_open:
            return

        if self.dry_run:
            return  # nothing to poll in dry_run

        mt5_tickets = set()
        try:
            positions = self.account_manager.get_all_open_positions(self.magic)
            mt5_tickets = {p.ticket for p in positions}
        except Exception as e:
            logger.error(f"Could not fetch MT5 positions: {e}")
            return

        for pos in db_open:
            if pos.ticket not in mt5_tickets:
                await self._handle_closure(pos)

    async def _handle_closure(self, pos):
        """A position disappeared from MT5 — determine if TP or SL."""
        # We can't easily tell if it was TP or SL after the fact from position history
        # without deal history. Mark as closed in DB and notify.
        await self.db.close_active_position(pos.ticket, exit_reason='auto_detected')
        logger.info(f"Position {pos.ticket} (TP{pos.tp_level}) detected as closed")

        # Check if other positions at higher TP levels still open — apply SL progression
        all_for_signal = await self.db.get_active_positions(status='open',
                                                             symbol=pos.symbol)
        signal_positions = [p for p in all_for_signal
                            if p.signal_id == pos.signal_id]

        # Apply TP-triggered SL rules on remaining positions
        closed_level = pos.tp_level
        remaining = [p for p in signal_positions if p.tp_level > closed_level]

        sl_moved_to = None
        if closed_level == 2 and remaining:
            sl_moved_to = pos.entry_price
        elif closed_level >= 3 and remaining:
            tp1 = next((p for p in signal_positions if p.tp_level == 1), None)
            sl_moved_to = tp1.tp_price if tp1 else None

        if sl_moved_to is not None:
            for rpos in remaining:
                await self.account_manager.modify_sl_for_tickets(
                    [rpos.ticket], sl_moved_to, rpos.account_name
                )
            await self.db.update_sl_state_bulk(
                [p.ticket for p in remaining],
                sl_moved_to,
                'breakeven' if closed_level == 2 else 'tp1'
            )
            await self.notifier.send_sl_modified(
                pos.symbol, sl_moved_to,
                f"TP{closed_level} hit → automatic SL progression"
            )

        await self.notifier.send_tp_hit(pos.symbol, closed_level, 0, sl_moved_to)

    async def _claude_review_open_trades(self):
        open_positions = await self.db.get_active_positions(status='open')
        if not open_positions:
            return

        snapshot = self._format_snapshot(open_positions)
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=[{"type": "text", "text": MONITOR_SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": snapshot}]
            )
            text = response.content[0].text.strip()
            jm = __import__('re').search(r'\{.*\}', text, __import__('re').DOTALL)
            if not jm:
                return
            action = json.loads(jm.group(0))
            logger.info(f"Claude monitor: {action}")

            if action.get("action") == "CLOSE_ALL" and not self.dry_run:
                logger.warning(f"Claude emergency close: {action.get('reasoning')}")
                await self.account_manager.close_all_bot_positions()
                await self.notifier.send_emergency_close(action.get("reasoning", ""))
                await self.trade_recorder.record_emergency_close(
                    open_positions, action.get("reasoning", "")
                )
        except Exception as e:
            logger.error(f"Claude review error: {e}")

    def _format_snapshot(self, positions: list) -> str:
        lines = [f"Open positions ({len(positions)} total):"]
        for p in positions:
            lines.append(
                f"  ticket={p.ticket} {p.symbol} TP{p.tp_level} "
                f"entry={p.entry_price} sl={p.sl_current} tp={p.tp_price} "
                f"sl_state={p.sl_state}"
            )
        return "\n".join(lines)

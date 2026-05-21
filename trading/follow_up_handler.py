from parser.signal_parser import ParsedMessage
from journal.database import Database
from tg.notifier import Notifier
from utils.logger import get_logger

logger = get_logger("follow_up_handler")


class FollowUpHandler:
    def __init__(self, db: Database, account_manager, notifier: Notifier,
                 trade_recorder):
        self.db = db
        self.account_manager = account_manager
        self.notifier = notifier
        self.trade_recorder = trade_recorder

    async def handle(self, msg: ParsedMessage, symbol: str = "XAUUSD"):
        open_positions = await self.db.get_active_positions(symbol=symbol)
        if not open_positions:
            logger.info(f"Follow-up received but no open {symbol} positions — ignoring")
            return

        action = msg.follow_up_action
        logger.info(f"Follow-up action: {action} symbol={symbol}")

        if action == "tp_hit":
            await self._on_tp_hit(msg.tp_level, open_positions)

        elif action == "close_all":
            await self.account_manager.close_all_bot_positions(symbol=symbol)
            await self.trade_recorder.record_provider_close(open_positions, msg.reason)
            await self.notifier.send_provider_close(symbol, msg.reason)

        elif action == "modify_sl":
            new_sl = msg.new_sl
            if new_sl == "breakeven":
                # Use entry price of the first open position as breakeven
                new_sl = open_positions[0].entry_price if open_positions else None
            if new_sl is None:
                logger.warning("modify_sl action but no new_sl value")
                return
            remaining = [p for p in open_positions if p.status == 'open']
            for pos in remaining:
                await self.account_manager.modify_sl_for_tickets(
                    [pos.ticket], float(new_sl), pos.account_name
                )
            await self.db.update_sl_state_bulk(
                [p.ticket for p in remaining], float(new_sl), 'provider'
            )
            await self.notifier.send_sl_modified(symbol, new_sl, "provider instruction")

        elif action == "extend_tp":
            if msg.new_tp:
                await self.db.extend_tp_for_symbol(symbol, msg.new_tp)
                await self.notifier.send_tp_extended(symbol, msg.new_tp)

    async def _on_tp_hit(self, tp_level: int, open_positions: list):
        """
        Immutable SL progression rules:
        TP1 hit → NO SL change (let trade breathe)
        TP2 hit → move remaining SL to entry (breakeven)
        TP3 hit → move remaining SL to TP1 price
        """
        remaining = [p for p in open_positions if p.tp_level > tp_level and p.status == 'open']
        sl_moved_to = None

        if tp_level == 2 and remaining:
            sl_moved_to = remaining[0].entry_price
        elif tp_level >= 3 and remaining:
            tp1_pos = next((p for p in open_positions if p.tp_level == 1), None)
            sl_moved_to = tp1_pos.tp_price if tp1_pos else None

        if sl_moved_to is not None:
            for pos in remaining:
                success = await self.account_manager.modify_sl_for_tickets(
                    [pos.ticket], sl_moved_to, pos.account_name
                )
            await self.db.update_sl_state_bulk(
                [p.ticket for p in remaining],
                sl_moved_to,
                'breakeven' if tp_level == 2 else 'tp1'
            )
            logger.info(f"TP{tp_level} hit → SL moved to {sl_moved_to} for {len(remaining)} positions")

        pnl = await self.trade_recorder.record_tp_hit(tp_level, open_positions)
        await self.notifier.send_tp_hit(
            open_positions[0].symbol if open_positions else "XAUUSD",
            tp_level, pnl, sl_moved_to
        )

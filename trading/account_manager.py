import asyncio
from typing import List, Optional
from trading.executor import TradeExecutor
from trading.position_sizer import calculate_lot_size, adjust_lots_for_tp_split
from utils.logger import get_logger

logger = get_logger("account_manager")


class AccountManager:
    def __init__(self, accounts_config: list, passwords: dict):
        self.accounts_config = accounts_config
        self.passwords = passwords

    def _make_executor(self, account: dict) -> TradeExecutor:
        mt5_path = account.get("path", account.get("mt5_path", ""))
        return TradeExecutor(mt5_path)

    # ── entry point for new signals ──────────────────────────────────────────

    async def execute_multi_tp(self, signal, entry_price: float, order_type: str,
                                risk_state, trading_cfg: dict,
                                tp_split_cfg: dict) -> list:
        """
        For each enabled account: open N sub-lots (one per TP level).
        Runs accounts SEQUENTIALLY (MT5 is not thread-safe).
        Returns list of per-account result dicts.
        """
        tps = signal.take_profits
        n = len(tps)
        if n == 0:
            logger.error("No take-profit levels — cannot execute")
            return []

        key = f"{n}_tp"
        weights = tp_split_cfg.get("weights", {}).get(key, [1.0 / n] * n)

        min_lot = trading_cfg.get("min_lot_size", 0.01)
        max_lot = trading_cfg.get("max_lot_size", 0.05)
        lot_step = trading_cfg.get("lot_step", 0.01)
        magic = trading_cfg.get("magic_number", 20240101)
        slippage = trading_cfg.get("slippage", 10)

        min_remaining = trading_cfg.get("min_remaining_chances",
                                        3)  # fall back if not in risk cfg

        results = []
        loop = asyncio.get_event_loop()

        for account in self.accounts_config:
            if not account.get("enabled", True):
                continue
            result = await loop.run_in_executor(
                None, self._execute_account,
                account, signal, entry_price, order_type,
                tps, weights, risk_state,
                min_lot, max_lot, lot_step, magic, slippage,
                min_remaining
            )
            results.append(result)

        return results

    def _execute_account(self, account, signal, entry_price, order_type,
                         tps, weights, risk_state,
                         min_lot, max_lot, lot_step, magic, slippage,
                         min_remaining) -> dict:
        login = account["login"]
        password = self.passwords.get(login)
        if not password:
            return {"login": login, "success": False, "error": "no password"}

        executor = self._make_executor(account)
        if not executor.connect(login, password, account["server"]):
            return {"login": login, "success": False, "error": "connection failed"}

        try:
            base_lot = calculate_lot_size(
                balance=risk_state.balance,
                daily_loss_limit_pct=risk_state.initial_balance and
                                     (risk_state.daily_budget_remaining * min_remaining /
                                      risk_state.initial_balance) or 6.0,
                daily_pnl=risk_state.daily_pnl,
                entry=entry_price,
                stop_loss=signal.stop_loss,
                min_lot=min_lot,
                max_lot=max_lot,
                lot_step=lot_step,
                min_remaining_chances=min_remaining,
            )

            if base_lot == 0.0:
                return {"login": login, "success": False,
                        "error": "lot=0 (daily budget exhausted)"}

            tp_lots = adjust_lots_for_tp_split(base_lot, weights, min_lot, lot_step)
            orders = []

            for tp_idx, sub_lot in tp_lots:
                tp = tps[tp_idx]
                if order_type == "LIMIT":
                    order = executor.place_limit_order(
                        signal.symbol, signal.direction, sub_lot,
                        entry_price, signal.stop_loss, tp, magic, slippage
                    )
                else:
                    order = executor.place_market_order(
                        signal.symbol, signal.direction, sub_lot,
                        signal.stop_loss, tp, magic, slippage
                    )

                if order:
                    orders.append({
                        "tp_level": tp_idx + 1,
                        "ticket": order["ticket"],
                        "fill_price": order.get("fill_price", entry_price),
                        "entry_price": entry_price,
                        "lot": sub_lot,
                        "tp_price": tp,
                        "sl": signal.stop_loss,
                        "symbol": signal.symbol,
                        "order_type": order_type.lower(),
                    })
                else:
                    logger.error(f"Order failed for TP{tp_idx+1} on {account['name']}")

            return {
                "login": login,
                "account_name": account["name"],
                "success": bool(orders),
                "orders": orders,
            }
        except Exception as e:
            logger.error(f"Execute error on {account['name']}: {e}")
            return {"login": login, "success": False, "error": str(e)}
        finally:
            executor.disconnect()

    # ── position management ──────────────────────────────────────────────────

    async def modify_sl_for_tickets(self, tickets: list, new_sl: float,
                                     account_name: str) -> bool:
        account = self._find_account(account_name)
        if not account:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._do_modify_sl, account, tickets, new_sl
        )

    def _do_modify_sl(self, account, tickets, new_sl) -> bool:
        login = account["login"]
        executor = self._make_executor(account)
        if not executor.connect(login, self.passwords.get(login), account["server"]):
            return False
        try:
            return all(executor.modify_position_sl(t, new_sl) for t in tickets)
        finally:
            executor.disconnect()

    async def close_all_bot_positions(self, symbol: str = None):
        loop = asyncio.get_event_loop()
        for account in self.accounts_config:
            if not account.get("enabled", True):
                continue
            await loop.run_in_executor(
                None, self._do_close_all, account, symbol
            )

    def _do_close_all(self, account, symbol: str = None):
        login = account["login"]
        executor = self._make_executor(account)
        if not executor.connect(login, self.passwords.get(login), account["server"]):
            return
        try:
            from config import MAGIC
        except Exception:
            from utils.logger import get_logger as _gl
            MAGIC = 20240101
        try:
            positions = executor.get_open_positions(magic=MAGIC)
            if symbol:
                positions = [p for p in positions if p.symbol == symbol]
            for p in positions:
                executor.close_position(p.ticket)
        finally:
            executor.disconnect()

    def get_all_open_positions(self, magic: int) -> list:
        """Aggregate open positions from all enabled accounts."""
        all_positions = []
        for account in self.accounts_config:
            if not account.get("enabled", True):
                continue
            login = account["login"]
            executor = self._make_executor(account)
            if executor.connect(login, self.passwords.get(login), account["server"]):
                try:
                    all_positions.extend(executor.get_open_positions(magic=magic))
                finally:
                    executor.disconnect()
        return all_positions

    def get_account_balance(self, account_name: str) -> Optional[float]:
        account = self._find_account(account_name)
        if not account:
            return None
        login = account["login"]
        executor = self._make_executor(account)
        if not executor.connect(login, self.passwords.get(login), account["server"]):
            return None
        try:
            info = executor.get_account_info()
            return info.balance if info else None
        finally:
            executor.disconnect()

    def _find_account(self, name_or_login: str) -> Optional[dict]:
        for acc in self.accounts_config:
            if str(acc["login"]) == str(name_or_login) or acc.get("name") == name_or_login:
                return acc
        return None

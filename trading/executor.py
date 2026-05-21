from datetime import datetime
from typing import Optional
import MetaTrader5 as mt5
from utils.logger import get_logger


class TradeExecutor:
    def __init__(self, mt5_path: str):
        self.mt5_path = mt5_path
        self.connected = False
        self._login = None
        self._password = None
        self._server = None
        self.logger = get_logger("executor")

    def connect(self, login: int, password: str, server: str) -> bool:
        self._login = login
        self._password = password
        self._server = server
        try:
            if not mt5.initialize(path=self.mt5_path):
                self.logger.error(f"MT5 init failed: {mt5.last_error()}")
                return False
            if not mt5.login(login=login, password=password, server=server):
                self.logger.error(f"MT5 login failed: {mt5.last_error()}")
                mt5.shutdown()
                return False
            self.connected = True
            self.logger.info(f"Connected MT5 account {login} on {server}")
            return True
        except Exception as e:
            self.logger.error(f"Connection error: {e}")
            self.connected = False
            return False

    def disconnect(self):
        if self.connected:
            mt5.shutdown()
            self.connected = False

    def _check_connection(self):
        if not mt5.initialize(path=self.mt5_path):
            raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None or (self._login and info.login != self._login):
            if not mt5.login(self._login, self._password, self._server):
                raise RuntimeError(f"MT5 re-login failed: {mt5.last_error()}")

    def place_market_order(self, symbol: str, direction: str, lot_size: float,
                           sl: float, tp: float, magic: int,
                           slippage: int = 20) -> Optional[dict]:
        if not self.connected:
            return None
        try:
            self._check_connection()
            order_type = mt5.ORDER_TYPE_BUY if direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                self.logger.error(f"No tick for {symbol}")
                return None
            price = tick.ask if direction.upper() == "BUY" else tick.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                "volume": lot_size, "type": order_type, "price": price,
                "sl": sl, "tp": tp, "deviation": slippage, "magic": magic,
                "comment": f"{direction} {symbol}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return {"ticket": result.order, "fill_price": result.price, "comment": result.comment}
            elif result.retcode in (mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED):
                result = mt5.order_send(request)
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    return {"ticket": result.order, "fill_price": result.price}
            self.logger.error(f"Market order failed: {result.retcode} {result.comment}")
            return None
        except Exception as e:
            self.logger.error(f"Exception placing market order: {e}")
            return None

    def place_limit_order(self, symbol: str, direction: str, lot_size: float,
                          entry: float, sl: float, tp: float,
                          magic: int, slippage: int = 20) -> Optional[dict]:
        if not self.connected:
            return None
        try:
            self._check_connection()
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT
            request = {
                "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol,
                "volume": lot_size, "type": order_type, "price": entry,
                "sl": sl, "tp": tp, "magic": magic,
                "comment": f"{direction} LIMIT {symbol}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                self.logger.info(f"Limit order placed: {direction} {lot_size} {symbol} @{entry} ticket={result.order}")
                return {"ticket": result.order, "entry_price": entry, "comment": result.comment}
            self.logger.error(f"Limit order failed: {result.retcode} {result.comment}")
            return None
        except Exception as e:
            self.logger.error(f"Exception placing limit order: {e}")
            return None

    def modify_position_sl(self, ticket: int, new_sl: float) -> bool:
        try:
            self._check_connection()
            pos = mt5.positions_get(ticket=ticket)
            if not pos:
                self.logger.warning(f"Position {ticket} not found for SL modify")
                return False
            p = pos[0]
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol,
                "position": ticket, "sl": new_sl, "tp": p.tp,
            })
            ok = result.retcode == mt5.TRADE_RETCODE_DONE
            if ok:
                self.logger.info(f"SL modified ticket={ticket} new_sl={new_sl}")
            else:
                self.logger.error(f"SL modify failed: {result.retcode} {result.comment}")
            return ok
        except Exception as e:
            self.logger.error(f"Exception modifying SL: {e}")
            return False

    def close_position(self, ticket: int) -> bool:
        try:
            self._check_connection()
            pos = mt5.positions_get(ticket=ticket)
            if not pos:
                return False
            p = pos[0]
            order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            tick = mt5.symbol_info_tick(p.symbol)
            price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
                "volume": p.volume, "type": order_type, "position": ticket,
                "price": price, "deviation": 20, "magic": p.magic,
                "comment": "bot close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            })
            ok = result.retcode == mt5.TRADE_RETCODE_DONE
            if ok:
                self.logger.info(f"Position {ticket} closed")
            else:
                self.logger.error(f"Close failed: {result.retcode} {result.comment}")
            return ok
        except Exception as e:
            self.logger.error(f"Exception closing position: {e}")
            return False

    def cancel_pending_order(self, ticket: int) -> bool:
        try:
            self._check_connection()
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
            ok = result.retcode == mt5.TRADE_RETCODE_DONE
            if ok:
                self.logger.info(f"Pending order {ticket} cancelled")
            else:
                self.logger.error(f"Cancel failed: {result.retcode} {result.comment}")
            return ok
        except Exception as e:
            self.logger.error(f"Exception cancelling order: {e}")
            return False

    def get_open_positions(self, magic: int = None) -> list:
        try:
            self._check_connection()
            positions = mt5.positions_get() or []
            return [p for p in positions if magic is None or p.magic == magic]
        except Exception:
            return []

    def get_pending_orders(self, magic: int = None) -> list:
        try:
            self._check_connection()
            orders = mt5.orders_get() or []
            return [o for o in orders if magic is None or o.magic == magic]
        except Exception:
            return []

    def get_deals_since(self, from_dt: datetime) -> list:
        try:
            self._check_connection()
            return list(mt5.history_deals_get(from_dt, datetime.utcnow()) or [])
        except Exception:
            return []

    def get_account_info(self) -> Optional[object]:
        try:
            self._check_connection()
            return mt5.account_info()
        except Exception:
            return None

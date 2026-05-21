import MetaTrader5 as mt5
from typing import Optional
from utils.logger import get_logger


class TradeExecutor:
    """Executes trades on MetaTrader5 platform."""

    def __init__(self, mt5_path: str):
        """
        Initialize TradeExecutor.

        Args:
            mt5_path: Path to MetaTrader5 terminal executable
        """
        self.mt5_path = mt5_path
        self.connected = False
        self.logger = get_logger("executor")

    def _check_connection(self):
        """
        Check if MT5 connection is healthy.

        Raises:
            RuntimeError: If MT5 is not initialized or connection is lost
        """
        if not mt5.initialize(path=self.mt5_path):
            self.logger.error(f"MT5 initialization check failed: {mt5.last_error()}")
            self.connected = False
            raise RuntimeError("MT5 connection lost or failed to initialize")
        return True

    def connect(self, login: int, password: str, server: str) -> bool:
        """
        Connect to MetaTrader5 account.

        Args:
            login: Account login number
            password: Account password
            server: Server name

        Returns:
            True if connection successful, False otherwise

        Raises:
            RuntimeError: If MT5 initialization fails
        """
        try:
            if not mt5.initialize(path=self.mt5_path):
                error_msg = f"MT5 initialization failed: {mt5.last_error()}"
                self.logger.error(error_msg)
                raise RuntimeError(error_msg)

            if not mt5.login(login=login, password=password, server=server):
                error_msg = f"MT5 login failed: {mt5.last_error()}"
                self.logger.error(error_msg)
                mt5.shutdown()
                raise RuntimeError(error_msg)

            self.logger.info(f"Connected to MT5 account {login} on {server}")
            self.connected = True
            return True
        except RuntimeError as e:
            self.logger.error(f"Connection error: {e}")
            self.connected = False
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error during connection: {e}", exc_info=True)
            self.connected = False
            raise RuntimeError(f"Unexpected connection error: {e}")

    def disconnect(self):
        """Disconnect from MetaTrader5."""
        if self.connected:
            mt5.shutdown()
            self.connected = False

    def place_market_order(
        self,
        symbol: str,
        direction: str,
        lot_size: float,
        sl: float,
        tp: float,
        magic: int,
        slippage: int,
    ) -> Optional[dict]:
        """
        Place a market order.

        Args:
            symbol: Trading symbol (e.g., "XAUUSD")
            direction: "BUY" or "SELL"
            lot_size: Lot size
            sl: Stop loss price
            tp: Take profit price
            magic: Magic number for order identification
            slippage: Maximum slippage in points

        Returns:
            Dict with ticket, fill_price, comment on success; None on failure
        """
        if not self.connected:
            self.logger.error("Cannot place order: MT5 not connected")
            return None

        try:
            # Check connection health before order
            self._check_connection()

            order_type = mt5.ORDER_TYPE_BUY if direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL

            # Get current tick price
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                self.logger.error(f"Failed to get tick for {symbol}")
                return None

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lot_size,
                "type": order_type,
                "price": tick.ask if direction.upper() == "BUY" else tick.bid,
                "sl": sl,
                "tp": tp,
                "deviation": slippage,
                "magic": magic,
                "comment": f"{direction} {symbol}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                self.logger.info(f"Market order placed: {direction} {lot_size} {symbol} ticket={result.order}")
                return {
                    "ticket": result.order,
                    "fill_price": result.price,
                    "comment": result.comment,
                }
            elif result.retcode in (mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED):
                # Retry once on requote or price change
                self.logger.warning(f"Requote/price change ({result.retcode}). Retrying order...")
                result = mt5.order_send(request)

                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.logger.info(f"Market order placed on retry: {direction} {lot_size} {symbol} ticket={result.order}")
                    return {
                        "ticket": result.order,
                        "fill_price": result.price,
                        "comment": result.comment,
                    }
                else:
                    self.logger.error(f"Order failed after retry: retcode={result.retcode} comment={result.comment}")
                    return None
            else:
                self.logger.error(f"Order rejected: retcode={result.retcode} comment={result.comment}")
                return None

        except Exception as e:
            self.logger.error(f"Exception placing market order: {e}", exc_info=True)
            return None

    def place_limit_order(
        self,
        symbol: str,
        direction: str,
        lot_size: float,
        entry: float,
        sl: float,
        tp: float,
        magic: int,
    ) -> Optional[dict]:
        """
        Place a limit order.

        Args:
            symbol: Trading symbol (e.g., "XAUUSD")
            direction: "BUY" or "SELL"
            lot_size: Lot size
            entry: Entry (limit) price
            sl: Stop loss price
            tp: Take profit price
            magic: Magic number for order identification

        Returns:
            Dict with ticket, entry_price, comment on success; None on failure
        """
        if not self.connected:
            self.logger.error("Cannot place limit order: MT5 not connected")
            return None

        try:
            # Check connection health before order
            self._check_connection()

            order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": lot_size,
                "type": order_type,
                "price": entry,
                "sl": sl,
                "tp": tp,
                "magic": magic,
                "comment": f"{direction} LIMIT {symbol}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                self.logger.info(f"Limit order placed: {direction} {lot_size} {symbol} @{entry} ticket={result.order}")
                return {
                    "ticket": result.order,
                    "entry_price": entry,
                    "comment": result.comment,
                }
            elif result.retcode in (mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED):
                # Retry once on requote or price change
                self.logger.warning(f"Requote/price change ({result.retcode}). Retrying limit order...")
                result = mt5.order_send(request)

                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.logger.info(f"Limit order placed on retry: {direction} {lot_size} {symbol} @{entry} ticket={result.order}")
                    return {
                        "ticket": result.order,
                        "entry_price": entry,
                        "comment": result.comment,
                    }
                else:
                    self.logger.error(f"Limit order failed after retry: retcode={result.retcode} comment={result.comment}")
                    return None
            else:
                self.logger.error(f"Limit order rejected: retcode={result.retcode} comment={result.comment}")
                return None

        except Exception as e:
            self.logger.error(f"Exception placing limit order: {e}", exc_info=True)
            return None

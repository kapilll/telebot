import asyncio
from typing import List, Dict, Optional
from trading.executor import TradeExecutor


class AccountManager:
    """Manages multiple MetaTrader5 accounts and executes trades across them."""

    def __init__(self, accounts_config: list, passwords: dict):
        """
        Initialize AccountManager.

        Args:
            accounts_config: List of account configurations with keys:
                - login: Account login number
                - server: Server name
                - enabled: Boolean to enable/disable account
                - mt5_path: Path to MT5 terminal
            passwords: Dict mapping login numbers to passwords
        """
        self.accounts_config = accounts_config
        self.passwords = passwords

    async def execute_on_all(self, signal: dict, lot_sizes: dict) -> List[dict]:
        """
        Execute trade on all enabled accounts in parallel.

        Args:
            signal: Trade signal dict with keys:
                - symbol: Trading symbol
                - direction: "BUY" or "SELL"
                - entry: Entry price
                - stop_loss: Stop loss price
                - take_profit: Take profit price
                - magic: Magic number
            lot_sizes: Dict mapping login numbers to lot sizes

        Returns:
            List of trade execution results (one per account)
        """
        loop = asyncio.get_event_loop()
        tasks = []

        for account in self.accounts_config:
            if not account.get("enabled", True):
                continue

            login = account["login"]
            lot_size = lot_sizes.get(login)

            if lot_size is None:
                continue

            task = loop.run_in_executor(
                None,
                self._execute_trade,
                account,
                signal,
                lot_size,
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        return [r for r in results if not isinstance(r, Exception)]

    def _execute_trade(self, account: dict, signal: dict, lot_size: float) -> dict:
        """
        Execute a single trade on an account (runs in thread pool).

        Args:
            account: Account configuration
            signal: Trade signal
            lot_size: Calculated lot size

        Returns:
            Trade execution result dict
        """
        login = account["login"]
        password = self.passwords.get(login)

        if not password:
            return {
                "login": login,
                "success": False,
                "error": "Password not found",
            }

        executor = TradeExecutor(account["mt5_path"])

        if not executor.connect(login, password, account["server"]):
            return {
                "login": login,
                "success": False,
                "error": "Connection failed",
            }

        try:
            result = executor.place_market_order(
                symbol=signal["symbol"],
                direction=signal["direction"],
                lot_size=lot_size,
                sl=signal["stop_loss"],
                tp=signal["take_profit"],
                magic=signal["magic"],
                slippage=signal.get("slippage", 50),
            )

            if result:
                return {
                    "login": login,
                    "success": True,
                    "ticket": result["ticket"],
                    "fill_price": result["fill_price"],
                }
            else:
                return {
                    "login": login,
                    "success": False,
                    "error": "Order placement failed",
                }
        finally:
            executor.disconnect()

    def get_account_balance(self, account_name: str) -> Optional[float]:
        """
        Get account balance by connecting and fetching it.

        Args:
            account_name: Account login identifier (can be login number or name)

        Returns:
            Account balance in USD, or None if failed
        """
        account = None
        for acc in self.accounts_config:
            if str(acc["login"]) == str(account_name) or acc.get("name") == account_name:
                account = acc
                break

        if not account:
            return None

        login = account["login"]
        password = self.passwords.get(login)

        if not password:
            return None

        executor = TradeExecutor(account["mt5_path"])

        if not executor.connect(login, password, account["server"]):
            return None

        try:
            import MetaTrader5 as mt5
            account_info = mt5.account_info()
            if account_info:
                return account_info.balance
            return None
        except Exception:
            return None
        finally:
            executor.disconnect()

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from utils.logger import get_logger

logger = get_logger("risk_engine")


@dataclass
class RiskState:
    balance: float
    equity: float
    daily_pnl: float               # positive = profit, negative = loss
    daily_loss_pct: float          # % of initial_balance lost today
    daily_budget_remaining: float  # USD left before daily limit hit
    open_position_count: int
    lot_multiplier: float          # 0.0 = hard stop, 1.0 = normal
    initial_balance: float


class RiskEngine:
    def __init__(self, config: dict):
        self.cfg = config.get("risk", {})
        self.daily_loss_limit_pct = self.cfg.get("daily_loss_limit_pct", 6.0)
        self.initial_balance = self.cfg.get("initial_balance", 5000.0)

    def get_state(self, executor) -> RiskState:
        """
        Read live account state from MT5 via the given executor.
        executor must already be connected.
        """
        try:
            import MetaTrader5 as mt5
            info = executor.get_account_info()
            if info is None:
                logger.warning("Could not fetch MT5 account info — using safe defaults")
                return self._safe_default()

            balance = info.balance
            equity = info.equity

            # Daily P&L: sum of closed deals since UTC midnight
            midnight = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            deals = executor.get_deals_since(midnight)
            daily_pnl = sum(getattr(d, 'profit', 0) for d in deals
                            if getattr(d, 'entry', None) == 1)  # entry=1 = deal out (close)

            daily_budget_total = self.initial_balance * self.daily_loss_limit_pct / 100
            daily_budget_remaining = daily_budget_total + daily_pnl
            daily_loss_pct = (-daily_pnl / self.initial_balance * 100) if daily_pnl < 0 else 0.0

            positions = executor.get_open_positions(magic=mt5.symbol_info("XAUUSD") and None)
            open_count = len(positions)

            multiplier = self._lot_multiplier(daily_loss_pct, daily_budget_remaining)

            return RiskState(
                balance=balance,
                equity=equity,
                daily_pnl=daily_pnl,
                daily_loss_pct=daily_loss_pct,
                daily_budget_remaining=max(0.0, daily_budget_remaining),
                open_position_count=open_count,
                lot_multiplier=multiplier,
                initial_balance=self.initial_balance,
            )
        except Exception as e:
            logger.error(f"RiskEngine.get_state error: {e}")
            return self._safe_default()

    def _lot_multiplier(self, daily_loss_pct: float, budget_remaining: float) -> float:
        if budget_remaining <= 0 or daily_loss_pct >= self.daily_loss_limit_pct - 0.5:
            return 0.0   # within 0.5% of limit → hard stop
        return 1.0       # position sizer handles natural scaling via budget formula

    def _safe_default(self) -> RiskState:
        return RiskState(
            balance=self.initial_balance,
            equity=self.initial_balance,
            daily_pnl=0.0,
            daily_loss_pct=0.0,
            daily_budget_remaining=self.initial_balance * self.daily_loss_limit_pct / 100,
            open_position_count=0,
            lot_multiplier=0.5,  # conservative when we can't read account
            initial_balance=self.initial_balance,
        )

    def get_state_dry_run(self, daily_pnl: float = 0.0) -> RiskState:
        """State for dry_run mode — uses config values, no MT5."""
        daily_budget_total = self.initial_balance * self.daily_loss_limit_pct / 100
        daily_budget_remaining = daily_budget_total + daily_pnl
        daily_loss_pct = (-daily_pnl / self.initial_balance * 100) if daily_pnl < 0 else 0.0
        multiplier = self._lot_multiplier(daily_loss_pct, daily_budget_remaining)
        return RiskState(
            balance=self.initial_balance,
            equity=self.initial_balance + daily_pnl,
            daily_pnl=daily_pnl,
            daily_loss_pct=daily_loss_pct,
            daily_budget_remaining=max(0.0, daily_budget_remaining),
            open_position_count=0,
            lot_multiplier=multiplier,
            initial_balance=self.initial_balance,
        )

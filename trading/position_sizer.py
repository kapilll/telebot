def calculate_lot_size(
    balance: float,
    risk_percent: float,
    entry: float,
    stop_loss: float,
    symbol: str,
    min_lot: float,
    max_lot: float,
    lot_step: float,
) -> float:
    """
    Calculate lot size based on risk management.

    For XAUUSD (gold): 1 lot = 100 oz, $1 price move = $100 per lot
    For other symbols: uses fallback formula

    Args:
        balance: Account balance in USD
        risk_percent: Risk percentage (e.g., 2 for 2%)
        entry: Entry price
        stop_loss: Stop loss price
        symbol: Trading symbol (e.g., "XAUUSD")
        min_lot: Minimum lot size allowed
        max_lot: Maximum lot size allowed
        lot_step: Lot size step (e.g., 0.01)

    Returns:
        Calculated and rounded lot size, clamped to [min_lot, max_lot]
    """
    risk_usd = balance * risk_percent / 100
    sl_distance = abs(entry - stop_loss)

    if symbol.upper() == "XAUUSD":
        lot_size = risk_usd / (sl_distance * 100)
    else:
        lot_size = risk_usd / (sl_distance * 100)

    rounded_lot_size = round(lot_size / lot_step) * lot_step

    clamped_lot_size = max(min_lot, min(rounded_lot_size, max_lot))

    return clamped_lot_size

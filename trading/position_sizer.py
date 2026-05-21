def calculate_lot_size(
    balance: float,
    daily_loss_limit_pct: float,
    daily_pnl: float,
    entry: float,
    stop_loss: float,
    min_lot: float,
    max_lot: float,
    lot_step: float,
    min_remaining_chances: int = 3,
) -> float:
    """
    Size lots so that min_remaining_chances more SL hits still fit in the daily budget.
    daily_pnl: positive = profit today, negative = loss today.
    """
    sl_distance = abs(entry - stop_loss)
    if sl_distance == 0:
        raise ValueError(f"SL distance is zero: entry={entry} stop_loss={stop_loss}")

    daily_budget_total = balance * daily_loss_limit_pct / 100
    daily_budget_remaining = daily_budget_total + daily_pnl  # daily_pnl negative shrinks this

    if daily_budget_remaining <= 0:
        return 0.0

    risk_per_trade = daily_budget_remaining / min_remaining_chances

    # XAUUSD: 1 lot = $100 per 1-unit price move
    lot_size = risk_per_trade / (sl_distance * 100)
    lot_size = round(lot_size / lot_step) * lot_step
    return max(min_lot, min(lot_size, max_lot))


def adjust_lots_for_tp_split(
    base_lot: float,
    weights: list,
    min_lot: float,
    lot_step: float,
) -> list:
    """
    Split base_lot across TP levels by weight.
    Drops the lowest-weight TPs if their sub-lot falls below min_lot.
    Returns list of (tp_index, sub_lot) pairs.
    """
    active = list(enumerate(weights))
    while active:
        sub_lots = [
            (i, max(min_lot, round(base_lot * w / lot_step) * lot_step))
            for i, w in active
        ]
        if all(sl >= min_lot for _, sl in sub_lots):
            return sub_lots
        active = active[:-1]  # drop lowest-weight TP
    return [(0, min_lot)]

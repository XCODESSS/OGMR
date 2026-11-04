"""Whole-lot sizing and long-option premium accounting; no broker operations."""
from decimal import Decimal

from .model import TradingError, number
from .options_model import OptionsConfig


def _quantity(value):
    if type(value) is not int or value <= 0:
        raise TradingError('Quantity/lot size must be a positive integer')


def trade_amounts(entry_fill: Decimal, exit_fill: Decimal, quantity: int,
                  config: OptionsConfig) -> tuple[Decimal, Decimal, Decimal]:
    _quantity(quantity)
    entry_fill = number(entry_fill, positive=True)
    exit_fill = number(exit_fill, positive=True)
    gross = quantity * (exit_fill - entry_fill)
    fees = (quantity * (entry_fill + exit_fill) * number(config.fee_bps) / 10000
            + 2 * number(config.fixed_fee_per_order))
    return gross, fees, gross - fees


def size_lots(equity: Decimal, entry_fill: Decimal, lot_size: int,
              config: OptionsConfig) -> int:
    _quantity(lot_size)
    equity, entry_fill = number(equity, positive=True), number(entry_fill, positive=True)
    stop = entry_fill * (1 - number(config.stop_fraction))
    fixed = 2 * number(config.fixed_fee_per_order)
    fees_per_lot = lot_size * (entry_fill + stop) * number(config.fee_bps) / 10000
    risk_per_lot = lot_size * (entry_fill - stop) + fees_per_lot
    debit_per_lot = lot_size * entry_fill + fees_per_lot
    risk_budget = equity * number(config.risk_fraction) - fixed
    debit_budget = min(equity, equity * number(config.premium_cap_fraction)) - fixed
    if risk_budget <= 0 or debit_budget <= 0:
        return 0
    # Both inequalities are affine in lots. Solve directly rather than loop
    # over potentially millions of unaffordable lots.
    return max(0, min(int(risk_budget / risk_per_lot), int(debit_budget / debit_per_lot)))

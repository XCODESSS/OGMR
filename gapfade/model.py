from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")
ZERO = Decimal("0")


class TradingError(Exception):
    """An explicit data, state, or broker failure; never silently skip it."""


def number(value, *, positive=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TradingError("Invalid numeric value") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise TradingError("Expected a finite" + (" positive" if positive else "") + " number")
    return result


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TradingError(f"Invalid ISO timestamp: {value}") from exc
    if result.tzinfo is None:
        raise TradingError("Timestamps must include a UTC offset")
    return result.astimezone(IST)


def at(day: date, clock: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), IST)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    start: date
    end: date
    long_allowed: bool
    short_allowed: bool

    def active(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class Reference:
    session: date
    symbol: str
    previous_session: date
    reference_close: Decimal
    official_open: Decimal
    available_at: datetime
    verified: bool


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: int
    gap: Decimal


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    open: Decimal
    previous_close: Decimal


def fill_price(price: Decimal, side: int, slippage_bps: Decimal) -> Decimal:
    return price * (1 + side * slippage_bps / 10000)


def size_position(equity: Decimal, price: Decimal, weight: Decimal,
                  fee_bps: Decimal, cap: Decimal | None = None) -> int:
    # Reserve both legs' estimated charges; never spend short-sale proceeds.
    budget = equity * weight
    if cap is not None:
        budget = min(budget, cap)
    return int(budget / (price * (1 + 2 * fee_bps / 10000)))

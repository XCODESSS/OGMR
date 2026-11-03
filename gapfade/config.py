from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path

from .model import TradingError, at, number
from datetime import date


@dataclass(frozen=True)
class Config:
    capital: str = "1000000"
    gap_threshold: str = "0.02"
    position_weight: str = "0.10"
    max_positions: int = 10
    entry_time: str = "09:16:00"
    exit_time: str = "15:00:00"
    entry_window_seconds: int = 20
    fee_bps_per_leg: str = "10"
    slippage_bps_per_leg: str = "5"
    max_daily_loss_fraction: str = "0.01"
    max_quote_age_seconds: int = 10
    max_spread_bps: str = "30"
    poll_seconds: int = 5
    allow_live: bool = False
    live_capital: str = "0"
    live_max_order_notional: str = "0"
    live_readiness_ack: bool = False

    def __post_init__(self):
        number(self.capital, positive=True)
        for name in ("gap_threshold", "position_weight", "max_daily_loss_fraction"):
            if not 0 < number(getattr(self, name)) <= 1:
                raise TradingError(f"{name} must be in (0, 1]")
        for name in ("fee_bps_per_leg", "slippage_bps_per_leg", "max_spread_bps",
                     "live_capital", "live_max_order_notional"):
            if number(getattr(self, name)) < 0:
                raise TradingError(f"{name} cannot be negative")
        if number(self.slippage_bps_per_leg) >= 10000:
            raise TradingError("Slippage must be below 10000 bps")
        if type(self.max_positions) is not int or not 1 <= self.max_positions <= 10:
            raise TradingError("max_positions must be an integer from 1 to 10")
        if self.max_positions * number(self.position_weight) > 1:
            raise TradingError("Gross allocation cannot exceed 100%")
        for name in ("allow_live", "live_readiness_ack"):
            if type(getattr(self, name)) is not bool:
                raise TradingError(f"{name} must be a JSON boolean")
        for name in ("entry_window_seconds", "max_quote_age_seconds", "poll_seconds"):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 60:
                raise TradingError(f"{name} must be an integer from 1 to 60")
        if self.poll_seconds < 2:
            raise TradingError("poll_seconds must be at least 2")
        day = date(2026, 1, 1)
        entry, exit_ = at(day, self.entry_time), at(day, self.exit_time)
        if entry.second or entry.microsecond or exit_.second or exit_.microsecond:
            raise TradingError("Entry and exit must be minute-aligned")
        if not at(day, "09:16") <= entry < exit_ <= at(day, "15:00"):
            raise TradingError("Use 09:16 <= entry < exit <= 15:00 IST")

    @classmethod
    def load(cls, path):
        obj = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict) or set(obj) - {f.name for f in fields(cls)}:
            raise TradingError("Invalid config or unknown config keys")
        return cls(**obj)

    def snapshot(self):
        return asdict(self)

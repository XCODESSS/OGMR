"""Domain records for offline, long-premium NIFTY research."""
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path

from .model import TradingError, number

VERSION = 'nifty-gap-fade-options-v1'


@dataclass(frozen=True)
class OptionsConfig:
    capital: str = '100000'
    gap_threshold: str = '0.005'
    confirmation: bool = True
    stop_fraction: str = '0.25'
    risk_fraction: str = '0.005'
    premium_cap_fraction: str = '0.02'
    slippage_bps: str = '25'
    fee_bps: str = '10'
    fixed_fee_per_order: str = '20'

    def __post_init__(self):
        number(self.capital, positive=True)
        if type(self.confirmation) is not bool:
            raise TradingError('confirmation must be a boolean')
        for name in ('gap_threshold', 'stop_fraction', 'risk_fraction', 'premium_cap_fraction'):
            if not 0 < number(getattr(self, name)) <= 1:
                raise TradingError(f'{name} must be in (0, 1]')
        if number(self.risk_fraction) > number(self.premium_cap_fraction):
            raise TradingError('risk_fraction cannot exceed premium_cap_fraction')
        for name in ('slippage_bps', 'fee_bps', 'fixed_fee_per_order'):
            if number(getattr(self, name)) < 0:
                raise TradingError(f'{name} cannot be negative')
        if number(self.slippage_bps) >= 10000:
            raise TradingError('slippage_bps must be below 10000')

    @classmethod
    def load(cls, path):
        obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(obj, dict) or set(obj) - set(cls.__dataclass_fields__):
            raise TradingError('Invalid or unknown options configuration fields')
        return cls(**obj)

    def snapshot(self):
        return asdict(self)


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    option_type: str
    strike: Decimal
    expiry_session: date
    listed_at: datetime
    valid_from: date
    valid_to: date
    lot_size: int


@dataclass(frozen=True)
class SpotReference:
    session: date
    previous_session: date
    official_open: Decimal
    previous_close: Decimal
    available_at: datetime
    verified: bool


@dataclass(frozen=True)
class OptionSignal:
    session: date
    option_type: str
    gap: Decimal
    decision_at: datetime
    spot_at_decision: Decimal
    target: Decimal
    failure_level: Decimal


@dataclass(frozen=True)
class SessionDecision:
    signal: OptionSignal | None
    reason: str

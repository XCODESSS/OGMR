from datetime import date

from .config import Config
from .model import Instrument, Reference, Signal, TradingError, at, number


def select(day: date, previous: date, instruments: list[Instrument],
           refs: dict[tuple, Reference], config: Config) -> list[Signal]:
    selected = []
    for instrument in instruments:
        if not instrument.active(day):
            continue
        ref = refs.get((day, instrument.symbol))
        if ref is None:
            raise TradingError(f"Missing reference for {day} {instrument.symbol}")
        if not ref.verified or ref.previous_session != previous:
            raise TradingError(f"Unverified or wrong previous session: {instrument.symbol} {day}")
        if not at(day, "09:15") <= ref.available_at < at(day, config.entry_time):
            raise TradingError(f"Signal not available before entry: {instrument.symbol} {day}")
        gap = ref.official_open / ref.reference_close - 1
        if abs(gap) < number(config.gap_threshold):
            continue
        side = -1 if gap > 0 else 1
        if (side == 1 and instrument.long_allowed) or (side == -1 and instrument.short_allowed):
            selected.append(Signal(instrument.symbol, side, gap))
    return sorted(selected, key=lambda x: (-abs(x.gap), x.symbol))[:config.max_positions]

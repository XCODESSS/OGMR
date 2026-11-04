"""Spot reversal decisions and point-in-time selection, independent of premium paths."""
from datetime import timedelta

from .model import TradingError, at, number
from .options_model import OptionSignal, SessionDecision


def decide(reference, opening_bars, config):
    day = reference.session
    if (not reference.verified or reference.previous_session >= day
        or reference.available_at.date() != day or reference.available_at >= at(day, '09:20')):
        raise TradingError(f'Unverified or late reference: {day}')
    expected = [at(day, '09:15') + timedelta(minutes=i) for i in range(5)]
    if (len(opening_bars) != 5 or any(bar is None for bar in opening_bars)
        or [bar.timestamp for bar in opening_bars] != expected
        or any(bar.symbol != 'NIFTY' for bar in opening_bars)):
        raise TradingError(f'Missing/invalid opening spot window: {day}')
    gap = reference.official_open / reference.previous_close - 1
    if abs(gap) < number(config.gap_threshold):
        return SessionDecision(None, 'below_gap_threshold')
    opening, close = opening_bars[0].open, opening_bars[-1].close
    bullish = gap < 0
    if (bullish and close >= reference.previous_close) or (not bullish and close <= reference.previous_close):
        return SessionDecision(None, 'already_filled')
    if config.confirmation and not (close > opening if bullish else close < opening):
        return SessionDecision(None, 'unconfirmed')
    failure = min(bar.low for bar in opening_bars) if bullish else max(bar.high for bar in opening_bars)
    return SessionDecision(OptionSignal(day, 'CE' if bullish else 'PE', gap, at(day, '09:20'),
                                       close, reference.previous_close, failure), 'signal')


def choose_contract(signal, contracts, sessions):
    if not sessions or any(c.expiry_session not in sessions for c in contracts):
        raise TradingError('Insufficient future exchange calendar for contract expiries')
    candidates, seen = [], set()
    for contract in contracts:
        if (contract.underlying != 'NIFTY' or contract.option_type != signal.option_type
            or not contract.valid_from <= signal.session <= contract.valid_to
            or contract.listed_at > signal.decision_at):
            continue
        identity = contract.option_type, contract.strike, contract.expiry_session
        if identity in seen:
            raise TradingError('Duplicate active contract series')
        seen.add(identity)
        if sum(signal.session < day <= contract.expiry_session for day in sessions) >= 2:
            candidates.append(contract)
    if not candidates:
        raise TradingError(f'No eligible historical contract: {signal.session} {signal.option_type}')
    return min(candidates, key=lambda c: (c.expiry_session, abs(c.strike - signal.spot_at_decision), c.strike))

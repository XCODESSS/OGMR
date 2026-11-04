from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import unittest

from gapfade.model import Bar, TradingError, at
from gapfade.options_model import OptionContract, OptionsConfig, SpotReference
from gapfade.options_strategy import choose_contract, decide
from options_fixtures import DAY


class OptionsStrategyTests(unittest.TestCase):
    def ref(self, opening='99'):
        return SpotReference(DAY, date(2026, 1, 2), D(opening), D('100'), at(DAY, '09:16'), True)

    def bars(self, opening='99', close='99.2'):
        return [Bar(at(DAY, f'09:{m}'), 'NIFTY', D(opening), max(D(opening), D(close)) + D('.1'),
                    min(D(opening), D(close)) - D('.1'), D(close), D('0')) for m in range(15, 20)]

    def test_inclusive_threshold_and_directions(self):
        for opening, close, kind in [('99.5', '99.7', 'CE'), ('100.5', '100.3', 'PE')]:
            result = decide(self.ref(opening), self.bars(opening, close), OptionsConfig())
            self.assertEqual(result.signal.option_type, kind)
            self.assertEqual(result.signal.spot_at_decision, D(close))
        self.assertEqual(decide(self.ref('99.51'), self.bars(), OptionsConfig()).reason,
                         'below_gap_threshold')

    def test_confirmation_and_baseline(self):
        for close in ('98.9', '99'):
            self.assertEqual(decide(self.ref(), self.bars(close=close), OptionsConfig()).reason, 'unconfirmed')
            self.assertIsNotNone(decide(self.ref(), self.bars(close=close),
                                       replace(OptionsConfig(), confirmation=False)).signal)
        for confirm in (True, False):
            self.assertEqual(decide(self.ref(), self.bars(close='100'),
                                    replace(OptionsConfig(), confirmation=confirm)).reason, 'already_filled')

    def test_missing_or_late_information(self):
        for bars, ref in [(self.bars()[:-1], self.ref()),
                          (self.bars(), replace(self.ref(), available_at=at(DAY, '09:20'))),
                          ([None] * 5, self.ref())]:
            with self.assertRaises(TradingError):
                decide(ref, bars, OptionsConfig())

    def contract(self, strike='100', **kwargs):
        return replace(OptionContract('DEMO:' + strike, 'NIFTY', 'CE', D(strike), date(2026, 1, 9),
            at(date(2026, 1, 1), '00:00'), date(2026, 1, 1), date(2026, 1, 9), 10), **kwargs)

    def test_nearest_expiry_strike_tie_and_listing(self):
        signal = replace(decide(self.ref(), self.bars(), OptionsConfig()).signal, spot_at_decision=D('100'))
        sessions = [DAY + timedelta(days=i) for i in range(5)]
        low, high = self.contract('99'), self.contract('101')
        late = self.contract('100', listed_at=at(DAY, '09:21'))
        self.assertEqual(choose_contract(signal, [high, late, low], sessions), low)
        too_soon = self.contract('100', expiry_session=DAY + timedelta(days=1))
        self.assertEqual(choose_contract(signal, [too_soon, low], sessions), low)
        with self.assertRaises(TradingError):
            choose_contract(signal, [low, low], sessions)
        with self.assertRaises(TradingError):
            choose_contract(signal, [late], sessions)

    def test_historical_lot_effective_dates_and_calendar_horizon(self):
        signal = decide(self.ref(), self.bars(), OptionsConfig()).signal
        prior = self.contract(valid_to=date(2026, 1, 4), lot_size=20)
        active = self.contract(valid_from=DAY, lot_size=10)
        sessions = [DAY + timedelta(days=i) for i in range(5)]
        self.assertEqual(choose_contract(signal, [prior, active], sessions).lot_size, 10)
        with self.assertRaises(TradingError):
            choose_contract(signal, [active], sessions[:-1])


if __name__ == '__main__':
    unittest.main()

from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import unittest

from gapfade.config import Config
from gapfade.model import Instrument, Reference, TradingError, at, size_position
from gapfade.strategy import select

DAY, PREV = date(2026, 1, 5), date(2026, 1, 2)


class StrategyTests(unittest.TestCase):
    def refs(self, prices):
        return {(DAY, s): Reference(DAY, s, PREV, D("100"), D(str(p)), at(DAY, "09:15:30"), True)
                for s, p in prices.items()}

    def instruments(self, names):
        return [Instrument(n, PREV, DAY, True, True) for n in names]

    def test_inclusive_threshold_and_directions(self):
        prices = {"NSE:A-EQ": 98, "NSE:B-EQ": 102, "NSE:C-EQ": "101.99"}
        result = select(DAY, PREV, self.instruments(prices), self.refs(prices), Config())
        self.assertEqual([(s.symbol, s.side) for s in result], [("NSE:A-EQ", 1), ("NSE:B-EQ", -1)])

    def test_ranking_ties_and_cap(self):
        prices = {f"NSE:{n}-EQ": p for n, p in [("C", 95), ("B", 105), ("A", 103)]}
        result = select(DAY, PREV, self.instruments(prices), self.refs(prices), Config(max_positions=2))
        self.assertEqual([s.symbol for s in result], ["NSE:B-EQ", "NSE:C-EQ"])

    def test_missing_or_unverified_reference_fails_entire_ranking(self):
        names = ["NSE:A-EQ", "NSE:B-EQ"]
        with self.assertRaises(TradingError):
            select(DAY, PREV, self.instruments(names), self.refs({names[0]: 97}), Config())
        refs = self.refs({names[0]: 97})
        refs[DAY, names[0]] = replace(refs[DAY, names[0]], verified=False)
        with self.assertRaises(TradingError):
            select(DAY, PREV, self.instruments(names[:1]), refs, Config())

    def test_no_same_time_signal_and_fill(self):
        refs = self.refs({"NSE:A-EQ": 97})
        refs[DAY, "NSE:A-EQ"] = replace(refs[DAY, "NSE:A-EQ"], available_at=at(DAY, "09:16"))
        with self.assertRaises(TradingError):
            select(DAY, PREV, self.instruments(["NSE:A-EQ"]), refs, Config())

    def test_short_permission_and_historical_membership(self):
        names = ["NSE:A-EQ"]
        instrument = replace(self.instruments(names)[0], short_allowed=False)
        self.assertEqual(select(DAY, PREV, [instrument], self.refs({names[0]: 103}), Config()), [])
        instrument = replace(instrument, start=date(2026, 1, 6))
        self.assertEqual(select(DAY, PREV, [instrument], {}, Config()), [])

    def test_size_reserves_costs_and_rejects_leverage_config(self):
        self.assertEqual(size_position(D("1000"), D("100"), D("1"), D("10")), 9)
        with self.assertRaises(TradingError):
            Config(position_weight="0.2", max_positions=10)
        with self.assertRaises(TradingError):
            Config(allow_live="false")


if __name__ == "__main__":
    unittest.main()

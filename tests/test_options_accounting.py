from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from gapfade.model import TradingError
from gapfade.options_model import OptionsConfig
from gapfade.options_accounting import size_lots, trade_amounts


class OptionsAccountingTests(unittest.TestCase):
    def test_lots_reserve_costs(self):
        self.assertEqual(size_lots(D('100000'), D('100'), 10, OptionsConfig()), 1)

    def test_pe_uses_long_premium_accounting(self):
        self.assertEqual(trade_amounts(D('100'), D('120'), 10, OptionsConfig()),
                         (D('200'), D('42.2'), D('157.8')))

    def test_one_lot_unaffordable(self):
        self.assertEqual(size_lots(D('1000'), D('100'), 10, OptionsConfig()), 0)

    def test_exact_boundary_and_cost_constraints(self):
        config = replace(OptionsConfig(), fee_bps='0', fixed_fee_per_order='0')
        self.assertEqual(size_lots(D('100000'), D('100'), 10, config), 2)
        self.assertEqual(size_lots(D('100000'), D('100'), 10,
                                   replace(config, fixed_fee_per_order='1000')), 0)

    def test_invalid_parameters(self):
        for kwargs in ({'capital': 'NaN'}, {'confirmation': 'true'},
                       {'slippage_bps': '10000'}, {'stop_fraction': '0'},
                       {'risk_fraction': '0.1'}, {'fee_bps': '-1'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TradingError):
                OptionsConfig(**kwargs)
        for size in (True, 0, -1, 1.5):
            with self.subTest(size=size), self.assertRaises(TradingError):
                size_lots(D('100000'), D('100'), size, OptionsConfig())

    def test_config_rejects_unknown_fields_and_zero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({'capital': '100000', 'live': True}))
            with self.assertRaises(TradingError):
                OptionsConfig.load(path)
        with self.assertRaises(TradingError):
            trade_amounts(D('100'), D('0'), 10, OptionsConfig())


if __name__ == '__main__':
    unittest.main()

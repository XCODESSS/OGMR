from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from gapfade import data
from gapfade.model import TradingError
from gapfade.options_model import OptionsConfig
from gapfade.options_backtest import run
from options_fixtures import DAY, BAR_FIELDS, CONTRACT_FIELDS, candle, fixture


class OptionsBacktestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = self.root / 'inputs'
        fixture(self.inputs)
        self.config = replace(OptionsConfig(), fee_bps='0', fixed_fee_per_order='0', slippage_bps='0')

    def replay(self, name='result', config=None):
        return run(config or self.config, self.inputs, DAY, DAY, self.root / name)

    def change(self, kind, edit):
        path = self.inputs / kind / f'{DAY}.csv'
        records = data.rows(path)
        edited = edit(records)
        data.write_csv(path, records if edited is None else edited, BAR_FIELDS)

    def trade(self):
        return data.rows(self.root / 'result' / 'trades.csv')[0]

    def test_ce_next_open_and_reproducibility(self):
        summary = self.replay()
        trade = self.trade()
        self.assertIn('09:21:00', trade['entry_time'])
        self.assertIn('09:22:00', trade['trigger_bar_time'])
        self.assertIn('09:23:00', trade['trigger_time'])
        self.assertIn('09:23:00', trade['exit_time'])
        self.assertEqual(trade['exit_reason'], 'gap_fill')
        self.assertEqual(D(trade['net_pnl']), D('400'))
        self.assertEqual(int(trade['quantity']), int(trade['lots']) * int(trade['lot_size']))
        self.assertEqual(D(summary['final_equity']), D('100400'))
        self.assertEqual(summary, self.replay('second'))
        for name in ('trades.csv', 'daily_equity.csv', 'manifest.json'):
            self.assertEqual((self.root / 'result' / name).read_bytes(),
                             (self.root / 'second' / name).read_bytes())

    def test_pe_can_lose_while_spot_falls(self):
        path = self.inputs / 'references.csv'
        rows = data.rows(path)
        rows[0]['official_open'] = '101'
        data.write_csv(path, rows, list(rows[0]))
        spot = [candle(f'09:{m}', open='101', high='101.1', low='100.7', close='100.8') for m in range(15, 20)]
        spot += [candle('09:20', open='100.8', high='100.9', low='100.7', close='100.8'),
                 candle('09:21', open='100.8', high='100.9', low='100.7', close='100.8'),
                 candle('09:22', open='100.5', high='100.6', low='99.9', close='100')]
        data.write_csv(self.inputs / 'spot' / f'{DAY}.csv', spot, BAR_FIELDS)
        self.change('options', lambda rows: [dict(r, symbol='DEMO:PE', open='90' if i == 2 else '100',
                                                 low='89', high='110', close='100') for i, r in enumerate(rows)])
        self.replay()
        self.assertEqual(self.trade()['option_type'], 'PE')
        self.assertLess(D(self.trade()['net_pnl']), 0)

    def test_premium_stop_gapped_fill_and_multiple_flags(self):
        def edit(rows):
            rows[1]['low'] = '70'
            rows[2].update(open='60', low='59', close='60')
        self.change('options', edit)
        self.replay()
        trade = self.trade()
        self.assertEqual(trade['exit_reason'], 'premium_stop')
        self.assertEqual(trade['multiple_triggers'], 'True')
        self.assertLess(D(trade['exit_price']), D('75'))

    def test_spot_failure(self):
        self.change('spot', lambda rows: [dict(r, low='98.8', high='99.3') if '09:22:' in r['timestamp'] else r for r in rows])
        self.replay()
        self.assertEqual(self.trade()['exit_reason'], 'spot_failure')

    def test_missing_exit_preserves_unresolved_position(self):
        self.change('options', lambda rows: rows[:-1])
        with self.assertRaises(TradingError):
            self.replay()
        result = self.root / 'result'
        self.assertFalse((result / 'summary.json').exists())
        self.assertTrue(json.loads((result / 'run_status.json').read_text())['position_open'])
        self.assertEqual(json.loads((result / 'unresolved_position.json').read_text())['symbol'], 'DEMO:CE')
        self.assertEqual(data.rows(result / 'coverage.csv')[0]['status'], 'invalid')

    def test_missing_entry_or_zero_volume_is_invalid(self):
        original = data.rows(self.inputs / 'options' / f'{DAY}.csv')
        for records in (original[1:], [dict(original[0], volume=0)] + original[1:]):
            data.write_csv(self.inputs / 'options' / f'{DAY}.csv', records, BAR_FIELDS)
            with self.assertRaises(TradingError):
                self.replay(str(len(list(self.root.iterdir()))))

    def test_target_before_entry_and_insufficient_capital(self):
        self.change('spot', lambda rows: [dict(r, high='100') if '09:20:' in r['timestamp'] else r for r in rows])
        self.assertEqual(self.replay()['trade_count'], 0)
        self.assertEqual(data.rows(self.root / 'result/daily_equity.csv')[0]['reason'], 'target_before_entry')
        fixture(self.inputs)
        self.assertEqual(self.replay('small', replace(self.config, capital='1000'))['trade_count'], 0)
        self.assertEqual(data.rows(self.root / 'small/daily_equity.csv')[0]['reason'], 'insufficient_capital')

    def test_special_and_no_signal_days_do_not_need_option_prices(self):
        path = self.inputs / 'sessions.csv'
        rows = data.rows(path)
        rows[0]['is_regular'] = 'false'
        data.write_csv(path, rows, list(rows[0]))
        self.assertEqual(self.replay()['special_sessions'], 1)
        rows[0]['is_regular'] = 'true'
        data.write_csv(path, rows, list(rows[0]))
        ref = self.inputs / 'references.csv'
        refs = data.rows(ref)
        refs[0]['official_open'] = '99.9'
        data.write_csv(ref, refs, list(refs[0]))
        (self.inputs / 'options' / f'{DAY}.csv').unlink()
        self.assertEqual(self.replay('none')['no_signal_sessions'], 1)

    def test_scheduled_exit_and_last_minute_trigger(self):
        from datetime import timedelta
        from gapfade.model import at
        spot, options = [], []
        current = at(DAY, '09:15')
        while current <= at(DAY, '15:00'):
            clock = current.strftime('%H:%M')
            spot.append(candle(clock, open='99', low='98.9') if clock < '09:20' else candle(clock))
            if clock >= '09:21':
                options.append(candle(clock, 'DEMO:CE', '100', '110', '99', '100', 100))
            current += timedelta(minutes=1)
        data.write_csv(self.inputs / 'spot' / f'{DAY}.csv', spot, BAR_FIELDS)
        data.write_csv(self.inputs / 'options' / f'{DAY}.csv', options, BAR_FIELDS)
        self.replay()
        self.assertEqual(self.trade()['exit_reason'], 'scheduled_exit')
        self.assertIn('15:00:00', self.trade()['exit_time'])
        self.change('spot', lambda rows: [dict(r, high='100') if '14:59:' in r['timestamp'] else r for r in rows])
        self.replay('last')
        trade = data.rows(self.root / 'last/trades.csv')[0]
        self.assertEqual(trade['exit_reason'], 'gap_fill')
        self.assertIn('15:00:00', trade['exit_time'])

    def test_atm_does_not_use_entry_spot_and_outputs_never_overwrite(self):
        self.change('spot', lambda rows: [dict(r, open='99.9', high='99.99', low='99.1')
                                         if '09:21:' in r['timestamp'] else r for r in rows])
        self.replay()
        self.assertEqual(self.trade()['spot_at_decision'], '99.2')
        with self.assertRaises((TradingError, FileExistsError)):
            self.replay()

    def test_missing_holding_spot_retains_position_and_early_exit_needs_no_later_bars(self):
        # Fixture stops at09:23; a successful early exit never needs14:00 bars.
        self.assertEqual(self.replay('early')['trade_count'], 1)
        self.change('spot', lambda rows: [r for r in rows if '09:22:' not in r['timestamp']])
        with self.assertRaises(TradingError):
            self.replay()
        self.assertTrue((self.root / 'result/unresolved_position.json').exists())
        coverage = data.rows(self.root / 'result/coverage.csv')[0]
        self.assertIn('09:22:00', coverage['first_missing_timestamp'])

    def test_later_day_failure_preserves_completed_trades(self):
        from gapfade.options_demo import create
        from datetime import date
        demo_root = self.root / 'demo'
        create(demo_root)
        second_day = demo_root / 'options/2026-01-06.csv'
        records = data.rows(second_day)
        data.write_csv(second_day, [r for r in records if '09:23:' not in r['timestamp']], BAR_FIELDS)
        with self.assertRaises(TradingError):
            run(self.config, demo_root, DAY, date(2026, 1, 9), self.root / 'result')
        self.assertEqual(len(data.rows(self.root / 'result/trades.csv')), 1)
        self.assertEqual([r['status'] for r in data.rows(self.root / 'result/coverage.csv')],
                         ['traded', 'invalid', 'not_processed', 'not_processed', 'not_processed'])
        self.assertFalse((self.root / 'result/summary.json').exists())

    def test_missing_selected_contract_never_uses_a_different_strike(self):
        path = self.inputs / 'contracts.csv'
        contracts = data.rows(path)
        contracts.append(dict(contracts[0], symbol='DEMO:OTHER', strike='90'))
        data.write_csv(path, contracts, CONTRACT_FIELDS)
        self.change('options', lambda rows: [dict(r, symbol='DEMO:OTHER') for r in rows])
        with self.assertRaisesRegex(TradingError, 'Missing/untradable'):
            self.replay()
        coverage = data.rows(self.root / 'result/coverage.csv')[0]
        self.assertEqual(coverage['selected_symbol'], 'DEMO:CE')
        self.assertEqual(coverage['status'], 'invalid')


if __name__ == '__main__':
    unittest.main()

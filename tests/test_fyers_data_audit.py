import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

from gapfade.fyers_data_audit import inventory_sources, profile_candles, reconcile_captures, audit_sources
from gapfade.model import IST, TradingError
from gapfade.options_data import OptionsDataset


FIELDS = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
STAMP = int(datetime(2026, 11, 2, 9, 15, tzinfo=IST).timestamp())


def candle(stamp=STAMP, volume=10):
    return [stamp, 100, 102, 99, 101, volume]


def export(root, name, symbol, records):
    obj = dict(s='ok', candles=records)
    if name != 'spot':
        obj.update(symbol=symbol, columns=FIELDS)
    (root / f'{name}_raw.json').write_text(json.dumps(obj))
    with (root / f'{name}.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['timestamp_ist', *FIELDS])
        for row in records:
            writer.writerow([datetime.fromtimestamp(row[0], IST).isoformat(), *row])


class DataAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sample, self.history = self.root / 'sample', self.root / 'history'
        self.sample.mkdir()
        self.history.mkdir()
        self.symbols = dict(CE='NSE:NIFTY26N0323250CE', PE='NSE:NIFTY26N0323250PE')
        for root in (self.sample, self.history):
            for side, symbol in self.symbols.items():
                export(root, side.lower(), symbol, [candle()])
        export(self.sample, 'spot', 'NIFTY', [candle(volume=0)])
        (self.sample / 'contracts.json').write_text(json.dumps(dict(s='ok', data=dict(
            expiry_date='2026-11-03', contracts=dict(options=list(self.symbols.values()))))))
        (self.sample / 'expiry_dates.json').write_text(json.dumps(dict(s='ok')))
        (self.sample / 'manifest.json').write_text(json.dumps(dict(expiry='2026-11-03', strike=23250,
            session='2026-11-02', options={side: dict(symbol=s) for side, s in self.symbols.items()},
            sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.sample.iterdir()})))
        (self.history / 'manifest.json').write_text(json.dumps(dict(requested_from='2026-10-01',
            requested_to='2026-11-03', contracts={side: dict(symbol=s) for side, s in self.symbols.items()})))

    def test_inventory_hashes_missing_and_escape(self):
        inventory = inventory_sources(self.sample, self.history)
        self.assertTrue(any(r['baseline_status'] == 'baseline_not_supplied' for r in inventory))
        path = self.sample / 'manifest.json'
        original = path.read_text()
        obj = json.loads(original)
        obj['sha256']['ce_raw.json'] = '0' * 64
        path.write_text(json.dumps(obj))
        with self.assertRaisesRegex(TradingError, 'source_hash_mismatch'):
            inventory_sources(self.sample, self.history)
        obj['sha256'] = {'../escape': '0' * 64}
        path.write_text(json.dumps(obj))
        with self.assertRaisesRegex(TradingError, 'source_path'):
            inventory_sources(self.sample, self.history)
        path.write_text(original)
        (self.history / 'ce.csv').unlink()
        with self.assertRaisesRegex(TradingError, 'missing_source'):
            inventory_sources(self.sample, self.history)

    def test_incomplete_successful_input_is_rejected(self):
        (self.sample / 'INCOMPLETE.txt').write_text('partial')
        with self.assertRaisesRegex(TradingError, 'incomplete'):
            inventory_sources(self.sample, self.history)

    def test_bad_candles_are_rejected_and_volume_semantics(self):
        invalid = [candle()[:-1], [True, 100, 102, 99, 101, 1],
                   ['1793591100', 100, 102, 99, 101, 1],
                   [STAMP + .5, 100, 102, 99, 101, 1],
                   candle(STAMP + 1), [STAMP, None, 102, 99, 101, 1],
                   [STAMP, float('nan'), 102, 99, 101, 1],
                   [STAMP, float('inf'), 102, 99, 101, 1],
                   [STAMP, 0, 102, 99, 101, 1], [STAMP, -1, 102, 99, 101, 1],
                   [STAMP, 100, 99, 99, 101, 1], candle(volume=-1)]
        for row in invalid:
            result = profile_candles([row], 'X', 'option', 'test')
            self.assertEqual(result['counts']['invalid_rows'], 1, row)
            self.assertEqual(result['valid_rows'], [])
        self.assertFalse(profile_candles([candle(volume=0)], 'NIFTY', 'spot', 'test')['issues'])
        self.assertEqual(profile_candles([candle(volume=0)], 'X', 'option', 'test')['issues'][0]['issue_code'],
                         'zero_volume_option')

    def test_duplicates_unsorted_and_conflicting_exports(self):
        duplicate = profile_candles([candle(), candle()], 'X', 'option', 'a')
        self.assertEqual(duplicate['counts']['duplicate_rows'], 2)
        self.assertFalse(duplicate['valid_rows'])
        unsorted = profile_candles([candle(STAMP + 60), candle()], 'X', 'option', 'a')
        self.assertTrue(any(i['issue_code'] == 'unsorted_timestamps' for i in unsorted['issues']))
        a = profile_candles([candle()], 'X', 'option', 'a')
        b = profile_candles([candle()], 'X', 'option', 'b')
        result = reconcile_captures([a, b])
        self.assertEqual(len(result['accepted']), 1)
        self.assertEqual(result['coalesced_rows'], 1)
        changed = candle()
        changed[4] = 100
        b = profile_candles([changed], 'X', 'option', 'b')
        result = reconcile_captures([a, b])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['overlaps'][0]['status'], 'conflict')

    def test_audit_offline_immutable_blocked_staging(self):
        before = {str(p): p.read_bytes() for root in (self.sample, self.history) for p in root.iterdir()}
        with patch('socket.create_connection', side_effect=AssertionError('network')):
            result = audit_sources(self.sample, self.history, self.root / 'audit')
        self.assertFalse(result['backtest_ready'])
        self.assertEqual(result['structural_status'], 'pass')
        self.assertEqual(result['counts']['unique_observations'], 3)
        self.assertTrue((self.root / 'audit/VALIDATION.md').exists())
        self.assertTrue((self.root / 'audit/staging/BACKTEST_BLOCKED.txt').exists())
        with self.assertRaises((TradingError, OSError)):
            OptionsDataset(self.root / 'audit/staging')
        for path, data in before.items():
            self.assertEqual(Path(path).read_bytes(), data)
        with self.assertRaises(FileExistsError):
            audit_sources(self.sample, self.history, self.root / 'audit')

    def test_missing_spot_day_and_opening_minutes_are_explicit(self):
        export(self.history, 'ce', self.symbols['CE'], [candle(STAMP - 86400)])
        result = audit_sources(self.sample, self.history, self.root / 'audit')
        codes = {r['issue_code'] for r in result['readiness_gaps']}
        self.assertIn('missing_spot_day', codes)
        self.assertIn('missing_spot_opening_minutes', codes)

    def test_csv_mismatch_is_not_accepted_for_staging(self):
        path = self.history / 'ce.csv'
        path.write_text(path.read_text().replace(',101,', ',100,'))
        result = audit_sources(self.sample, self.history, self.root / 'audit')
        self.assertEqual(result['structural_status'], 'fail')
        self.assertTrue(any(i['code'] == 'raw_csv_mismatch' for i in result['findings']))

    def test_postmarket_rows_quarantined_and_zero_volume_flagged(self):
        export(self.history, 'ce', self.symbols['CE'], [candle(), candle(STAMP + 384 * 60, volume=0)])
        result = audit_sources(self.sample, self.history, self.root / 'audit')
        self.assertEqual(result['counts']['unique_outside_session'], 1)
        self.assertEqual(result['counts']['quarantined_source_observations'], 1)
        self.assertTrue(any(i['code'] == 'zero_volume_option' for i in result['findings']))
        with (self.root / 'audit/quarantine.csv').open() as stream:
            quarantine = list(csv.DictReader(stream))
        self.assertEqual(quarantine[0]['issue_code'], 'outside_provisional_session')

    def test_csv_multiplicity_and_columns_mismatch(self):
        path = self.history / 'pe.csv'
        lines = path.read_text().splitlines()
        path.write_text('\n'.join([*lines, lines[-1]]) + '\n')
        result = audit_sources(self.sample, self.history, self.root / 'duplicate-csv')
        self.assertEqual(result['structural_status'], 'fail')
        raw_path = self.history / 'ce_raw.json'
        obj = json.loads(raw_path.read_text())
        obj['columns'] = ['timestamp'] * 6
        raw_path.write_text(json.dumps(obj))
        with self.assertRaisesRegex(TradingError, 'invalid_columns'):
            audit_sources(self.sample, self.history, self.root / 'bad-columns')

    def test_source_mutation_is_detected(self):
        from gapfade.fyers_data_audit import reconcile_captures as original
        def mutate(captures):
            (self.history / 'ce.csv').write_text('changed during audit')
            return original(captures)
        with patch('gapfade.fyers_data_audit.reconcile_captures', side_effect=mutate):
            with self.assertRaisesRegex(TradingError, 'source_changed_during_audit'):
                audit_sources(self.sample, self.history, self.root / 'audit')
        self.assertFalse((self.root / 'audit/validation.json').exists())
        self.assertEqual(json.loads((self.root / 'audit/run_status.json').read_text())['status'], 'failed')

    def test_cli_completed_blocked_exit_code(self):
        result = subprocess.run([sys.executable, 'scripts/audit_fyers_data.py', '--sample', str(self.sample),
                                 '--history', str(self.history), '--out', str(self.root / 'cli')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(json.loads(result.stdout)['backtest_ready'])


if __name__ == '__main__':
    unittest.main()

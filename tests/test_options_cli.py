from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gapfade.__main__ import main


class OptionsCLITests(unittest.TestCase):
    def test_offline_demo_audit_and_both_variants(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                'gapfade.__main__.FyersClient', side_effect=AssertionError('unexpected network')):
            root = Path(directory)
            inputs = root / 'inputs'
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['options-demo', '--out', str(inputs)]), 0)
                common = ['--data', str(inputs), '--start', '2026-01-05', '--end', '2026-01-09']
                self.assertEqual(main(['options-audit', *common, '--out', str(root / 'audit')]), 0)
                self.assertEqual(main(['options-backtest', *common, '--out', str(root / 'confirmed')]), 0)
                self.assertEqual(main(['options-backtest', *common, '--no-confirmation',
                                       '--out', str(root / 'baseline')]), 0)
                self.assertEqual(main(['options-demo', '--out', str(inputs)]), 1)
                self.assertEqual(main(['options-backtest', *common, '--out', str(root / 'confirmed')]), 1)
            confirmed = json.loads((root / 'confirmed/summary.json').read_text())
            baseline = json.loads((root / 'baseline/summary.json').read_text())
            self.assertEqual(confirmed['data_kind'], 'synthetic')
            self.assertEqual(baseline['trade_count'], confirmed['trade_count'] + 1)
            self.assertEqual(confirmed['trade_count'], 3)
            first = json.loads((root / 'confirmed/manifest.json').read_text())
            second = json.loads((root / 'baseline/manifest.json').read_text())
            self.assertTrue(first['config'].pop('confirmation'))
            self.assertFalse(second['config'].pop('confirmation'))
            self.assertEqual(first['config'], second['config'])
            # The baseline consumes an extra option-day file for its extra trade.
            # Every shared input and the raw source evidence must be identical.
            self.assertEqual(first['provenance'], second['provenance'])
            for path, digest in first['input_sha256'].items():
                self.assertEqual(digest, second['input_sha256'][path])
            self.assertEqual(set(second['input_sha256']) - set(first['input_sha256']),
                             {'options/2026-01-07.csv'})

    def test_bad_date_returns_error(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(['options-backtest', '--data', 'missing', '--start', 'bad',
                                   '--end', '2026-01-09', '--out', 'runs/not-created']), 1)


if __name__ == '__main__':
    unittest.main()

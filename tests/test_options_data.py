import json
from pathlib import Path
import tempfile
import unittest

from gapfade import data
from gapfade.model import TradingError
from gapfade.options_data import OptionsDataset
from options_fixtures import DAY, fixture, BAR_FIELDS, CONTRACT_FIELDS


class OptionsDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fixture(self.root)

    def test_zero_index_volume_and_date_range(self):
        dataset = OptionsDataset(self.root)
        self.assertTrue(all(b.volume == 0 for b in dataset.spot(DAY).values()))
        self.assertEqual(dataset.validate_range(DAY, DAY), [DAY])

    def test_bad_bar_inputs(self):
        path = self.root / 'spot' / f'{DAY}.csv'
        original = data.rows(path)
        mutations = [original + [original[0]],
                     [dict(original[0], low='105')],
                     [dict(original[0], timestamp=f'{DAY}T09:15:00')],
                     [dict(original[0], timestamp=f'{DAY}T09:15:01+05:30')]]
        for records in mutations:
            data.write_csv(path, records, BAR_FIELDS)
            with self.assertRaises(TradingError):
                OptionsDataset(self.root).spot(DAY)

    def test_contract_overlap_and_unknown_expiry(self):
        path = self.root / 'contracts.csv'
        original = data.rows(path)
        for rows in (original + [original[0]], [dict(original[0], expiry_session='2026-01-10')]):
            data.write_csv(path, rows, CONTRACT_FIELDS)
            with self.assertRaises(TradingError):
                OptionsDataset(self.root)

    def test_provenance_failure_cases(self):
        path = self.root / 'provenance.json'
        original = json.loads(path.read_text())
        mutations = [dict(original, data_kind='historical', catalog_verified=False),
                     dict(original, exported_at=123),
                     dict(original, source_files=[dict(path='../outside.txt', sha256='0' * 64)]),
                     dict(original, source_files=[dict(path='missing.txt', sha256='0' * 64)]),
                     dict(original, source_files=[dict(path='source.txt', sha256='0' * 64)])]
        for obj in mutations:
            path.write_text(json.dumps(obj))
            with self.assertRaises((TradingError, OSError)):
                OptionsDataset(self.root)

    def test_missing_reference_and_incomplete_download(self):
        path = self.root / 'references.csv'
        path.write_text(path.read_text().splitlines()[0] + '\n')
        with self.assertRaises(TradingError):
            OptionsDataset(self.root).validate_range(DAY, DAY)
        (self.root / 'INCOMPLETE.txt').write_text('interrupted')
        with self.assertRaises(TradingError):
            OptionsDataset(self.root)

    def test_input_changes_after_loading_are_detected(self):
        dataset = OptionsDataset(self.root)
        path = self.root / 'source.txt'
        path.write_text('changed')
        with self.assertRaisesRegex(TradingError, 'changed'):
            dataset.input_hashes()

    def test_historical_data_requires_frozen_chronological_splits(self):
        path = self.root / 'provenance.json'
        obj = json.loads(path.read_text())
        obj['data_kind'] = 'historical'
        path.write_text(json.dumps(obj))
        with self.assertRaisesRegex(TradingError, 'frozen'):
            OptionsDataset(self.root)
        obj.update(research_splits=dict(development=['2025-01-01', '2025-06-30'],
                   validation=['2025-07-01', '2025-12-31'], test=['2026-01-01', '2026-01-09']),
                   splits_frozen_at='2026-01-01T00:00:00+05:30')
        path.write_text(json.dumps(obj))
        contracts = self.root / 'contracts.csv'
        records = data.rows(contracts)
        for row in records:
            row['symbol'] = row['symbol'].replace('DEMO:', 'HISTORY:')
        data.write_csv(contracts, records, CONTRACT_FIELDS)
        self.assertEqual(OptionsDataset(self.root).provenance['research_splits'], obj['research_splits'])
        obj['research_splits']['validation'] = ['2025-06-30', '2025-12-31']
        path.write_text(json.dumps(obj))
        with self.assertRaisesRegex(TradingError, 'non-overlapping'):
            OptionsDataset(self.root)

    def test_unselected_contract_bars_are_not_retained(self):
        dataset = OptionsDataset(self.root)
        # PE exists in the catalog but has no candles in this fixture.
        self.assertEqual(dataset.option(DAY, 'DEMO:PE'), {})


if __name__ == '__main__':
    unittest.main()

from pathlib import Path
import tempfile
import unittest

from gapfade.options_demo import create
from gapfade.options_data import OptionsDataset
from options_fixtures import DAY


class OptionsDemoTests(unittest.TestCase):
    def test_demo_is_synthetic_reproducible_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'a', Path(directory) / 'b'
            create(first)
            create(second)
            dataset = OptionsDataset(first)
            self.assertEqual(dataset.provenance['data_kind'], 'synthetic')
            self.assertTrue(all(b.volume == 0 for b in dataset.spot(DAY).values()))
            for path in first.rglob('*'):
                if path.is_file():
                    self.assertEqual(path.read_bytes(), (second / path.relative_to(first)).read_bytes())
            with self.assertRaises(FileExistsError):
                create(first)


if __name__ == '__main__':
    unittest.main()

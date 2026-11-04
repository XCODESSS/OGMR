"""Audited metadata and session-partitioned offline inputs. No network access."""
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from .data import boolean, write_csv
from .model import Bar, TradingError, at, number, timestamp
from .options_model import OptionContract, SpotReference, VERSION

BAR_FIELDS = 'timestamp symbol open high low close volume'.split()
COVERAGE_FIELDS = 'session status reason selected_symbol first_missing_timestamp position_open'.split()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + '\n', encoding='utf-8')


class OptionsDataset:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._hashes = {}
        if not self.root.is_dir():
            raise TradingError('Options dataset directory does not exist')
        if next(self.root.rglob('INCOMPLETE.txt'), None) is not None:
            raise TradingError('Historical download is incomplete')
        path = self.path('provenance.json')
        self._hashes['provenance.json'] = digest(path)
        self.provenance = json.loads(path.read_text(encoding='utf-8-sig'))
        self._validate_provenance()
        self.calendar, self.regular = {}, {}
        last = None
        for row in self.rows('sessions.csv', 'session previous_session is_regular'.split()):
            day, previous = date.fromisoformat(row['session']), date.fromisoformat(row['previous_session'])
            if day in self.calendar or previous >= day or (last is not None and previous != last):
                raise TradingError('Duplicate, unordered or discontinuous options calendar')
            self.calendar[day], self.regular[day] = previous, boolean(row['is_regular'])
            last = day
        if not self.calendar:
            raise TradingError('Empty options calendar')
        self.references = {}
        for row in self.rows('references.csv',
                             'session previous_session official_open previous_close available_at verified'.split()):
            ref = SpotReference(date.fromisoformat(row['session']), date.fromisoformat(row['previous_session']),
                number(row['official_open'], positive=True), number(row['previous_close'], positive=True),
                timestamp(row['available_at']), boolean(row['verified']))
            if ref.session in self.references or self.calendar.get(ref.session) != ref.previous_session:
                raise TradingError('Duplicate reference or wrong previous session')
            self.references[ref.session] = ref
        self.contracts = []
        fields = 'symbol underlying option_type strike expiry_session listed_at valid_from valid_to lot_size'.split()
        by_symbol, by_series = {}, {}
        for row in self.rows('contracts.csv', fields):
            if not re.fullmatch(r'[1-9][0-9]*', row['lot_size']):
                raise TradingError('Contract lot size must be a positive integer')
            contract = OptionContract(row['symbol'], row['underlying'], row['option_type'],
                number(row['strike'], positive=True), date.fromisoformat(row['expiry_session']),
                timestamp(row['listed_at']), date.fromisoformat(row['valid_from']),
                date.fromisoformat(row['valid_to']), int(row['lot_size']))
            if (not contract.symbol or contract.underlying != 'NIFTY' or contract.option_type not in ('CE', 'PE')
                or contract.valid_from > contract.valid_to or contract.valid_to > contract.expiry_session
                or contract.expiry_session not in self.calendar
                or contract.listed_at.date() > contract.valid_from):
                raise TradingError('Invalid contract metadata or insufficient expiry calendar')
            if contract.symbol.startswith('DEMO:') and self.provenance['data_kind'] != 'synthetic':
                raise TradingError('DEMO contracts require synthetic provenance')
            series = contract.option_type, contract.strike, contract.expiry_session
            for other in by_symbol.get(contract.symbol, []):
                if (other.option_type, other.strike, other.expiry_session) != series:
                    raise TradingError('Contract symbol changes series identity')
            for other in by_symbol.get(contract.symbol, []) + by_series.get(series, []):
                if max(other.valid_from, contract.valid_from) <= min(other.valid_to, contract.valid_to):
                    raise TradingError('Overlapping or duplicate contract metadata')
            by_symbol.setdefault(contract.symbol, []).append(contract)
            by_series.setdefault(series, []).append(contract)
            self.contracts.append(contract)
        if not self.contracts:
            raise TradingError('Missing historical contract catalog')
        self._contract_symbols = set(by_symbol)

    def path(self, relative):
        path = (self.root / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(self.root):
            raise TradingError('Dataset path escapes its root')
        return path

    def _validate_provenance(self):
        obj = self.provenance
        if not isinstance(obj, dict) or type(obj.get('schema_version')) is not int or obj['schema_version'] != 1:
            raise TradingError('Invalid options provenance schema')
        if obj.get('data_kind') not in ('historical', 'synthetic') or obj.get('bar_timestamp_convention') != 'minute_start':
            raise TradingError('Invalid data kind or bar timestamp convention')
        for field in ('provider', 'spot_reference_convention', 'audit_notes'):
            if not isinstance(obj.get(field), str) or not obj[field].strip():
                raise TradingError(f'Missing provenance {field}')
        if not isinstance(obj.get('exported_at'), str):
            raise TradingError('Provenance exported_at must be an ISO timestamp')
        timestamp(obj['exported_at'])
        for flag in ('calendar_verified', 'references_verified', 'catalog_verified'):
            if obj.get(flag) is not True:
                raise TradingError(f'Provenance requires documented {flag}=true')
        sources = obj.get('source_files')
        if not isinstance(sources, list) or not sources:
            raise TradingError('Source evidence is required')
        seen = set()
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get('path'), str):
                raise TradingError('Invalid source file evidence')
            relative, expected = source['path'], source.get('sha256')
            if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
                raise TradingError('Invalid source hash')
            path = self.path(relative)
            if relative in seen or any(part.startswith('.env') or part == '.secrets' for part in Path(relative).parts):
                raise TradingError('Duplicate or credential source path')
            seen.add(relative)
            if digest(path) != expected:
                raise TradingError(f'Source hash mismatch: {relative}')
            self._hashes[relative] = expected
        if obj['data_kind'] == 'historical':
            splits = obj.get('research_splits')
            if not isinstance(splits, dict) or set(splits) != {'development', 'validation', 'test'}:
                raise TradingError('Historical research requires frozen development/validation/test dates')
            previous_end = None
            for name in ('development', 'validation', 'test'):
                interval = splits[name]
                if not isinstance(interval, list) or len(interval) != 2 or not all(isinstance(v, str) for v in interval):
                    raise TradingError('Each research split must contain two ISO dates')
                start, end = map(date.fromisoformat, interval)
                if start > end or (previous_end is not None and start <= previous_end):
                    raise TradingError('Research splits must be ordered and non-overlapping')
                previous_end = end
            if not isinstance(obj.get('splits_frozen_at'), str):
                raise TradingError('Research splits require an ISO splits_frozen_at timestamp')
            frozen = timestamp(obj['splits_frozen_at'])
            if frozen > timestamp(obj['exported_at']):
                raise TradingError('Research splits must be frozen before this export/run')

    def rows(self, relative, fields):
        path = self.path(relative)
        before = digest(path)
        if relative in self._hashes and self._hashes[relative] != before:
            raise TradingError(f'Input changed during run: {relative}')
        with path.open(newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != fields:
                raise TradingError(f'Unexpected CSV columns: {relative}')
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise TradingError(f'Malformed CSV row: {relative}')
                yield row
        if digest(path) != before:
            raise TradingError(f'Input changed while reading: {relative}')
        self._hashes[relative] = before

    def _bars(self, day, symbol, kind):
        result, seen = {}, set()
        for row in self.rows(f'{kind}/{day}.csv', BAR_FIELDS):
            ts = timestamp(row['timestamp'])
            prices = [number(row[k], positive=True) for k in ('open', 'high', 'low', 'close')]
            volume = number(row['volume'])
            bar = Bar(ts, row['symbol'], *prices, volume)
            key = ts, bar.symbol
            if ts.date() != day or ts.second or ts.microsecond or key in seen:
                raise TradingError(f'Invalid date, minute alignment or duplicate bar: {key}')
            if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high or volume < 0:
                raise TradingError(f'Invalid OHLCV: {key}')
            if ((kind == 'spot' and bar.symbol != 'NIFTY')
                or (kind == 'options' and bar.symbol not in self._contract_symbols)):
                raise TradingError(f'Unknown bar symbol: {bar.symbol}')
            seen.add(key)
            if bar.symbol == symbol:
                result[ts] = bar
        return result

    def spot(self, day):
        return self._bars(day, 'NIFTY', 'spot')

    def option(self, day, symbol):
        return self._bars(day, symbol, 'options')

    def validate_range(self, start, end):
        if start > end or start not in self.calendar or end not in self.calendar:
            raise TradingError('Evaluation dates must be ordered exchange sessions in the calendar')
        days = [day for day in self.calendar if start <= day <= end]
        for day in days:
            if self.provenance['data_kind'] == 'historical':
                intervals = self.provenance['research_splits'].values()
                if not any(date.fromisoformat(begin) <= day <= date.fromisoformat(finish)
                           for begin, finish in intervals):
                    raise TradingError(f'Evaluation date outside frozen research splits: {day}')
            if self.regular[day]:
                ref = self.references.get(day)
                if ref is None or not ref.verified or ref.previous_session != self.calendar[day]:
                    raise TradingError(f'Missing/unverified reference: {day}')
                if ref.available_at.date() != day or ref.available_at >= at(day, '09:20'):
                    raise TradingError(f'Reference not available before decision: {day}')
        return days

    def input_hashes(self):
        # Detect changes to previously consumed metadata/source files too.
        for relative, expected in self._hashes.items():
            if digest(self.path(relative)) != expected:
                raise TradingError(f'Input changed during run: {relative}')
        return dict(sorted(self._hashes.items()))


def audit(dataset: OptionsDataset, start: date, end: date, output: Path) -> dict:
    from .options_strategy import decide
    from .options_model import OptionsConfig
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    coverage = []
    try:
        for day in dataset.validate_range(start, end):
            row = dict(session=day, status='valid', reason='structural_only', selected_symbol='',
                       first_missing_timestamp='', position_open=False)
            coverage.append(row)
            if not dataset.regular[day]:
                row.update(status='excluded', reason='excluded_special_session')
                continue
            spot = dataset.spot(day)
            opening = [spot.get(at(day, f'09:{minute}')) for minute in range(15, 20)]
            decide(dataset.references[day], opening, OptionsConfig())
        manifest = dict(strategy=VERSION, data_kind=dataset.provenance['data_kind'],
                        audit_scope='structural_only', start=start, end=end,
                        input_sha256=dataset.input_hashes(), provenance=dataset.provenance)
        write_json(output / 'manifest.json', manifest)
        result = dict(status='valid', audit_scope='structural_only', sessions=len(coverage),
                      data_kind=dataset.provenance['data_kind'])
    except (TradingError, OSError, ValueError, KeyError) as exc:
        if coverage:
            coverage[-1].update(status='invalid', reason=str(exc))
        write_json(output / 'run_status.json', dict(status='invalid', error=str(exc)))
        raise
    finally:
        write_csv(output / 'coverage.csv', coverage, COVERAGE_FIELDS)
    write_json(output / 'run_status.json', result)
    return result

"""Offline FYERS candle-quality assessment. No broker, network or replay imports."""
from collections import Counter, defaultdict
import csv
from datetime import datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from .model import IST, TradingError, number

CORE = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
ISSUE_FIELDS = ['source', 'symbol', 'row_number', 'timestamp', 'issue_code', 'detail']
SOURCE_FILES = {
    'sample': ['manifest.json', 'expiry_dates.json', 'contracts.json', 'spot_raw.json', 'spot.csv',
               'ce_raw.json', 'ce.csv', 'pe_raw.json', 'pe.csv'],
    'history': ['manifest.json', 'ce_raw.json', 'ce.csv', 'pe_raw.json', 'pe.csv'],
}


def sha256_file(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + '\n', encoding='utf-8')


def _csv(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def inventory_sources(sample_root, history_root):
    result = []
    for name, directory in [('sample', sample_root), ('history', history_root)]:
        root = Path(directory).resolve()
        if not root.is_dir():
            raise TradingError(f'missing_source_directory: {name}')
        if next(root.rglob('INCOMPLETE.txt'), None) is not None:
            raise TradingError(f'incomplete_source: {name}')
        paths = {}
        for relative in SOURCE_FILES[name]:
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise TradingError(f'source_path_escape: {name}/{relative}')
            if not path.is_file():
                raise TradingError(f'missing_source: {name}/{relative}')
            paths[relative] = path
        manifest = _read(paths['manifest.json'])
        if not isinstance(manifest, dict):
            raise TradingError(f'malformed_manifest: {name}')
        baselines = manifest.get('sha256', {})
        if not isinstance(baselines, dict) or set(baselines) - set(SOURCE_FILES[name]):
            raise TradingError(f'source_path_not_allowed: {name}')
        for relative, path in paths.items():
            actual = sha256_file(path)
            baseline = baselines.get(relative)
            if baseline is not None and (not isinstance(baseline, str)
                    or not re.fullmatch('[0-9a-f]{64}', baseline) or baseline != actual):
                raise TradingError(f'source_hash_mismatch: {name}/{relative}')
            result.append(dict(source=name, relative_path=relative, path=str(path), size=path.stat().st_size,
                               sha256=actual, baseline_status='verified' if baseline else 'baseline_not_supplied',
                               incomplete=False))
    # Only the two explicitly known failed download attempts are inspected.
    for directory in ('fyers-expired-sample-20261105-01', 'fyers-expired-sample-20261105-02'):
        root = Path(sample_root).parent / directory
        if root.is_dir() and root.resolve() not in (Path(sample_root).resolve(), Path(history_root).resolve()):
            result.append(dict(source=directory, relative_path='', path=str(root.resolve()), size='', sha256='',
                               baseline_status='excluded_failed_attempt', incomplete=(root / 'INCOMPLETE.txt').exists()))
    return result


def _issue(source, symbol, row_number, timestamp, code, detail):
    return dict(source=source, symbol=symbol, row_number=row_number, timestamp=timestamp,
                issue_code=code, detail=detail)


def profile_candles(rows, symbol, kind, source):
    if kind not in ('spot', 'option') or not isinstance(rows, list):
        raise TradingError('Invalid capture kind or candle array')
    records, issues, previous = [], [], None
    for index, raw in enumerate(rows, 1):
        record = dict(source=source, symbol=symbol, kind=kind, row_number=index, raw=raw,
                      valid=False, reasons=[], timestamp='', values=None)
        records.append(record)
        try:
            if not isinstance(raw, list) or len(raw) != 6:
                raise TradingError('row_width')
            epoch = raw[0]
            if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not Decimal(str(epoch)).is_finite() or int(epoch) != epoch:
                raise TradingError('invalid_epoch')
            moment = datetime.fromtimestamp(epoch, IST)
            record['timestamp'] = moment.isoformat()
            if moment.second or moment.microsecond:
                raise TradingError('not_minute_aligned')
            values = [number(value, positive=True) for value in raw[1:5]]
            opening, high, low, close = values
            volume = number(raw[5])
            if low > min(opening, close) or high < max(opening, close) or low > high or volume < 0:
                raise TradingError('invalid_ohlcv')
            record.update(valid=True, moment=moment, values=(int(epoch), *values, volume))
            if previous is not None and epoch < previous:
                issues.append(_issue(source, symbol, index, record['timestamp'], 'unsorted_timestamps', 'Source order decreases; staged rows will be sorted.'))
            previous = epoch
            if kind == 'option' and volume == 0:
                issues.append(_issue(source, symbol, index, record['timestamp'], 'zero_volume_option', 'Not evidence of an executable fill.'))
        except (TradingError, ValueError, TypeError, OverflowError, OSError) as exc:
            code = str(exc) if isinstance(exc, TradingError) else 'invalid_candle'
            record['reasons'].append(code)
            issues.append(_issue(source, symbol, index, record['timestamp'], code, json.dumps(raw, default=str)))
    counts = Counter(record['values'][0] for record in records if record['valid'])
    for record in records:
        if record['valid'] and counts[record['values'][0]] > 1:
            record['valid'] = False
            record['reasons'].append('duplicate_timestamp')
            issues.append(_issue(source, symbol, record['row_number'], record['timestamp'], 'duplicate_timestamp', 'All observations at this within-export key withheld.'))
    return dict(source=source, symbol=symbol, kind=kind, records=records, issues=issues,
                valid_rows=[r for r in records if r['valid']],
                counts=dict(raw_rows=len(rows), invalid_rows=sum(not r['valid'] for r in records),
                            duplicate_rows=sum('duplicate_timestamp' in r['reasons'] for r in records)))


def load_capture(path, descriptor):
    obj = _read(path)
    kind, symbol, source = (descriptor[key] for key in ('kind', 'symbol', 'source'))
    if not isinstance(obj, dict) or obj.get('s') not in ('ok', 'no_data') or not isinstance(obj.get('candles'), list):
        raise TradingError(f'malformed_response: {source}')
    if obj.get('symbol', symbol) != symbol:
        raise TradingError(f'capture_symbol_mismatch: {source}')
    columns = obj.get('columns') if 'columns' in obj else CORE if kind == 'spot' else None
    if (not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)
        or len(columns) != len(set(columns)) or not set(CORE).issubset(columns)):
        raise TradingError(f'invalid_columns: {source}')
    canonical = []
    for row in obj['candles']:
        canonical.append([row[columns.index(field)] for field in CORE]
                         if isinstance(row, list) and len(row) == len(columns) else [])
    capture = profile_candles(canonical, symbol, kind, source)
    for record, raw in zip(capture['records'], obj['candles']):
        record['raw'] = raw
    csv_path = Path(descriptor['csv_path'])
    actual, csv_valid = [], True
    with csv_path.open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['timestamp_ist', *columns]:
            csv_valid = False
        else:
            for row in reader:
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError('CSV width')
                    epoch = Decimal(row['timestamp'])
                    moment = datetime.fromisoformat(row['timestamp_ist'])
                    if not epoch.is_finite() or epoch != int(epoch) or moment.tzinfo is None or moment.timestamp() != int(epoch):
                        raise ValueError('CSV timestamp')
                    actual.append((int(epoch), *(number(row[field]) for field in CORE[1:])))
                except (TradingError, ValueError, TypeError, OverflowError):
                    csv_valid = False
    raw_values = [r['values'] for r in capture['records'] if r['values'] is not None]
    if not csv_valid or Counter(raw_values) != Counter(actual) or len(raw_values) != len(canonical):
        capture['issues'].append(_issue(source, symbol, '', '', 'raw_csv_mismatch',
            'CSV header/timestamps/values/multiplicities do not reconcile with raw JSON; this capture withheld from staging.'))
        for record in capture['records']:
            record['valid'] = False
            record['reasons'].append('raw_csv_mismatch')
    for record in capture['records']:
        if record['valid']:
            day = record['moment'].date().isoformat()
            if not descriptor['range_from'] <= day <= descriptor['range_to']:
                record['valid'] = False
                record['reasons'].append('outside_requested_range')
                capture['issues'].append(_issue(source, symbol, record['row_number'], record['timestamp'], 'outside_requested_range', 'Outside the manifest request date bounds.'))
    capture['valid_rows'] = [r for r in capture['records'] if r['valid']]
    capture['counts']['invalid_rows'] = sum(not r['valid'] for r in capture['records'])
    return capture


def reconcile_captures(captures):
    grouped = defaultdict(list)
    for capture in captures:
        for record in capture['valid_rows']:
            grouped[(record['symbol'], record['values'][0])].append(record)
    accepted, overlaps, conflicts, coalesced = [], [], [], 0
    for (symbol, epoch), observations in sorted(grouped.items()):
        values = {r['values'] for r in observations}
        if len(observations) > 1:
            overlaps.append(dict(symbol=symbol, timestamp=datetime.fromtimestamp(epoch, IST).isoformat(),
                                 status='identical' if len(values) == 1 else 'conflict',
                                 sources=';'.join(f"{r['source']}:{r['row_number']}" for r in observations),
                                 values=json.dumps([list(map(str, r['values'])) for r in observations])))
        if len(values) > 1:
            conflicts.extend(observations)
        else:
            record = dict(observations[0], lineage=[dict(source=r['source'], row_number=r['row_number']) for r in observations])
            accepted.append(record)
            coalesced += len(observations) - 1
    return dict(accepted=accepted, overlaps=overlaps, conflicts=conflicts, coalesced_rows=coalesced)


def _inside(record):
    return time(9, 15) <= record['moment'].time() < time(15, 30)


def _coverage(captures, merged):
    spot = {r['moment'] for r in merged if r['kind'] == 'spot' and _inside(r)}
    spot_days = {moment.date() for moment in spot}
    groups = defaultdict(list)
    for capture in captures:
        for record in capture['records']:
            if record.get('values') is not None:
                groups[(record['source'], record['symbol'], record['moment'].date())].append(record)
    daily, missing = [], []
    for (source, symbol, day), observations in sorted(groups.items()):
        template = {datetime.combine(day, time(9, 15), IST) + timedelta(minutes=i) for i in range(375)}
        valid = {r['moment'] for r in observations if r['valid']}
        inside = valid & template
        absent = template - valid
        missing.extend(dict(source=source, symbol=symbol, session=str(day), timestamp=m.isoformat()) for m in sorted(absent))
        last_regular = next((r['values'][1:] for r in observations if r['valid'] and r['moment'].time() == time(15, 29)), None)
        daily.append(dict(source=source, symbol=symbol, session=str(day), observed_rows=len(observations),
            expected_template_minutes=375, in_window_distinct_minutes=len(inside), missing_template_minutes=len(absent),
            out_of_window_rows=sum(not _inside(r) for r in observations),
            zero_volume_minutes=sum(r['values'][-1] == 0 for r in observations),
            matching_spot_minutes=len(inside & spot),
            postmarket_rows_equal_last_regular=sum(not _inside(r) and r['values'][1:] == last_regular for r in observations)))
    gaps = []
    option_days = sorted({r['moment'].date() for r in merged if r['kind'] == 'option'})
    for day in option_days:
        if day not in spot_days:
            gaps.append(_issue('merged', 'NIFTY', '', str(day), 'missing_spot_day', 'Option candles exist but no accepted regular-session spot candles exist.'))
    for day in sorted(spot_days):
        opening = [datetime.combine(day, time(9, 15), IST) + timedelta(minutes=i) for i in range(6)]
        absent = [moment.isoformat() for moment in opening if moment not in spot]
        if absent:
            gaps.append(_issue('merged', 'NIFTY', '', str(day), 'missing_spot_opening_minutes', ';'.join(absent)))
    option_groups = defaultdict(set)
    for record in merged:
        if record['kind'] == 'option' and _inside(record):
            option_groups[(record['symbol'], record['moment'].date())].add(record['moment'])
    for (symbol, day), moments in sorted(option_groups.items()):
        for clock in (time(9, 21), time(15, 0)):
            if datetime.combine(day, clock, IST) not in moments:
                gaps.append(_issue('merged', symbol, '', str(day), 'missing_option_checkpoint', clock.isoformat() + ' unavailable; not a simulated trade requirement.'))
    return daily, missing, gaps


def audit_sources(sample_root, history_root, output):
    roots = {'sample': Path(sample_root).resolve(), 'history': Path(history_root).resolve()}
    output = Path(output).resolve()
    if any(output.is_relative_to(root) or root.is_relative_to(output) for root in roots.values()):
        raise TradingError('Audit output must be separate from input directories')
    output.mkdir(parents=True, exist_ok=False)
    inventory = []
    try:
        _json(output / 'run_status.json', dict(status='running'))
        inventory = inventory_sources(sample_root, history_root)
        manifests = {name: _read(root / 'manifest.json') for name, root in roots.items()}
        captures = []
        for name, root in roots.items():
            manifest = manifests[name]
            descriptor = manifest['options'] if name == 'sample' else manifest['contracts']
            for side in ('CE', 'PE'):
                symbol = descriptor[side]['symbol']
                if name == 'history' and symbol != manifests['sample']['options'][side]['symbol']:
                    raise TradingError('manifest_contract_mismatch')
                captures.append(load_capture(root / f'{side.lower()}_raw.json', dict(
                    source=f'{name}/{side.lower()}_raw.json', symbol=symbol, kind='option',
                    csv_path=root / f'{side.lower()}.csv',
                    range_from=manifest['session'] if name == 'sample' else manifest['requested_from'],
                    range_to=manifest['session'] if name == 'sample' else manifest['requested_to'])))
        # The ordinary spot export covers a preceding week, not just sample session.
        spot_obj = _read(roots['sample'] / 'spot_raw.json')
        spot_epochs = [r[0] for r in spot_obj.get('candles', []) if isinstance(r, list) and r and type(r[0]) in (int, float)]
        if not spot_epochs:
            raise TradingError('missing_spot_candles')
        captures.append(load_capture(roots['sample'] / 'spot_raw.json', dict(source='sample/spot_raw.json',
            symbol='NIFTY', kind='spot', csv_path=roots['sample'] / 'spot.csv',
            range_from=datetime.fromtimestamp(min(spot_epochs), IST).date().isoformat(),
            range_to=datetime.fromtimestamp(max(spot_epochs), IST).date().isoformat())))
        reconciliation = reconcile_captures(captures)
        merged = reconciliation['accepted']
        daily, missing, gaps = _coverage(captures, merged)
        issues = [issue for capture in captures for issue in capture['issues']]
        for overlap in reconciliation['overlaps']:
            if overlap['status'] == 'conflict':
                issues.append(_issue(overlap['sources'], overlap['symbol'], '', overlap['timestamp'], 'cross_export_conflict', overlap['values']))
        catalog = _read(roots['sample'] / 'contracts.json')
        if catalog.get('s') != 'ok' or catalog['data']['expiry_date'] != manifests['sample']['expiry']:
            raise TradingError('catalog_expiry_mismatch')
        catalog_symbols = catalog['data']['contracts']['options']
        for side in ('CE', 'PE'):
            symbol = manifests['sample']['options'][side]['symbol']
            if symbol not in catalog_symbols:
                issues.append(_issue('sample/contracts.json', symbol, '', '', 'missing_catalog_symbol', 'Downloaded contract absent from returned expiry catalog.'))
        requirements = {
            'calendar_unverified': 'Official exchange sessions and special-session status through expiry are not supplied.',
            'references_unverified': 'Official NIFTY open/prior-close and availability before 09:20 are not supplied.',
            'lot_sizes_missing': 'Date-effective historical lot sizes are not supplied.',
            'listing_history_missing': 'Original listing timestamps and metadata validity intervals are not supplied.',
            'eligible_catalog_unverified': 'One expiry catalog does not establish all eligible historical strikes and expiries.',
            'timestamp_convention_unverified': 'Minute-start convention is assumed, not independently verified.',
            'execution_coverage_unverified': 'Required selected-contract execution paths have not been established; no trades simulated.',
            'research_splits_missing': 'Chronological frozen research intervals are not supplied.',
        }
        gaps.extend(_issue('metadata', '', '', '', code, detail) for code, detail in requirements.items())
        quarantine, staging = [], []
        for capture in captures:
            for record in capture['records']:
                if not record['valid']:
                    quarantine.append(_issue(record['source'], record['symbol'], record['row_number'], record['timestamp'],
                                             ';'.join(record['reasons']), json.dumps(record['raw'], default=str)))
        for record in reconciliation['conflicts']:
            quarantine.append(_issue(record['source'], record['symbol'], record['row_number'], record['timestamp'], 'cross_export_conflict', json.dumps(record['raw'], default=str)))
        excluded = []
        for record in merged:
            if not _inside(record):
                excluded.append(record)
                for lineage in record['lineage']:
                    quarantine.append(_issue(lineage['source'], record['symbol'], lineage['row_number'], record['timestamp'],
                                             'outside_provisional_session', json.dumps(record['raw'], default=str)))
            else:
                staging.append(record)
        if excluded:
            gaps.append(_issue('merged', '', '', '', 'postmarket_convention_unresolved', f'{len(excluded)} unique observations outside the provisional regular-session template.'))
        regular_options = [record for record in staging if record['kind'] == 'option']
        zero_regular_options = sum(record['values'][-1] == 0 for record in regular_options)
        partitioned = defaultdict(list)
        lineage_rows = []
        for record in staging:
            values = record['values']
            relative = f"{ 'spot' if record['kind'] == 'spot' else 'options'}/{record['moment'].date()}.csv"
            partitioned[relative].append(dict(zip(['timestamp', 'symbol', *CORE[1:]], [record['timestamp'], record['symbol'], *values[1:]])))
            lineage_rows.append(dict(symbol=record['symbol'], timestamp=record['timestamp'],
                                     sources=json.dumps(record['lineage']), staged_file=relative,
                                     executable_volume=record['kind'] == 'spot' or values[-1] > 0))
        for relative, records in partitioned.items():
            _csv(output / 'staging' / relative, sorted(records, key=lambda r: (r['timestamp'], r['symbol'])), ['timestamp', 'symbol', *CORE[1:]])
        (output / 'staging').mkdir(exist_ok=True)
        (output / 'staging/BACKTEST_BLOCKED.txt').write_text('Provisional bars only. Missing verified metadata and coverage evidence.\n', encoding='utf-8')
        _csv(output / 'staging_lineage.csv', lineage_rows, ['symbol', 'timestamp', 'sources', 'staged_file', 'executable_volume'])
        warning_codes = {'zero_volume_option', 'unsorted_timestamps'}
        structural_issues = [issue for issue in issues if issue['issue_code'] not in warning_codes]
        raw_total = sum(capture['counts']['raw_rows'] for capture in captures)
        # Coalesced rows outside the template are counted with quarantine, not twice.
        staged_coalesced = sum(len(record['lineage']) - 1 for record in staging)
        accounting = len(staging) + staged_coalesced + len(quarantine)
        if raw_total != accounting:
            raise TradingError('Audit row accounting does not reconcile')
        profiles = []
        for capture in captures:
            moments = [r['moment'] for r in capture['records'] if r.get('values') is not None]
            profiles.append(dict(source=capture['source'], symbol=capture['symbol'], kind=capture['kind'],
                **capture['counts'], first_timestamp=min(moments).isoformat() if moments else None,
                last_timestamp=max(moments).isoformat() if moments else None,
                observed_dates=len({m.date() for m in moments})))
        findings = [dict(code=code, severity='high' if code not in warning_codes else 'medium',
                         evidence=f'{count} observations/issues; see candle_issues.csv',
                         impact='Rows may be invalid or non-executable; source/staging interpretation requires review.',
                         remediation='Inspect the referenced raw rows; preserve conflicts and do not fill gaps.')
                    for code, count in sorted(Counter(i['issue_code'] for i in issues).items())]
        findings.extend(dict(code=code, severity='high', evidence=detail,
                             impact='Historical strategy readiness is blocked.', remediation='Supply and audit source evidence offline.')
                        for code, detail in requirements.items())
        result = dict(status='blocked', backtest_ready=False, structural_status='fail' if structural_issues else 'pass',
            coverage_status='gaps_or_unverified', metadata_status='blocked', source_hashes_unchanged=True,
            counts=dict(raw_observations=raw_total, unique_observations=len(merged), staged_observations=len(staging),
                        coalesced_staged_observations=staged_coalesced, quarantined_source_observations=len(quarantine),
                        conflicts=sum(r['status'] == 'conflict' for r in reconciliation['overlaps']),
                        identical_overlap_keys=sum(r['status'] == 'identical' for r in reconciliation['overlaps']),
                        unique_outside_session=len(excluded), missing_template_minutes=len(missing)),
            profiles=profiles, findings=findings, readiness_gaps=gaps,
            assumptions=['09:15-15:29 IST, 375 minute starts per observed regular session; calendar unverified.',
                         'Sparse option minutes are unavailable observations, not proven provider corruption.',
                         'Same-provider agreement is not independent verification of price accuracy.',
                         'No backtest or profitability analysis was run.'])
        result['counts'].update(staged_option_observations=len(regular_options),
            staged_zero_volume_option_observations=zero_regular_options,
            staged_positive_volume_option_observations=len(regular_options) - zero_regular_options,
            staged_spot_observations=len(staging) - len(regular_options))
        for record in inventory:
            if record['sha256'] and sha256_file(record['path']) != record['sha256']:
                raise TradingError(f"source_changed_during_audit: {record['source']}/{record['relative_path']}")
        _csv(output / 'source_inventory.csv', inventory, ['source', 'relative_path', 'size', 'sha256', 'baseline_status', 'incomplete'])
        _csv(output / 'candle_issues.csv', issues, ISSUE_FIELDS)
        _csv(output / 'overlap_checks.csv', reconciliation['overlaps'], ['symbol', 'timestamp', 'status', 'sources', 'values'])
        _csv(output / 'daily_coverage.csv', daily, list(daily[0]) if daily else ['source', 'symbol', 'session'])
        _csv(output / 'missing_minutes.csv', missing, ['source', 'symbol', 'session', 'timestamp'])
        _csv(output / 'readiness_gaps.csv', gaps, ISSUE_FIELDS)
        _csv(output / 'quarantine.csv', quarantine, ISSUE_FIELDS)
        _json(output / 'audit_manifest.json', dict(audit_version=1, sources=inventory,
              row_accounting=dict(raw=raw_total, staged=len(staging), coalesced=staged_coalesced, quarantine=len(quarantine)),
              assumptions=result['assumptions']))
        _json(output / 'validation.json', result)
        report = ['# FYERS local data validation', '', '**Verdict: not ready for strategy backtesting.**', '',
                  f"Structural checks: {result['structural_status']}. Source files remained unchanged.", '',
                  '## Source profiles', '', '| Source | Rows | Dates | Invalid rows |', '|---|---:|---:|---:|']
        report.extend(f"| {p['source']} | {p['raw_rows']} | {p['observed_dates']} | {p['invalid_rows']} |" for p in profiles)
        report.extend(['', '## Reconciliation', '',
            f"{raw_total} source observations = {len(staging)} unique staged observations + {staged_coalesced} coalesced staged duplicates + {len(quarantine)} quarantined source observations.",
            f"Identical overlapping keys: {result['counts']['identical_overlap_keys']}; conflicting keys: {result['counts']['conflicts']}.",
            '', '## Candle and execution-quality findings', '',
            f"Invalid source rows: {sum(p['invalid_rows'] for p in profiles)}. Within-export duplicate rows: {sum(p['duplicate_rows'] for p in profiles)}. Raw/CSV mismatches: {sum(i['issue_code'] == 'raw_csv_mismatch' for i in issues)}.",
            f"Regular-window unique option observations: {len(regular_options)}. Of these, {zero_regular_options} have zero volume and {len(regular_options) - zero_regular_options} have positive volume. Zero-volume observations cannot be used as executable fills; positive volume alone does not prove an attainable fill.",
            f"Missing minute starts across observed source/symbol dates: {len(missing)} against the provisional 375-minute template. Missing whole trading dates cannot be certified without an official calendar. Sparse option minutes are not automatically corruption.",
            f"Post-market source observations identical in all OHLCV fields to the 15:29 candle: {sum(r['postmarket_rows_equal_last_regular'] for r in daily)}; see the per-source/date counts in daily_coverage.csv. This pattern does not establish the cause of all out-of-window rows.",
            '', '## Readiness blockers', ''])
        report.extend(f'- {code}: {detail}' for code, detail in requirements.items())
        report.extend(['', f"Observed option dates without matching spot: {sum(g['issue_code'] == 'missing_spot_day' for g in gaps)}.",
            f"Unique out-of-template observations: {len(excluded)}; see quarantine.csv and daily_coverage.csv.",
            '', '## Interpretation and next evidence', '',
            'Only structurally valid, non-conflicting observations inside the provisional market-hours template were staged. Raw exports were not modified. Out-of-template rows were retained in quarantine, not timestamp-shifted. Zero-volume option candles, when present, remain non-executable observations.',
            'The sample manifest hashes can verify export identity; the longer export has no original hash baseline. New audit hashes do not retroactively establish original integrity. Overlap agreement is between exports from the same provider.',
            'Supply an official calendar, historical listing/lot-size evidence, verified spot references and historical eligible contracts. Resolve the post-market timestamp convention and obtain missing required spot/contract observations before performance analysis. No network requests were made.',
            '', '## Assumptions', ''])
        report.extend('- ' + value for value in result['assumptions'])
        (output / 'VALIDATION.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
        _json(output / 'run_status.json', dict(status='completed_blocked', backtest_ready=False, source_hashes_unchanged=True))
        return result
    except (TradingError, OSError, ValueError, KeyError, TypeError) as exc:
        (output / 'validation.json').unlink(missing_ok=True)
        _json(output / 'run_status.json', dict(status='failed', error=str(exc), backtest_ready=False))
        _json(output / 'audit_manifest.json', dict(audit_version=1, status='failed', sources=inventory))
        raise

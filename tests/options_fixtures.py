"""Small hand-authored datasets, independent of the demonstration generator."""
from datetime import date, timedelta
import hashlib
import json

from gapfade.data import write_csv

DAY = date(2026, 1, 5)
BAR_FIELDS = 'timestamp symbol open high low close volume'.split()
CONTRACT_FIELDS = 'symbol underlying option_type strike expiry_session listed_at valid_from valid_to lot_size'.split()


def candle(minute, symbol='NIFTY', open='99.2', high='99.3', low='99.1', close='99.2', volume=0, day=DAY):
    return dict(zip(BAR_FIELDS, [f'{day}T{minute}:00+05:30', symbol, open, high, low, close, volume]))


def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    sessions = []
    previous = date(2026, 1, 2)
    for offset in range(5):
        day = DAY + timedelta(days=offset)
        sessions.append(dict(session=day, previous_session=previous, is_regular='true'))
        previous = day
    write_csv(root / 'sessions.csv', sessions, 'session previous_session is_regular'.split())
    write_csv(root / 'references.csv', [dict(session=DAY, previous_session='2026-01-02',
        official_open='99', previous_close='100', available_at=f'{DAY}T09:16:00+05:30', verified='true')],
        'session previous_session official_open previous_close available_at verified'.split())
    contracts = [dict(symbol=f'DEMO:{kind}', underlying='NIFTY', option_type=kind, strike='100',
        expiry_session='2026-01-09', listed_at='2026-01-01T00:00:00+05:30',
        valid_from='2026-01-01', valid_to='2026-01-09', lot_size='10') for kind in ('CE', 'PE')]
    write_csv(root / 'contracts.csv', contracts, CONTRACT_FIELDS)
    spot = [candle(f'09:{minute:02d}', open='99', low='98.9') for minute in range(15, 20)]
    spot += [candle('09:20'), candle('09:21'), candle('09:22', high='100.1')]
    write_csv(root / 'spot' / f'{DAY}.csv', spot, BAR_FIELDS)
    options = [candle(f'09:{minute}', 'DEMO:CE', '100', '121', '99', '110', 100)
               for minute in (21, 22, 23)]
    options[-1].update(open='120', close='120')
    write_csv(root / 'options' / f'{DAY}.csv', options, BAR_FIELDS)
    (root / 'source.txt').write_text('Fictitious independent fixture.', encoding='utf-8')
    provenance = dict(schema_version=1, data_kind='synthetic', provider='fixture',
        exported_at='2026-01-12T00:00:00+05:30', calendar_verified=True,
        references_verified=True, catalog_verified=True, bar_timestamp_convention='minute_start',
        spot_reference_convention='synthetic previous close', audit_notes='Fictitious tests only.',
        source_files=[dict(path='source.txt', sha256=hashlib.sha256((root / 'source.txt').read_bytes()).hexdigest())])
    (root / 'provenance.json').write_text(json.dumps(provenance), encoding='utf-8')

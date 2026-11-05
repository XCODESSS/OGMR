"""Hand-checkable synthetic walk-forward data. Every price and date here is fictitious.

Each plan entry is one session. A premium such as '110' is a 1% gap-down CE trade: bought at 100
at 09:21 and sold at that premium at 09:23. With a 10-unit lot and no costs, '110' earns Rs 100.
'flat' is a 0.04% gap, which is below every threshold and produces no trade.
"""
from datetime import date, timedelta
import hashlib
import json

from gapfade.data import write_csv

BAR_FIELDS = 'timestamp symbol open high low close volume'.split()
CONTRACT_FIELDS = 'symbol underlying option_type strike expiry_session listed_at valid_from valid_to lot_size'.split()
FIRST = date(2026, 1, 5)


def sessions_from(first, count):
    days, day = [], first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def bar(day, minute, symbol, open, high, low, close, volume):
    return dict(zip(BAR_FIELDS, [f'{day}T09:{minute}:00+05:30', symbol, open, high, low, close, volume]))


def contract(symbol, expiry, *, strike='25000', kind='CE', listed='2026-01-01', valid_from='2026-01-01',
             valid_to=None, lot=10):
    return dict(symbol=symbol, underlying='NIFTY', option_type=kind, strike=strike, expiry_session=expiry,
                listed_at=f'{listed}T00:00:00+05:30', valid_from=valid_from, valid_to=valid_to or expiry, lot_size=lot)


def build(root, plan, *, counts=None, contracts=None, horizon=3):
    """counts = sessions in (development, validation, test); default is all development with a far test."""
    root.mkdir(parents=True)
    days = sessions_from(FIRST, len(plan) + horizon)
    previous, sessions = date(2026, 1, 2), []
    for day in days:
        sessions.append(dict(session=day, previous_session=previous, is_regular='true'))
        previous = day
    write_csv(root / 'sessions.csv', sessions, 'session previous_session is_regular'.split())
    catalog = contracts(days) if contracts else [contract('SYN:CE', days[-1])]
    write_csv(root / 'contracts.csv', catalog, CONTRACT_FIELDS)
    references = []
    symbols = sorted({row['symbol'] for row in catalog})
    for index, entry in enumerate(plan):
        day, flat = days[index], entry == 'flat'
        opening, close = ('24990', '24995') if flat else ('24750', '24800')
        references.append(dict(session=day, previous_session=sessions[index]['previous_session'], official_open=opening,
                               previous_close='25000', available_at=f'{day}T09:16:00+05:30', verified='true'))
        spot = [bar(day, m, 'NIFTY', opening, int(close) + 1, int(opening) - 1, close, 0) for m in range(15, 20)]
        spot += [bar(day, m, 'NIFTY', close, int(close) + 1, int(close) - 1, close, 0) for m in (20, 21)]
        spot += [bar(day, 22, 'NIFTY', close, 25001 if not flat else int(close) + 1, int(close) - 1, close, 0),
                 bar(day, 23, 'NIFTY', close, int(close) + 1, int(close) - 1, close, 0)]
        write_csv(root / 'spot' / f'{day}.csv', spot, BAR_FIELDS)
        options = []
        for symbol in symbols:
            options += [bar(day, m, symbol, '100', '101', '99', '100', 100) for m in (21, 22)]
            premium = '100' if flat else entry
            options.append(bar(day, 23, symbol, premium, float(premium) + 1, float(premium) - 1, premium, 100))
        write_csv(root / 'options' / f'{day}.csv', options, BAR_FIELDS)
    write_csv(root / 'references.csv', references,
              'session previous_session official_open previous_close available_at verified'.split())
    development, validation, test = counts or (len(plan) - 2, 1, 1)
    edges = (days[0], days[development - 1], days[development], days[development + validation - 1],
             days[development + validation], days[development + validation + test - 1])
    (root / 'raw').mkdir()
    (root / 'raw' / 'source.txt').write_text('Fictitious walk-forward fixture.', encoding='utf-8')
    (root / 'provenance.json').write_text(json.dumps(dict(
        schema_version=1, data_kind='synthetic', provider='fixture', exported_at='2026-01-02T00:00:00+05:30',
        calendar_verified=True, references_verified=True, catalog_verified=True,
        bar_timestamp_convention='minute_start', spot_reference_convention='synthetic', audit_notes='Fictitious only.',
        research_splits=dict(development=[str(edges[0]), str(edges[1])], validation=[str(edges[2]), str(edges[3])],
                             test=[str(edges[4]), str(edges[5])]),
        splits_frozen_at='2026-01-01T00:00:00+05:30',
        source_files=[dict(path='raw/source.txt',
                           sha256=hashlib.sha256((root / 'raw' / 'source.txt').read_bytes()).hexdigest())])),
        encoding='utf-8')
    return days

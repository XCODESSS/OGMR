"""Fictitious inputs that exercise walk-forward plumbing; never evidence of a trading edge."""
from datetime import date, timedelta
from pathlib import Path

from .data import write_csv
from .model import at
from .options_data import BAR_FIELDS, digest, write_json

CONTRACT_FIELDS = 'symbol underlying option_type strike expiry_session listed_at valid_from valid_to lot_size'.split()
SESSION_COUNT, HORIZON = 20, 3
# Option exit premium by session; None is a small gap with no trade. All numbers are made up.
PATTERN = ('108', None, '95', '112', '97', None, '104', '92')


def _weekdays(first, count):
    days, day = [], first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def create(destination):
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    (root / 'SYNTHETIC.txt').write_text('Fictitious contracts, calendar and prices. Plumbing demonstration only.\n',
                                        encoding='utf-8')
    days = _weekdays(date(2026, 1, 5), SESSION_COUNT + HORIZON)
    previous, sessions = date(2026, 1, 2), []
    for day in days:
        sessions.append(dict(session=day, previous_session=previous, is_regular='true'))
        previous = day
    write_csv(root / 'sessions.csv', sessions, 'session previous_session is_regular'.split())
    near, far = days[11], days[-1]
    contracts = [dict(symbol=f'DEMO:NIFTY:{tag}:{kind}', underlying='NIFTY', option_type=kind, strike='25000',
                      expiry_session=expiry, listed_at='2026-01-01T00:00:00+05:30', valid_from='2026-01-01',
                      valid_to=expiry, lot_size=lot)
                 for tag, expiry, lot in (('NEAR', near, 10), ('FAR', far, 5)) for kind in ('CE', 'PE')]
    write_csv(root / 'contracts.csv', contracts, CONTRACT_FIELDS)
    references = []
    for index, row in enumerate(sessions[:SESSION_COUNT]):
        day, exit_premium = row['session'], PATTERN[index % len(PATTERN)]
        opening = '24990' if exit_premium is None else '24750'
        references.append(dict(session=day, previous_session=row['previous_session'], official_open=opening,
                               previous_close='25000', available_at=f'{day}T09:16:00+05:30', verified='true'))
        close = '24995' if exit_premium is None else '24800'
        spot = []
        for minute in range(15, 24):
            bar_open = opening if minute < 20 else close
            high = max(float(bar_open), float(close)) + 1
            low = min(float(bar_open), float(close)) - 1
            if minute == 22 and exit_premium is not None:
                high = 25001
            spot.append(dict(zip(BAR_FIELDS, [at(day, f'09:{minute}').isoformat(), 'NIFTY', bar_open,
                                              high, low, close, 0])))
        write_csv(root / 'spot' / f'{day}.csv', spot, BAR_FIELDS)
        options = []
        for contract in contracts:
            for minute in (21, 22, 23):
                premium = exit_premium if minute == 23 and exit_premium else '100'
                options.append(dict(zip(BAR_FIELDS, [at(day, f'09:{minute}').isoformat(), contract['symbol'], premium,
                                                     float(premium) + 1, float(premium) - 1, premium, 100])))
        write_csv(root / 'options' / f'{day}.csv', options, BAR_FIELDS)
    write_csv(root / 'references.csv', references,
              'session previous_session official_open previous_close available_at verified'.split())
    (root / 'raw').mkdir()
    write_json(root / 'raw' / 'synthetic.json', dict(description='Fictitious generator pattern', pattern=PATTERN))
    write_json(root / 'provenance.json', dict(
        schema_version=1, data_kind='synthetic', provider='OGMR fictitious walk-forward demonstration',
        exported_at='2026-01-01T00:00:00+05:30', calendar_verified=True, references_verified=True,
        catalog_verified=True, bar_timestamp_convention='minute_start',
        spot_reference_convention='synthetic previous close',
        audit_notes='Synthetic prices, contracts, lots and calendar. Not a market-data backtest.',
        research_splits=dict(development=[days[0], days[11]], validation=[days[12], days[15]],
                             test=[days[16], days[19]]),
        splits_frozen_at='2026-01-01T00:00:00+05:30',
        source_files=[dict(path='raw/synthetic.json', sha256=digest(root / 'raw' / 'synthetic.json'))]))

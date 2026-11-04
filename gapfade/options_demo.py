"""Deterministic fictitious option prices; never evidence of a trading edge."""
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

from .data import write_csv
from .model import at
from .options_data import BAR_FIELDS, digest, write_json


def create(destination):
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    first, previous = date(2026, 1, 5), date(2026, 1, 2)
    sessions = []
    for offset in range(5):
        day = first + timedelta(days=offset)
        sessions.append(dict(session=day, previous_session=previous, is_regular='true'))
        previous = day
    sessions.append(dict(session='2026-01-12', previous_session='2026-01-09', is_regular='true'))
    sessions.append(dict(session='2026-01-13', previous_session='2026-01-12', is_regular='true'))
    write_csv(root / 'sessions.csv', sessions, 'session previous_session is_regular'.split())
    contracts = [dict(symbol=f'DEMO:NIFTY:{kind}', underlying='NIFTY', option_type=kind,
        strike='25000', expiry_session='2026-01-13', listed_at='2026-01-01T00:00:00+05:30',
        valid_from='2026-01-01', valid_to='2026-01-13', lot_size=10) for kind in ('CE', 'PE')]
    write_csv(root / 'contracts.csv', contracts,
              'symbol underlying option_type strike expiry_session listed_at valid_from valid_to lot_size'.split())
    references, raw = [], []
    # CE winner, PE winner, unconfirmed CE (baseline only), subthreshold,
    # then a CE premium stop. All numbers are deliberately fictitious.
    patterns = [('24750', '24800'), ('25250', '25200'), ('24750', '24700'),
                ('24950', '24970'), ('24750', '24800')]
    for index, (opening, close) in enumerate(patterns):
        day = first + timedelta(days=index)
        references.append(dict(session=day, previous_session=sessions[index]['previous_session'],
            official_open=opening, previous_close='25000', available_at=f'{day}T09:16:00+05:30', verified='true'))
        o, c = D(opening), D(close)
        spot, options = [], []
        for minute in range(15, 24):
            current = at(day, f'09:{minute}')
            so = o if minute < 20 else c
            sc = c
            high, low = max(so, sc) + 5, min(so, sc) - 5
            if minute == 22 and index in (0, 1, 2):
                if index == 1:
                    low = D('24999')
                else:
                    high = D('25001')
            spot.append(dict(zip(BAR_FIELDS, [current.isoformat(), 'NIFTY', so, high, low, sc, 0])))
            if minute >= 21:
                for kind in ('CE', 'PE'):
                    premium = D('110') if minute == 23 else D('100')
                    if index == 4 and minute == 23:
                        premium = D('60')
                    option_low = D('70') if index == 4 and minute == 22 else premium - 1
                    options.append(dict(zip(BAR_FIELDS, [current.isoformat(), f'DEMO:NIFTY:{kind}',
                        premium, premium + 1, option_low, premium, 100])))
        write_csv(root / 'spot' / f'{day}.csv', spot, BAR_FIELDS)
        write_csv(root / 'options' / f'{day}.csv', options, BAR_FIELDS)
        raw.append(dict(session=day, spot=spot, options=options))
    write_csv(root / 'references.csv', references,
              'session previous_session official_open previous_close available_at verified'.split())
    (root / 'raw').mkdir()
    write_json(root / 'raw' / 'synthetic.json', dict(description='Fictitious generated candles', sessions=raw))
    write_json(root / 'provenance.json', dict(schema_version=1, data_kind='synthetic',
        provider='OGMR fictitious demonstration', exported_at='2026-01-13T00:00:00+05:30',
        calendar_verified=True, references_verified=True, catalog_verified=True,
        bar_timestamp_convention='minute_start', spot_reference_convention='synthetic previous close',
        audit_notes='Synthetic prices, contracts, lots and calendar. Not a market-data backtest.',
        source_files=[dict(path='raw/synthetic.json', sha256=digest(root / 'raw' / 'synthetic.json'))]))

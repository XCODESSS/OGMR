"""Forward-only simulated options positions, using live read-only market data."""
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .data import boolean
from .model import IST, TradingError, at, fill_price, number
from .options_accounting import size_lots, trade_amounts
from .options_model import OptionSignal, SpotReference, VERSION
from .options_strategy import decide, choose_contract
from .options_paper_feed import MarketData, SPOT


def calendar(path):
    import csv
    result, previous_day = {}, None
    with Path(path).open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['session', 'previous_session', 'is_regular']:
            raise TradingError('Calendar needs session,previous_session,is_regular columns')
        for row in reader:
            day, previous = date.fromisoformat(row['session']), date.fromisoformat(row['previous_session'])
            if day in result or previous >= day or (previous_day is not None and previous != previous_day):
                raise TradingError('Calendar must contain consecutive ordered exchange sessions')
            result[day] = dict(previous=previous, regular=boolean(row['is_regular']))
            previous_day = day
    if not result:
        raise TradingError('Empty session calendar')
    return result


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class Journal:
    """Separate SQLite process lock auto-releases on crash; state commits atomically."""
    def __init__(self, output):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = sqlite3.connect(self.output / 'process-lock.sqlite', timeout=0)
        try:
            self.lock.execute('BEGIN EXCLUSIVE')
            self.db = sqlite3.connect(self.output / 'journal.sqlite')
            self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
            self.db.commit()
        except sqlite3.OperationalError:
            self.lock.close()
            raise TradingError('Another paper process owns this output directory') from None

    def load(self):
        row = self.db.execute('SELECT document FROM state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state, event):
        state_text = json.dumps(state, default=str)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1, ?)', (state_text,))
            self.db.execute('INSERT INTO events(document) VALUES (?)', (json.dumps(event, default=str),))
        temporary = self.output / 'state.tmp'
        temporary.write_text(state_text + '\n', encoding='utf-8')
        temporary.replace(self.output / 'state.json')

    def close(self):
        self.db.close()
        self.lock.close()


class PaperSession:
    def __init__(self, config, day, sessions, contracts, journal, fingerprint):
        self.config, self.day, self.sessions, self.contracts, self.journal = config, day, sessions, contracts, journal
        existing = journal.load()
        if existing and existing['fingerprint'] != fingerprint:
            raise TradingError('Session/config/calendar/catalog differs from the saved paper journal')
        self.state = existing or dict(mode='paper_only', strategy=VERSION, session=str(day), fingerprint=fingerprint,
            phase='waiting', equity=config.capital, position=None, trade=None, signal=None,
            reference=None, last_processed=None, error=None, data_errors=[])

    def save(self, event, now, **details):
        self.state['updated_at'] = now.isoformat()
        self.journal.save(self.state, dict(event=event, observed_at=now.isoformat(), **details))

    def signal(self):
        raw = self.state['signal']
        return OptionSignal(date.fromisoformat(raw['session']), raw['option_type'], number(raw['gap']),
            datetime.fromisoformat(raw['decision_at']), number(raw['spot_at_decision']),
            number(raw['target']), number(raw['failure_level']))

    def _close(self, feed, now, reason):
        position = self.state['position']
        quote = feed.quote(position['symbol'])
        received = quote.get('received_at', now)
        earliest = (at(self.day, '15:00') if reason == 'scheduled_exit' else
                    datetime.fromisoformat(self.state['exit_not_before']) if reason != 'kill_switch' and self.state.get('exit_not_before')
                    else now - timedelta(seconds=10))
        # The source timestamp must be fresh relative to the decision, too.
        if received.date() != self.day or not -2 <= (received - quote['timestamp']).total_seconds() <= 10 or quote['timestamp'] < earliest:
            raise TradingError('Stale quote; paper exit remains unresolved')
        exit_price = fill_price(quote['bid'], -1, number(self.config.slippage_bps))
        gross, fees, net = trade_amounts(number(position['entry_price']), exit_price, position['quantity'], self.config)
        trade = dict(position, exit_price=exit_price, exit_time=received.isoformat(), exit_quote_time=quote['timestamp'].isoformat(),
                     exit_bid=quote['bid'], exit_reason=reason, gross_pnl=gross, fees=fees, net_pnl=net,
                     data_errors_seen=len(self.state['data_errors']))
        self.state.update(phase='closed', position=None, trade=trade, equity=number(self.state['equity']) + net, error=None)
        self.save('simulated_exit', received, trade=trade)

    def step(self, now, feed, stopped=False):
        if now.tzinfo is None or now.astimezone(IST).date() != self.day:
            raise TradingError('Paper session clock must be offset-aware and belong to the journal session')
        now = now.astimezone(IST)
        phase = self.state['phase']
        if phase in ('closed', 'no_trade', 'halted'):
            return
        if stopped:
            if self.state['position']:
                self._close(feed, now, 'kill_switch')
            else:
                self.state.update(phase='halted', reason='kill_switch')
                self.save('halted_before_entry', now)
            return
        if self.state['position'] and now >= at(self.day, '15:00'):
            self._close(feed, now, 'scheduled_exit')
            return
        if phase == 'waiting':
            if now >= at(self.day, '09:20:20'):
                self.state.update(phase='no_trade', reason='missed_signal_window')
                self.save('no_trade', now)
                return
            if now < at(self.day, '09:16'):
                return
            if self.state['reference'] is None:
                opening, previous = feed.reference()
                observed = feed.now_fn() if hasattr(feed, 'now_fn') else now
                self.state['reference'] = dict(official_open=opening, previous_close=previous,
                                               available_at=observed.isoformat())
                self.save('reference_observed', observed, reference=self.state['reference'])
            if now < at(self.day, '09:20'):
                return
            raw = self.state['reference']
            reference = SpotReference(self.day, self.sessions[self.day]['previous'], number(raw['official_open']),
                                      number(raw['previous_close']), datetime.fromisoformat(raw['available_at']), True)
            opening = feed.bars(SPOT, at(self.day, '09:15'), at(self.day, '09:19'))
            observed = feed.now_fn() if hasattr(feed, 'now_fn') else now
            if observed >= at(self.day, '09:20:20'):
                self.state.update(phase='no_trade', reason='missed_signal_window')
                self.save('no_trade', observed)
                return
            decision = decide(reference, [opening[at(self.day, f'09:{m}')] for m in range(15, 20)], self.config)
            if decision.signal is None:
                self.state.update(phase='no_trade', reason=decision.reason)
                self.save('no_trade', now)
                return
            contract = choose_contract(decision.signal, self.contracts, list(self.sessions))
            self.state.update(phase='entry_pending',
                signal=json.loads(json.dumps(asdict(decision.signal), default=str)),
                contract=json.loads(json.dumps(asdict(contract), default=str)))
            self.save('signal_and_contract_frozen', observed, signal=self.state['signal'], contract=self.state['contract'])
            return
        if phase == 'entry_pending':
            if now < at(self.day, '09:21'):
                return
            if now > at(self.day, '09:21:20'):
                self.state.update(phase='no_trade', reason='missed_entry_window')
                self.save('no_trade', now)
                return
            signal = self.signal()
            pending = feed.bars(SPOT, at(self.day, '09:20'), at(self.day, '09:20'))[at(self.day, '09:20')]
            if (pending.high >= signal.target if signal.option_type == 'CE' else pending.low <= signal.target):
                self.state.update(phase='no_trade', reason='target_before_entry')
                self.save('no_trade', now)
                return
            contract = self.state['contract']
            quote = feed.quote(contract['symbol'])
            received = quote.get('received_at', now)
            if received > at(self.day, '09:21:20'):
                self.state.update(phase='no_trade', reason='missed_entry_window')
                self.save('no_trade', received)
                return
            if quote['timestamp'] < at(self.day, '09:21') or not -2 <= (received - quote['timestamp']).total_seconds() <= 10:
                raise TradingError('Stale quote; paper entry refused')
            entry = fill_price(quote['ask'], 1, number(self.config.slippage_bps))
            lots = size_lots(number(self.state['equity']), entry, contract['lot_size'], self.config)
            if not lots:
                self.state.update(phase='no_trade', reason='insufficient_capital')
                self.save('no_trade', now)
                return
            position = dict(symbol=contract['symbol'], lot_size=contract['lot_size'], lots=lots,
                quantity=lots * contract['lot_size'], entry_price=entry, entry_ask=quote['ask'],
                entry_time=received.isoformat(), entry_quote_time=quote['timestamp'].isoformat())
            self.state.update(phase='open', position=position, last_processed=at(self.day, '09:20').isoformat(), error=None)
            self.save('simulated_entry', received, position=position)
            return
        if phase == 'open':
            signal, position = self.signal(), self.state['position']
            if self.state.get('exit_pending'):
                self._close(feed, now, self.state['exit_pending'])
                return
            first = datetime.fromisoformat(self.state['last_processed']) + timedelta(minutes=1)
            last = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
            if first > last:
                return
            spots = feed.bars(SPOT, first, last)
            options = feed.bars(position['symbol'], first, last)
            minute = first
            while minute <= last:
                spot, option = spots[minute], options[minute]
                if option.volume <= 0:
                    raise TradingError('Zero-volume option minute; trigger monitoring unresolved')
                bullish = signal.option_type == 'CE'
                flags = dict(premium_stop=option.low <= number(position['entry_price']) * (1 - number(self.config.stop_fraction)),
                    spot_failure=spot.low < signal.failure_level if bullish else spot.high > signal.failure_level,
                    gap_fill=spot.high >= signal.target if bullish else spot.low <= signal.target)
                self.state['last_processed'] = minute.isoformat()
                if any(flags.values()):
                    reason = next(key for key, active in flags.items() if active)
                    self.state['exit_pending'] = reason
                    self.state['exit_not_before'] = (minute + timedelta(minutes=1)).isoformat()
                    self.save('exit_trigger_observed', now, trigger_minute=minute, flags=flags)
                    self._close(feed, now, reason)
                    return
                minute += timedelta(minutes=1)
            self.save('completed_minutes_processed', now)


def run(config, calendar_file, token_file, output, kill_file='STOP_OPTIONS_PAPER',
        *, now_fn=lambda: datetime.now(IST), sleep_fn=time.sleep, feed_factory=MarketData):
    now = now_fn().astimezone(IST)
    sessions, day = calendar(calendar_file), now.date()
    if day not in sessions or not sessions[day]['regular']:
        raise TradingError('Today is absent or excluded in the supplied exchange calendar')
    if now >= at(day, '15:30'):
        raise TradingError('Paper recovery cutoff 15:30 reached; start the next session before 09:16 IST')
    journal = Journal(output)
    try:
        # A first launch after the decision window never needs to contact FYERS.
        if journal.load() is None and now >= at(day, '09:20:20'):
            raise TradingError('Too late for a new paper session; start before 09:16 IST next session')
        feed = feed_factory(token_file, output, now_fn=now_fn)
        contracts = feed.master(sessions)
        fingerprint = _hash(dict(session=str(day), config=config.snapshot(),
            calendar_sha256=hashlib.sha256(Path(calendar_file).read_bytes()).hexdigest(), contracts=[asdict(c) for c in contracts]))
        engine = PaperSession(config, day, sessions, contracts, journal, fingerprint)
        engine.save('started_or_resumed', now)
        print('OPTIONS PAPER ONLY: live data, local simulated fills, no order endpoints.')
        while True:
            now = now_fn().astimezone(IST)
            if now.date() != day or now >= at(day, '15:30'):
                if engine.state['position']:
                    engine.state['error'] = 'Session ended with unresolved paper position; no synthetic closing price invented'
                    engine.save('unresolved_session_end', now)
                break
            try:
                engine.step(now, feed, Path(kill_file).exists())
            except (TradingError, OSError, ValueError, KeyError) as exc:
                engine.state['error'] = str(exc)
                engine.state['data_errors'].append(dict(observed_at=now.isoformat(), error=str(exc),
                                                        position_open=engine.state['position'] is not None))
                engine.save('data_error_no_fill', now, error=str(exc))
                print(f'{now.isoformat()} DATA ERROR: {exc}')
                if getattr(feed, 'requests', 0) >= getattr(feed, 'limit', 1000):
                    raise TradingError('Paper data budget exhausted; saved position is not assumed closed') from None
            if engine.state['phase'] in ('closed', 'no_trade', 'halted'):
                break
            sleep_fn(5)
        if engine.state['position']:
            raise TradingError('Paper position remains unresolved; see saved state.json')
        return engine.state
    except KeyboardInterrupt:
        print('Paper process stopped; saved positions remain in the journal for restart.')
        return journal.load() or dict(phase='interrupted', position=None)
    finally:
        journal.close()

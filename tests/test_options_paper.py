from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gapfade.model import Bar, IST, TradingError, at
from gapfade.options_model import OptionsConfig, OptionContract
from gapfade.options_paper import Journal, PaperSession, run
from gapfade.options_paper_feed import MarketData, parse_master, SPOT

DAY = date(2026, 1, 5)


class FakeFeed:
    def __init__(self):
        self.now = at(DAY, '09:16')
        self.stale = False
        self.target = False
        self.missing = False
        self.calls = []

    def reference(self):
        return D('99'), D('100')

    def quote(self, symbol):
        self.calls.append(('quote', symbol))
        bid = D('110') if self.now >= at(DAY, '09:22') else D('99')
        return dict(bid=bid, ask=bid + 1, timestamp=self.now - timedelta(seconds=60 if self.stale else 0))

    def bars(self, symbol, first, last):
        self.calls.append(('bars', symbol, first, last))
        if self.missing:
            raise TradingError('Missing live minute')
        result = {}
        minute = first
        while minute <= last:
            if symbol == SPOT:
                opening = D('99') if minute.time() < at(DAY, '09:20').time() else D('99.2')
                high = D('100.1') if self.target and minute >= at(DAY, '09:21') else D('99.3')
                result[minute] = Bar(minute, 'NIFTY', opening, high, D('98.9'), D('99.2'), D('0'))
            else:
                result[minute] = Bar(minute, symbol, D('100'), D('101'), D('99'), D('100'), D('10'))
            minute += timedelta(minutes=1)
        return result


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'paper'
        self.journal = Journal(self.output)
        self.addCleanup(self.journal.close)
        self.sessions = {DAY + timedelta(days=i): dict(previous=DAY - timedelta(days=3) if i == 0 else DAY + timedelta(days=i-1), regular=True) for i in range(5)}
        self.contracts = [OptionContract('NSE:NIFTYTESTCE', 'NIFTY', 'CE', D('100'), DAY + timedelta(days=4),
                                        at(DAY, '09:00'), DAY, DAY + timedelta(days=4), 10)]
        self.config = replace(OptionsConfig(), fee_bps='0', fixed_fee_per_order='0', slippage_bps='0')
        self.feed = FakeFeed()
        self.engine = PaperSession(self.config, DAY, self.sessions, self.contracts, self.journal, 'same')

    def step(self, clock, **kwargs):
        self.feed.now = at(DAY, clock)
        self.engine.step(self.feed.now, self.feed, **kwargs)

    def enter(self):
        self.step('09:16')
        self.step('09:20:03')
        self.step('09:21:03')

    def test_forward_entry_exit_and_persistent_restart(self):
        self.enter()
        self.assertEqual(self.engine.state['position']['quantity'], 20)
        self.assertEqual(D(self.engine.state['position']['entry_price']), D('100'))
        restarted = PaperSession(self.config, DAY, self.sessions, self.contracts, self.journal, 'same')
        self.engine = restarted
        self.feed.target = True
        self.step('09:22:03')
        self.assertEqual(self.engine.state['phase'], 'closed')
        self.assertEqual(D(self.engine.state['trade']['net_pnl']), D('200'))
        entries = self.journal.db.execute("SELECT document FROM events").fetchall()
        self.assertEqual(sum(json.loads(row[0])['event'] == 'simulated_entry' for row in entries), 1)
        self.step('09:23')
        self.assertEqual(self.engine.state['phase'], 'closed')

    def test_stale_entry_rejected_and_no_late_chasing(self):
        self.step('09:16')
        self.step('09:20:03')
        self.feed.stale = True
        with self.assertRaisesRegex(TradingError, 'Stale'):
            self.step('09:21:03')
        self.assertIsNone(self.engine.state['position'])
        self.step('09:21:21')
        self.assertEqual(self.engine.state['reason'], 'missed_entry_window')

    def test_missing_data_and_stale_exit_do_not_flatten_position(self):
        self.enter()
        self.feed.missing = True
        with self.assertRaises(TradingError):
            self.step('09:22:03')
        self.assertIsNotNone(self.engine.state['position'])
        self.feed.missing = False
        self.feed.stale = True
        with self.assertRaises(TradingError):
            self.step('15:00:03')
        self.assertIsNotNone(self.engine.state['position'])
        self.feed.stale = False
        self.step('15:00:08')
        self.assertEqual(self.engine.state['trade']['exit_reason'], 'scheduled_exit')

    def test_kill_switch_and_fingerprint_and_process_lock(self):
        self.enter()
        self.step('09:21:08', stopped=True)
        self.assertEqual(self.engine.state['trade']['exit_reason'], 'kill_switch')
        with self.assertRaises(TradingError):
            PaperSession(self.config, DAY, self.sessions, self.contracts, self.journal, 'changed')
        with self.assertRaises(TradingError):
            Journal(self.output)

    def test_early_and_late_launch_and_capital_skip(self):
        self.step('09:10')
        self.assertFalse(self.feed.calls)
        self.step('09:20:21')
        self.assertEqual(self.engine.state['reason'], 'missed_signal_window')
        path = Path(self.tmp.name) / 'calendar.csv'
        path.write_text('session,previous_session,is_regular\n2026-01-05,2026-01-02,true\n')
        with patch('gapfade.options_paper.MarketData', side_effect=AssertionError('network')):
            with self.assertRaisesRegex(TradingError, 'Too late'):
                run(self.config, path, 'missing-token', Path(self.tmp.name) / 'late', now_fn=lambda: at(DAY, '12:00'))

    def test_whole_lot_budget_is_not_relaxed(self):
        self.engine.contracts = [replace(self.contracts[0], lot_size=75)]
        self.enter()
        self.assertEqual(self.engine.state['reason'], 'insufficient_capital')
        self.assertIsNone(self.engine.state['position'])

    def test_complete_forward_loop_uses_no_network_with_fake_feed(self):
        path = Path(self.tmp.name) / 'calendar.csv'
        path.write_text('session,previous_session,is_regular\n' + '\n'.join(
            f'{day},{values["previous"]},true' for day, values in self.sessions.items()) + '\n')
        times = iter(['09:16', '09:20:03', '09:21:03', '09:22:03'])
        clock = [at(DAY, next(times))]
        feed = FakeFeed()
        feed.target = True
        feed.master = lambda sessions: self.contracts
        def advance(seconds):
            clock[0] = at(DAY, next(times))
            feed.now = clock[0]
        with patch('gapfade.options_paper_feed.urlopen', side_effect=AssertionError('network')):
            result = run(self.config, path, 'unused-token', Path(self.tmp.name) / 'loop',
                         now_fn=lambda: clock[0], sleep_fn=advance,
                         feed_factory=lambda *args, **kwargs: feed)
        self.assertEqual(result['phase'], 'closed')
        self.assertEqual(D(result['trade']['net_pnl']), D('200'))


class FeedTests(unittest.TestCase):
    def test_current_master_lot_size_and_expiry_are_used(self):
        row = ['token', 'name', '14', '75', '.05', '', '', '', str(int(at(DAY + timedelta(days=3), '15:30').timestamp())),
               'NSE:NIFTYTESTCE', '', '', '', 'NIFTY', '', '25000', 'CE', '']
        contracts = parse_master(','.join(row), at(DAY, '09:10'), [DAY + timedelta(days=i) for i in range(5)])
        self.assertEqual(contracts[0].lot_size, 75)
        self.assertEqual(contracts[0].expiry_session, DAY + timedelta(days=3))

    def test_adapter_has_no_order_endpoints_and_rejects_partial_bars(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token.json'
            token.write_text(json.dumps(dict(client_id='test', access_token='private-token')))
            feed = MarketData(token, directory, now_fn=lambda: at(DAY, '09:20'))
            with patch('gapfade.options_paper_feed.urlopen', side_effect=AssertionError('unexpected request')):
                with self.assertRaises(TradingError):
                    feed.get('/api/v3/orders', {})
                with self.assertRaises(TradingError):
                    feed.bars(SPOT, at(DAY, '09:20'), at(DAY, '09:20'))
            for name in ('place', 'cancel', 'orders', 'positions'):
                self.assertFalse(hasattr(feed, name))

    def test_request_budget_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token.json'
            token.write_text(json.dumps(dict(client_id='test', access_token='private-token')))
            usage = Path(directory) / 'data_request_usage.json'
            usage.write_text(json.dumps(dict(requests=1000)))
            feed = MarketData(token, directory)
            with patch('gapfade.options_paper_feed.urlopen', side_effect=AssertionError('network')):
                with self.assertRaisesRegex(TradingError, 'budget exhausted'):
                    feed.get('/data/quotes', {'symbols': SPOT})

    def test_provider_rejects_stale_and_crossed_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token.json'
            token.write_text(json.dumps(dict(client_id='test', access_token='private-token')))
            feed = MarketData(token, directory, now_fn=lambda: at(DAY, '09:21'))
            for values in (dict(tt=int(at(DAY, '09:20').timestamp()), bid=99, ask=100),
                           dict(tt=int(at(DAY, '09:21').timestamp()), bid=101, ask=100)):
                with patch.object(feed, 'values', return_value=values):
                    with self.assertRaises(TradingError):
                        feed.quote('TEST')


if __name__ == '__main__':
    unittest.main()

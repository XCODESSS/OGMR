from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal as D
import io
import json
import math
from pathlib import Path
from statistics import mean, stdev
import tempfile
import unittest
from unittest.mock import patch

from gapfade import data, options_walkforward as wf
from gapfade.__main__ import main
from gapfade.model import TradingError
from gapfade.options_data import OptionsDataset
from gapfade.options_model import OptionsConfig
from walkforward_fixtures import contract, build, sessions_from, FIRST

# Rs 60,000 buys exactly one lot of this fixture under either a fixed-lot or a capital-limit sizing rule.
ZERO_COST = replace(OptionsConfig(), capital='60000', fee_bps='0', fixed_fee_per_order='0', slippage_bps='0')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def load(self, *parts):
        return json.loads(self.root.joinpath(*parts).read_text())

    def ledger(self, *parts):
        return data.rows(self.root.joinpath(*parts))

    def spec(self, train=2, oos=1, mode='rolling'):
        return wf.WalkForwardSpec(mode, train, oos)


class WindowTests(unittest.TestCase):
    days = sessions_from(FIRST, 11)

    def test_rolling_boundaries_and_trailing_sessions(self):
        folds, unused = wf.build_folds(self.days, wf.WalkForwardSpec('rolling', 4, 2))
        self.assertEqual([(f.train[0], f.train[-1], f.oos[0], f.oos[-1]) for f in folds],
                         [(self.days[0], self.days[3], self.days[4], self.days[5]),
                          (self.days[2], self.days[5], self.days[6], self.days[7]),
                          (self.days[4], self.days[7], self.days[8], self.days[9])])
        self.assertEqual(unused, [self.days[10]])

    def test_expanding_training_keeps_first_session(self):
        folds, _ = wf.build_folds(self.days, wf.WalkForwardSpec('expanding', 4, 2))
        self.assertEqual({f.train[0] for f in folds}, {self.days[0]})
        self.assertEqual([len(f.train) for f in folds], [4, 6, 8])

    def test_overlap_and_gaps_are_rejected(self):
        with self.assertRaisesRegex(TradingError, 'Overlapping'):
            wf.WalkForwardSpec('rolling', 4, 2, 1)
        with self.assertRaisesRegex(TradingError, 'unevaluated'):
            wf.WalkForwardSpec('rolling', 4, 2, 3)
        first = wf.Fold(1, tuple(self.days[:2]), tuple(self.days[2:4]))
        second = wf.Fold(2, tuple(self.days[:3]), tuple(self.days[3:5]))
        with self.assertRaisesRegex(TradingError, 'overlapping'):
            wf.validate_folds([first, second])

    def test_training_after_evaluation_is_rejected(self):
        with self.assertRaisesRegex(TradingError, 'must end before'):
            wf.validate_folds([wf.Fold(1, tuple(self.days[3:5]), tuple(self.days[2:3]))])

    def test_insufficient_history_states_the_shortfall(self):
        with self.assertRaisesRegex(TradingError, 'Insufficient history: need 10 training and 2'):
            wf.build_folds(self.days, wf.WalkForwardSpec('rolling', 10, 2))

    def test_bad_lengths_are_rejected(self):
        for args in (('sideways', 4, 2), ('rolling', 0, 2), ('rolling', 4, 0), ('rolling', 4.0, 2)):
            with self.subTest(args=args), self.assertRaises(TradingError):
                wf.WalkForwardSpec(*args)


class CarryForwardTests(Base):
    PLAN = ['flat', 'flat', '110', '120', '90', 'flat', 'flat']

    def run_plan(self):
        build(self.root / 'in', self.PLAN, counts=(5, 1, 1))
        return wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')

    def test_stitched_carries_equity_and_independent_folds_restart(self):
        calls = []
        real = wf.replay_session

        def spy(config, view, day, equity):
            calls.append((day, equity))
            return real(config, view, day, equity)

        with patch.object(wf, 'replay_session', spy):
            summary = self.run_plan()
        days = sessions_from(FIRST, 7)
        # Hand check: +100, +200, -100, then a no-trade session, from Rs 60,000.
        stitched = self.ledger('out', 'stitched_oos', 'daily_equity.csv')
        self.assertEqual([D(r['start_equity']) for r in stitched], [D(60000), D(60100), D(60300), D(60200)])
        self.assertEqual([D(r['end_equity']) for r in stitched], [D(60100), D(60300), D(60200), D(60200)])
        self.assertEqual(D(stitched[1]['return']), D(200) / D(60100))
        for fold, net in ((1, 100), (2, 200), (3, -100), (4, 0)):
            rows = self.ledger('out', 'independent_folds', f'fold_{fold:03d}', 'daily_equity.csv')
            self.assertEqual(D(rows[0]['start_equity']), D(60000))
            self.assertEqual(D(rows[0]['end_equity']), D(60000) + net)
        self.assertEqual(D(self.ledger('out', 'independent_folds', 'fold_002', 'daily_equity.csv')[0]['return']),
                         D(200) / D(60000))
        self.assertEqual(summary['stitched_out_of_sample']['final_equity'], '60200')
        # The stitched pass replays each session with the running equity, not fold results.
        self.assertIn((days[3], D(60100)), calls)
        self.assertIn((days[3], D(60000)), calls)

    def test_metrics_are_hand_checkable(self):
        summary = self.run_plan()['stitched_out_of_sample']
        self.assertEqual((summary['net_profit'], summary['gross_pnl'], summary['fees'], summary['trade_count']),
                         ('200', '200', '0', 3))
        self.assertAlmostEqual(summary['max_drawdown_daily_close'], -100 / 60300)
        returns = [100 / 60000, 200 / 60100, -100 / 60300, 0]
        self.assertAlmostEqual(summary['sharpe_daily_annualized_rf_zero'], mean(returns) / stdev(returns) * math.sqrt(252))
        self.assertIn('sample stdev', summary['sharpe_convention'])
        self.assertIn('not intraday', summary['drawdown_disclosure'])
        self.assertEqual(summary['coverage_counts'], {'traded': 3, 'no_trade': 1})

    def test_costs_and_slippage_reuse_the_existing_model(self):
        build(self.root / 'in', ['flat', 'flat', '110', 'flat', 'flat'], counts=(3, 1, 1))
        config = replace(OptionsConfig(), fee_bps='0', fixed_fee_per_order='20', slippage_bps='100')
        summary = wf.run_walkforward(config, wf.WalkForwardSpec('rolling', 2, 2), self.root / 'in',
                                     self.root / 'out')['stitched_out_of_sample']
        # Fills 101 and 108.9, 10 units: gross 79, fees 40, net 39, slippage 10 + 11.
        self.assertEqual(D(summary['gross_pnl']), D(79))
        self.assertEqual(D(summary['fees']), D(40))
        self.assertEqual(D(summary['net_profit']), D(39))
        self.assertEqual(D(summary['modeled_slippage_cost']), D(21))


class LeakageTests(Base):
    def test_view_refuses_other_sessions_and_later_listings(self):
        def catalog(days):
            return [contract('SYN:CE', days[-1]), contract('SYN:LATE', days[-1], strike='24800', listed=str(days[3]),
                                                           valid_from=str(days[3]))]
        days = build(self.root / 'in', ['110'] * 6, contracts=catalog)
        view = wf.SessionView(OptionsDataset(self.root / 'in'), [days[1]])
        with self.assertRaisesRegex(TradingError, 'Point-in-time'):
            view.spot(days[2])
        with self.assertRaisesRegex(TradingError, 'Point-in-time'):
            view.option(days[2], 'SYN:CE')
        self.assertNotIn(days[2], view.references)
        self.assertEqual([c.symbol for c in view.contracts], ['SYN:CE'])

    def test_future_prices_cannot_change_earlier_folds(self):
        for name, last in (('a', '110'), ('b', '50')):
            build(self.root / name, ['110', '110', '110', '110', last, '110', 'flat'], counts=(5, 1, 1))
            wf.run_walkforward(ZERO_COST, self.spec(), self.root / name, self.root / f'{name}-out',
                               selection=wf.SelectionSpec({'gap_threshold': ['0.02', '0.005']}, 'net_pnl', 0))
        for fold in ('fold_001', 'fold_002'):
            for file in ('trades.csv', 'daily_equity.csv', 'coverage.csv'):
                self.assertEqual(self.root.joinpath('a-out/independent_folds', fold, file).read_bytes(),
                                 self.root.joinpath('b-out/independent_folds', fold, file).read_bytes())
        # Earlier folds really traded, so identical ledgers are meaningful.
        self.assertEqual(len(self.ledger('a-out', 'independent_folds', 'fold_001', 'trades.csv')), 1)
        first = [r for r in self.ledger('a-out', 'training_candidates.csv') if r['fold'] in ('1', '2')]
        second = [r for r in self.ledger('b-out', 'training_candidates.csv') if r['fold'] in ('1', '2')]
        self.assertEqual(first, second)
        self.assertNotEqual(self.ledger('a-out', 'independent_folds', 'fold_003', 'trades.csv'),
                            self.ledger('b-out', 'independent_folds', 'fold_003', 'trades.csv'))

    def test_contract_selection_uses_listings_and_lot_sizes_of_the_day(self):
        def catalog(days):
            return [contract('SYN:CE', days[-1], valid_to=str(days[3]), lot=5),
                    contract('SYN:CE', days[-1], valid_from=str(days[4]), lot=10),
                    contract('SYN:LATE', days[-1], strike='24800', listed=str(days[5]), valid_from=str(days[5]), lot=10)]
        days = build(self.root / 'in', ['flat', 'flat', '110', '110', '110', '110', 'flat', 'flat'], counts=(6, 1, 1),
                     contracts=catalog)
        wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')
        trades = self.ledger('out', 'stitched_oos', 'trades.csv')
        self.assertEqual([(t['session'], t['symbol'], t['lot_size']) for t in trades],
                         [(str(days[2]), 'SYN:CE', '5'), (str(days[3]), 'SYN:CE', '5'),
                          (str(days[4]), 'SYN:CE', '10'), (str(days[5]), 'SYN:LATE', '10')])

    def test_two_session_expiry_rule_uses_the_calendar_without_future_prices(self):
        def catalog(days):
            return [contract('SYN:NEAR', days[3]), contract('SYN:FAR', days[-1], lot=5)]
        days = build(self.root / 'in', ['flat', 'flat', '110', '110', 'flat', 'flat'], counts=(4, 1, 1), contracts=catalog)
        wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')
        trades = self.ledger('out', 'stitched_oos', 'trades.csv')
        # days[2] has one session left before the near expiry, so both trades use the far contract.
        self.assertEqual([t['symbol'] for t in trades], ['SYN:FAR', 'SYN:FAR'])
        self.assertEqual(trades[0]['session'], str(days[2]))


class SelectionTests(Base):
    GRID = {'gap_threshold': ['0.02', '0.005']}  # c000 never trades a 1% gap; c001 does.

    def selected(self, name='out'):
        return self.load(name, 'selected_configs.json')

    def run_selection(self, plan, name='out', source='in', grid=None, objective='net_pnl', minimum=0, config=ZERO_COST):
        if not self.root.joinpath(source).exists():
            build(self.root / source, plan, counts=(len(plan) - 2, 1, 1))
        return wf.run_walkforward(config, wf.WalkForwardSpec('rolling', 2, 1), self.root / source, self.root / name,
                                  selection=wf.SelectionSpec(grid or self.GRID, objective, minimum))

    def test_evaluation_outcomes_cannot_affect_selection(self):
        self.run_selection(['110', '110', '50', 'flat', 'flat'], 'bad', 'in-bad')
        self.run_selection(['110', '110', '190', 'flat', 'flat'], 'good', 'in-good')
        self.assertEqual(self.selected('bad')[0]['candidate_id'], 'c001')
        self.assertEqual(self.selected('bad')[0], self.selected('good')[0])
        # The evaluation loss is reported, not avoided.
        self.assertEqual(self.ledger('bad', 'independent_folds', 'fold_001', 'trades.csv')[0]['net_pnl'], '-500')

    def test_training_losses_choose_the_candidate_that_avoided_them(self):
        self.run_selection(['90', '90', '190', 'flat', 'flat'])
        self.assertEqual(self.selected()[0]['candidate_id'], 'c000')
        self.assertEqual(self.ledger('out', 'independent_folds', 'fold_001', 'trades.csv'), [])

    def test_candidates_replay_training_only_and_freeze_before_evaluation(self):
        plan = ['110', '110', '110', '90', 'flat', 'flat']
        days = sessions_from(FIRST, len(plan))
        build(self.root / 'in', plan, counts=(4, 1, 1))
        real, events = wf.replay_session, []
        output = self.root / 'out'

        def spy(config, view, day, equity):
            frozen = json.loads((output / 'selected_configs.json').read_text()) \
                if (output / 'selected_configs.json').exists() else []
            events.append((day, config.gap_threshold, len(frozen)))
            return real(config, view, day, equity)

        with patch.object(wf, 'replay_session', spy):
            wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', output,
                               selection=wf.SelectionSpec(self.GRID, 'net_pnl', 0))
        # Fold 1 trains on days 0-1, then evaluates day 2 after one frozen record exists.
        self.assertEqual(events[:4], [(days[0], '0.02', 0), (days[1], '0.02', 0), (days[0], '0.005', 0), (days[1], '0.005', 0)])
        self.assertEqual(events[4], (days[2], '0.005', 1))
        self.assertEqual(events[5], (days[2], '0.005', 1))
        # Fold 2 trains on days 1-2 and has not read day 3 until its own record is frozen.
        self.assertTrue(all(day in days[1:3] for day, _, count in events[6:10] if count == 1))
        self.assertEqual(events[10][2], 2)

    def test_every_candidate_is_recorded(self):
        self.run_selection(['110', '110', '110', '110', 'flat', 'flat'])
        rows = self.ledger('out', 'training_candidates.csv')
        self.assertEqual([(r['fold'], r['candidate_id']) for r in rows if r['fold'] == '1'], [('1', 'c000'), ('1', 'c001')])
        self.assertEqual(len(rows), 2 * 3)  # Three folds over five research sessions.
        self.assertEqual({r['selected'] for r in rows if r['candidate_id'] == 'c001'}, {'True'})

    def test_ties_keep_the_first_canonical_candidate(self):
        self.run_selection(['110', '110', 'flat', 'flat', 'flat'], grid={'gap_threshold': ['0.03', '0.02']})
        rows = [r for r in self.ledger('out', 'training_candidates.csv') if r['fold'] == '1']
        self.assertEqual([(r['candidate_id'], r['selected'], r['tie_break']) for r in rows],
                         [('c000', 'True', 'first_canonical_candidate'), ('c001', 'False', '')])

    def test_minimum_trades_makes_candidates_ineligible_and_blocks_when_none_qualify(self):
        build(self.root / 'in-flat', ['flat'] * 5, counts=(3, 1, 1))
        with self.assertRaisesRegex(TradingError, 'withheld'):
            self.run_selection([], minimum=1, name='blocked', source='in-flat')
        self.assertFalse((self.root / 'blocked/summary.json').exists())
        self.assertIn('No candidate met', self.ledger('blocked', 'folds.csv')[0]['error'])

    def test_failed_candidate_invalidates_the_fold_instead_of_being_dropped(self):
        build(self.root / 'in', ['110', '110', '110', 'flat', 'flat'], counts=(3, 1, 1))
        day = sessions_from(FIRST, 1)[0]
        path = self.root / f'in/options/{day}.csv'
        rows = [r for r in data.rows(path) if ':22:' not in r['timestamp']]
        data.write_csv(path, rows, list(rows[0]))
        with self.assertRaisesRegex(TradingError, 'withheld'):
            self.run_selection([], source='in')
        candidates = [r for r in self.ledger('out', 'training_candidates.csv') if r['fold'] == '1']
        self.assertEqual({r['candidate_id']: r['status'] for r in candidates}, {'c000': 'valid', 'c001': 'invalid'})
        self.assertEqual(self.ledger('out', 'folds.csv')[0]['training_status'], 'invalid')

    def test_grid_limits(self):
        with self.assertRaisesRegex(TradingError, 'limit is 64'):
            wf.SelectionSpec({'gap_threshold': [str(0.001 + i / 1000) for i in range(65)]}, 'net_pnl')
        for grid in ({'fee_bps': ['0']}, {'capital': ['1']}, {}, {'gap_threshold': []},
                     {'gap_threshold': ['0.01', '0.01']}):
            with self.subTest(grid=grid), self.assertRaises(TradingError):
                wf.SelectionSpec(grid, 'net_pnl')
        with self.assertRaisesRegex(TradingError, 'objective'):
            wf.SelectionSpec(self.GRID, 'oos_net_pnl')
        with self.assertRaises(TradingError):
            wf.SelectionSpec({'gap_threshold': ['0']}, 'net_pnl').candidates(ZERO_COST)

    def test_fixed_mode_does_not_replay_training(self):
        plan = ['110', '90', '110', 'flat', 'flat']
        days = sessions_from(FIRST, len(plan))
        build(self.root / 'in', plan, counts=(3, 1, 1))
        calls, real = [], wf.replay_session

        def spy(config, view, day, equity):
            calls.append(day)
            return real(config, view, day, equity)

        with patch.object(wf, 'replay_session', spy):
            wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')
        self.assertEqual(set(calls), {days[2], days[3]})
        self.assertEqual(self.ledger('out', 'training_candidates.csv'), [])
        self.assertEqual(self.load('out', 'selected_configs.json')[0]['candidate_id'], 'fixed')


class HeldOutTests(Base):
    PLAN = ['flat', 'flat', '110', '90', '120', 'flat']

    def setUp(self):
        super().setUp()
        self.days = build(self.root / 'in', self.PLAN, counts=(3, 1, 2))

    def frozen(self, name='dev'):
        wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / name)
        return self.root / name

    def test_research_default_never_reads_the_test_split(self):
        calls, real = [], wf.replay_session

        def spy(config, view, day, equity):
            calls.append(day)
            return real(config, view, day, equity)

        with patch.object(wf, 'replay_session', spy):
            self.frozen()
        self.assertEqual(max(calls), self.days[3])

    def test_explicit_dates_cannot_reach_the_test_split(self):
        with self.assertRaisesRegex(TradingError, 'Held-out test'):
            wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'bad', end=self.days[4])
        self.assertEqual(self.load('bad', 'run_status.json')['status'], 'invalid')
        self.assertFalse((self.root / 'bad/summary.json').exists())

    def test_test_requires_frozen_run_uses_its_procedure_and_runs_once(self):
        frozen = self.frozen()
        summary = wf.run_heldout_test(frozen, self.root / 'in', self.root / 'test')
        self.assertEqual(summary['run_kind'], 'held_out_test')
        self.assertEqual([r['oos_start'] for r in summary['independent_folds']], [str(self.days[4]), str(self.days[5])])
        self.assertEqual(summary['procedure_sha256'], self.load('dev', 'manifest.json')['procedure_sha256'])
        with self.assertRaisesRegex(TradingError, 'already evaluated'):
            wf.run_heldout_test(frozen, self.root / 'in', self.root / 'test2')

    def test_invalid_or_tampered_frozen_runs_are_refused_without_a_claim(self):
        frozen = self.frozen()
        spot = self.root / f'in/spot/{self.days[0]}.csv'
        spot.write_text(spot.read_text().replace('24990', '24991'))
        with self.assertRaisesRegex(TradingError, 'Input changed after'):
            wf.run_heldout_test(frozen, self.root / 'in', self.root / 't1')
        self.assertFalse((frozen / wf.TEST_CLAIM).exists())
        (frozen / 'run_status.json').write_text('{"status": "invalid"}')
        with self.assertRaisesRegex(TradingError, 'valid frozen'):
            wf.run_heldout_test(frozen, self.root / 'in', self.root / 't2')
        with self.assertRaisesRegex(TradingError, 'missing'):
            wf.run_heldout_test(self.root / 'nowhere', self.root / 'in', self.root / 't3')

    def test_changed_procedure_or_splits_are_refused(self):
        frozen = self.frozen()
        manifest = self.load('dev', 'manifest.json')
        original = json.dumps(manifest)
        manifest['procedure']['base_config']['gap_threshold'] = '0.001'
        (frozen / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(TradingError, 'recorded hash'):
            wf.run_heldout_test(frozen, self.root / 'in', self.root / 't1')
        manifest = json.loads(original)
        manifest['provenance']['research_splits']['test'] = ['2030-01-01', '2030-01-02']
        (frozen / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(TradingError, 'splits changed'):
            wf.run_heldout_test(frozen, self.root / 'in', self.root / 't2')


class InvalidFoldTests(Base):
    PLAN = ['flat', 'flat', '110', '120', '90', 'flat', 'flat']

    def setUp(self):
        super().setUp()
        self.days = build(self.root / 'in', self.PLAN, counts=(5, 1, 1))

    def drop_option_bar(self, day, minute):
        path = self.root / f'in/options/{day}.csv'
        rows = [r for r in data.rows(path) if f'T09:{minute}:' not in r['timestamp']]
        data.write_csv(path, rows, list(rows[0]))

    def test_missing_execution_bar_blocks_aggregate_and_keeps_diagnostics(self):
        self.drop_option_bar(self.days[3], 22)
        with self.assertRaisesRegex(TradingError, 'aggregate performance withheld'):
            wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')
        self.assertFalse((self.root / 'out/summary.json').exists())
        diagnostics = self.load('out', 'diagnostics.json')
        self.assertIsNone(diagnostics['aggregate_performance'])
        self.assertEqual([f['fold'] for f in diagnostics['invalid_folds']], [2])
        status = self.load('out', 'run_status.json')
        self.assertEqual((status['status'], status['position_open']), ('invalid', True))
        self.assertEqual({p['result'] for p in self.load('out', 'unresolved_positions.json')}, {'independent', 'stitched'})
        # Other folds keep independent diagnostics; the failed fold is not skipped.
        self.assertEqual([r['oos_status'] for r in self.ledger('out', 'folds.csv')], ['valid', 'invalid', 'valid', 'valid'])
        self.assertIsNone(self.load('out', 'independent_folds', 'fold_002', 'result.json')['metrics'])
        # Stitched equity cannot pass the unresolved session.
        coverage = self.ledger('out', 'stitched_oos', 'coverage.csv')
        self.assertEqual([r['status'] for r in coverage], ['traded', 'invalid', 'not_processed', 'not_processed'])
        self.assertEqual(self.load('out', 'stitched_oos', 'result.json')['metrics'], None)
        self.assertEqual(len(self.ledger('out', 'stitched_oos', 'trades.csv')), 1)

    def test_no_trade_sessions_stay_distinct_from_invalid_data(self):
        wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')
        coverage = self.ledger('out', 'stitched_oos', 'coverage.csv')
        self.assertEqual(coverage[3]['status'], 'no_trade')
        self.assertEqual(coverage[3]['reason'], 'below_gap_threshold')
        self.assertEqual(self.ledger('out', 'stitched_oos', 'daily_equity.csv')[3]['return'], '0')

    def test_missing_training_data_invalidates_that_fold(self):
        path = self.root / f'in/spot/{self.days[1]}.csv'
        rows = [r for r in data.rows(path) if 'T09:17:' not in r['timestamp']]
        data.write_csv(path, rows, list(rows[0]))
        with self.assertRaisesRegex(TradingError, 'withheld'):
            wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')
        folds = self.ledger('out', 'folds.csv')
        self.assertEqual(folds[0]['training_status'], 'invalid')
        self.assertIn('not eligible', folds[0]['error'])
        self.assertEqual(self.ledger('out', 'stitched_oos', 'coverage.csv')[0]['status'], 'not_processed')

    def test_insufficient_history_leaves_a_status_and_no_results(self):
        with self.assertRaisesRegex(TradingError, 'Insufficient history'):
            wf.run_walkforward(ZERO_COST, wf.WalkForwardSpec('rolling', 50, 1), self.root / 'in', self.root / 'out')
        self.assertEqual(self.load('out', 'run_status.json')['status'], 'invalid')
        self.assertFalse((self.root / 'out/stitched_oos').exists())

    def test_synthetic_inputs_without_research_splits_are_refused(self):
        path = self.root / 'in/provenance.json'
        provenance = json.loads(path.read_text())
        del provenance['research_splits']
        path.write_text(json.dumps(provenance))
        with self.assertRaisesRegex(TradingError, 'research_splits'):
            wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')

    def test_existing_output_is_never_overwritten(self):
        (self.root / 'out').mkdir()
        (self.root / 'out/keep.txt').write_text('keep')
        with self.assertRaises(FileExistsError):
            wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')
        self.assertEqual(sorted(p.name for p in (self.root / 'out').iterdir()), ['keep.txt'])


class ReproducibilityTests(Base):
    def test_reruns_are_byte_identical(self):
        build(self.root / 'in', ['110', '90', '110', '90', 'flat', 'flat'], counts=(4, 1, 1))
        for name in ('a', 'b'):
            wf.run_walkforward(ZERO_COST, wf.WalkForwardSpec('expanding', 2, 1), self.root / 'in', self.root / name,
                               selection=wf.SelectionSpec({'confirmation': [True, False], 'gap_threshold': ['0.02', '0.005']},
                                                          'net_expectancy_per_trade', 1))
        first = {p.relative_to(self.root / 'a'): p.read_bytes() for p in (self.root / 'a').rglob('*') if p.is_file()}
        second = {p.relative_to(self.root / 'b'): p.read_bytes() for p in (self.root / 'b').rglob('*') if p.is_file()}
        self.assertEqual(first, second)
        self.assertGreater(len(first), 10)

    def test_manifest_records_hashes_folds_and_information_rules(self):
        build(self.root / 'in', ['flat', 'flat', '110', 'flat', 'flat'], counts=(3, 1, 1))
        wf.run_walkforward(ZERO_COST, self.spec(), self.root / 'in', self.root / 'out')
        manifest = self.load('out', 'manifest.json')
        self.assertEqual(manifest['status'], 'valid')
        self.assertEqual(len(manifest['folds']), 2)
        self.assertEqual(manifest['information_set']['purge_sessions'], 0)
        self.assertEqual(manifest['procedure_sha256'], wf.canonical_hash(manifest['procedure']))
        self.assertIn('provenance.json', manifest['input_sha256'])
        self.assertIn('raw/source.txt', manifest['input_sha256'])
        self.assertIn('synthetic', manifest['synthetic_warning'].lower())
        self.assertEqual((manifest['network_access'], manifest['orders_submitted']), (False, 0))


class CLITests(Base):
    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_offline_demonstration_commands(self):
        grid = Path(__file__).resolve().parent.parent / 'examples/options/walkforward-selection.json'
        config = Path(__file__).resolve().parent.parent / 'config/options-research.json'
        with patch('gapfade.__main__.FyersClient', side_effect=AssertionError('unexpected network')), \
                patch('gapfade.fyers.urlopen', side_effect=AssertionError('unexpected network')):
            inputs = str(self.root / 'demo')
            self.assertEqual(self.run_cli('options-walkforward-demo', '--out', inputs)[0], 0)
            common = ['--config', str(config), '--data', inputs, '--mode', 'rolling', '--train-sessions', '4', '--oos-sessions', '4']
            code, out, _ = self.run_cli('options-walkforward', *common, '--out', str(self.root / 'fixed'))
            self.assertEqual(code, 0)
            result = json.loads(out)
            self.assertEqual((result['data_kind'], result['fold_count']), ('synthetic', 3))
            self.assertEqual(self.run_cli('options-walkforward', *common, '--selection', str(grid),
                                          '--out', str(self.root / 'selected'))[0], 0)
            self.assertEqual(self.run_cli('options-walkforward', *common, '--out', str(self.root / 'fixed'))[0], 1)
            test = ['options-walkforward-test', '--frozen-run', str(self.root / 'fixed'), '--data', inputs]
            self.assertEqual(self.run_cli(*test, '--out', str(self.root / 'test'))[0], 0)
            code, _, err = self.run_cli(*test, '--out', str(self.root / 'test2'))
            self.assertEqual(code, 1)
            self.assertIn('already evaluated', err)

    def test_bad_arguments_return_errors(self):
        code, _, err = self.run_cli('options-walkforward', '--data', 'missing', '--mode', 'rolling', '--train-sessions', '4',
                                    '--oos-sessions', '2', '--step-sessions', '1', '--out', str(self.root / 'x'))
        self.assertEqual(code, 1)
        self.assertIn('Overlapping', err)


if __name__ == '__main__':
    unittest.main()

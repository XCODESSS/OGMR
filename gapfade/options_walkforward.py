"""Offline walk-forward orchestration around the existing options replay.

No trading rule lives here. Every session goes through options_backtest.replay_session.
No network, FYERS or order code is imported.
"""
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
import hashlib
from itertools import product
import json
import os
from pathlib import Path

from .data import write_csv
from .model import IST, TradingError, ZERO, at, number
from .options_backtest import LIMITATIONS, ReplayError, _ledgers, performance, replay_session
from .options_data import OptionsDataset, digest, write_json
from .options_model import OptionsConfig, VERSION
from .options_strategy import decide

MAX_CANDIDATES = 64
SELECTABLE = ('confirmation', 'gap_threshold', 'stop_fraction')
OBJECTIVES = ('net_pnl', 'net_expectancy_per_trade', 'sharpe_daily_rf_zero')
RESEARCH_RUN, TEST_RUN = 'research_walk_forward', 'held_out_test'
TEST_CLAIM = 'test_evaluation_claimed.json'
SHARPE_CONVENTION = ('mean(daily simple return) / sample stdev (n-1) * sqrt(252), risk-free rate 0, using '
                     'every regular session including zero-return no-trade sessions; special sessions excluded')
DRAWDOWN_DISCLOSURE = 'Measured at daily closes only. This is not intraday drawdown; intraday losses may be larger.'
SIZING_NOTE = ('Position size comes from the existing replay rule, unchanged. Stitched sessions receive the running equity '
               'and independent folds receive configured capital; a rule that ignores equity gives both the same per-trade rupee P&L.')
INFORMATION_SET = dict(
    purge_sessions=0,
    purge_reason='Every position is flat by 15:00 on its entry session, so no training outcome overlaps a later decision.',
    warmup_sessions=0,
    warmup_reason="No indicators: each decision uses that session's opening window and the reference row's previous close.",
    future_information=('The exchange calendar after the decision date is visible for the two-session expiry rule. '
                        'It is an operator-supplied calendar, so unscheduled closures are an audit assumption. '
                        'Future prices, outcomes and later listings are not visible.'))
INDEPENDENT_LABEL = 'independent_fold: starts from configured capital; do not add or average into a portfolio result'
STITCHED_LABEL = 'stitched_out_of_sample: chronological replay carrying equity forward across folds'
FOLD_FIELDS = ('fold train_start train_end train_session_count oos_start oos_end oos_session_count oos_periods '
               'selection_mode selected_candidate selected_config_sha256 training_status oos_status '
               'trade_count net_profit error').split()
CANDIDATE_FIELDS = ('fold candidate_id parameters status trade_count net_profit objective objective_value '
                    'eligible selected tie_break error').split()


def canonical_hash(obj):
    text = json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class WalkForwardSpec:
    mode: str
    train_sessions: int
    oos_sessions: int
    step_sessions: int | None = None

    def __post_init__(self):
        if self.mode not in ('rolling', 'expanding'):
            raise TradingError('Walk-forward mode must be rolling or expanding')
        for name in ('train_sessions', 'oos_sessions'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise TradingError(f'{name} must be a positive whole number of exchange sessions')
        step = self.oos_sessions if self.step_sessions is None else self.step_sessions
        if type(step) is not int or step < self.oos_sessions:
            raise TradingError('Overlapping evaluation windows: step_sessions cannot be smaller than oos_sessions')
        if step > self.oos_sessions:
            raise TradingError('step_sessions larger than oos_sessions would leave sessions unevaluated')
        object.__setattr__(self, 'step_sessions', step)


@dataclass(frozen=True)
class Fold:
    number: int
    train: tuple
    oos: tuple


def validate_folds(folds):
    previous_end = None
    for fold in folds:
        if not fold.train or not fold.oos or fold.train[-1] >= fold.oos[0]:
            raise TradingError(f'Fold {fold.number}: training must end before its evaluation window')
        if previous_end is not None and fold.oos[0] <= previous_end:
            raise TradingError(f'Fold {fold.number}: overlapping evaluation windows')
        previous_end = fold.oos[-1]


def build_folds(sessions, spec, first_oos=None):
    """Return (folds, unused trailing sessions). first_oos anchors the first evaluation window."""
    sessions = list(sessions)
    start = spec.train_sessions if first_oos is None else sessions.index(first_oos)
    if start < spec.train_sessions or start + spec.oos_sessions > len(sessions):
        raise TradingError(f'Insufficient history: need {spec.train_sessions} training and {spec.oos_sessions} '
                           f'evaluation sessions; {start} are available before evaluation and '
                           f'{len(sessions) - start} from its start')
    folds, cursor = [], start
    while cursor + spec.oos_sessions <= len(sessions):
        first = cursor - spec.train_sessions if spec.mode == 'rolling' else 0
        folds.append(Fold(len(folds) + 1, tuple(sessions[first:cursor]),
                          tuple(sessions[cursor:cursor + spec.oos_sessions])))
        cursor += spec.step_sessions
    validate_folds(folds)
    return folds, sessions[cursor:]


@dataclass(frozen=True)
class SelectionSpec:
    grid: dict
    objective: str
    min_training_trades: int = 1

    def __post_init__(self):
        if self.objective not in OBJECTIVES:
            raise TradingError(f'Selection objective must be one of {", ".join(OBJECTIVES)}')
        if type(self.min_training_trades) is not int or self.min_training_trades < 0:
            raise TradingError('min_training_trades must be a nonnegative whole number')
        if not isinstance(self.grid, dict) or not self.grid or set(self.grid) - set(SELECTABLE):
            raise TradingError(f'Candidate grid may only contain {", ".join(SELECTABLE)}')
        total = 1
        for name, values in self.grid.items():
            if (not isinstance(values, list) or not values
                    or len({json.dumps(v) for v in values}) != len(values)):
                raise TradingError(f'Grid values for {name} must be a nonempty list without duplicates')
            total *= len(values)
        if total > MAX_CANDIDATES:
            raise TradingError(f'Candidate grid has {total} candidates; the limit is {MAX_CANDIDATES}')

    @classmethod
    def load(cls, path):
        obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(obj, dict) or set(obj) - {'grid', 'objective', 'min_training_trades'}:
            raise TradingError('Invalid or unknown selection fields')
        if 'grid' not in obj or 'objective' not in obj:
            raise TradingError('Selection requires a predeclared grid and objective')
        return cls(**obj)

    def candidates(self, base):
        """Canonical order: parameter names sorted, values in declared order."""
        names = sorted(self.grid)
        result = []
        for index, values in enumerate(product(*(self.grid[name] for name in names))):
            overrides = dict(zip(names, values))
            result.append((f'c{index:03d}', overrides, replace(base, **overrides)))
        return result


class SessionView:
    """The dataset as visible when deciding allowed sessions; other prices raise an error."""

    def __init__(self, dataset, allowed):
        self._dataset, self._allowed = dataset, frozenset(allowed)
        self.calendar, self.regular = dataset.calendar, dataset.regular
        self.references = {day: ref for day, ref in dataset.references.items() if day in self._allowed}
        last = max(self._allowed)
        self.contracts = [c for c in dataset.contracts if c.listed_at.date() <= last]

    def _check(self, day):
        if day not in self._allowed:
            raise TradingError(f'Point-in-time violation: prices for {day} are not visible here')

    def spot(self, day):
        self._check(day)
        return self._dataset.spot(day)

    def option(self, day, symbol):
        self._check(day)
        return self._dataset.option(day, symbol)


@dataclass
class Simulation:
    trades: list = field(default_factory=list)
    daily: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    end_equity: object = None
    error: str = ''
    position: object = None


def _pending(day):
    return dict(session=day, status='not_processed', reason='', selected_symbol='',
                first_missing_timestamp='', position_open=False)


def simulate(config, dataset, days, equity):
    """Replay sessions in order, carrying equity. Stops at the first unresolved session."""
    result = Simulation(end_equity=equity)
    for index, day in enumerate(days):
        try:
            trade, row = replay_session(config, SessionView(dataset, [day]), day, result.end_equity)
        except ReplayError as exc:
            result.coverage.append(exc.coverage)
            result.error, result.position = f'{day}: {exc}', exc.position
            result.coverage.extend(_pending(later) for later in days[index + 1:])
            return result
        result.coverage.append(row)
        result.daily.append(row)
        if trade:
            result.trades.append(trade)
        result.end_equity = row['end_equity']
        if result.end_equity <= 0:
            result.error = f'{day}: Simulated capital exhausted'
            result.coverage.extend(_pending(later) for later in days[index + 1:])
            return result
    return result


def metrics(simulation, capital):
    base = performance(simulation.trades, simulation.daily, capital)
    counts = {}
    for row in simulation.coverage:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    gross = sum((t['gross_pnl'] for t in simulation.trades), ZERO)
    fees = sum((t['fees'] for t in simulation.trades), ZERO)
    slippage = sum((t['quantity'] * (t['entry_price'] - t['raw_entry_price'] + t['raw_exit_price'] - t['exit_price'])
                    for t in simulation.trades), ZERO)
    return dict(base, gross_pnl=str(gross), fees=str(fees), modeled_slippage_cost=str(slippage),
                coverage_counts=counts, sharpe_convention=SHARPE_CONVENTION,
                drawdown_disclosure=DRAWDOWN_DISCLOSURE, sizing_note=SIZING_NOTE)


def _objective_value(objective, result):
    if objective == 'net_pnl':
        return number(result['net_profit'])
    if objective == 'net_expectancy_per_trade':
        value = result['net_expectancy_per_trade']
        return None if value is None else number(value)
    return result['sharpe_daily_annualized_rf_zero']


def check_training_eligibility(dataset, fold):
    """Structural only: references and opening spot windows. Nothing is fitted or traded."""
    view = SessionView(dataset, fold.train)
    dataset.validate_range(fold.train[0], fold.train[-1])
    config = OptionsConfig()
    for day in fold.train:
        if dataset.regular[day]:
            spot = view.spot(day)
            decide(view.references[day], [spot.get(at(day, f'09:{minute}')) for minute in range(15, 20)], config)


def select_configuration(selection, base, dataset, fold):
    """Evaluate every candidate on training sessions only. Returns (id, overrides, config, rows, error)."""
    rows, evaluated, failures = [], [], []
    for candidate_id, overrides, config in selection.candidates(base):
        simulation = simulate(config, dataset, fold.train, number(base.capital))
        row = dict(fold=fold.number, candidate_id=candidate_id, parameters=json.dumps(overrides, sort_keys=True),
                   status='invalid' if simulation.error else 'valid', objective=selection.objective,
                   objective_value='', eligible=False, selected=False, tie_break='', error=simulation.error,
                   trade_count=len(simulation.trades), net_profit='')
        rows.append(row)
        if simulation.error:
            failures.append(candidate_id)
            continue
        result = performance(simulation.trades, simulation.daily, base.capital)
        value = _objective_value(selection.objective, result)
        row.update(net_profit=result['net_profit'], objective_value='' if value is None else str(value))
        if value is not None and len(simulation.trades) >= selection.min_training_trades:
            row['eligible'] = True
            evaluated.append((value, candidate_id, overrides, config, row))
    if failures:
        return None, None, None, rows, f'Training replay invalid for {", ".join(failures)}; no selection made'
    if not evaluated:
        return None, None, None, rows, 'No candidate met the predeclared training requirements'
    best = evaluated[0]
    for item in evaluated[1:]:
        if item[0] > best[0]:  # An equal value keeps the earlier canonical candidate.
            best = item
    best[4]['selected'] = True
    if sum(item[0] == best[0] for item in evaluated) > 1:
        best[4]['tie_break'] = 'first_canonical_candidate'
    return best[1], best[2], best[3], rows, ''


def research_periods(dataset):
    """Frozen development/validation/test intervals; also required for synthetic inputs."""
    splits = dataset.provenance.get('research_splits')
    if not isinstance(splits, dict) or set(splits) != {'development', 'validation', 'test'}:
        raise TradingError('Walk-forward requires frozen development/validation/test research_splits')
    if not isinstance(dataset.provenance.get('splits_frozen_at'), str):
        raise TradingError('Walk-forward requires splits_frozen_at')
    periods, previous = {}, None
    for name in ('development', 'validation', 'test'):
        interval = splits[name]
        if not isinstance(interval, list) or len(interval) != 2:
            raise TradingError('Each research split must contain two ISO dates')
        first, last = map(date.fromisoformat, interval)
        if first > last or (previous is not None and first <= previous):
            raise TradingError('Research splits must be ordered and non-overlapping')
        periods[name], previous = (first, last), last
    return periods


def period_of(day, periods):
    for name, (first, last) in periods.items():
        if first <= day <= last:
            return name
    return None


def authorized_sessions(dataset, periods, start, end, allow_test=False):
    if start > end or start not in dataset.calendar or end not in dataset.calendar:
        raise TradingError('Walk-forward dates must be ordered exchange sessions in the calendar')
    sessions = [day for day in dataset.calendar if start <= day <= end]
    for day in sessions:
        period = period_of(day, periods)
        if period is None:
            raise TradingError(f'Session outside frozen research splits: {day}')
        if period == 'test' and not allow_test:
            raise TradingError(f'Held-out test session {day} is excluded; use options-walkforward-test '
                               'with a frozen research run')
    return sessions


def _write_ledgers(directory, simulation, label, body):
    directory.mkdir(parents=True)
    _ledgers(directory, simulation.trades, simulation.daily, simulation.coverage)
    write_json(directory / 'result.json', dict(result_kind=label, **body))


def _execute(base, spec, selection, dataset, folds, periods, output):
    """Freeze, evaluate and stitch each fold in order. Returns state for reporting."""
    capital = number(base.capital)
    fold_rows, candidate_rows, selected, invalid, unresolved = [], [], [], [], []
    stitched, equity, stitched_error = Simulation(), capital, ''

    def flush():
        write_csv(output / 'training_candidates.csv', candidate_rows, CANDIDATE_FIELDS)
        write_json(output / 'selected_configs.json', selected)

    for fold in folds:
        row = dict(fold=fold.number, train_start=fold.train[0], train_end=fold.train[-1],
                   train_session_count=len(fold.train), oos_start=fold.oos[0], oos_end=fold.oos[-1],
                   oos_session_count=len(fold.oos),
                   oos_periods='+'.join(sorted({str(period_of(day, periods)) for day in fold.oos})),
                   selection_mode='training_only_selection' if selection else 'fixed',
                   selected_candidate='', selected_config_sha256='', training_status='valid',
                   oos_status='not_processed', trade_count=0, net_profit='', error='')
        config, overrides, candidate_id, error = None, {}, 'fixed', ''
        try:
            check_training_eligibility(dataset, fold)
            if selection:
                candidate_id, overrides, config, rows, error = select_configuration(selection, base, dataset, fold)
                candidate_rows.extend(rows)
            else:
                config = base
        except (TradingError, OSError, ValueError, KeyError) as exc:
            error = f'Training window not eligible: {exc}'
        if error:
            row.update(training_status='invalid', selected_candidate='', error=error)
        else:
            digest_value = canonical_hash(config.snapshot())
            row.update(selected_candidate=candidate_id, selected_config_sha256=digest_value)
            # Frozen on disk before any session in this fold's evaluation window is read.
            selected.append(dict(fold=fold.number, candidate_id=candidate_id, overrides=overrides,
                                 config=config.snapshot(), config_sha256=digest_value,
                                 frozen_before_session=fold.oos[0]))
        flush()

        boundaries = dict(fold=fold.number, train_start=fold.train[0], train_end=fold.train[-1],
                          oos_start=fold.oos[0], oos_end=fold.oos[-1], candidate_id=candidate_id)
        directory = output / 'independent_folds' / f'fold_{fold.number:03d}'
        if error:
            blocked = Simulation(coverage=[_pending(day) for day in fold.oos], error=error)
            _write_ledgers(directory, blocked, INDEPENDENT_LABEL, dict(boundaries, status='invalid', error=error))
            invalid.append(dict(fold=fold.number, kind='training', error=error))
        else:
            independent = simulate(config, dataset, fold.oos, capital)
            body = dict(boundaries, status='invalid' if independent.error else 'valid', error=independent.error,
                        metrics=None if independent.error else metrics(independent, base.capital))
            _write_ledgers(directory, independent, INDEPENDENT_LABEL, body)
            row.update(oos_status='invalid' if independent.error else 'valid', trade_count=len(independent.trades),
                       error=independent.error,
                       net_profit='' if independent.error else str(independent.end_equity - capital))
            if independent.error:
                invalid.append(dict(fold=fold.number, kind='independent_evaluation', error=independent.error))
                if independent.position is not None:
                    unresolved.append(dict(result='independent', fold=fold.number, position=independent.position))
        fold_rows.append(row)

        if stitched_error or error:
            stitched_error = stitched_error or f'Fold {fold.number}: {error}'
            stitched.coverage.extend(_pending(day) for day in fold.oos)
        else:
            part = simulate(config, dataset, fold.oos, equity)
            stitched.trades.extend(part.trades)
            stitched.daily.extend(part.daily)
            stitched.coverage.extend(part.coverage)
            equity = part.end_equity
            if part.error:
                stitched_error = f'Fold {fold.number}: {part.error}'
                if part.position is not None:
                    unresolved.append(dict(result='stitched', fold=fold.number, position=part.position))
    write_csv(output / 'folds.csv', fold_rows, FOLD_FIELDS)
    stitched.end_equity = equity
    return fold_rows, selected, invalid, unresolved, stitched, stitched_error


def _run(base, spec, selection, dataset_root, output, *, run_kind, start=None, end=None, procedure_extra=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    dataset, folds, manifest = None, [], {}
    procedure = dict(strategy=VERSION, base_config=base.snapshot(), walk_forward=asdict(spec),
                     selection=None if selection is None else asdict(selection))
    try:
        if selection:
            selection.candidates(base)  # Reject an invalid grid before reading any data.
        dataset = OptionsDataset(dataset_root)
        periods = research_periods(dataset)
        if run_kind == TEST_RUN:
            test_first, test_last = periods['test']
            first_session = min(day for day in dataset.calendar if day >= periods['development'][0])
            sessions = authorized_sessions(dataset, periods, first_session, test_last, allow_test=True)
            test_days = [day for day in sessions if period_of(day, periods) == 'test']
            folds, unused = build_folds(sessions, spec, first_oos=test_days[0])
        else:
            research = [day for day in dataset.calendar
                        if periods['development'][0] <= day <= periods['validation'][1]]
            if not research:
                raise TradingError('No exchange sessions in the authorized research periods')
            sessions = authorized_sessions(dataset, periods, start or research[0], end or research[-1])
            folds, unused = build_folds(sessions, spec)
        manifest = dict(strategy=VERSION, run_kind=run_kind, status='running', data_kind=dataset.provenance['data_kind'],
            provenance=dataset.provenance, procedure=procedure, procedure_sha256=canonical_hash(procedure),
            base_config_sha256=canonical_hash(base.snapshot()),
            selection_sha256=None if selection is None else canonical_hash(asdict(selection)),
            research_periods=periods, first_session=sessions[0], last_session=sessions[-1],
            folds=[dict(fold=f.number, train_start=f.train[0], train_end=f.train[-1], oos_start=f.oos[0],
                        oos_end=f.oos[-1]) for f in folds],
            unused_trailing_sessions=unused, information_set=INFORMATION_SET,
            sharpe_convention=SHARPE_CONVENTION, drawdown_disclosure=DRAWDOWN_DISCLOSURE,
            sizing_note=SIZING_NOTE, network_access=False, orders_submitted=0,
            synthetic_warning=('Synthetic inputs test code behavior only; results are not market evidence.'
                               if dataset.provenance['data_kind'] == 'synthetic' else None),
            limitations=LIMITATIONS, **(procedure_extra or {}))
        fold_rows, selected, invalid, unresolved, stitched, stitched_error = _execute(
            base, spec, selection, dataset, folds, periods, output)
        stitched_valid = not stitched_error
        _write_ledgers(output / 'stitched_oos', stitched, STITCHED_LABEL, dict(
            status='valid' if stitched_valid else 'invalid', error=stitched_error,
            metrics=metrics(stitched, base.capital) if stitched_valid else None))
        if unresolved:
            write_json(output / 'unresolved_positions.json', unresolved)
        if invalid or stitched_error:
            raise _BlockedRun(invalid, stitched_error, bool(unresolved))
        manifest.update(status='valid', input_sha256=dataset.input_hashes())
        write_json(output / 'manifest.json', manifest)
        summary = dict(strategy=VERSION, run_kind=run_kind, data_kind=dataset.provenance['data_kind'],
            synthetic_warning=manifest['synthetic_warning'], procedure_sha256=manifest['procedure_sha256'],
            first_session=sessions[0], last_session=sessions[-1], fold_count=len(folds),
            unused_trailing_sessions=unused,
            independent_folds=[{k: r[k] for k in ('fold', 'oos_start', 'oos_end', 'selected_candidate',
                                                  'trade_count', 'net_profit')} for r in fold_rows],
            independent_folds_label=INDEPENDENT_LABEL,
            stitched_out_of_sample=metrics(stitched, base.capital), stitched_label=STITCHED_LABEL)
        write_json(output / 'summary.json', summary)
        write_json(output / 'run_status.json', dict(status='valid', position_open=False))
        return json.loads(json.dumps(summary, default=str))
    except (TradingError, OSError, ValueError, KeyError) as exc:
        # Partial ledgers stay for diagnosis, but no aggregate performance is published.
        (output / 'summary.json').unlink(missing_ok=True)
        blocked = isinstance(exc, _BlockedRun)
        write_json(output / 'diagnostics.json', dict(
            status='invalid', aggregate_performance=None,
            blocked_reason='Aggregate performance is withheld until every required session and execution path resolves.',
            error=str(exc), invalid_folds=exc.invalid if blocked else [],
            stitched_error=exc.stitched_error if blocked else ''))
        write_json(output / 'run_status.json', dict(status='invalid', error=str(exc),
            position_open=exc.position_open if blocked else False))
        if dataset is not None:
            manifest = manifest or dict(strategy=VERSION, run_kind=run_kind, procedure=procedure,
                                        procedure_sha256=canonical_hash(procedure), provenance=dataset.provenance)
            manifest.update(status='invalid', consumed_input_sha256=dict(dataset._hashes))
            write_json(output / 'manifest.json', manifest)
        raise


class _BlockedRun(TradingError):
    def __init__(self, invalid, stitched_error, position_open):
        folds = sorted({item['fold'] for item in invalid})
        super().__init__(f'Walk-forward invalid; aggregate performance withheld. Invalid folds: {folds or "none"}; '
                         f'stitched: {stitched_error or "complete"}')
        self.invalid, self.stitched_error, self.position_open = invalid, stitched_error, position_open


def run_walkforward(config, spec, dataset_root, output, *, selection=None, start=None, end=None):
    """Research run: authorized development/validation sessions only."""
    return _run(config, spec, selection, dataset_root, output, run_kind=RESEARCH_RUN, start=start, end=end)


def run_heldout_test(frozen_run, dataset_root, output):
    """Evaluate the untouched test split once, using a frozen research run's procedure unchanged."""
    frozen = Path(frozen_run)
    try:
        status = json.loads((frozen / 'run_status.json').read_text(encoding='utf-8'))
        manifest = json.loads((frozen / 'manifest.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise TradingError('Frozen run is missing its manifest or status') from None
    procedure = manifest.get('procedure')
    if (status.get('status') != 'valid' or manifest.get('status') != 'valid'
            or manifest.get('run_kind') != RESEARCH_RUN):
        raise TradingError('Held-out test requires a valid frozen research walk-forward run')
    if canonical_hash(procedure) != manifest.get('procedure_sha256') or procedure.get('strategy') != VERSION:
        raise TradingError('Frozen procedure does not match its recorded hash or strategy version')
    output = Path(output)
    if output.exists():
        raise TradingError('Use a new output directory; existing experiments are never overwritten')
    dataset = OptionsDataset(dataset_root)
    if dataset.provenance.get('research_splits') != manifest['provenance'].get('research_splits'):
        raise TradingError('Research splits changed after the procedure was frozen')
    for relative, expected in manifest.get('input_sha256', {}).items():
        if digest(dataset.path(relative)) != expected:
            raise TradingError(f'Input changed after the procedure was frozen: {relative}')
    base = OptionsConfig(**procedure['base_config'])
    spec = WalkForwardSpec(**procedure['walk_forward'])
    selection = SelectionSpec(**procedure['selection']) if procedure['selection'] else None
    claim = frozen / TEST_CLAIM
    try:
        descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise TradingError('The held-out test was already evaluated for this frozen procedure') from None
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(dict(procedure_sha256=manifest['procedure_sha256'], claimed_at=datetime.now(IST).isoformat()), handle)
    return _run(base, spec, selection, dataset_root, output, run_kind=TEST_RUN,
                procedure_extra=dict(frozen_procedure_sha256=manifest['procedure_sha256']))

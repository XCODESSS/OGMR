"""Offline options replay with next-minute execution and explicit invalid runs."""
from datetime import timedelta
from pathlib import Path
from statistics import mean, stdev
import math

from .data import write_csv
from .model import TradingError, ZERO, at, fill_price, number
from .options_accounting import size_lots, trade_amounts
from .options_data import COVERAGE_FIELDS, OptionsDataset, write_json
from .options_model import VERSION
from .options_strategy import choose_contract, decide

TRADE_FIELDS = ('session symbol option_type strike expiry_session lot_size gap spot_at_decision '
                'failure_level target decision_time entry_time trigger_bar_time trigger_time exit_time '
                'raw_entry_price raw_exit_price entry_price exit_price lots quantity premium_stop '
                'spot_failure gap_fill multiple_triggers gross_pnl fees net_pnl exit_reason').split()
DAILY_FIELDS = 'session status reason start_equity end_equity return'.split()
LIMITATIONS = [
    'Completed minute triggers execute at the next option open, not the trigger price.',
    'Candle volume does not establish bid/ask spread, depth or attainable fills.',
    'Fees and slippage are modeled scenarios, not a verified historical tax schedule.',
    'Planned stop risk is not guaranteed; full premium plus charges remains at risk.',
    'Drawdown is measured at daily closes only; intraday losses may be larger.',
    'Provenance audit flags are operator assertions, not independent price verification.',
]


class ReplayError(TradingError):
    def __init__(self, message, coverage, position=None):
        super().__init__(message)
        self.coverage = coverage
        self.position = position


def replay_session(config, dataset, day, equity):
    row = dict(session=day, status='no_trade', reason='', selected_symbol='',
               first_missing_timestamp='', position_open=False,
               start_equity=equity, end_equity=equity, return_=ZERO)
    position = None

    def require(bars, moment, option=False):
        bar = bars.get(moment)
        if bar is None or (option and bar.volume <= 0):
            row['first_missing_timestamp'] = moment.isoformat()
            raise TradingError(f'Missing/untradable required {"option" if option else "spot"} bar: {moment}')
        return bar

    try:
        if not dataset.regular[day]:
            row.update(status='excluded', reason='excluded_special_session')
            return None, row
        spot = dataset.spot(day)
        opening = [require(spot, at(day, f'09:{m}')) for m in range(15, 20)]
        decision = decide(dataset.references[day], opening, config)
        row['reason'] = decision.reason
        if decision.signal is None:
            return None, row
        signal = decision.signal
        contract = choose_contract(signal, dataset.contracts, list(dataset.calendar))
        row['selected_symbol'] = contract.symbol
        pending = require(spot, at(day, '09:20'))
        if (pending.high >= signal.target if signal.option_type == 'CE' else pending.low <= signal.target):
            row['reason'] = 'target_before_entry'
            return None, row
        options = dataset.option(day, contract.symbol)
        entry_at = at(day, '09:21')
        entry_bar = require(options, entry_at, True)
        entry_fill = fill_price(entry_bar.open, 1, number(config.slippage_bps))
        lots = size_lots(equity, entry_fill, contract.lot_size, config)
        if not lots:
            row['reason'] = 'insufficient_capital'
            return None, row
        quantity = lots * contract.lot_size
        position = dict(session=day, symbol=contract.symbol, option_type=contract.option_type,
                        strike=contract.strike, expiry_session=contract.expiry_session,
                        lot_size=contract.lot_size, gap=signal.gap,
                        spot_at_decision=signal.spot_at_decision,
                        failure_level=signal.failure_level, target=signal.target,
                        decision_time=signal.decision_at, entry_time=entry_at,
                        raw_entry_price=entry_bar.open, entry_price=entry_fill,
                        lots=lots, quantity=quantity)
        row['position_open'] = True
        moment, exit_at, reason = entry_at, at(day, '15:00'), 'scheduled_exit'
        trigger_bar, trigger_time = '', ''
        flags = dict(premium_stop=False, spot_failure=False, gap_fill=False)
        while moment < at(day, '15:00'):
            underlying = require(spot, moment)
            option = require(options, moment, True)
            bullish = signal.option_type == 'CE'
            flags = dict(
                premium_stop=option.low <= entry_fill * (1 - number(config.stop_fraction)),
                spot_failure=(underlying.low < signal.failure_level if bullish
                              else underlying.high > signal.failure_level),
                gap_fill=(underlying.high >= signal.target if bullish else underlying.low <= signal.target))
            if any(flags.values()):
                reason = next(key for key, active in flags.items() if active)
                exit_at = moment + timedelta(minutes=1)
                trigger_bar, trigger_time = moment, exit_at
                break
            moment += timedelta(minutes=1)
        exit_bar = require(options, exit_at, True)
        exit_fill = fill_price(exit_bar.open, -1, number(config.slippage_bps))
        gross, fees, net = trade_amounts(entry_fill, exit_fill, quantity, config)
        trade = dict(position, trigger_bar_time=trigger_bar, trigger_time=trigger_time,
                     exit_time=exit_at, raw_exit_price=exit_bar.open, exit_price=exit_fill,
                     **flags, multiple_triggers=sum(flags.values()) > 1,
                     gross_pnl=gross, fees=fees, net_pnl=net, exit_reason=reason)
        row.update(status='traded', reason=reason, position_open=False,
                   end_equity=equity + net, return_=net / equity)
        return trade, row
    except (TradingError, OSError, ValueError, KeyError) as exc:
        row.update(status='invalid', reason=str(exc))
        raise ReplayError(str(exc), row, position) from exc


def performance(trades, daily, capital):
    """Ledger metrics shared by single runs and walk-forward; daily closes only."""
    initial = number(capital)
    equity = daily[-1]['end_equity'] if daily else initial
    pnl = [t['net_pnl'] for t in trades]
    wins = sum((p for p in pnl if p > 0), ZERO)
    losses = -sum((p for p in pnl if p < 0), ZERO)
    peak, drawdown = initial, ZERO
    for row in daily:
        peak = max(peak, row['end_equity'])
        drawdown = min(drawdown, row['end_equity'] / peak - 1)
    regular_returns = [float(row['return_']) for row in daily if row['status'] != 'excluded']
    return dict(
        evaluated_sessions=len(daily), regular_sessions=sum(row['status'] != 'excluded' for row in daily),
        special_sessions=sum(row['status'] == 'excluded' for row in daily),
        no_signal_sessions=sum(row['reason'] in ('below_gap_threshold', 'unconfirmed', 'already_filled',
                                                'target_before_entry') for row in daily),
        trade_count=len(trades), initial_equity=capital, final_equity=str(equity),
        net_profit=str(equity - initial),
        total_return=float(equity / initial - 1),
        net_expectancy_per_trade=str(sum(pnl, ZERO) / len(pnl)) if pnl else None,
        win_rate=sum(p > 0 for p in pnl) / len(pnl) if pnl else None,
        profit_factor=float(wins / losses) if losses else None,
        max_drawdown_daily_close=float(drawdown), drawdown_basis='daily_close_only',
        sharpe_daily_annualized_rf_zero=(mean(regular_returns) / stdev(regular_returns) * math.sqrt(252)
            if len(regular_returns) > 1 and stdev(regular_returns) > 0 else None))


def _ledgers(output, trades, daily, coverage):
    write_csv(output / 'trades.csv', trades, TRADE_FIELDS)
    rows = [dict(session=d['session'], status=d['status'], reason=d['reason'],
                 start_equity=d['start_equity'], end_equity=d['end_equity'],
                 **{'return': d['return_']}) for d in daily]
    write_csv(output / 'daily_equity.csv', rows, DAILY_FIELDS)
    write_csv(output / 'coverage.csv', [{key: row.get(key, '') for key in COVERAGE_FIELDS}
                                       for row in coverage], COVERAGE_FIELDS)


def _manifest(config, dataset, start, end, coverage):
    return dict(strategy=VERSION, start=start, end=end, config=config.snapshot(),
                data_kind=dataset.provenance['data_kind'], provenance=dataset.provenance,
                input_sha256=dataset.input_hashes(),
                selected_contracts=sorted({row['selected_symbol'] for row in coverage if row['selected_symbol']}),
                decision_time='09:20:00+05:30', entry_time='09:21:00+05:30',
                scheduled_exit='15:00:00+05:30',
                cost_model='premium turnover bps plus fixed fee per order; adverse bps fills',
                limitations=LIMITATIONS)


def run(config, dataset_root, start, end, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    trades, daily, coverage, dataset = [], [], [], None
    equity = number(config.capital)
    try:
        dataset = OptionsDataset(dataset_root)
        coverage = [dict(session=day, status='not_processed', reason='', selected_symbol='',
                         first_missing_timestamp='', position_open=False)
                    for day in dataset.calendar if start <= day <= end]
        days = dataset.validate_range(start, end)
        for index, day in enumerate(days):
            try:
                trade, row = replay_session(config, dataset, day, equity)
            except ReplayError as exc:
                coverage[index] = exc.coverage
                raise
            coverage[index] = row
            daily.append(row)
            if trade:
                trades.append(trade)
            equity = row['end_equity']
            if equity <= 0:
                raise TradingError('Simulated capital exhausted')
        manifest = _manifest(config, dataset, start, end, coverage)
        write_json(output / 'manifest.json', manifest)
        summary = dict(strategy=VERSION, data_kind=dataset.provenance['data_kind'],
            provider=dataset.provenance['provider'], start=str(start), end=str(end),
            **performance(trades, daily, config.capital),
            limitations=LIMITATIONS)
        _ledgers(output, trades, daily, coverage)
        write_json(output / 'summary.json', summary)
        write_json(output / 'run_status.json', dict(status='valid', position_open=False))
        return summary
    except (TradingError, OSError, ValueError, KeyError) as exc:
        # No valid performance report may survive a later output/write error.
        (output / 'summary.json').unlink(missing_ok=True)
        opened = isinstance(exc, ReplayError) and exc.position is not None
        status = dict(status='invalid', error=str(exc), position_open=opened,
                      session=str(exc.coverage['session']) if isinstance(exc, ReplayError) else '')
        _ledgers(output, trades, daily, coverage)
        write_json(output / 'run_status.json', status)
        if opened:
            write_json(output / 'unresolved_position.json', exc.position)
        if dataset is not None:
            # An invalid ledger may retain hashes already consumed, explicitly
            # without asserting that changing files passed final validation.
            write_json(output / 'manifest.json', dict(strategy=VERSION, status='invalid',
                start=start, end=end, config=config.snapshot(), provenance=dataset.provenance,
                consumed_input_sha256=dict(dataset._hashes)))
        raise

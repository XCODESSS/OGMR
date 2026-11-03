# OGMR: Overnight Gap Mean Reversion

An independent Python implementation of an opening-gap mean-reversion hypothesis, with CSV backtesting, FYERS market-data paper trading, and explicitly armed FYERS live execution. Python 3.11+ is required. The runtime uses only the Python standard library.

The reference rules buy gaps at or below -2% and short gaps at or above +2%, select at most 10 stocks by absolute gap, and allocate up to 10% of start-of-day capital per position. The bot captures its signal before 09:16 IST, enters after that time, and exits at 15:00 or on a configured daily-loss trigger. Both directions require explicit universe permissions. The example watchlist permits longs only until short eligibility is verified.

This version supports NSE cash equities. Futures and options are not implemented; the code rejects those instruments. The strategy is an independent reconstruction inspired by [CuriousObservator's published gap-fade research](https://github.com/CuriousObservator/Systematic-Intraday-Mean-reversion-Strategy). It does not reproduce a verified profitable strategy or establish the original author's exact rules.

## Start here

Read [FYERS integration and operation](docs/FYERS_INTEGRATION.md) for login, data formats, paper operation and live controls. Capital is configurable; the example Rs 10 lakh is simulated capital, not a live allocation recommendation.

From PowerShell, in the repository root:

```powershell
python -m venv .venv --without-pip
.\.venv\Scripts\python.exe -m gapfade --help
.\.venv\Scripts\python.exe -m gapfade demo --out data/my-demo
.\.venv\Scripts\python.exe -m gapfade backtest --universe data/my-demo/universe.csv --calendar data/my-demo/sessions.csv --references data/my-demo/references.csv --bars data/my-demo/bars.csv --out runs/my-demo-backtest
```

Use fresh output names on subsequent runs. Demo instruments and prices are fictitious. Trade logs, daily equity, metrics and a config/input-hash manifest appear in the selected output directory.

## What has been verified

The offline tests cover threshold boundaries, ranking, historical membership, invalid references, missing bars, both trade directions, concurrent allocation, fees, next-bar loss exits, paper sessions, live arming, partial fills, unknown submissions, restart behavior, position reconciliation, quote outages and a simulated FYERS execution session.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Real FYERS authentication, quote response compatibility on your account, order execution, exchange fills and profitable historical results have not been verified. No broker credentials were used and no real orders were submitted during development. This is an initial supervised integration, not a validated unattended production service.

## Files

- [Strategy implementation](gapfade/strategy.py)
- [Backtester](gapfade/backtest.py)
- [FYERS adapter](gapfade/fyers.py)
- [Paper/live session runner](gapfade/runner.py)
- [Durable execution journal](gapfade/execution.py)
- [Default configuration](config/paper.json)

Historical input data, run state, credentials and local research artifacts are ignored by Git.

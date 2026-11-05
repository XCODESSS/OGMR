# NIFTY options gap-fade research

The options path tests an opening-gap reversal by buying a NIFTY CE or PE. Offline research reads actual historical option premiums from local CSVs. The separate [options-paper command](OPTIONS_PAPER.md) performs live-market testing with simulated fills and a read-only FYERS market-data adapter. There is no real-money options execution mode or downloader producing normalized backtest inputs. A [standalone FYERS expired-options sample downloader](FYERS_EXPIRED_SAMPLE.md) saves raw data for access checks and later normalization. The existing `fetch`, `paper` and `live` commands remain cash-equity commands.

Strategy version: `nifty-gap-fade-options-v1`. Its thresholds and risk settings are proposed research choices, not parameters proven to make money. No real FYERS data or broker account was used to validate this implementation.

## Frozen rules

1. Calculate NIFTY's official opening gap against the previous exchange session's close. Require an absolute gap of at least 0.5%, including equality.
2. Aggregate the five completed spot minutes starting at 09:15 through 09:19. After a gap down, require that candle to close above its open and below the previous close, then choose a CE. After a gap up, require it to close below its open and above the previous close, then choose a PE. A doji fails confirmation. A gap already filled by the opening-window close produces no trade in either variant.
3. Freeze the signal at 09:20. Choose the nearest listed ATM strike using the completed 09:19 spot close. Use the earliest listed expiry with at least two exchange sessions after entry day and on/before expiry. Break equal-distance strike ties using the lower strike. Use historical listings and lot sizes; no fixed expiry weekday or current lot-size constant is assumed.
4. If the completed 09:20 spot minute has already reached the previous close, cancel entry. Otherwise buy the selected option at the 09:21 open plus modeled adverse slippage. This one-minute delay allows the completed opening candle to become known before execution. The contract cannot change based on the 09:21 spot price.
5. Allow one long-option entry per day. No option selling, re-entry, averaging, futures or overnight positions.

All times are IST. Special sessions are excluded explicitly in the input calendar. One entry attempt per day also means no replacement contract when the chosen contract lacks data.

## Exits

For a CE, the gap-fill target is spot reaching the previous close; the failure stop is spot breaking below the opening window's low. For a PE, target is spot reaching the previous close from above; failure is breaking above the opening window's high. Target equality triggers; opening-range failure requires a strict break.

The option premium stop triggers when a completed option minute's low is at or below 75% of the entry fill. All three triggers are observed using completed minutes while the position is open, from 09:21 through 14:59. Exit at the next minute's option open with adverse slippage. Otherwise exit at 15:00.

This model does not promise execution at the target or stop price. A sharp move can produce a loss larger than the planned 25% premium stop. Simultaneous triggers record every flag; the primary reason uses premium-stop, spot-failure, gap-fill priority. They all execute at the same next open, so the model does not invent an ordering inside a minute to obtain a favorable threshold fill.

For example, a gap-down reversal buys a CE. If spot reaches the previous close during the 10:00 minute, the trigger becomes available at 10:01 and the CE exits at that minute's modeled open. A gap-up reversal buys a PE: it profits only if that PE's exit premium exceeds its entry premium after costs. Falling spot alone does not establish a profitable PE trade.

## Sizing and costs

`config/options-research.json` supplies these defaults:

| Setting | Research default |
| --- | --- |
| Simulated capital | Rs 100,000 |
| Planned stop risk | 0.5% of start-of-day equity |
| Premium plus estimated round-trip fees | At most 2% of start-of-day equity |
| Premium stop | 25% of entry fill |
| Adverse slippage | 25 bps per leg |
| Modeled fees | 10 bps of premium turnover per leg, plus Rs 20 per order |

Sizing rounds down to historical whole lots and reserves estimated charges at the stop premium. If one lot cannot fit, the day is recorded as `insufficient_capital`. There is no leverage or fractional-lot trade.

For a fictitious 10-unit lot and Rs 100 entry fill, two lots cost Rs 2,000 before fees, which exceeds the Rs 2,000 premium budget once charges are reserved. One lot has planned premium loss Rs 250 plus estimated round-trip charges Rs 41.75, fitting the Rs 500 planned-risk budget. This uses an already adjusted entry fill; raw candle fills also receive slippage.

These charges are a scenario, not a verified historical statutory schedule. Minute candles do not establish spreads, available size or attainable fills. The whole premium plus charges remains at risk if an exit cannot execute; the planned stop-risk amount is not a guaranteed daily loss cap.

## Run the fictitious demonstration

Run from the repository root with Python 3.11 or newer. The runtime needs no third-party packages. Use fresh folder names if these already exist.

```powershell
.\.venv\Scripts\python.exe -m gapfade options-demo --out data/options-demo-01
.\.venv\Scripts\python.exe -m gapfade options-audit --data data/options-demo-01 --start 2026-01-05 --end 2026-01-09 --out runs/options-audit-01
.\.venv\Scripts\python.exe -m gapfade options-backtest --config config/options-research.json --data data/options-demo-01 --start 2026-01-05 --end 2026-01-09 --out runs/options-confirmed-01
.\.venv\Scripts\python.exe -m gapfade options-backtest --config config/options-research.json --data data/options-demo-01 --start 2026-01-05 --end 2026-01-09 --no-confirmation --out runs/options-baseline-01
```

The confirmed demo has three trades. The baseline has four because one deliberately unconfirmed fixture enters only in that variant. Both retain `data_kind=synthetic`. Their returns demonstrate code behavior only.

The baseline disables just the opening-candle direction check. It keeps the same gap threshold, already-filled exclusions, entry timing, contract selection, exits, sizing and costs. This makes it a comparison of the confirmation filter rather than a comparison of different execution times.

## Give your friend this data contract

Use [the export format](../examples/options/FORMAT.md) to normalize FYERS exports. Before a real-data run, your friend needs to establish:

- Access to actual expired option prices for the chosen date range and minute resolution.
- Verified NIFTY opening/prior-close references, timestamp conventions and exchange calendar through catalog expiries.
- Historical contract listings, strikes, expiries and date-effective lot sizes.
- Source files and hashes, with documented calendar/reference/catalog audit assertions.
- Frozen chronological development, validation and untouched-test dates in provenance before inspecting strategy returns.

The existing stock downloader does not produce this schema. This implementation does not assume an account plan, retention period or expired-contract API entitlement. Those checks and raw FYERS export mapping belong to the supplied data, not the offline engine.

## Walk-forward research

The walk-forward commands run the same replay session by session, reading local files only. They make no FYERS requests, submit no orders and change no trading rule. Window lengths count exchange sessions, including special sessions recorded as excluded.

### Windows

Each fold has a training window followed by an evaluation window, and evaluation windows run back to back.

- `rolling` trains on the previous `--train-sessions` sessions. `expanding` trains on everything from the first session up to the evaluation window, with `--train-sessions` as the minimum.
- `--step-sessions` defaults to `--oos-sessions`. A smaller value is rejected as overlapping, and a larger value is rejected because it would leave sessions unevaluated.
- Too little history is an error. Sessions left over after the last full window are listed as `unused_trailing_sessions` and are not evaluated.

### Fixed parameters (default)

Training windows only establish chronology and data eligibility: verified references and complete opening spot windows. Nothing is fitted, and no training trades are replayed.

```powershell
.\.venv\Scripts\python.exe -m gapfade options-walkforward-demo --out data/wf-demo-01
.\.venv\Scripts\python.exe -m gapfade options-walkforward --config config/options-research.json --data data/wf-demo-01 --mode rolling --train-sessions 4 --oos-sessions 4 --out runs/wf-fixed-01
```

### Optional training-only selection

Pass `--selection` with a predeclared grid and objective ([example](../examples/options/walkforward-selection.json)):

```powershell
.\.venv\Scripts\python.exe -m gapfade options-walkforward --config config/options-research.json --data data/wf-demo-01 --mode expanding --train-sessions 4 --oos-sessions 4 --selection examples/options/walkforward-selection.json --out runs/wf-selected-01
```

- The grid may only contain `confirmation`, `gap_threshold` and `stop_fraction`, with at most 64 candidates. Costs and capital cannot be selected.
- Objectives are `net_pnl`, `net_expectancy_per_trade` and `sharpe_daily_rf_zero`. Candidates below `min_training_trades`, or with an undefined objective, are ineligible.
- Candidates are ordered by sorted parameter name, then declared value order. A tie goes to the first candidate.
- Every candidate is recorded in `training_candidates.csv`. The selection is written to `selected_configs.json` before that fold's evaluation sessions are read.
- If any candidate's training replay is invalid, or none qualifies, the fold is invalid. Choosing among the survivors could hide the failure.

Selection adds its own overfitting risk, because a good grid result can come from chance. Keep the grid small and judge it only on evaluation results.

### Research boundary and the held-out test

`provenance.json` must contain `research_splits` and `splits_frozen_at` for these commands, including synthetic data. Research runs default to the development start through the validation end, and they reject any session in the test split or outside the splits.

The test split needs a separate command, which takes its procedure from a valid frozen research run and accepts no parameter overrides:

```powershell
.\.venv\Scripts\python.exe -m gapfade options-walkforward-test --frozen-run runs/wf-fixed-01 --data data/wf-demo-01 --out runs/wf-test-01
```

It refuses to run if the procedure hash, the research splits or any recorded input file has changed. It then creates `test_evaluation_claimed.json` in the frozen run, and a second attempt is refused, even if the first one failed. That marker is a discipline control and not tamper-proof: deleting it would allow a rerun but would also break the promise of a once-only test.

### Point-in-time rules

Each session is replayed through a view that only exposes that session's prices and references and hides contracts listed later. Any other price request raises an error and invalidates the fold.

- **Future calendar:** later exchange sessions stay visible, because the two-session expiry rule needs them. This assumes the supplied calendar was known in advance, so unscheduled closures are an audit item.
- **Purge: 0 sessions.** Every position is flat by 15:00 on its entry session, so no training outcome overlaps the next session's decision.
- **Warm-up: 0 sessions.** The strategy has no indicators. Each decision uses that session's opening window and the reference row's previous close.

### Outputs

| Path | Contents |
| --- | --- |
| `independent_folds/fold_NNN/` | Each fold from the configured capital: trades, daily equity, coverage and `result.json`. Do not add or average these into a portfolio result. |
| `stitched_oos/` | One chronological replay that carries equity forward across folds, with the same ledgers. |
| `folds.csv` | Fold boundaries, selected candidate and status. |
| `training_candidates.csv`, `selected_configs.json` | Every candidate tried and each frozen configuration. |
| `manifest.json` | Procedure, config and input hashes, folds, research periods and information rules. |
| `summary.json` | Written only when the whole run is valid. |
| `diagnostics.json`, `unresolved_positions.json` | Written when a run is invalid, with aggregate performance withheld. |

**Sizing:** position size comes from the existing replay rule described above, unchanged. Stitched sessions receive the running equity and independent folds receive the configured capital. If that rule ever ignores equity, such as a fixed lot, both results have the same per-trade rupee P&L and differ only in percentage returns.

**Metrics:** net P&L, gross P&L, fees, modeled slippage, trade count and coverage counts. Sharpe is the mean daily simple return divided by the sample standard deviation (n-1), times the square root of 252, with a zero risk-free rate. It includes zero-return no-trade sessions and excludes special sessions. Drawdown is measured at daily closes only and is not intraday drawdown.

### Failed folds

`no_trade` means the rules produced no trade. `invalid` means required data or an execution path failed. `not_processed` means the session was never reached. A failed fold is never skipped:

- Independent evaluation continues in later folds for diagnosis.
- The stitched portfolio stops at the first failure, because equity cannot carry past an unresolved session.
- The command exits nonzero and withholds `summary.json`.

Passing on synthetic data shows the code behaves as specified. It says nothing about profitability, and real runs remain blocked until audited expired-option data exists in [the export format](../examples/options/FORMAT.md).

## Read results and failures

Valid runs write `trades.csv`, `daily_equity.csv`, `coverage.csv`, `summary.json`, `manifest.json` and `run_status.json`. The manifest captures config, source evidence, consumed file hashes, selected contracts and timing/cost assumptions.

The trade ledger includes contract identity, lot size, quantity, raw and adjusted option fills, trigger timestamps and flags, fees and premium P&L. The daily ledger includes zero-trade sessions. Special-session exclusions remain in the ledger but are excluded from the regular-session Sharpe calculation. Drawdown is measured at daily closes only; it is not maximum intraday drawdown.

Coverage distinguishes `traded`, `no_trade`, `excluded`, `invalid` and `not_processed`. No-trade reasons include `below_gap_threshold`, `unconfirmed`, `already_filled`, `target_before_entry` and `insufficient_capital`.

A missing required spot/option bar, zero-volume selected option bar or invalid metadata produces a nonzero exit. Invalid runs withhold `summary.json`; `run_status.json` explains the failure. If the failure occurs after entry, `unresolved_position.json` retains the simulated open position. Completed earlier trades remain in the ledger. Later unprocessed dates are not counted as zero-return successful sessions. The program never drops the unresolved trade to make the test finish.

Missing bars after an actual early exit are not required. Consumed CSVs still undergo structural validation, so duplicate or corrupt rows are errors even outside the holding period. The audit command is structural only; replay checks the selected contract's required execution path.

## Verify the implementation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m gapfade options-backtest --help
```

Implementation verification on 4 November 2026: the full suite and all four demonstration commands passed. These runs used synthetic inputs only; no FYERS login, account data or orders were used.

Tests exercise premiums, lot constraints, historical contract choice, next-minute exits, data failures, both variants and mocked absence of broker access. Successful synthetic tests do not establish a trading edge. Compare net expectancy, drawdown, trade count and skipped sessions on audited real data before changing strategy parameters. Record cost-stress experiments separately and preserve the untouched period.

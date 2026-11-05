# NIFTY Options Walk-Forward Runner Plan

**Goal:** Add an offline walk-forward runner around the existing `nifty-gap-fade-options-v1` replay, without changing any trading rule.

**Architecture:** One new orchestration module, `gapfade/options_walkforward.py`, calls the existing `replay_session` once per session through a guard that only exposes that session's prices. A separate generator, `gapfade/options_walkforward_demo.py`, produces synthetic inputs. The only change to existing code is moving the summary metrics out of `options_backtest.run` into a shared `performance` function with identical output.

**Tech stack:** Python >=3.11, standard library, Decimal, unittest. No network, FYERS or order code.

## Constraints from the request

- Reuse contract selection, sizing, accounting, costs and next-minute fills through `replay_session`.
- Local validated data only. Never invent candles, metadata, lot sizes or references.
- No data fetching, strategy changes, commits or pushes.

## Sizing

Sizing stays inside `replay_session` and this runner does not change it. Stitched sessions pass the running equity and independent folds pass configured capital. If the sizing rule ignores equity, such as a fixed one-lot rule, both give the same per-trade rupee P&L and differ only in percentage returns. Every summary states this.

## Windows

- Lengths count exchange sessions from `sessions.csv`, including special sessions that are recorded as excluded.
- `rolling`: the training window is the previous `train_sessions` sessions. `expanding`: it runs from the first available session up to the session before the evaluation window.
- `step_sessions` defaults to `oos_sessions`. A smaller step is rejected as overlapping. A larger step is rejected because it would leave sessions unevaluated.
- Too few sessions for one fold is a clear error. Leftover sessions too few for a final full window are recorded as `unused_trailing_sessions`, never silently evaluated.

## Modes

- **Fixed (default):** training windows only check chronology and data eligibility (references, opening spot windows). Nothing is fitted, and training trades are not replayed.
- **Selection (explicit file):** a bounded grid of at most 64 candidates over `confirmation`, `gap_threshold` and `stop_fraction`. Costs and capital are not selectable. Objectives: `net_pnl`, `net_expectancy_per_trade`, `sharpe_daily_rf_zero`. Candidates with too few training trades or an undefined objective are ineligible.
- **Tie-break:** the first candidate in canonical order wins (parameter names sorted, values in declared order).
- **Failed candidates:** if any candidate's training replay is invalid, the fold is invalid. Choosing among the survivors could hide the failure.
- **Freezing:** the selected configuration is written to disk before that fold's evaluation sessions run. Every candidate is recorded.

## Research boundary

- `research_splits` is required for walk-forward runs, including synthetic data.
- Research runs default to the development start through the validation end. Any session in the test split or outside the splits is rejected.
- The test split needs the separate `options-walkforward-test` command, which takes a valid frozen research run and reuses its procedure, with no parameter overrides.
- The command checks the procedure hash, the research splits and every input file hash from the frozen run.
- A marker file in the frozen run is created exclusively before evaluation, so a second attempt is refused, even after a failed attempt.

## Point-in-time rules

- `SessionView` exposes spot and option bars and references only for allowed sessions, and hides contracts listed later. Any other access raises an error and invalidates the fold.
- The future exchange calendar stays visible, because the rule of two sessions to expiry needs it. Future prices and outcomes do not.
- **Purge is 0 sessions:** every position is flat by 15:00 on its entry session, so no training outcome overlaps the next session's decision.
- **Warm-up is 0 sessions:** the strategy has no indicators, and each decision uses that session's opening window plus the reference row's previous close.

## Outputs (new `--out` directory)

- `manifest.json`, `run_status.json`, `folds.csv`, `training_candidates.csv`, `selected_configs.json`
- `independent_folds/fold_NNN/`: `trades.csv`, `daily_equity.csv`, `coverage.csv`, `result.json`. Each fold starts from configured capital.
- `stitched_oos/`: the same ledgers plus `result.json`. These come from a real chronological replay that carries equity forward, not from averaging folds.
- `summary.json` only for fully valid runs. Otherwise `diagnostics.json` with aggregate performance withheld, plus `unresolved_positions.json` if a simulated position stayed open.
- Metrics: net P&L, gross P&L, fees, modeled slippage, trade count, coverage counts, daily-close drawdown with its disclosure, and the exact Sharpe convention.

## Failure rules

- A failed fold never disappears. Independent evaluation continues in later folds for diagnostics.
- The stitched portfolio stops at the first failure, because equity cannot carry past an unresolved session. Remaining sessions are `not_processed`.
- `no_trade` stays distinct from `invalid` and `not_processed`.
- Any invalid fold returns a nonzero exit and withholds `summary.json`.

## Tasks

- [x] Extract `performance` from `options_backtest.run`, with existing tests still passing.
- [x] Add windows, selection spec, research boundary and `SessionView`.
- [x] Add simulation, freezing, independent and stitched ledgers, manifest and failure handling.
- [x] Add the held-out test command.
- [x] Add the synthetic demo generator, example grid and CLI commands.
- [x] Add hand-checkable tests: leakage, selection isolation, overlap, insufficient history, held-out access, historical contracts, invalid folds, deterministic reruns, equity carry-forward.
- [x] Update `docs/NIFTY_OPTIONS.md`, `examples/options/FORMAT.md` and `README.md`.
- [x] Run the full test suite and the offline demonstration on Python 3.12.

## Remains blocked

A real run needs audited expired-option data in the FORMAT.md layout, which FYERS did not supply on this account when checked on 2026-11-04.

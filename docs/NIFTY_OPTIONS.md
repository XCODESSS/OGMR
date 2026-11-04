# NIFTY options gap-fade research

The options path tests an opening-gap reversal by buying a NIFTY CE or PE. It reads actual historical option premiums from local CSVs. It has no options paper/live runner or FYERS downloader. The existing `fetch`, `paper` and `live` commands remain cash-equity commands.

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

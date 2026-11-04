# Options data export format

Prepare this directory from your FYERS historical exports. The options commands read local files; they do not log in to FYERS or download contracts. Use actual expired option prices and the contract catalog that existed on each date.

```text
data/friend-options/
  provenance.json
  sessions.csv
  references.csv
  contracts.csv
  raw/
  spot/2026-01-05.csv
  options/2026-01-05.csv
```

CSV files use UTF-8, the exact column order below, ISO dates and offset-aware timestamps. Booleans are lowercase `true`/`false`. Numbers must be finite. All examples below are fictitious.

## Exchange calendar

`sessions.csv`:

```csv
session,previous_session,is_regular
2026-01-05,2026-01-02,true
2026-01-06,2026-01-05,true
2026-01-07,2026-01-06,true
```

Include consecutive exchange sessions from the evaluation start through every catalog expiry, including holidays through their absence from the calendar. Each row must reference the preceding exchange session, including the first row's prior session outside the interval. The engine cannot independently verify your holiday calendar. Mark special sessions `false`; the engine records their exclusion and does not trade them.

The two-session expiry rule counts exchange sessions strictly after the trade date and on/before expiry. Supply the future calendar horizon; do not substitute calendar days.

## NIFTY spot references

`references.csv`:

```csv
session,previous_session,official_open,previous_close,available_at,verified
2026-01-05,2026-01-02,24750,25000,2026-01-05T09:16:00+05:30,true
```

Include one row for every evaluated regular session. Use the official NIFTY open and previous exchange session's close. Audit the reference convention and signal availability before asserting `verified=true`. `available_at` must be on the session date and strictly before 09:20. A daily candle response alone does not establish live availability at that time.

## Historical contract catalog

`contracts.csv`:

```csv
symbol,underlying,option_type,strike,expiry_session,listed_at,valid_from,valid_to,lot_size
DEMO:NIFTY:CE,NIFTY,CE,25000,2026-01-07,2026-01-01T00:00:00+05:30,2026-01-01,2026-01-07,10
```

Replace the synthetic symbol and metadata with real historical contract records. `DEMO:` symbols are rejected for historical datasets. `symbol` is an opaque ID shared with the option bars; the engine does not parse a FYERS symbol to guess its expiry.

| Column | Meaning |
| --- | --- |
| underlying | Exactly `NIFTY`. |
| option_type | `CE` or `PE`. |
| strike | Positive listed strike. |
| expiry_session | Actual expiry, present in sessions.csv. |
| listed_at | When the contract became listed/available, with UTC offset. |
| valid_from, valid_to | Inclusive dates for this metadata/lot-size version, ending no later than expiry. |
| lot_size | Positive integer historical units per lot. |

Use a new non-overlapping row when a lot size changes. The symbol must retain its series identity. Include the actual listed strikes and eligible expiries, not just a winning contract chosen after seeing returns. Audit catalog completeness and its historical availability. The engine can check duplicate series and dates, but it cannot detect a plausible omitted strike without independent evidence.

## Minute bars

Both `spot/YYYY-MM-DD.csv` and `options/YYYY-MM-DD.csv` use:

```csv
timestamp,symbol,open,high,low,close,volume
2026-01-05T09:15:00+05:30,NIFTY,24750,24805,24745,24800,0
```

Normalize the spot symbol to `NIFTY`. Option rows must use catalog symbols. A day's option file may contain multiple contracts; the reader retains only the selected contract's bars. It checks the structure and duplicate keys of all rows it reads. OHLC values must be positive, internally consistent and minute-start aligned; volume must be nonnegative. Timestamps are normalized to IST and must belong to the filename's date.

Zero index volume is accepted. Zero-volume selected option bars are not treated as executable. Required missing bars invalidate the run. Missing bars after a completed exit do not matter; structural corruption anywhere in a consumed file still fails validation.

Provide spot minutes 09:15–09:20 for signal formation and pending-entry cancellation, then spot/selected option minutes from 09:21 while a position remains open. Provide the option bar at the actual next-minute exit, or 15:00 for scheduled exit. Full-day exports are easiest. Do not trim history using the returns you hope to obtain.

## Source evidence and research dates

`provenance.json` requires:

| Field | Value |
| --- | --- |
| schema_version | Integer `1`. |
| data_kind | `historical` for real inputs; `synthetic` only for fictitious fixtures. |
| provider | Nonempty provider description, for example `FYERS historical export`. |
| exported_at | Offset-aware ISO export timestamp. |
| calendar_verified, references_verified, catalog_verified | JSON `true`, only after the corresponding documented audit. |
| bar_timestamp_convention | Exactly `minute_start`. |
| spot_reference_convention | Description of how spot open/prior close were obtained. |
| audit_notes | What was verified, against which evidence, and any remaining limitations. |
| source_files | Nonempty list of objects with relative `path` and lowercase SHA-256 `sha256`. |
| research_splits | Historical datasets: object with `development`, `validation`, `test`, each an inclusive two-element ISO date array. |
| splits_frozen_at | Historical datasets: offset-aware timestamp no later than exported_at. |

The three research intervals must be chronological and non-overlapping. Every evaluated historical session must fall inside one of them. Freeze them before inspecting strategy returns; the recorded timestamp is an operator assertion, not proof of that research discipline. Synthetic demos omit these two research-date fields. Each run copies the dates and source evidence into its manifest.

Keep source candle/metadata exports inside `raw/` without credentials. For each source file, obtain its hash with PowerShell, for example:

```powershell
(Get-FileHash -LiteralPath data/friend-options/raw/fyers-candles.json -Algorithm SHA256).Hash.ToLowerInvariant()
```

Source paths must stay within the dataset root; missing files or mismatching hashes fail. An `INCOMPLETE.txt` marker anywhere under the dataset root blocks use. The program also hashes consumed normalized files and checks for changes during a run. A hash proves file identity, not price accuracy; the audit flags remain your assertions.

## Audit before replay

From the repository root, replace the dates with the frozen evaluation interval:

```powershell
.\.venv\Scripts\python.exe -m gapfade options-audit --data data/friend-options --start 2026-01-05 --end 2026-01-09 --out runs/friend-options-audit-01
```

Use a fresh output folder every time. This is a structural audit of provenance, metadata, references and opening spot windows. It does not certify option execution paths or independently validate market prices. Replay performs the selected contract's holding/exit coverage checks. Read [the strategy and commands](../../docs/NIFTY_OPTIONS.md) before interpreting any performance.

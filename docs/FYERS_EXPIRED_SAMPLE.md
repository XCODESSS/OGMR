# Fetch an expired NIFTY options sample

The standalone downloader makes at most five read-only GET requests: discover
recent expired options, retrieve the contract catalog, fetch spot minutes, and
fetch one session for a CE/PE pair. It selects the latest returned past expiry
and a strike near the preceding session's 09:19 spot close. This is an access
sample, not the backtest strategy's contract-selection rule.

Use a current FYERS access token in the existing ignored login file:

```powershell
.\.venv\Scripts\python.exe -m gapfade login
.\.venv\Scripts\python.exe scripts\fetch_fyers_expired.py --token-file .secrets/fyers.json --out data/fyers-expired-sample-01
```

An app API key alone is insufficient. The token file must contain `client_id`
and `access_token` fields, as produced by login. Never commit credentials.

Each run needs a new output directory. Output includes original API JSON,
spot/CE/PE CSVs with IST timestamps, the returned contract catalog and a hashed
manifest. Failures leave `INCOMPLETE.txt`. The script does not retry rejected
requests and does not submit orders. Its five-request cap is per run; account
usage from previous runs or other clients still counts against FYERS limits.

These files are **not** the normalized input for `options-backtest`. Exchange
calendar, historical lot sizes, reference prices and coverage still need an
audit and mapping to [the options input format](../examples/options/FORMAT.md).
No performance claim follows from a successful download.

## Validate existing downloads offline

The local audit reads only downloaded JSON, CSVs and manifests. It verifies
available original hashes, candle values, raw/CSV agreement, overlapping exports,
observed-date minute coverage and missing evidence. It makes no API requests and
does not run a backtest or open credentials.

```powershell
.\.venv\Scripts\python.exe scripts\audit_fyers_data.py --sample data/fyers-expired-sample-20261105-03 --history data/fyers-expired-history-20261105-01 --out runs/fyers-data-validation-01
```

Use a new output directory for each run. Exit code 2 means the assessment completed
with readiness blockers; 1 means the audit failed. This raw-export audit does not
certify missing historical metadata, so a structurally valid capture can still
receive a blocked verdict.

Read `VALIDATION.md`, `validation.json`, `daily_coverage.csv`, `readiness_gaps.csv`
and `quarantine.csv`. `staging_lineage.csv` traces every provisionally staged bar
to its source row(s). Exact overlapping observations are coalesced; conflicting,
invalid or out-of-template rows are withheld. Original files remain unchanged.
The regular-session template is provisionally 09:15-15:29 IST on observed dates;
it does not establish the exchange calendar or explain post-market candles.
Zero-volume option bars are not executable fills, and positive volume does not
establish spread or attainable size. No missing minutes are filled.

Staging retains `BACKTEST_BLOCKED.txt` and has no fabricated verified provenance,
calendar, references, listings or lot sizes. It is deliberately not a ready input
for `options-backtest`.

References: [expired F&O API](https://fyers.in/api-docs/v3/api/market-data/expired-fo-data/)
and [plan limits](https://fyers.in/api-docs/v3/getting-started/plans-and-limits/).

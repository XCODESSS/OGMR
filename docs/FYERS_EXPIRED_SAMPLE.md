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

References: [expired F&O API](https://fyers.in/api-docs/v3/api/market-data/expired-fo-data/)
and [plan limits](https://fyers.in/api-docs/v3/getting-started/plans-and-limits/).

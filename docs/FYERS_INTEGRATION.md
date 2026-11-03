# Connect the gap-fade bot to FYERS

Run these commands from the repository root. This guide was checked against FYERS v3 documentation on 3 November 2026. No account-specific calls or live trades were made during development.

## Run the offline demonstration

Use Python 3.11 or newer. No API package, Pandas, pip installation or account login is required for the backtester. The integration uses FYERS' REST endpoints directly.

```powershell
python -m venv .venv --without-pip
.\.venv\Scripts\python.exe -m gapfade demo --out data/demo2
.\.venv\Scripts\python.exe -m gapfade backtest --config config/paper.json --universe data/demo2/universe.csv --calendar data/demo2/sessions.csv --references data/demo2/references.csv --bars data/demo2/bars.csv --out runs/demo2
```

Outputs are `trades.csv`, `daily_equity.csv`, `summary.json`, and `manifest.json`. The manifest records config and input hashes. Existing experiment folders are never overwritten. The demonstration uses fictitious instruments and prices, including deliberate winning and losing periods.

## Authenticate your FYERS app

Your app needs Basic permission for the profile identity check, Market Data permission for quotes/history and Order Placement permission only when eventually trading live. Use a registered redirect URL under your control. Trading requests must follow FYERS' current static-IP and app-permission requirements. Sources: [profile](https://fyers.in/api-docs/v3/api/account/profile/), [authentication](https://fyers.in/api-docs/v3/getting-started/authentication/), [quotes](https://fyers.in/api-docs/v3/api/market-data/quotes/).

Set the app ID and the redirect URL in your own terminal. These are placeholders:

```powershell
$env:FYERS_CLIENT_ID="YOUR_APP_ID"
$env:FYERS_REDIRECT_URI="YOUR_REGISTERED_REDIRECT_URL"
.\.venv\Scripts\python.exe -m gapfade login
```

The command asks for the app secret using a hidden prompt. It prints a FYERS authorization URL. Open it, log in yourself, and copy the complete resulting redirect URL into the hidden terminal prompt. The command verifies OAuth state and redirect identity before exchanging the code. Do not paste the secret, callback URL or token into chat.

The token is saved in `.secrets/fyers.json`, ignored by Git. It is not encrypted. Keep your Windows account and the folder's access permissions private; Git ignore rules do not provide access control. The bot never needs your account password, PIN or TOTP seed. It does not automate those login factors.

FYERS documents access-token expiry at 06:30 daily; generate a valid token for the session and handle authentication failures explicitly. Source: [FYERS access-token flow](https://fyers.in/api-docs/v3/api/authentication/individual-app/validate-auth-code/).

Check connectivity without submitting an order:

```powershell
.\.venv\Scripts\python.exe -m gapfade check --symbol NSE:SBIN-EQ
```

This reads profile identity and a quote, printing only authentication status and market fields. It does not prove the quote is fresh enough for entry or that the account has live-order permission. If response fields differ on your account, preserve the sanitized error and resolve the mapping before trading.

## Prepare the market inputs

Copy and edit the CSV templates under `examples/`. Their dates and prices are placeholders, and they are deliberately not ready to trade. The sample names illustrate FYERS identifiers, not stock recommendations.

### Universe

`universe.csv`:

```csv
symbol,start_date,end_date,long_allowed,short_allowed
NSE:SBIN-EQ,2026-11-01,2026-12-31,true,false
```

The date interval controls membership. Historical intervals must reflect the universe available at the time, not today's survivors. Use separate non-overlapping intervals where eligibility changes. `short_allowed=true` is an operator assertion that intraday short eligibility has been checked for that interval and intended product. Do not enable it merely because shorting appears profitable in a backtest.

Only `NSE:...-EQ` symbols are accepted. This initial runner supports up to 50 active stocks per snapshot, respecting the standard Quotes request size. Source: [FYERS Quotes](https://fyers.in/api-docs/v3/api/market-data/quotes/).

### Session calendar

`sessions.csv`:

```csv
session,previous_session
2026-11-03,2026-11-02
```

Provide actual exchange sessions, including the previous exchange session. For a historical run, include every consecutive session in the chosen evaluation interval, even days with no signals. The loader rejects discontinuities between supplied rows. It cannot independently detect an omitted initial/final session or an incorrect asserted holiday calendar. Verify the dates against exchange records. Special sessions are unsupported unless their schedule is explicitly compatible with the configured entry and exit times.

### Prior-close baselines for paper/live

`baselines.csv`:

```csv
session,symbol,previous_session,reference_close,verified
2026-11-03,NSE:SBIN-EQ,2026-11-02,1,false
```

Replace `1` with the audited, corporate-action-compatible prior-session reference close. Mark `verified=true` only after checking it. Include every active stock. The runner obtains today's opening price itself from FYERS after 09:15 and requires FYERS' `prev_close_price` to equal the baseline. A mismatch blocks entries and calls for a corporate-action/reference-price investigation.

The runner captures the complete cross-sectional signal in the 30 seconds before configured entry, then freezes it. Starting after entry without that saved snapshot produces no trades. A restart with a saved snapshot can enter only inside the configured 20-second entry window; a restart during an entry batch skips the remaining entries. It never chases a missed opening signal.

## Download historical data and backtest

After preparing your universe and calendar:

```powershell
.\.venv\Scripts\python.exe -m gapfade fetch --universe data/universe.csv --calendar data/sessions.csv --out data/fyers-history
```

The downloader uses daily and one-minute History requests in chunks of at most 90 calendar days. It saves decoded source candle arrays in `raw/`, normalized `bars.csv`, proposed `references.csv` and `provenance.json`. If it fails, `INCOMPLETE.txt` remains and the backtester refuses that directory. Use a new directory for a clean retry; existing data is not overwritten.

Historical access depends on your FYERS plan. The docs distinguish Standard and Prime retention and allow at most 100 days per intraday request. Do not assume your account can reproduce an eleven-year test. Source: [FYERS historical data](https://fyers.in/api-docs/v3/api/market-data/historical-data/).

The generated reference rows have `verified=false`. They combine daily open/prior close with a provisional signal-availability timestamp of 09:15:59. Audit corporate actions, provider adjustment conventions, calendar alignment and the availability assumption before marking rows verified. Daily History is not proof of what a live bot knew at that timestamp.

For a backtest, `references.csv` also includes `official_open` and offset-aware `available_at`:

```csv
session,symbol,previous_session,reference_close,official_open,available_at,verified
2026-01-05,NSE:DEMOA-EQ,2026-01-02,100,97,2026-01-05T09:15:30+05:30,true
```

That row is synthetic. Prices and dates for real instruments must come from your audited data.

`bars.csv` contains minute-start `timestamp,symbol,open,high,low,close,volume`. Every timestamp must include an offset. The engine requires every minute from entry through scheduled exit for selected candidates, with valid OHLC and positive volume. It aborts on duplicate, invalid or missing required bars. It does not interpolate fills or silently remove incomplete trades.

```powershell
.\.venv\Scripts\python.exe -m gapfade backtest --config config/paper.json --universe data/universe.csv --calendar data/sessions.csv --references data/fyers-history/references.csv --bars data/fyers-history/bars.csv --out runs/research-001
```

Start with a few stocks and a bounded date range. This version loads the CSV bars into memory; large multi-year universes need a partitioned or database-backed data layer before scaling. The downloader also buffers normalized rows. Watch machine memory and split datasets into manageable research runs without changing the frozen evaluation rules.

The default model charges 10 bps per leg on actual simulated turnover, plus 5 bps adverse price slippage per leg. These are adjustable scenarios, not a complete verified historical statutory-charge schedule. Paper fills use bid/ask plus adverse slippage, so spread is included through quotes rather than added again as a separate charge. Minute-bar backtests cannot establish available depth or actual fills.

Freeze chronological development, validation and untouched evaluation intervals before inspecting returns. Preserve every experiment folder and record parameter changes. The code supplies reproducible experiments; it does not enforce your research split or correct survivorship-biased inputs automatically.

## Run paper trading with FYERS data

Adjust `config/paper.json` to your chosen simulated capital and limits. Keep `allow_live=false`. Prepare a verified baseline for the upcoming session and start before 09:15:30 IST:

```powershell
.\.venv\Scripts\python.exe -m gapfade paper --config config/paper.json --universe data/universe.csv --calendar data/sessions.csv --baselines data/baselines.csv --database runs/paper.sqlite
```

The paper broker cannot call an order endpoint. It simulates fills using current bid/ask plus configured slippage and records them in SQLite. It does not simulate exchange queue position, quote size constraints or realistic partial fills; treat its fills as a paper model, not evidence of attained executions.

Keep the process and machine awake through exit. Each date's config, account/mode identity and input hashes are fixed in its journal. Do not edit those files during a session or switch databases to bypass an error. Use the same paths and config when recovering that date. A new date with unresolved prior state is blocked. Closed paper equity carries to the next session; live allocation remains your explicit configured capital.

Inspect local records:

```powershell
.\.venv\Scripts\python.exe -m gapfade status --database runs/paper.sqlite
```

The local status command is not a broker reconciliation. An `unknown` order means the real submission outcome has not been established.

## What changes before real money

Live execution code is present but has only been tested with simulated FYERS responses. Do not treat passing mocks as live broker certification. Before enabling it, establish a plausible independent net edge, complete forward paper validation, verify API permissions/static IP, confirm current broker product eligibility/margins and inspect account-specific response behavior. Choose actual capital and loss limits then; none has been assumed for your account.

Use an isolated INTRADAY book. The live runner refuses to proceed when account intraday positions or pending intraday orders differ from its journal. It never closes unrelated CNC holdings or invokes an account-wide exit. Do not manually trade the same intraday book while the runner is active.

Copy the paper config to `config/live.json`. Set all of the following deliberately:

- `allow_live=true`.
- `live_readiness_ack=true` after the validation above.
- A positive `live_capital`, representing your allocated strategy capital.
- A positive `live_max_order_notional`, capping the quoted notional used to size each entry.
- Your chosen position weight, maximum positions and daily loss fraction.

In your terminal, only when you intend to permit actual orders:

```powershell
$env:FYERS_LIVE_ARM="I_ACCEPT_LIVE_ORDERS"
.\.venv\Scripts\python.exe -m gapfade live --config config/live.json --universe data/universe.csv --calendar data/sessions.csv --baselines data/baselines.csv --database runs/live.sqlite
```

This command can place real FYERS orders. It uses market orders with `productType=INTRADAY`, observes actual cumulative fills and reconciles positions. Order acceptance is not treated as a fill. Partial fills are tracked; pending orders are monitored and cancellation requested after the bounded wait. Unknown submissions are persisted before any possible recovery and never blindly retried. Sources: [single order](https://fyers.in/api-docs/v3/api/trading/order-placement/sync/single-order/), [order status and partial fills](https://fyers.in/api-docs/v3/api/trading/order-book/get-orders/).

The quoted notional cap is not a guaranteed final market-fill notional. Market prices can move, and the allocated capital is not automatically derived from account funds. FYERS can reject insufficient margin or disallowed orders. Slippage and statutory costs can exceed the model. The daily loss threshold is a software-observed trigger, not a guaranteed maximum loss or a broker-resident stop. The code does not currently attach exchange/broker stop orders.

The reference backtest sees completed minute closes and acts at the next minute open. The paper/live runner polls quotes every five seconds by default, so its loss-trigger timing and execution will differ. Compare those differences explicitly during paper validation.

## Shutdown and recovery

To request a controlled shutdown while the runner is working:

```powershell
New-Item -ItemType File -Path .\STOP_TRADING -Force
```

The runner stops adding positions and attempts to flatten only its own positions. Leave the file present until you have inspected closure. At the next intended session, remove it deliberately after checking positions. A local file has no effect if the process has stopped, the machine is asleep or broker connectivity is unavailable.

Ctrl+C stops the process; it does not guarantee liquidation. Check pending orders and positions in FYERS immediately. A rejected/unknown order, reconciliation mismatch, authorization problem or broker API failure can stop the runner and require manual intervention. Unavailable quotes while monitoring are retried; scheduled live exits do not require the quote feed, but they still require working order/position APIs.

On restart, existing unknown submissions are looked up by their deterministic order tags. If an order is absent, ambiguous or still pending, the runner stops for manual reconciliation instead of resubmitting. If exit attempts leave a residual position, inspect and manage it in FYERS. Never delete the live journal to bypass recovery.

The `.lock` next to the SQLite journal prevents two processes sharing it. A crash can leave a stale lock. Verify the original process has exited and inspect the broker before removing that exact stale lock. Using another database file does not provide account-wide locking; run only one bot instance for this intraday book.

## F&O scope

This release rejects futures/options identifiers. Futures need expiry/roll handling, point value, lot sizes, margin and contract-level price data. Options additionally need expiry/strike selection, premium-based fills, bid/ask liquidity and whole-lot sizing. The stock signal may become an input, but its stock backtest does not validate either derivative strategy. F&O remains a separate implementation and validation stage.

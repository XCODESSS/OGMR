# Live-market options paper testing

This mode runs on your computer during the current market session. FYERS supplies
live quotes and today's completed minute candles; entries and exits are simulated
locally. There is no order-placement API on the paper adapter. No historical
backtest or expired-options dataset is required.

## Start here on Windows

Use Python 3.11 or later and run from `D:\intra-strat`. Generate a valid FYERS
session token locally using the existing login command:

```powershell
.\.venv\Scripts\python.exe -m gapfade login
```

Then, on a regular exchange session, start around **09:10 IST**, before 09:16:

```powershell
.\.venv\Scripts\python.exe -m gapfade options-paper --calendar examples/options/paper-sessions-2026-nov-dec.csv --out runs/options-paper-2026-11-06
```

Change the output suffix to the actual session date. Reuse that exact output
directory if restarting the same day's process; use a new directory next session.
The default token path is `.secrets/fyers.json` and the default strategy config
is `config/options-research.json`. No account credentials go into the calendar.
On macOS/Linux, use the repository's Python 3.11+ interpreter in place of
`.\.venv\Scripts\python.exe`.

The supplied calendar covers November 5 through December 31, 2026 and was derived
from the [NSE holiday page](https://www.nseindia.com/resources/exchange-communication-holidays)
on November 5. November 10, November 24 and December 25 are excluded. The declared
November 8 Muhurat session is included as `is_regular=false`: it counts as an
exchange session for expiry distance, but this strategy will not run during it.
The file assumes no later exchange circular changes these dates. Recheck exchange
notices before use; do not extend the file by guessing holidays. Its schema is
`session,previous_session,is_regular`, with consecutive exchange sessions through
at least an eligible contract expiry. The first prior session is November 4.

## What happens during the session

- The runner downloads the current public FYERS NIFTY option symbol master once
  and records when it was observed. It uses the returned symbols, strikes, expiry
  dates and current lot sizes. Far expiries beyond the calendar horizon are not
  considered; expiry dates within the horizon must exist in the calendar.
- After 09:16 it observes today's official-open/prior-close quote fields. At 09:20
  it reads the completed 09:15-09:19 spot window, freezes the strategy decision and
  selects the eligible ATM CE/PE using the existing minimum-two-session rule.
- After the 09:20 minute completes, it checks pending-entry cancellation. A paper
  buy uses the first accepted fresh ask from 09:21 through 09:21:20 plus adverse
  slippage. Whole-lot risk and premium budgets remain unchanged. An unaffordable
  lot is a no-trade outcome; no fractional lot or extra capital is invented.
- Each newly completed spot/option minute drives the existing premium-stop,
  opening-range failure and gap-fill conditions. A simulated sell uses a fresh bid
  after the trigger becomes observable, with adverse slippage. Otherwise it
  attempts to exit at/after 15:00. There is one entry attempt per day.

The runner polls every five seconds, but caches completed candles. The History
API here supplies only minutes that have just completed **today**; it is not
replaying old sessions. Partial minutes and missing candles are refused.

## Journal, restart and stopping

In the output directory:

- `state.json`: readable current paper position, final simulated trade/P&L, or
  no-trade reason. This is simulated performance, never broker fills.
- `journal.sqlite`: committed state and an append-only event table. Signal,
  selection, entry and exit state survive a restart without duplicate entries.
- `process-lock.sqlite`: automatically released process lock. Do not run two
  paper processes against the same output, or copy it to bypass the lock.
- `market_data.jsonl`: timestamped, credential-free source responses and request
  parameters for later audit.
- `symbol_master.csv`, `symbol_master_received.json`: today's cached catalog.
- `data_request_usage.json`: attempted authenticated data GETs in this output.

Read status from another PowerShell terminal:

```powershell
Get-Content runs/options-paper-2026-11-06/state.json
```

Create the paper-specific kill file to cancel entry or attempt a simulated exit:

```powershell
New-Item -ItemType File -Path STOP_OPTIONS_PAPER
```

Remove that exact file before starting another intended session. A missing/stale
exit quote never makes a position flat. Ctrl+C preserves the journal; restart
using the same output. At the paper recovery cutoff of 15:30, unresolved positions
remain explicitly unresolved and the program exits nonzero. This cutoff is a
program limit, not a claim about the exchange's derivatives closing time.

The local cap is **1,000 data requests per output session**, persisted across
restarts. It is separate from FYERS' account-wide limits and does not count other
programs using your account. Do not create another output to bypass an exhausted
budget or duplicate a position. There are no automatic order requests.

Calendar-source inspection also found that the current NSE page lists equity
derivatives closing at 15:40. Therefore the earlier raw-data audit's provisional
15:30 quarantine boundary does not prove that 15:30-15:39 candles are invalid.
Reassess that audit convention separately; raw files were preserved. The paper
strategy still schedules its simulated exit at 15:00.

## Interpretation and remaining checks

Freshness requires an option quote timestamp within ten seconds and a valid
bid/ask. Unreliable timestamps, crossed quotes, missing minutes and zero-volume
option minutes block fills or trigger monitoring. Errors remain in the event
journal and are counted on the closing trade. A scheduled exit may still close a
paper position using a valid current quote after a monitoring gap; inspect those
errors before treating it as a complete strategy observation.

Paper fills use observed bid/ask with a cost scenario, not guaranteed executable
prices or depth. Five-second polling and API delays mean these are actual observed
paper timestamps, not backtest candle-open fills. A whole completed entry-minute
candle may include movement before a fill a few seconds into that minute; the
intraminute ordering cannot be reconstructed from minute bars. Feed latency and
such ambiguities must remain visible when comparing paper and backtest results.

This implementation is tested with mocked forward-moving feeds. It has not yet
been authenticated and observed through an actual full market session. Initial
use is a feed/behavior check. Test success is not evidence of profitability.

Official API references: [quotes](https://fyers.in/api-docs/v3/api/market-data/quotes/),
[completed candles](https://fyers.in/api-docs/v3/api/market-data/historical-data/),
[symbol-master schema](https://fyers.in/api-docs/v3/api/market-information/symbol-master/symbol-master-file/).

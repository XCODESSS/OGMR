# NIFTY Options Gap-Fade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Adapt the existing opening-gap mean-reversion hypothesis into a reproducible, offline NIFTY option-buying backtest using actual historical option prices supplied by the user's friend through FYERS.

**Architecture:** Add focused `gapfade/options_*.py` modules and separate CLI commands; reuse Decimal/time/CSV helpers, not the stock selector, stock sizing or execution runner. Keep spot signals separate from contract metadata and premium accounting. Read one session's spot bars and only the selected option contract's bars at a time.

**Tech Stack:** Python >=3.11, standard library only, dataclasses, Decimal, csv, json, hashlib, unittest; Windows PowerShell. Current local interpreter verified: Python 3.12.6.

**Spec:** No existing options specification exists. The normative specification is embedded below, based on this conversation's request to improve the current strategy for options. `research/CANDIDATE_SPEC.md` describes the inspected stock hypothesis only and is ignored by Git; it is background, not the options specification. All new numerical choices are unvalidated research assumptions.

## Global Constraints

- Planning is authorized now; implementation, authenticated downloads, backtests against real data, paper execution and live orders have not been requested by this planning turn.
- Preserve the gap-fade idea: gap down expresses a bullish reversal through a purchased CE; gap up expresses a bearish reversal through a purchased PE.
- NIFTY options only for the new path: one long option position, one entry attempt per evaluated session, no stock ranking, option selling, futures, averaging, re-entry or overnight carry.
- Python >=3.11; no new runtime or testing dependencies. Match existing unittest discovery conventions.
- All financial calculations use Decimal, integer lots and date-effective historical lot sizes.
- Time zone is Asia/Kolkata. Offset-aware minute-start timestamps are mandatory.
- Use actual historical option candles and historical contract metadata. Never synthesize premiums from spot returns or reconstruct the historical chain using today's contracts.
- Keep existing stock CLI behavior and live-order gates unchanged. New commands never instantiate `FyersClient` or call an order endpoint.
- No real data, credentials, caches or run artifacts enter Git. Keep templates under `examples/options/`; normalized data under ignored `data/`; results under ignored `runs/`.
- Label modeled costs and fills as assumptions, not verified historical taxes or attainable fills. No performance improvement or profitability claim before evidence.
- Existing experiment folders must never be overwritten. Preserve invalid-run diagnostics without publishing valid performance for an incomplete ledger.
- Do not repair the old stock downloader, broaden stock-universe support, or restructure unrelated code in this feature.
- The header's execution skills are not present in this session's available skill catalog. At execution time check availability; do not invent their instructions or spawn agents without an applicable authorization. The plan itself requires no delegation.

---

## Frozen research specification: `nifty-gap-fade-options-v1`

### Signal and timing

1. On each regular exchange session, compute `gap = official_open / previous_session_close - 1` from verified NIFTY spot references. The previous session must match the calendar exactly. No corporate-stock universe or stock reference adjustments are reused.
2. Inclusive threshold: `abs(gap) >= Decimal('0.005')`. Smaller gaps produce `no_signal`.
3. Aggregate exactly the five spot minutes starting 09:15 through 09:19: open = 09:15 open, close = 09:19 close, high/low = extrema of those five bars. Index volume may be zero.
4. With confirmation enabled: negative gap requires `opening_close > opening_open` and `opening_close < previous_close`, selecting CE; positive gap requires `opening_close < opening_open` and `opening_close > previous_close`, selecting PE. Equality is not confirmation. If already at/beyond previous close, no trade in either variant.
5. Freeze the decision at 09:20 from completed bars. Use the 09:19 spot close for ATM selection. Fill at the 09:21 option bar open with adverse slippage. This intentionally adds one minute of modeled decision/order latency to the earlier conversational 09:20 proposal. Do not use the 09:21 spot open to change the selected strike.
6. If the completed 09:20 spot candle has already reached the gap-fill target before entry, cancel the pending entry (`target_before_entry`). Do not evaluate an opening-range stop before a position exists.
7. Reference `available_at` must be strictly before 09:20. Historical bar availability is modeled as one minute after its start; document that feed latency is an assumption.
8. Special sessions are recorded as `excluded_special_session`, never forced into regular clock times. Every evaluated date appears in the coverage ledger.

### Contract choice

- Use only NIFTY option contracts listed by decision time and date-effective on the session.
- Choose the earliest expiry with at least two exchange sessions strictly after entry day and on/before expiry. Calendar data must extend through the furthest catalog expiry; reject insufficient horizon, not guess weekdays or expiry rules.
- Within that expiry and CE/PE direction, choose the available listed strike nearest frozen spot. Break an equal-distance tie by lower strike; duplicate active records for an identical series are invalid data.
- No fallback to a different strike/expiry because the selected contract's later price path is missing or unattractive. Missing catalog coverage is a data error, not evidence that no contract existed.
- Use the historical symbol as an opaque identity. Do not derive expiry/lot size by parsing a FYERS symbol or hard-code weekly expiry weekdays, strike intervals or current lot sizes.

### Exits and information ordering

- For an open CE, the spot target is `bar.high >= previous_close` and the spot failure stop is `bar.low < opening_low`.
- For an open PE, target is `bar.low <= previous_close` and failure stop is `bar.high > opening_high`.
- Option premium stop is `option_bar.low <= entry_fill * Decimal('0.75')`.
- Observe triggers using completed minutes, beginning with the entry minute 09:21 and ending at 14:59. Exit at the next minute's option open, with adverse slippage. This is a bar-close evaluation model; it does not simulate broker-resident stops or guaranteed threshold fills.
- Record all simultaneous trigger flags; deterministic primary reason priority is premium stop, spot failure, gap fill. Since every trigger uses the same next open, ordering does not invent a threshold-price fill. Mark `multiple_triggers=true` when applicable.
- If none fires, exit at the 15:00 option open. A 14:59 trigger also fills at 15:00, retaining its trigger reason.
- Require positive-volume option bars for entry and the observed holding/exit path. A missing/zero-volume required option bar or missing required spot bar invalidates the run with unresolved position information if already entered. Do not forward-fill, erase the trade, assume a zero P&L exit, or search forward for a convenient price.
- Stop requesting bars after the actual exit. Missing 14:00 data cannot invalidate a trade closed at 10:00. Validate structure for every encountered input row; validate temporal coverage only where the strategy needs it.
- Gap/stop logic uses spot prices; gross P&L always uses option premium fills.

### Sizing and costs

- Defaults are research settings: capital Rs 100,000; planned stop-risk fraction 0.005; premium cap fraction 0.02; premium stop fraction 0.25; adverse slippage 25 bps per leg; modeled fees 10 bps of premium turnover plus Rs 20 per filled order per leg. These costs are scenarios, not a statutory schedule.
- Buy fill `E = raw_open * (1 + slippage_bps/10000)`; sell fill `X = raw_open * (1 - slippage_bps/10000)`.
- Round down to whole lots, then reduce lots until all constraints pass, including estimated charges. With `q=lots*lot_size`, estimated exit `S=E*(1-stop_fraction)`, and round-trip fees `F=q*(E+S)*fee_bps/10000+2*fixed_fee_per_order`:
  - planned stop risk `q*(E-S)+F <= equity*0.005`;
  - premium plus estimated charges `q*E+F <= equity*0.02`;
  - premium plus estimated charges cannot exceed available simulated equity.
- If one lot cannot satisfy limits, record `insufficient_capital`, not a fractional lot. No leverage and no reuse of same-day gains.
- Actual fees `q*(E+X)*fee_bps/10000 + 2*fixed_fee_per_order`; gross P&L `q*(X-E)` for both CE and PE; net P&L subtracts actual fees. A profitable PE still needs rising premium.
- Planned stop risk is not a guaranteed loss limit. Full premium plus charges remains at risk. No separate daily stop is required because there is one position/attempt per day.

### Comparison and research boundaries

- Primary candidate has opening-candle confirmation enabled.
- Baseline disables only its directional candle test; it retains the minimum gap, not-already-filled condition, 09:21 entry, contract selection, sizing, stops, costs and calendar. This isolates the confirmation filter at equal execution timing.
- Both variants use identical frozen dates and audited datasets; report zero-trade sessions in returns. Do not tune 0.5%, 25%, entry time or expiry selection against final evaluation returns.
- Before real-data performance inspection, write exact chronological development/validation/untouched-test dates into the experiment manifest. Dates depend on the friend's audited coverage and are not invented in this plan.

## File map and interfaces

Existing files inspected: `gapfade/data.py:21` restricts equity symbols; `gapfade/backtest.py:34` loads the whole stock dataset; `gapfade/strategy.py:7` ranks stocks; `gapfade/model.py` provides reusable Decimal and time helpers; `gapfade/__main__.py:14` owns CLI routing. `tests/test_backtest.py` uses TemporaryDirectory and synthetic CSVs. `pyproject.toml` declares zero dependencies.

| File | Responsibility |
| --- | --- |
| Create `gapfade/options_model.py` | Immutable options configuration, metadata, signal, decision and trade records; schema validation. |
| Create `gapfade/options_data.py` | Normalize/read audited friend-supplied CSVs, calendar/catalog/reference checks, per-session bar loading and source hashes. |
| Create `gapfade/options_strategy.py` | Pure spot signal and point-in-time contract selection. |
| Create `gapfade/options_accounting.py` | Long-premium fills, whole-lot sizing, costs and ledger arithmetic. |
| Create `gapfade/options_backtest.py` | Session state machine, reports, invalid-run diagnostics and config/input manifest. |
| Create `gapfade/options_demo.py` | Deterministic fictitious fixtures with CE and PE cases and explicit provenance. |
| Create `config/options-research.json` | Frozen candidate defaults; confirmation=true; no broker/live settings. |
| Modify `gapfade/__main__.py` | Register `options-demo`, `options-audit`, `options-backtest` and dispatch independently. |
| Create `examples/options/FORMAT.md` | Friend-facing export contract and exact file schemas. |
| Create `docs/NIFTY_OPTIONS.md` | Strategy, timing, commands, validation and current coverage limits. |
| Modify `README.md`, `docs/FYERS_INTEGRATION.md` | Distinguish offline options support from existing equity-only paper/live and fetch commands. |
| Create six `tests/test_options_*.py` files | Model/data, signal, accounting, replay, CLI and synthetic demo acceptance tests listed below. |

### Input directory contract

`data/friend-options/` is a normalized export supplied by the friend, not a new authenticated downloader. Its files:

```text
provenance.json
sessions.csv
references.csv
contracts.csv
spot/2026-01-05.csv
options/2026-01-05.csv
```

- `sessions.csv`: `session,previous_session,is_regular`; strict true/false, unique ordered dates, each row after the first points to the immediately preceding supplied session. Include the evaluation interval plus sessions through every catalog expiry, and references to the previous evaluation session.
- `references.csv`: `session,previous_session,official_open,previous_close,available_at,verified`; one NIFTY row per evaluated regular session. Positive prices, exact calendar reference, verified=true backed by the provenance audit. Do not infer availability from the daily history response.
- `contracts.csv`: `symbol,underlying,option_type,strike,expiry_session,listed_at,valid_from,valid_to,lot_size`; canonical underlying `NIFTY`, type CE/PE, positive strike, positive integer lot size, non-overlapping inclusive validity intervals for each symbol, valid exchange expiry session. Contract series identity must be unique in an active snapshot. `listed_at` includes offset and must predate decision; lot-size changes use dated rows.
- Day bar files: `timestamp,symbol,open,high,low,close,volume`; spot symbol is normalized to `NIFTY`, option symbol matches the catalog. Validate positive finite OHLC, `low <= min(open,close) <= max(open,close) <= high`, nonnegative volume, minute alignment, file date and duplicate `(timestamp,symbol)`. Accept zero spot volume; option volume is a separate execution-path check.
- `provenance.json`: `schema_version`=1, `data_kind`=`historical` or `synthetic`, `provider`, `exported_at`, `calendar_verified`, `references_verified`, `catalog_verified`, `bar_timestamp_convention`=`minute_start`, `spot_reference_convention`, `audit_notes`, and `source_files` (nonempty list of objects with relative `path` and lowercase `sha256`). Relative source files must exist within the dataset root, pass hash verification, and must not include credentials. Historical audit flags cannot be manufactured by an importer; the friend must document what was checked in audit_notes. Hash normalized files as well in each run manifest. Synthetic provenance must always reach summary labels.
- `coverage.csv` is produced by the audit/run with one row per evaluated day: session, status, reason, selected_symbol, first_missing_timestamp, position_open. Distinguish genuine strategy exclusions from invalid/incomplete data.
- No quotes/depth are required in v1; report that candle volume alone cannot establish spread or available size. Do not present the 25-bps scenario as measured spread.

### Shared types

Use `gapfade.model.Bar`, `TradingError`, `number`, `timestamp`, `at`, `fill_price`; leave their existing behavior unchanged. New records use dataclasses and Decimal fields:

```python
@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    option_type: str
    strike: Decimal
    expiry_session: date
    listed_at: datetime
    valid_from: date
    valid_to: date
    lot_size: int

@dataclass(frozen=True)
class SpotReference:
    session: date
    previous_session: date
    official_open: Decimal
    previous_close: Decimal
    available_at: datetime
    verified: bool

@dataclass(frozen=True)
class OptionSignal:
    session: date
    option_type: str
    gap: Decimal
    decision_at: datetime
    spot_at_decision: Decimal
    target: Decimal
    failure_level: Decimal

@dataclass(frozen=True)
class SessionDecision:
    signal: OptionSignal | None
    reason: str  # signal, below_gap_threshold, unconfirmed, already_filled
```

`OptionsConfig` has capital, gap_threshold, confirmation, stop_fraction, risk_fraction, premium_cap_fraction, slippage_bps, fee_bps and fixed_fee_per_order fields using the defaults above; money/fractions stored as strings in JSON, bool confirmation. Methods `load(path)` and `snapshot()` follow the stock Config pattern. Clock times are fixed by strategy version, not exposed as extra tunable knobs. Reject unknown fields, non-finite values, capital<=0, fractions outside (0,1], slippage>=10000, negative charges and non-bool confirmation. Require risk_fraction<=premium_cap_fraction. Validate lot sizes without accepting booleans or fractional strings.

## Task 1: Options domain, configuration and deterministic sizing

**Files:** Create `gapfade/options_model.py`, `gapfade/options_accounting.py`, `config/options-research.json`, `tests/test_options_accounting.py`.

**Interfaces:** Consumes shared Decimal/helpers. Produces the records above, `OptionsConfig`, `size_lots(equity: Decimal, entry_fill: Decimal, lot_size: int, config: OptionsConfig) -> int`, and `trade_amounts(entry_fill: Decimal, exit_fill: Decimal, quantity: int, config: OptionsConfig) -> tuple[Decimal, Decimal, Decimal]` returning gross, fees, net.

- [x] Add focused failing tests, including exact long-premium arithmetic and whole-lot sizing:

```python
import unittest
from decimal import Decimal as D
from gapfade.options_model import OptionsConfig
from gapfade.options_accounting import size_lots, trade_amounts

class OptionsAccountingTests(unittest.TestCase):
    def test_lots_reserve_costs(self):
        self.assertEqual(size_lots(D('100000'), D('100'), 10,
                                  OptionsConfig()), 1)
    def test_pe_uses_long_premium_accounting(self):
        gross, fees, net = trade_amounts(D('100'), D('120'), 10,
                                       OptionsConfig())
        self.assertEqual((gross, fees, net),
                         (D('200'), D('42.2'), D('157.8')))
    def test_one_lot_can_be_unaffordable(self):
        self.assertEqual(size_lots(D('1000'), D('100'), 10,
                                  OptionsConfig()), 0)
```

- [x] Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_options_accounting.py -v`; expect import failure before implementation.
- [x] Implement types/config and fill arithmetic. Implement `size_lots` using initial `lots = int(min(equity, equity * number(config.premium_cap_fraction)) / (entry_fill * lot_size))`, then decrement until all three sizing inequalities pass; return zero if no positive lots fit. For invalid lot_size, equity or price raise TradingError. `trade_amounts` rejects nonpositive quantity/nonpositive prices.
- [x] Add cases for exact budget boundaries, high fixed charges, non-finite fields, unknown JSON keys, bool lot size, non-bool confirmation and slippage >=10000. Verify quantity is always an integer multiple of lot_size and all limits hold for returned lots.
- [x] Save `config/options-research.json` with the nine numeric/bool fields and defaults above. Run the focused command; expect all assertions to pass.
- [x] Commit only these four files with `git commit -m "feat: define options research configuration and premium accounting"` after explicit-path `git add`.

## Task 2: Auditable day-partitioned inputs

**Files:** Create `gapfade/options_data.py`, `examples/options/FORMAT.md`, `tests/test_options_data.py`.

**Interfaces:** Consumes OptionsConfig/OptionContract/SpotReference and shared Bar. Produces `OptionsDataset(root: Path)`, with attributes `calendar: dict[date,date]`, `regular: dict[date,bool]`, `contracts: list[OptionContract]`, `references: dict[date,SpotReference]`, `provenance: dict`. Methods `spot(day: date) -> dict[datetime,Bar]`, `option(day: date, symbol: str) -> dict[datetime,Bar]`, `input_hashes() -> dict[str,str]`, `validate_range(start: date,end: date) -> list[date]`. `audit(dataset: OptionsDataset,start: date,end: date,output: Path) -> dict` writes coverage/manifest and returns a structural coverage summary, never performance.

- [x] Write failing tests with TemporaryDirectory CSVs: zero-volume NIFTY accepted; duplicate bars rejected; bad OHLC rejected; offset-free timestamps rejected; absent daily references rejected; overlapping contract lot intervals rejected. Example central assertion:

```python
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from gapfade.options_data import OptionsDataset

class OptionsDataTests(unittest.TestCase):
    def test_index_volume_is_not_a_liquidity_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'spot').mkdir()
            (root / 'source.txt').write_text('fictitious fixture', encoding='utf-8')
            (root / 'sessions.csv').write_text(
                'session,previous_session,is_regular\n'
                '2026-01-05,2026-01-02,true\n'
                '2026-01-06,2026-01-05,true\n'
                '2026-01-07,2026-01-06,true\n', encoding='utf-8')
            (root / 'references.csv').write_text(
                'session,previous_session,official_open,previous_close,available_at,verified\n'
                '2026-01-05,2026-01-02,99,100,2026-01-05T09:16:00+05:30,true\n',
                encoding='utf-8')
            (root / 'contracts.csv').write_text(
                'symbol,underlying,option_type,strike,expiry_session,listed_at,valid_from,valid_to,lot_size\n'
                'DEMO:CE,NIFTY,CE,100,2026-01-07,2026-01-01T00:00:00+05:30,2026-01-01,2026-01-07,10\n',
                encoding='utf-8')
            (root / 'spot' / '2026-01-05.csv').write_text(
                'timestamp,symbol,open,high,low,close,volume\n' + ''.join(
                    f'2026-01-05T09:{minute:02d}:00+05:30,NIFTY,99,99.3,98.9,99.2,0\n'
                    for minute in range(15, 20)), encoding='utf-8')
            provenance = dict(
                schema_version=1, data_kind='synthetic', provider='fixture',
                exported_at='2026-01-08T00:00:00+05:30',
                calendar_verified=True, references_verified=True, catalog_verified=True,
                bar_timestamp_convention='minute_start',
                spot_reference_convention='synthetic previous session close',
                audit_notes='Fictitious inputs for parser tests only.',
                source_files=[dict(path='source.txt', sha256=hashlib.sha256(
                    (root / 'source.txt').read_bytes()).hexdigest())])
            (root / 'provenance.json').write_text(json.dumps(provenance), encoding='utf-8')
            bars = OptionsDataset(root).spot(date(2026, 1, 5))
            self.assertEqual(len(bars), 5)
            self.assertTrue(all(bar.volume == 0 for bar in bars.values()))
```

- [x] Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_options_data.py -v`; expect import failure.
- [x] Implement metadata loaders with the exact schemas above; use explicit csv.DictReader field checks and shared validation helpers. Read a day's option CSV sequentially; retain only the requested contract. No list of all intraday records across dates.
- [x] Implement path containment via Path.resolve()/relative_to(root.resolve()) before reading provenance source files; verify hashes and reject an `INCOMPLETE.txt` marker anywhere in the dataset root. Never use a source path as a shell command.
- [x] Implement date-range/calendar horizon and provenance checks. Audit source completeness, opening spot window and references, plus regular-session classification; bar execution-path completeness is checked by replay after contract selection. Label the audit `structural_only`, because it does not certify truth of provider prices or provenance assertions.
- [x] Add tests for missing source files, hash mismatch, path escape, false historical audit flags, unknown expiry session and insufficient future calendar coverage. For synthetic fixtures retain `data_kind=synthetic` throughout.
- [x] Document every input column and the distinction between source evidence, operator audit assertions, execution validity and genuine no-trade days in FORMAT.md. State that FYERS account/API access and raw-export mapping are supplied by the friend; this task does not add an API adapter.
- [x] Run the focused test command; expect all assertions to pass. Commit the three paths with `git commit -m "feat: load audited session-partitioned options inputs"`.

## Task 3: Frozen spot signals and historical contract selection

**Files:** Create `gapfade/options_strategy.py`, `tests/test_options_strategy.py`.

**Interfaces:** Consumes Task 1 records and Task 2 mappings. Produces `decide(reference: SpotReference, opening_bars: list[Bar], config: OptionsConfig) -> SessionDecision`, `choose_contract(signal: OptionSignal, contracts: list[OptionContract], sessions: list[date]) -> OptionContract`. `decide` requires exactly timestamps 09:15..09:19 for reference.session and validates reference availability; unknown data raises TradingError rather than returning no_signal.

- [x] Write failing tests using actual dataclass construction, including this negative-gap signal:

```python
def test_gap_down_rejection_buys_ce(self):
    from datetime import date
    from decimal import Decimal as D
    from gapfade.model import Bar, at
    from gapfade.options_model import OptionsConfig, SpotReference
    from gapfade.options_strategy import decide
    day, previous = date(2026, 1, 5), date(2026, 1, 2)
    ref = SpotReference(day, previous, D('99'), D('100'),
                        at(day, '09:16'), True)
    bars = [Bar(at(day, f'09:{minute:02d}'), 'NIFTY', D('99'),
                D('99.3'), D('98.9'), D('99.2'), D('0'))
            for minute in range(15, 20)]
    decision = decide(ref, bars, OptionsConfig())
    self.assertEqual(decision.signal.option_type, 'CE')
    self.assertEqual(decision.signal.spot_at_decision, D('99.2'))
    self.assertEqual(decision.signal.failure_level, D('98.9'))
```

- [x] Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_options_strategy.py -v`; expect missing module/function failure.
- [x] Implement the numbered signal rules exactly; no stochastic filter or technical indicator dependencies. Construct the combined candle using first open, last close and extrema, not an average of closes.
- [x] Implement contract filters in order: known/listed/date-effective NIFTY metadata; correct CE/PE; minimum two later sessions through expiry; nearest eligible expiry; nearest listed strike; lower-strike tie. Detect duplicate series rather than breaking duplicates by symbol.
- [x] Add tests for inclusive +/-0.5%, below threshold, doji, already-filled gap, unconfirmed candle, PE direction, baseline disabling only confirmation, missing opening minute, late reference, expiry holiday via supplied calendar, listing after decision, lot-size effective dates and nearest-strike tie.
- [x] Run the focused command; expect all assertions to pass. Commit with `git commit -m "feat: select NIFTY gap-fade signals and historical ATM options"`.

## Task 4: Session replay, invalid-run evidence and performance ledger

**Files:** Create `gapfade/options_backtest.py`, `tests/test_options_backtest.py`.

**Interfaces:** Consumes Tasks 1-3. Produces `run(config: OptionsConfig, dataset_root: Path, start: date, end: date, output: Path) -> dict`. Its return value is the valid summary. Use an internal `replay_session(config, dataset, day, equity) -> tuple[dict | None, dict]` returning a trade row or None and a daily row; TradingError carries invalid data failures. Helpers and records defined in this module are internal, not imported by earlier tasks.

- [x] Write a failing integrated CE case using a small hand-authored dataset in TemporaryDirectory; set fee/slippage to zero via dataclasses.replace. Assert entry 09:21, trigger 09:22 known at09:23, exit09:23, quantity=lots*lot_size and exact premium P&L. Add a PE whose spot falls but premium also falls; assert its trade loses money.
- [x] Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_options_backtest.py -v`; expect missing module/function failure.
- [x] Implement the state machine in this order:

```text
for each requested calendar date:
    classify special session, or require audited reference/opening spot data
    decide once at09:20; record deterministic no-signal reason if absent
    choose contract from the point-in-time catalog
    inspect completed09:20 spot high/low; cancel if target already reached
    require selected09:21 option open and volume; compute adverse fill and lots
    if lots==0: record insufficient_capital; never open a position
    for t=09:21,...,14:59 while open:
        require t spot and selected option candle; require option volume>0
        accumulate the three trigger flags using this completed minute
        if triggered: require t+1 option candle with positive volume;
                      sell at adverse next-open fill; close; break
    if still open: require15:00 selected option candle with volume>0;
                  sell at adverse open; close
    calculate actual fees, net P&L, end equity and daily return
```

- [x] Use one entry per day and only valid complete days for a successful report. For consumed-row data failure write `run_status.json` with status=`invalid`, error, session and position_open; preserve `coverage.csv` and completed trade records plus the unresolved entry in `unresolved_position.json` if necessary; return a nonzero CLI exit via TradingError. Do not write `summary.json` for invalid runs. Create the output directory only if new and perform no overwrite.
- [x] For valid runs write `trades.csv`, `daily_equity.csv`, `coverage.csv`, `summary.json`, `manifest.json`, `run_status.json` status=`valid`. Trade fields include contract identity/expiry/strike/type/lot size, gap, spot decision price, opening failure/target levels, decision/entry/trigger/exit times, raw/fill premiums, lots/quantity, all trigger flags, gross/fees/net, reason. Daily fields include session/status/reason/start equity/end equity/return.
- [x] Summary fields: version, data_kind/provider, explicit start/end, evaluated sessions, regular/special/no-signal/size-skip counts, trades, final equity, net profit, total return, net expectancy, win rate, profit factor (null with no losses), close-to-close daily max drawdown and daily annualized zero-risk-free Sharpe (null with insufficient variance). Label drawdown `daily_close_only`; do not imply intraday or option-low marked drawdown. Include planned risk and full-premium-risk caveats.
- [x] Manifest includes full config, version, normalized input hashes, source hashes, provenance audit assertions, fixed clock conventions, selected contract IDs, cost model and data limitations. Every consumed day file is hashed as streamed; paths/order deterministic and independent of output folder name.
- [x] Add regressions for every exit, simultaneous flags, 14:59 trigger, loss beyond stop from next-open gap, missing entry bar, zero option volume, absent exit while open, missing post-exit bars ignored, no signal, special session and whole-lot skip. Assert no same-bar signal/fill and that a different09:21 spot price cannot alter the09:20 contract choice.
- [x] Replay identical inputs into two new outputs; assert byte-identical CSV ledgers and identical summaries. Reconcile sum(net P&L) with equity change and per-day compounding. Run focused tests and existing `test_backtest.py`; expect all assertions to pass. Commit with `git commit -m "feat: replay options gap-fade sessions with auditable premium ledger"`.

## Task 5: Synthetic demonstration and CLI handoff

**Files:** Create `gapfade/options_demo.py`, `tests/test_options_demo.py`, `tests/test_options_cli.py`; modify `gapfade/__main__.py:14`.

**Interfaces:** `options_demo.create(destination: Path) -> None` writes a new synthetic dataset conforming to Task 2. Extend main(argv=None) with these commands:

```powershell
.\.venv\Scripts\python.exe -m gapfade options-demo --out data/options-demo-01
.\.venv\Scripts\python.exe -m gapfade options-audit --data data/options-demo-01 --start 2026-01-05 --end 2026-01-09 --out runs/options-audit-01
.\.venv\Scripts\python.exe -m gapfade options-backtest --config config/options-research.json --data data/options-demo-01 --start 2026-01-05 --end 2026-01-09 --out runs/options-confirmed-01
.\.venv\Scripts\python.exe -m gapfade options-backtest --config config/options-research.json --data data/options-demo-01 --start 2026-01-05 --end 2026-01-09 --no-confirmation --out runs/options-baseline-01
```

- [x] Write failing CLI tests calling main([...]) under redirected stdout/stderr, plus `unittest.mock.patch('gapfade.__main__.FyersClient', side_effect=AssertionError('unexpected network'))`. Assert no network client is instantiated, valid commands return0, bad inputs return1 and existing stock commands retain their flags.
- [x] Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_options_cli.py -v`; expect argparse to reject the new subcommands.
- [x] Implement the synthetic generator: five regular evaluation sessions05..09January2026; extend synthetic calendar through catalog expiry; include CE and PE, a failed confirmation where only baseline enters, one already-filled/no-signal case and one stop case. Synthetic symbols begin `DEMO:` and must be accepted as opaque offline contract IDs only with synthetic provenance; no production symbol catalog is inferred from them. Generate sufficient zero-volume spot bars and positive-volume selected option bars, date-effective lot metadata and deterministic source hashes. Refuse an existing destination.
- [x] Register the three commands. Parse start/end with date.fromisoformat; use dataclasses.replace(config,confirmation=False) for --no-confirmation. Use OptionsDataset/audit or options_backtest.run; print compact JSON. New commands do not load token files. Keep existing try/except return conventions.
- [x] Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_options_*.py' -v`; expect all options tests to pass. Add assertion that both summaries retain `data_kind=synthetic`, confirmed vs baseline differs only in confirmation config and outputs never overwrite.
- [x] Run the four commands above only during implementation, after verifying the proposed output paths do not exist; if they exist use fresh suffixes consistently. Expected audit status valid structural coverage, both backtests produce valid synthetic ledgers, and baseline adds the intentionally unconfirmed fixture trade. Do not treat synthetic returns as strategy evidence.
- [x] Commit only the four source/test paths with `git commit -m "feat: expose offline options audit demo and backtest commands"`.

## Task 6: Friend-facing documentation and final regression

**Files:** Create `docs/NIFTY_OPTIONS.md`; modify `README.md:7`, `docs/FYERS_INTEGRATION.md:182`; use `examples/options/FORMAT.md` from Task2.

**Interfaces:** Documentation describes the implemented CLI, the frozen strategy version and exact normalized CSV contract; no new runtime API.

- [x] Write the options guide with the embedded specification, explicit09:20/09:21 distinction, examples for CE and PE, all no-trade/invalid statuses, sizing worked example and all four working demo commands. Link FORMAT.md for the friend's FYERS export mapping.
- [x] Update README to state separately: stock paper/live exists; options supports offline historical CSV testing; stock fetch does not download options; options paper/live and authenticated expired-contract ingestion are outside this implementation. Update the F&O section consistently, preserving the current equity live instructions.
- [x] Include a handoff checklist for the friend: audited NIFTY references; calendar including expiry horizon; contract catalog as known historically; date-effective lots; minute-start timestamps; real expired CE/PE prices; source files/hashes; account-specific data access confirmed by the friend. Existing FYERS `download()` remains unsuitable for this schema. Do not assert a specific paid plan or retention based on old documentation.
- [x] Explain the research comparison: exact chronological dates recorded before return inspection; identical data and costs for both variants; report trades/skips/errors and net daily metrics; sensitivity to worse slippage is a separately logged experiment, not a rule selected using the final test. Real-data execution is a subsequent authorized step, not part of this documentation task.
- [x] Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`; expect all existing stock/execution tests and new options tests to pass. Do not quote the old27-test count as current evidence. Run `.\.venv\Scripts\python.exe -m gapfade --help` and `.\.venv\Scripts\python.exe -m gapfade options-backtest --help`; expect the documented flags.
- [x] Run `git diff --check` and `git status --short`; inspect tracked changes for accidental raw prices, credentials or run artifacts. Commit only README.md and the two documentation paths with `git commit -m "docs: explain NIFTY options research and FYERS data handoff"`.

## Acceptance and handoff

- [x] A synthetic CE and PE replay each reconciles premium P&L, fees, integer-lot sizing and equity exactly.
- [x] Spot zero volume is accepted; selected option zero volume is not assumed executable.
- [x] Contract/lot/expiry choices use historical metadata and the supplied exchange calendar, not current-market constants.
- [x] Confirmation-vs-baseline comparison changes exactly one filter at the same entry timing.
- [x] Missing data while a position is open produces an invalid run with unresolved position evidence, never a successful truncated backtest.
- [x] A complete early-exit trade does not require unrelated later bars.
- [x] CLI commands operate entirely offline and preserve stock behavior and live gates.
- [x] Reports distinguish synthetic demonstration, operator-audited historical inputs and unverified execution assumptions.
- [x] Friend can map FYERS exports using one concrete schema and run audit before an authorized real-data test.

Planning completion is not a claim that the option-buying variant has an edge. The next implementation outcome is a testable engine and a clear handoff; real historical performance and eventual paper/live support require separate evidence.


## Implementation record: 4 November 2026

Implemented inline because the execution sub-skills named in the original header are not installed. No agents were spawned. The user's subsequent implementation instruction authorizes the implementation and synthetic checks; it does not authorize broker access or orders.

- Added the six planned options modules, research config, three offline CLI commands and friend-facing documentation. Added `tests/options_fixtures.py` as a shared independent fixture helper.
- Sizing solves the two affine whole-lot budget inequalities directly rather than decrementing potentially millions of lots. It preserves the planned budgets and fee reservations.
- Historical provenance additionally requires frozen chronological `research_splits` and `splits_frozen_at`; evaluated historical dates must belong to those intervals. These fields make the planned experiment-date requirement inspectable before replay.
- Full regression command: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`; 62 tests passed, including existing equity/execution tests and 35 new options tests.
- All four documented CLI demonstration commands completed using `data/options-demo-01` and `runs/options-{audit,confirmed,baseline}-01`. The confirmed fixture has three trades and baseline four. Results remain explicitly synthetic.
- Missing holding/exit data retains unresolved-position evidence and completed earlier trades, suppresses performance summaries and returns an error. Selected-contract gaps never cause a switch to another strike.
- Source/config/tests/docs are committed in the task sequence. Synthetic data, run outputs and test logs remain ignored.
- Not performed: authenticated FYERS requests, real historical-data tests, paper sessions with market data, live orders, pushes or external submissions.

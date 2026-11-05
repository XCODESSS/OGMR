# FYERS Downloaded Data Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Determine whether the downloaded expired NIFTY option candles and matching spot data are fit for the existing strategy, with reproducible local evidence and explicit readiness blockers.

**Architecture:** Add a strictly offline audit that reads the original JSON and exported CSVs, checks row integrity and cross-export agreement, and writes diagnostics to a fresh directory. Stage only traceable structurally usable bars; keep metadata and strategy-readiness gates separate. Do not modify the existing trading strategy or backtesting engine.

**Tech Stack:** Python >=3.11, standard library only; Decimal, datetime, csv, json, hashlib, pathlib and unittest. PowerShell on Windows.

**Spec:** The user's correction in this conversation: validate the downloaded data, undo the unintended walk-forward implementation, then make a plan. Existing requirements are documented in `examples/options/FORMAT.md`, `docs/NIFTY_OPTIONS.md` and `docs/FYERS_EXPIRED_SAMPLE.md`. There is no separate validation specification; the scope and checks below supply it.

## Global Constraints

- Planning was completed first; the user subsequently authorized implementation. Execute only the local data audit, never strategy performance analysis.
- Execution must consume zero FYERS/API requests, make no network connections, read no credentials and submit no orders.
- No new backtester or walk-forward runner; preserve the existing committed options engine and strategy.
- Never overwrite or delete raw sources. Do not forward-fill candles, interpolate prices, infer zero-volume execution, shift timestamps to force a market-hours match, or silently select one conflicting observation.
- Keep secrets, market data and run artifacts out of Git. Do not commit or push without a new request.
- Existing access-sample selection is not the strategy's point-in-time contract selection. Do not use the two downloaded contracts as a substitute for all eligible historical contracts.
- Clearly distinguish structural validity, provisional session coverage, source agreement and historical strategy readiness. A successful download or consistent hash cannot prove market-price accuracy.
- Preserve `OptionsDataset` verification gates. Do not set calendar/reference/catalog verification flags to true without documented evidence.

## Verified source inventory and known limitations

These observations come from the existing files/manifests and prior inspections, not from the comprehensive audit proposed here.

1. `data/fyers-expired-sample-20261105-03/` contains `manifest.json`, `expiry_dates.json`, `contracts.json`, `spot_raw.json`, `spot.csv`, `ce_raw.json`, `ce.csv`, `pe_raw.json`, and `pe.csv`.
2. `data/fyers-expired-history-20261105-01/` contains `manifest.json`, `ce_raw.json`, `ce.csv`, `pe_raw.json`, and `pe.csv`.
3. The contracts are `NSE:NIFTY26N0323250CE` and `NSE:NIFTY26N0323250PE`, expiry `2026-11-03`, strike 23250. The returned catalog identifies symbols, but does not establish historical lot sizes or original listing timestamps.
4. The sample contains 385 candles per option for November 2. The longer export reports 10,146 CE rows over 27 observed dates and 8,227 PE rows over 22 observed dates. These are overlapping exports: do not add the sample counts to the history counts as unique observations.
5. Some option candles extend to 15:39 IST. Their interpretation is unresolved. The available spot export covers only a few sessions, not the whole September-November option interval.
6. The original sample manifest contains file hashes. The longer export manifest does not supply an original hash baseline; hashing it now establishes a new audit snapshot only.
7. `data/fyers-expired-sample-20261105-01/` and `-02/` are failed/incomplete attempts. Inventory their status, but exclude them from candidate data; do not merge them or claim they are successful downloads.
8. No fully audited `sessions.csv`, `references.csv`, `contracts.csv` or research provenance package has been established for these raw downloads.

## File map and responsibilities

- Create `gapfade/fyers_data_audit.py`: offline source loading, candle validation, reconciliation, coverage and readiness assessment. Reuse Decimal/IST rules from `gapfade/model.py`; do not import the FYERS client or call the replay engine.
- Create `scripts/audit_fyers_data.py`: command-line wrapper with `--sample`, `--history`, and `--out` arguments only. No authentication or fetch flags.
- Create `tests/test_fyers_data_audit.py`: small hand-authored source fixtures and tests of corruption, mismatches and readiness failures.
- Modify `docs/FYERS_EXPIRED_SAMPLE.md`: document audit invocation and clarify access success versus verified data.
- Write generated artifacts only below a fresh `runs/fyers-data-validation-01/` directory, which is already ignored by Git.
- Leave `gapfade/options_backtest.py`, `gapfade/options_strategy.py`, `gapfade/options_data.py`, the existing CLI and all trading configurations unchanged.

## Output contract

The audit produces:

```text
runs/fyers-data-validation-01/
  audit_manifest.json
  run_status.json
  validation.json
  VALIDATION.md
  source_inventory.csv
  candle_issues.csv
  overlap_checks.csv
  daily_coverage.csv
  missing_minutes.csv
  readiness_gaps.csv
  quarantine.csv
  staging/
    BACKTEST_BLOCKED.txt
    spot/YYYY-MM-DD.csv
    options/YYYY-MM-DD.csv
```

- `audit_manifest.json`: source-relative names, SHA-256, file sizes, existing-hash verification results, source/output row counts, audit version and assumptions. Hash inputs before and after reading; abort on a change.
- `validation.json`: separate `structural_status`, `coverage_status`, `metadata_status` and `backtest_ready`; include counts and findings with `code`, `severity`, `evidence`, `impact`, `remediation`.
- CSV issue records include `source`, `symbol`, `row_number`, `timestamp`, `issue_code` and `detail`. Unknown timestamps stay empty; retain the original row as JSON in quarantine details.
- `daily_coverage.csv` grain: `(source, symbol, observed_session)`. Include observed rows, in-window distinct minutes, missing template minutes, zero-volume minutes, out-of-window rows and matching spot-minute count. Denominators must be explicit.
- `overlap_checks.csv` grain: `(symbol, timestamp)` where at least two exports provide the same candle. Report identical or conflicting values, with both source references.
- `readiness_gaps.csv`: exact local deficiencies: missing spot dates/minutes, absent metadata and unresolved conventions. Do not produce speculative API-call estimates or initiate downloads in this task.
- No `summary.json`, profit metrics or trade ledger is produced by this data audit.

### Task 1: Establish immutable source evidence

**Files:** Create `gapfade/fyers_data_audit.py` and `tests/test_fyers_data_audit.py`.

**Interfaces:** `inventory_sources(sample_root: Path, history_root: Path) -> list[dict]`; each record contains source identity, relative path, SHA-256, size, baseline status and incomplete-download status. Missing expected successful input files are blocking errors.

- [x] Write a test with a small source file and an advertised wrong SHA-256; assert `source_hash_mismatch`, not a passing integrity claim.
- [x] Write cases for a missing advertised source, an `INCOMPLETE.txt` marker, a source escaping its root and an export with no original hash baseline.
- [x] Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_fyers_data_audit.py -v`; confirm failure before implementation.
- [x] Implement path containment with `resolve()`/`is_relative_to()`. Restrict reads to the expected data filenames; never recursively scan `.secrets`, `.env` or the repository for account details.
- [x] Use chunked hashing, with this intended implementation:

```python
def sha256_file(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()
```

- [x] Verify sample manifest hashes individually; label the longer export `baseline_not_supplied` rather than claiming historical integrity. Record a new audit-time hash for every consumed file.
- [x] Re-run focused tests; require pass. No commits.

### Task 2: Validate raw candles and CSV conversion

**Files:** Extend `gapfade/fyers_data_audit.py` and tests.

**Interfaces:** `profile_candles(rows: list, symbol: str, kind: str, source: str) -> dict`, with `kind` exactly `spot` or `option`. Return `counts`, `valid_rows` and `issues`; rows remain linked to original row numbers. `load_capture(path: Path, descriptor: dict) -> dict` checks JSON response/schema and calls the profiler. Resolve symbol/kind from the explicit manifest descriptor, not from guessed strike/date substrings.

- [x] Add fixtures for duplicate timestamps, unsorted input, invalid row widths, bool/string/non-integral epoch values, nulls, NaN/infinity, negative volume, zero/negative OHLC, invalid high/low bounds, and timestamps with nonzero seconds.
- [x] Add a zero-volume spot fixture that remains valid, and a zero-volume option fixture that receives a non-executable finding without invented volume.
- [x] Map option rows by the returned `columns`; the ordinary spot response has no `columns`, so use its documented six-field core order. Reject missing/duplicate required columns and mismatched row widths. Check the response symbol against manifest metadata where provided.
- [x] Apply the existing price invariants explicitly:

```python
prices = [Decimal(str(value)) for value in (opening, high, low, close)]
valid_prices = all(value.is_finite() and value > 0 for value in prices)
valid_bounds = low <= min(opening, close) <= max(opening, close) <= high
```

- [x] Convert Unix seconds directly to offset-aware IST; require minute-start alignment. Label minute-start as an assumed provider convention until independently established, even when timestamps align.
- [x] Read every exported CSV row and reconcile numeric values and timestamps against raw JSON at `(symbol, epoch)`. Check `timestamp_ist` against its epoch field. Record unequal multiplicities as well as unequal values; do not use a dictionary that silently removes duplicates.
- [x] Re-run focused tests; require concrete issue codes and source row references for every fixture.

### Task 3: Reconcile overlapping exports and inspect coverage

**Files:** Extend the audit module and tests.

**Interfaces:** `reconcile_captures(captures: list[dict]) -> dict` returns unique accepted observations, conflicts, duplicate lineage and coverage tables. Conflicts exclude the affected observation from staging and block readiness.

- [x] Write one identical sample/history overlap and one changed close; assert identical rows coalesce with both source references while conflicting observations retain both values and cannot be silently accepted.
- [x] Profile each source separately first. Then calculate unique merged counts; do not count November 2's sample observations twice.
- [x] Construct a provisional regular-session template only for observed dates:

```python
expected = {datetime.combine(day, time(9, 15), IST) + timedelta(minutes=i)
            for i in range(375)}
missing = sorted(expected - observed_minute_starts)
outside = sorted(observed_minute_starts - expected)
```

- [x] Label the 09:15-15:29 minute-start template an explicit regular-session assumption, not an exchange-calendar certification. Do not use weekdays as proof of trading sessions; missing full dates remain `calendar_unverified` without official calendar evidence.
- [x] Enumerate missing and out-of-window minutes for every observed option and spot date. Investigate the 15:30-15:39 rows: compare OHLC/volume with the last regular minute, identify repeated values, and retain all raw observations. Do not assert a timezone error or delete these rows from source data.
- [x] Join option and spot timestamps without many-to-many expansion. Enumerate option dates with no spot data, and missing 09:15-09:20 spot windows where a spot date exists. Record missing option 09:21 and 15:00 bars as availability diagnostics, not proof that a hypothetical strategy would require every minute on every day.
- [x] Report the difference between sparse/nontrading candles and proven corruption. Missing option minutes are unresolved executability gaps until contract selection and the actual required execution path are known; never forward-fill them.
- [x] Re-run focused tests, including a fixture where an entire spot day is absent and another where only one early spot minute is absent.

### Task 4: Produce staged bars and a truthful readiness decision

**Files:** Extend audit module/tests; create `scripts/audit_fyers_data.py`; update `docs/FYERS_EXPIRED_SAMPLE.md`.

**Interfaces:** `audit_sources(sample_root: Path, history_root: Path, output: Path) -> dict`. Require a new output directory; write progress/failure diagnostics on errors. The wrapper returns 0 only when `backtest_ready=true`, 2 when the audit completed with readiness blockers, and 1 for fatal malformed/missing sources or execution failure. A blocked status is an assessment result, not a successful backtest certification.

- [x] Test that missing calendar, references, lot sizes and listing evidence result in `backtest_ready=false`, even if every candle is structurally valid.
- [x] Check catalog membership against the returned November 3 catalog. Establish only symbol membership for that queried expiry; do not interpret the returned list as a complete point-in-time catalog for every prior session.
- [x] Enumerate these independent blockers: official exchange calendar through expiry; verified official open/prior-close references and availability before 09:20; date-effective lot sizes; original listings and metadata validity; historical eligible strikes/expiries; timestamp convention; full required spot/selected-contract coverage; frozen research splits before results inspection.
- [x] Normalize only structurally valid, non-conflicting observations inside the provisional regular-minute template into session-partitioned staging CSVs with `timestamp,symbol,open,high,low,close,volume`. Normalize spot symbol to `NIFTY`; retain exact option IDs. Count zero-volume option rows explicitly as non-executable.
- [x] Preserve excluded/out-of-template/conflicting rows in `quarantine.csv`, including source lineage and reasons. Reconcile accepted/coalesced/quarantined counts to source counts. Stage only under the new output tree, never the raw folders.
- [x] Always leave `staging/BACKTEST_BLOCKED.txt` while readiness is false. Do not generate fake `sessions.csv`, `references.csv`, `contracts.csv` or verified provenance. The staging directory must fail the existing `OptionsDataset` loader; add a test for that refusal.
- [x] Patch `socket.create_connection` to raise during the audit test. Assert no API client is instantiated, no credentials are opened, and the before/after raw hashes match.
- [x] Write `VALIDATION.md` with verdict, confirmed checks, exact blocker evidence, quarantined-row policy, assumptions and the smallest remaining evidence needed. Do not call a provisional staged dataset fully cleaned or validated for performance.

### Task 5: Execute the local audit and deliver findings

**Files:** Generated artifacts under `runs/fyers-data-validation-01/`; no raw edits.

- [x] Run the focused tests and then the existing full test suite once. Expected: all tests pass, no network access in audit tests, original strategy behavior unchanged.
- [x] Execute this exact command after implementation, using a fresh output name if the directory already exists:

```powershell
.\.venv\Scripts\python.exe scripts\audit_fyers_data.py --sample data/fyers-expired-sample-20261105-03 --history data/fyers-expired-history-20261105-01 --out runs/fyers-data-validation-01
```

- [x] Inspect all report files, reconcile unique totals and overlap counts, and verify source hashes again. Given the known missing metadata, expect exit code 2 with a completed blocked assessment, not an executable historical dataset.
- [x] Independently spot-check one regular candle, one post-market candle and one overlap using raw JSON and its CSV representation. Record checks in the report.
- [x] Run `git diff --check`. Confirm only audit implementation/tests/docs changed; data, credentials and artifacts remain ignored. Do not commit or push.
- [x] Deliver a short verdict with links to the report and evidence tables. Clearly state what passed locally, what failed and which missing evidence blocks the next step. Stop before any backtest or further download.

## Completion criteria

The complete existing successful downloads have been profiled locally; raw-to-CSV and export-to-export differences are explicit; coverage and metadata gaps are enumerated; source data is unchanged; staged rows retain lineage and quarantine evidence; no verification flag, candle, lot size or listing history is fabricated; no allowance is consumed. The task can complete with `backtest_ready=false` when the evidence supports that verdict.

## Planning handoff

At the original planning handoff, the audit implementation and real-data assessment had not been executed. Implementation was subsequently authorized and completed as recorded below. The unintended walk-forward additions and generated demonstrations were moved to the ignored `research/reverted-walkforward-20261105/` archive, and the three tracked files they changed were restored to the committed version. The existing FYERS downloader and raw data remain available.

## Execution record - 5 November 2026

Implemented inline without delegation. Eleven focused data-audit tests and the full 77-test suite passed. The completed assessment is in `runs/fyers-data-validation-02/`; exit code 2 correctly indicates completed validation with readiness blockers. The first exploratory assessment remains in `runs/fyers-data-validation-01/`. All 14 consumed file hashes were independently rechecked and remained unchanged; three additional raw/CSV/overlap spot checks passed. No broker requests, credential reads, orders, strategy performance runs, commits or pushes were made.

Structural checks passed for 21,018 source observations, with 770 identical cross-export overlap keys and no conflicts. The audit staged 19,758 unique regular-window observations with lineage and quarantined 510 source observations representing 490 unique out-of-window observations. Zero volume occurred in 10,573 of 17,883 staged option observations. Spot data is absent on 22 observed option dates; 492 missing minute starts were found against the provisional regular-session template across observed source/symbol dates. Metadata and historical strategy readiness remain blocked.

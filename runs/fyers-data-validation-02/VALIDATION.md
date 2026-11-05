# FYERS local data validation

**Verdict: not ready for strategy backtesting.**

Structural checks: pass. Source files remained unchanged.

## Source profiles

| Source | Rows | Dates | Invalid rows |
|---|---:|---:|---:|
| sample/ce_raw.json | 385 | 1 | 0 |
| sample/pe_raw.json | 385 | 1 | 0 |
| history/ce_raw.json | 10146 | 27 | 0 |
| history/pe_raw.json | 8227 | 22 | 0 |
| sample/spot_raw.json | 1875 | 5 | 0 |

## Reconciliation

21018 source observations = 19758 unique staged observations + 750 coalesced staged duplicates + 510 quarantined source observations.
Identical overlapping keys: 770; conflicting keys: 0.

## Candle and execution-quality findings

Invalid source rows: 0. Within-export duplicate rows: 0. Raw/CSV mismatches: 0.
Regular-window unique option observations: 17883. Of these, 10573 have zero volume and 7310 have positive volume. Zero-volume observations cannot be used as executable fills; positive volume alone does not prove an attainable fill.
Missing minute starts across observed source/symbol dates: 492 against the provisional 375-minute template. Missing whole trading dates cannot be certified without an official calendar. Sparse option minutes are not automatically corruption.
Post-market source observations identical in all OHLCV fields to the 15:29 candle: 199; see the per-source/date counts in daily_coverage.csv. This pattern does not establish the cause of all out-of-window rows.

## Readiness blockers

- calendar_unverified: Official exchange sessions and special-session status through expiry are not supplied.
- references_unverified: Official NIFTY open/prior-close and availability before 09:20 are not supplied.
- lot_sizes_missing: Date-effective historical lot sizes are not supplied.
- listing_history_missing: Original listing timestamps and metadata validity intervals are not supplied.
- eligible_catalog_unverified: One expiry catalog does not establish all eligible historical strikes and expiries.
- timestamp_convention_unverified: Minute-start convention is assumed, not independently verified.
- execution_coverage_unverified: Required selected-contract execution paths have not been established; no trades simulated.
- research_splits_missing: Chronological frozen research intervals are not supplied.

Observed option dates without matching spot: 22.
Unique out-of-template observations: 490; see quarantine.csv and daily_coverage.csv.

## Interpretation and next evidence

Only structurally valid, non-conflicting observations inside the provisional market-hours template were staged. Raw exports were not modified. Out-of-template rows were retained in quarantine, not timestamp-shifted. Zero-volume option candles, when present, remain non-executable observations.
The sample manifest hashes can verify export identity; the longer export has no original hash baseline. New audit hashes do not retroactively establish original integrity. Overlap agreement is between exports from the same provider.
Supply an official calendar, historical listing/lot-size evidence, verified spot references and historical eligible contracts. Resolve the post-market timestamp convention and obtain missing required spot/contract observations before performance analysis. No network requests were made.

## Assumptions

- 09:15-15:29 IST, 375 minute starts per observed regular session; calendar unverified.
- Sparse option minutes are unavailable observations, not proven provider corruption.
- Same-provider agreement is not independent verification of price accuracy.
- No backtest or profitability analysis was run.

## Independent spot checks

A separate raw-JSON/CSV calculation confirmed a regular CE candle, a post-market CE candle, and a sample/history CE overlap. All matched. All 14 consumed file hashes were independently rechecked after report generation. Details: `spot_checks.json`. These checks confirm export agreement, not independent exchange-price accuracy.

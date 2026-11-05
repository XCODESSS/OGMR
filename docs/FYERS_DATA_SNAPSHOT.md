# Audited FYERS data snapshot

The downloaded exports and completed offline audit are committed at these paths:

- [Sample exports](../data/fyers-expired-sample-20261105-03/): spot data, CE/PE candles and contract metadata.
- [Longer option exports](../data/fyers-expired-history-20261105-01/): CE/PE candles and download manifest.
- [Audit report](../runs/fyers-data-validation-02/VALIDATION.md): findings, assumptions and readiness blockers.
- [Staged observations](../runs/fyers-data-validation-02/staging/): provisional session-filtered observations; not complete backtest inputs.

**Structural checks passed; the dataset is not ready for strategy backtesting.**
Historical calendar, lot sizes, contract eligibility, spot references, timestamp
convention and execution coverage remain unresolved. Consult the report before use.
No strategy performance is established by this snapshot.

Raw exports remain unchanged. The audit includes hashes, lineage, quarantined rows,
coverage findings and independent export spot checks. Paths recorded in the audit
refer to the original local workspace; the corresponding repository paths above
contain the source files. Git attributes preserve exact bytes for hash checks.

Only these specific data and audit directories were added. Other data, runs and
credentials remain ignored.

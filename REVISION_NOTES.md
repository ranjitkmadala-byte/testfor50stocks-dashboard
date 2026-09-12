# v2.9 revision — same-strike OI scoring + backtest

- Adds same-strike OI contribution to Early Detector scoring, capped at 1.5 points per direction.
- Persists signal, contribution, persistence, strike, unwind %, and opposite-side OI % in public.early_detector_snapshots.
- Shows these fields on the live state/conviction board.
- Adds a five-trading-day backtest expander with target-before-stop and cost-adjusted output.
- Default research assumptions used in that backtest: target +0.50%, stop -0.30%, round-trip cost 10 bps.
- Historical results require stored same-strike baseline/pair fields. Pre-revision sessions cannot be reconstructed from the old six-row OTM basket alone.

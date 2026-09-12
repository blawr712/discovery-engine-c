# Architecture

Universe Engine
↓
Pre-Filter Engine
↓
Data Engine
↓
Scoring Engine
↓
Research Engine
↓
Reporting Engine

## Responsibilities

Universe Engine: Build and maintain investable universe.

Pre-Filter Engine: Remove companies that do not meet baseline requirements.

Data Engine: Retrieve market and metadata through provider-agnostic sources.
Persistent caching decorates a source and can be enabled or tuned without
changing scoring or provider implementations.
The orchestration engine collects metadata and price histories in separate,
bounded-concurrency phases so filtering occurs before expensive price calls.
Transient provider failures are retried below the cache layer with bounded
exponential backoff and jitter; permanent failures pass directly to the engine.
Uncached provider calls pass through shared pacing. A rate-limit response
creates a global cooldown so concurrent workers cannot amplify throttling.
Repeated rate limits open a per-process circuit breaker, preserving unresolved
rows for a later resume instead of holding thousands of queued requests open.
Run State stores an input fingerprint, manifest, and atomic per-company
checkpoints so compatible interrupted runs can resume safely.
Completed runs containing provider errors remain resumable; successful and
analytically rejected rows are reused while only error rows run again.

Scoring Engine: Calculate Discovery Score only. A shadow Score v2 runs beside
it in two pure stages: per-company point-in-time raw signals computed with the
price history, then one cross-sectional percentile pass over the completed run
that ranks each signal across candidates (within sector when the group is
large enough) and blends them into a 0-100 score with explanations. Score v2
never alters official scores or ranks, is written back to checkpoints so saved
runs stay authoritative, and reuses the same two functions inside the Backtest
Engine so historical and live behavior cannot diverge.

Research Engine: Generate explainable research summaries.

Backtest Engine: Replay the Scoring Engine's technical factor functions at
historical month-ends using only price data available on each date, then
measure rank correlation, quantile spreads, and top-N outcomes against forward
benchmark-relative returns. It reuses the scoring functions rather than
re-implementing them, reads its universe from a completed saved run, collects
long price histories through the same cached, paced, retried provider stack,
and never changes official scores or ranks. Its configuration lives in
`config/backtest.json`, outside the run fingerprint.

Reporting Engine: Export reports and serve the local dashboard. Moonshot
terminal queries read deterministic analysis, calibration, and immutable
baseline artifacts through a separate bounded store; they never initialize the
market-data or AI layers.

## Configuration

Runtime configuration belongs in:
- config/settings.json
- config/strategy.json
- config/backtest.json (offline validation only; excluded from run fingerprints)

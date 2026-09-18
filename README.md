# Discovery Engine

> Discover exceptional companies before they become widely recognized.

Discovery Engine is a modular Python platform that analyzes the North American equity universe and produces a transparent, explainable **Discovery Score** to prioritize research opportunities.

## Current Status

**Version:** v0.4 Platform — Sprint 4 complete

The project now applies an explainable metadata pre-filter, persistent cache,
bounded concurrent collection, transient retry/backoff, and resumable runs.
Provider calls are globally paced, and partial runs can retry unresolved errors.
The release was validated across all 6,424 configured North American listings
with zero provider or pipeline errors.

v0.3 introduced structural asset classification, explainable factor results,
score confidence, enhanced financial signals, and research reporting.
Fundamental factors currently run in shadow mode so their coverage and score
distributions can be validated before ranking weights change. Valuation and
risk factors follow explicit data-quality rules: stale and invalid inputs do
not score, while usable undated inputs reduce confidence.

The v0.3 release is documented in `V0.3_RELEASE.md`. v0.4 begins with an
offline SQLite history index while retaining completed run files as the source
of truth:

```powershell
python main.py --index-run RUN_ID
python main.py --history-summary
```

Every run also produces a shadow **Score v2** beside the official Discovery
Score. Each successful company receives point-in-time raw signals from its own
price history (long and medium momentum with the latest month skipped,
proximity to the trailing high, annualized volatility, volume trend, and
one-month reversal). After collection, one cross-sectional pass ranks each
signal as a percentile across the run's candidates, within sector when at
least the configured group size is present, and blends the percentiles by the
weights in `config/strategy.json` under `scoring_v2`. Rows carry `score_v2`,
`score_v2_rank`, `score_v2_confidence`, and a per-signal `score_v2_breakdown`.
Score v2 changes no official score or rank; it exists to be validated by the
backtest before any weighting decision.

A second shadow model, **Score v3**, blends point-in-time SEC fundamentals
with a price tilt using the same cross-sectional machinery and the weights in
`strategy.json` under `scoring_v3`. It first applies percentile-based
exclusion filters (bottom-quintile operating margin, top-quintile share
dilution, top-quintile revenue growth), then ranks the remainder on sales
yield, net cash to market cap, free-cash-flow yield and margin, operating
margin, low dilution, trailing-high proximity, momentum, and low volatility.
Candidates below the configured minimum confidence, which in practice means
names without SEC fundamentals, receive no v3 score. Rows carry `score_v3`,
`score_v3_rank`, `score_v3_confidence`, `score_v3_excluded`,
`score_v3_exclusion_reasons`, and a per-signal `score_v3_breakdown`.

When `SEC_USER_AGENT` is set (see `.env.example`), live runs collect SEC
company facts for U.S. filers with per-company failure isolation and record a
`fundamentals_status` on each row; set `fundamentals.live_collection_enabled`
to `false` in `config/settings.json` to turn this off.

Measure whether the configured technical screen has historically related to
forward returns by replaying it point-in-time for a completed run's universe:

```powershell
python main.py --backtest-run RUN_ID
python main.py --backtest-run RUN_ID --backtest-limit 200
```

The backtest collects ten-year adjusted price histories through the normal
cached, paced provider stack (cached for a week under `config/backtest.json`),
rebuilds the volume, relative-strength, trend, and liquidity factors plus the
Score v2 signals at every historical month-end using only data available on
that date, scores each month's cross-section with the same v2 pass, and measures
Spearman rank correlation, score-quantile spreads, top-N excess returns, hit
rates, turnover, and compounded top-N portfolios against SPY or XIU.TO at 1M,
3M, 6M, and 1Y horizons. It writes `backtest_*.json`, `backtest_periods_*.csv`,
`backtest_observations_*.csv.gz`, and `backtest_summary_*.md` under
`data/exports/` and records provenance in the run manifest. Official scores and
ranks never change. The artifacts state their limitations explicitly: the
universe is survivorship-biased toward companies still listed, market-cap and
sector points are static, and no result is a return forecast.

Add point-in-time fundamentals for U.S. filers to the same backtest. The SEC
requires a contact `User-Agent`; put it in an untracked `.env` file (see
`.env.example`) or set the environment variable directly:

```powershell
python main.py --backtest-run RUN_ID --with-fundamentals
```

This pulls SEC XBRL company facts once per filer (paced below the SEC limit
and cached for a week as a compact extract of only the needed concepts), then
derives revenue growth and acceleration, gross and operating margin levels and
trends, cash-flow margins and conversion, net cash, one-year share dilution,
and market-cap ratios at every month-end from filings made on or before that
date. Restatements become visible only from their restating filing. The
resulting `pit_*` signals join the rank-correlation tables so fundamentals can
be judged on the same periods as the technical signals. Canadian and IFRS
filers carry no fundamental signals yet.

Compare two previously indexed runs without making provider calls:

```powershell
python main.py --compare-runs OLDER_RUN_ID NEWER_RUN_ID
```

The comparison writes JSON and CSV reports under `data/exports/`, including
entrants, exits, status transitions, score changes, candidate rank movement,
confidence changes, and fundamental data-quality changes.
The JSON report also includes a status-transition matrix and country/sector
composition deltas.

Export one ticker's complete indexed timeline:

```powershell
python main.py --ticker-history TICKER
```

Build a Markdown weekly briefing from the latest complete runs with matching
universe sizes:

```powershell
python main.py --weekly-report
```

Weekly reports explicitly warn when run fingerprints differ, because rank and
score movements may then include configuration changes as well as market data.

Launch the local history dashboard:

```powershell
python main.py --dashboard
```

Then open `http://127.0.0.1:8765`. Use `--dashboard-port PORT` to choose a
different loopback port. The dashboard is self-contained, makes no provider
calls, and opens the history database with SQLite write access disabled. Only
explicit watchlist actions write local workflow state, in a separate JSON file.

Candidate details explain how each stored score was constructed. Discovery and
Fundamental Scores receive within-run percentile descriptors, while technical
and fundamental confidence are described separately as data coverage. Factor
points, maximums, source explanations, data quality, and status reasons remain
visible; none of these fields are presented as a return forecast or recommendation.

Re-indexing a completed run now attaches eligible cached one-year adjusted-price
snapshots for successful candidates and their country benchmarks. Cache files
newer than the run are rejected. Candidate details display 1W, 1M, 3M, 6M, and
1Y percentage changes plus interactive normalized ticker-versus-benchmark charts,
all from the read-only local history database. The dashboard uses dark mode by
default.

The dashboard presents each candidate as a plain-language Score Story: its rank
within the successful-candidate pool, distance from the run median, strongest
technical drivers, largest unused factor capacity, and separate technical and
fundamental coverage. These descriptions explain the configured screen; they do
not predict returns or make investment recommendations.

Charts default to adjusted candlesticks when OHLCV is available and otherwise
use the stored adjusted closing price. `Vs benchmark` rebases the stock and its
country benchmark to 0% at the selected period's start. U.S.
equities use SPY (the S&P 500 ETF) and Canadian equities use XIU.TO (the
S&P/TSX 60 ETF). “Vs benchmark” is the stock return minus the benchmark return
in percentage points. Possible split or corporate-action discontinuities are
flagged, and returns crossing them are hidden rather than reported as reliable.

New market-data collections retain reported split and dividend events. When a
declared split explains a discontinuity, the dashboard adjusts pre-event prices
and volume into a consistent basis; already-adjusted histories are left alone.
Charts label their price-data quality as clean, verified, verified adjusted, or
unresolved, and the dashboard reports snapshot coverage for the latest run.

The candidate chart workspace also provides YTD performance, latest-price and
volume context, line and candlestick views, linear and logarithmic price axes,
20/50/200-session simple moving averages, a 20-session exponential moving
average, 20-session Bollinger Bands, zoom, full OHLCV crosshair values, and
split/dividend markers. Actual adjusted prices remain separate from the
benchmark-relative percentage view.

Completed runs indexed before OHLCV support remain usable as close-only line
charts. Re-run `python main.py --index-run RUN_ID` to replace that run's
compressed price snapshots with provenance-compatible OHLCV from its existing
cache files; this remains an offline indexing operation and never refreshes
market data.

The dashboard candidate-comparison workspace accepts two to five comma-separated
tickers from the selected run. It aligns ranks, scores, percentile descriptors,
confidence, price quality, available returns, normalized price histories, and
technical/fundamental factor strength without producing a recommendation.

Named dashboard watchlists persist in `data/history/watchlists.json`, separate
from the read-only history database. A tracked ticker can be added directly or
from the candidate explorer, then reviewed against the immediately preceding
indexed run for status, rank, and Discovery Score changes. Watchlists are local
workflow state and never trigger market-data or AI requests.

The dashboard also includes a read-only Moonshot Research Terminal for runs
with saved Moonshot artifacts. It separates Priority Research, Asymmetric
Watch, Speculative Watch, and control cohorts; shows Upside Potential beside
Risk of Ruin; and explains factor evidence, warnings, and model sensitivity.
Calibration gates, scenario overlap, country/size/sector selection rates, and
the immutable forward baseline remain visible in the same run context. Fixed
1M, 3M, 6M, and 1Y outcomes stay labeled pending until their eligibility dates.
Changing a dashboard filter never makes a market-data or AI request.

## Core Principles

- Discovery over prediction
- Transparency over black boxes
- Configuration over hardcoding
- Modular architecture
- Explainable scoring

## Running

Run the complete configured universe:

```powershell
python main.py
```

Refresh the Canadian universe by placing the latest TMX workbook at
`data/raw/canada/tsx-and-tsxv-listed-companies.xlsx` and running:

```powershell
python -m src.build_canadian_universe
```

Older workbooks may be retained under `data/raw/canada/archive`; only `.xlsx`
files directly inside the Canadian input directory are considered. Dated TMX
metric headers and their source date are detected from the current workbook.

Run a controlled smoke test by ticker or universe size:

```powershell
python main.py --tickers DCBO.TO WELL.TO SOFI IONQ RKLB
python main.py --limit 25
python main.py --balanced-sample 200
```

Each completed run exports a full diagnostic report, a curated top-candidate
report, and JSON coverage analytics. Balanced samples deterministically
interleave Canadian and U.S. listings for representative validation.

Runs also export an officially ordered calibration CSV and a JSON calibration
summary. These include relative percentiles, factor distributions, rank
correlation, top-list overlap, outliers, low-confidence flags, and experimental
blend scenarios. Experimental ranks never change the official candidate order.

Calibration treats extreme inputs through configurable cap/invalid rules,
removes sector-inappropriate factors from the applicable scoring denominator,
and requires sufficient fundamental confidence before a company enters a
blended scenario. Original provider values remain visible for audit.

Coverage-neutral calibration selects only factors with adequate data in both
Canada and the United States. Technical scoring defines the candidate pool;
fundamentals compare companies with suitable peers and can reorder only within
bounded technical bands. Configurable gates reject scenarios that create
country bias, excessive movement, or insufficient top-list retention.

Completed runs can be recalibrated without Yahoo or any other provider call:

```powershell
python main.py --recalibrate-run RUN_ID
```

The command rebuilds calibration artifacts, exports decision-ready research
queues for every passing weighted scenario, creates a combined top-25 review,
summarizes country and sector composition, and records provenance in the
original run manifest. It fails safely when a run is incomplete or its
checkpoints cannot be reconstructed losslessly.

Build a separate Moonshot Discovery research lane from any completed run:

```powershell
python main.py --moonshot-run RUN_ID
```

Moonshot Discovery evaluates operating equities between $2 million and
$50 million in market capitalization. It produces independent Upside Potential
and Risk of Ruin scores, confidence and missing-input diagnostics, nano-cap and
micro-cap labels, and review classifications. The command is entirely offline:
it reconstructs saved run results, makes no provider or AI calls, and cannot
change the official Discovery Score, candidate rank, or report.

The initial shadow model uses available growth, profitability, cash generation,
sales valuation, size, cash runway, balance-sheet, leverage, and disclosure
signals. Increment 2 adds 30-session average dollar volume, annualized realized
volatility, one-year maximum drawdown, reported share-count dilution, and
reverse-split history. Candidates cannot receive a watch or priority label until
market-risk coverage passes its configured gate. Moonshot scores organize
speculative research; they are not return forecasts, price targets, or investment
recommendations.

The offline command automatically reuses price caches that are no newer than the
completed source run. To explicitly collect missing price and 18-month share
history from Yahoo for a controlled smoke set:

```powershell
python main.py --moonshot-run RUN_ID --collect-market-risk --moonshot-limit 10
```

Omit `--moonshot-limit` to collect the complete Moonshot universe. This is an
explicit network action and uses the same persistent cache, pacing, retry,
cooldown, circuit-breaker, and per-company failure isolation as the main engine.
Collected evidence is saved separately and reused by later offline Moonshot
runs. It never changes the official Discovery Score or candidate ordering.
Bounded collections are intended for smoke testing and may mix capture dates;
the analysis reports whether coverage and capture timing are sufficient for a
cross-sectionally comparable universe. Use a full collection before comparing
Moonshot ranks as one synchronized research cohort.
The comparability gate requires at least 99% synchronized evidence by default
and always lists the residual excluded tickers and reasons.

Stress-test a comparable Moonshot cohort entirely offline and freeze it for
forward validation:

```powershell
python main.py --calibrate-moonshot RUN_ID
```

The command compares the baseline model with growth-emphasis,
survival-first, and trading-viability scenarios; reports top-list overlap,
rank sensitivity, factor distributions, outliers, and country, size, and
sector selection rates; and exports plain-language driver and risk summaries
for selected candidates. Configurable review gates identify material cohort
selection-rate differences without imposing country quotas or changing any
rank. A failed review gate produces `needs_review`; a data-integrity failure
produces `fail`.

For a synchronized cohort, the same command creates an immutable forward-test
baseline containing every comparable Moonshot candidate, including low-signal
control groups. Its fixed 1-month, 3-month, 6-month, and 1-year eligibility
dates prevent premature outcome evaluation. Re-running the command may
recreate identical files, but it refuses to overwrite a changed baseline with
the same run and model identity. Calibration measures structural stability and
data quality; it is not evidence of future returns.

Passing weighted scenarios are compared before one controls the v0.3 research
queue. The comparison exports consensus rank, rank sensitivity, and candidate
agreement at several cutoffs. The configured 80% technical / 20% fundamental
scenario is selected only while it passes every calibration gate; the original
Discovery Score and official technical report remain unchanged.

Deterministic research packets can be generated from a completed run without
network or AI calls:

```powershell
python main.py --research-run RUN_ID --top 10
```

The command uses the selected passing research scenario and exports JSON and
Markdown containing identity, technical signals, core fundamental evidence,
peer context, data-quality notes, and research questions. AI synthesis is
disabled by default. The provider boundary, prompt versioning, response cache,
and per-company failure isolation are present for optional sourced synthesis;
neither packets nor future AI output can modify ranking.

Primary-source evidence collection is a separate, explicit network action:

```powershell
$env:SEC_USER_AGENT = "Discovery Engine your-email@example.com"
python main.py --research-run RUN_ID --top 10 --collect-sources
```

The collector retrieves recent SEC filings for U.S. candidates, conservatively
paces requests, caches raw responses for seven days, and records publisher,
URL, dates, source type, priority, and SHA-256 hash. Unsupported companies and
source failures remain visible per candidate. A reviewed local manifest can
add approved sources with `--source-manifest PATH`; its domains must first be
allowlisted in `config/settings.json`. Terminal and manifest summaries report
evidence documents, failures, cache hits, misses, expiry, and read errors. AI
synthesis remains disabled.

For a country-balanced research batch, select an equal maximum from the
ranked Canadian and U.S. queues:

```powershell
python main.py --research-run RUN_ID --balanced-research 3
```

Automated SEDAR+ collection is intentionally unsupported because its public
terms prohibit scraping and automated access. Canadian evidence instead uses
explicitly curated issuer-primary URLs. Review
`config/canadian_sources.example.json`, allowlist only issuer domains you
intend to contact, and run:

```powershell
$env:SOURCE_USER_AGENT = "Discovery Engine your-email@example.com"
python main.py --research-run RUN_ID --balanced-research 3 --collect-sources --canadian-source-manifest config/canadian_sources.example.json
```

The engine fetches only the listed HTTPS URLs, caches their content, computes
SHA-256 hashes, optionally enforces `expected_content_hash`, and records
per-candidate provider provenance. SEC collection remains restricted to U.S.
candidates; issuer-manifest collection remains restricted to Canadian
candidates.

Validated cited synthesis is an additional opt-in action. First set
`research.ai_synthesis_enabled` to `true` in `config/settings.json`, then keep
the API key and SEC contact identity only in the local terminal environment:

```powershell
$env:OPENAI_API_KEY = "your-api-key"
$env:SEC_USER_AGENT = "Discovery Engine your-email@example.com"
python main.py --research-run RUN_ID --top 5 --collect-sources --synthesize
```

The OpenAI Responses API receives bounded evidence excerpts and returns a
strict JSON-schema research brief. Sourced facts must cite an attached URL and
SHA-256 hash; interpretations are labeled separately. Unsupported citations,
uncited sourced facts, refusals, and malformed outputs fail only that company.
Validated responses are cached by packet, evidence hashes, prompt version,
provider, and model. The API request disables response storage. Readable briefs
and structured validation metrics are exported without changing any score or
rank. Candidates without evidence are skipped and do not trigger an AI call.

Audit the latest saved research artifact without SEC or AI calls:

```powershell
python main.py --audit-research RUN_ID
```

This exports automated evidence, synthesis, citation, section-completeness,
error, sourced-evidence-use, and ranking-integrity gates plus a claim-level
triage artifact and a risk-based `research_human_review` CSV. Material
interpretations always require review; other interpretations plus sourced
risk, numeric, material, and citation-complexity claims receive elevated
sampling priority. Lower-risk claims are selected by a deterministic configured
sample, with at least one claim retained from every populated candidate
section. Stable claim IDs bind the saved audit to
the exact review queue, so removing or duplicating sampled rows blocks
finalization. Automated success produces only
`pending_human_review`. Reviewers must mark every row's `accuracy_review` and
`citation_support_review` as `pass` or `fail`, and `human_review_status` as
`approved` or `rejected`. After saving the CSV, finalize it offline:

```powershell
python main.py --finalize-research-review RUN_ID
```

Finalization produces a separate approval, rejection, or incomplete decision
record, a candidate-level release report, and manifest provenance. The audit
also exports a candidate-level CSV so missing evidence, synthesis failures,
cache use, and validation outcomes remain visible across a batch. Live
synthesis reports input, output, and total token use; cache hits add no new
provider tokens. Finalization cannot approve a run whose automated gates did
not pass.

## Documentation

- PROJECT_CONTEXT.md
- VISION.md
- MANIFESTO.md
- ROADMAP.md
- ARCHITECTURE.md
- PROMPT.md

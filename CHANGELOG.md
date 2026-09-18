# Changelog

## v0.4 Platform (in progress)

- Added an offline-first `--backtest-run` command that replays the configured
  technical scoring functions point-in-time at historical month-ends
- Added Spearman information coefficients, score-quantile spreads, top-N
  excess returns, hit rates, turnover, and compounded top-N comparisons
  against country benchmarks at 1M, 3M, 6M, and 1Y horizons
- Added `config/backtest.json` so validation settings never alter the run
  fingerprint used for resume compatibility
- Added deterministic backtest JSON, per-period CSV, compressed observation,
  and Markdown artifacts with explicit survivorship and static-factor caveats
- Recorded backtest provenance in the run manifest without changing scores
- Added shadow Score v2: point-in-time long/medium momentum, trailing-high
  proximity, volatility, volume trend, and one-month reversal raw signals
- Added a cross-sectional percentile pass that ranks each signal across the
  run, within sector when the group is large enough, and blends the
  percentiles into a 0-100 score with confidence, rank, and explanations
- Kept Score v2 entirely separate from official Discovery Scores and ranks
- Added Score v2 to backtest composites so v1 and v2 are compared on
  identical periods, horizons, and universes
- Added a paced, compact-cached SEC XBRL company-facts source that keeps only
  the concepts Discovery Engine derives signals from, with filing dates
- Added point-in-time fundamental derivation: direct and differenced
  quarterly flows, restatement-aware lookups, TTM growth, revenue
  acceleration, margin levels and trends, cash conversion, net cash,
  dilution, and market-cap ratios as of any calendar date
- Added `--with-fundamentals` to `--backtest-run` so U.S. fundamental signals
  are evaluated at each historical month-end using only prior filings
- Added median, win-rate, and 2%-trimmed-mean statistics per score quantile
  and median excess for top-N selections, because mean excess returns in
  this universe are dominated by a few extreme rebounds
- Added `--rescore-run` to reapply the current shadow models to a completed
  run from cached data only: fills missing SEC fundamentals and refreshes
  Form 4 signals as of the run's completion date, rewrites checkpoints and
  reports, and re-exports the research queue with no provider calls
- Added a compact, cached source for the SEC's quarterly insider-transaction
  data sets (Forms 3/4/5), keeping open-market purchases and sales with
  filing dates, values, and reporting-owner relationships
- Added point-in-time Form 4 signals (purchase and sale counts, distinct
  buyers, officer purchases, net value and its ratio to market cap, cluster
  buying, days since last purchase) with a data-through date
- Added `--with-insiders` to `--backtest-run` and live-run insider lookup
  for U.S. filers, with an `insiders_status` field on each row
- Accepted `ins_*` signals in the shadow-model registry and exclusions
- Weighted net insider purchases (10 points) in Score v3 in place of medium
  momentum after the full-universe backtest showed a one-year IC of 0.07
  positive in 91% of months; v3's one-year IC rose from 0.10 to 0.11 and its
  top-quintile median, trimmed mean, and win-rate spreads all widened
- Added a research queue ordered by validated evidence: Score v3 first,
  Score v2 for companies without fundamentals, then the official Discovery
  Score; exported as `research_queue_*.csv/json` with basis, tier, drivers,
  exclusion reasons, and the preserved official rank
- Switched `--research-run` packets to the research queue by default, with
  `research_ranking.packet_source` able to restore the calibration scenario
- Generalized the cross-sectional scorer into a signal registry that accepts
  both price signals and point-in-time `pit_*` fundamentals, with
  percentile-based exclusion filters and a minimum-confidence gate
- Added shadow Score v3, an evidence-weighted value and quality composite
  (sales yield, net cash, free-cash-flow yield and margin, operating margin,
  low dilution, plus a price tilt) that excludes bottom-quintile margins,
  top-quintile dilution, and top-quintile revenue growth
- Collected SEC company facts for U.S. filers during live runs when
  `SEC_USER_AGENT` is set, with per-company failure isolation and a
  `fundamentals_status` field; `pit_*` signals and `latest_close` now appear
  on successful rows
- Added excluded-versus-retained filter diagnostics to the backtest
- Preferred the highest-priority taxonomy tag per reported period and kept
  trailing-twelve-month windows within one tag, so component revenue lines
  can no longer replace consolidated totals
- Named limited backtest artifacts with a `_limitN` suffix so samples never
  overwrite full-universe results for the same source run
- Loaded an untracked `.env` file at startup so `SEC_USER_AGENT` can stay
  out of shell history and configuration files; added `.env.example`
- Fixed the end-of-run summary crashing on undefined provider statistics
  after every completed run
- Converted `requirements.txt` from UTF-16 to UTF-8 so pip can read it
- Added an idempotent offline SQLite index for completed manifests and results
- Added exclusive `--index-run` and `--history-summary` commands
- Kept existing run files authoritative while establishing a queryable history layer
- Added offline two-run comparison with presence, status, rank, score,
  confidence, and data-quality change classification
- Added deterministic JSON and CSV history-comparison reports
- Added status-transition matrices and country/sector composition deltas
- Added offline per-ticker timelines across indexed run history
- Added automatic weekly Markdown briefings from the latest comparable runs
- Added compatibility warnings for strategy/configuration fingerprint changes
- Added a loopback-only, dependency-free historical intelligence dashboard
- Added enforced read-only SQLite connections for all history query surfaces
- Added interactive candidate filters, ticker timelines, and weekly movers
- Added within-run score percentiles and plain-language standing descriptors
- Added score and confidence definitions that distinguish signals from forecasts
- Added technical and fundamental factor explanations to candidate details
- Added compressed run-linked price and benchmark snapshots with provenance gates
- Added offline period-return and benchmark-relative performance calculations
- Added interactive 1M/3M/6M/1Y candidate price charts
- Changed the local dashboard to a responsive dark visual theme
- Redesigned price charts with actual-price and benchmark-comparison modes,
  date axes, range controls, and hover values
- Added possible corporate-action detection and suppression of contaminated returns
- Added plain-language SPY and XIU.TO benchmark definitions
- Added split and dividend collection to future Yahoo price histories
- Added verified split adjustment without double-adjusting normalized prices
- Added chart quality classifications, split markers, volume bars, and coverage
- Added read-only side-by-side comparison for two to five indexed candidates
- Added multi-ticker normalized performance charts and factor-strength heatmaps
- Added persistent named dashboard watchlists isolated from indexed history
- Added tracked status, rank, and score changes versus the prior indexed run
- Redesigned the dashboard as a responsive fintech research terminal
- Added plain-language Score Story summaries with rank, median, driver,
  constraint, coverage, and missing-data context
- Added YTD performance, price summaries, 20/50/200-session moving averages,
  chart zoom, crosshairs, and corporate-action annotations
- Preserved adjusted daily OHLCV in compressed run-linked price snapshots
- Added candlestick charts, color-coded volume, full OHLCV hover values, and
  linear/logarithmic price axes
- Added EMA 20 and Bollinger Band overlays alongside SMA 20/50/200
- Added explicit OHLCV coverage and close-only legacy chart fallback
- Fixed Canadian universe refreshes for changing TMX dated metric headers
- Derived Canadian source dates from each TMX workbook instead of hardcoding May
- Added an offline Moonshot Discovery lane for $2M–$50M operating equities
- Added separate Upside Potential, Risk of Ruin, confidence, and missing-data
  analytics without changing official scores or ranks
- Added deterministic Moonshot CSV/JSON exports and manifest provenance
- Added nano-cap/micro-cap cohorts and explicit pending liquidity, drawdown,
  dilution, and reverse-split requirements
- Added Moonshot average-dollar-volume, realized-volatility, maximum-drawdown,
  share-dilution, and reverse-split risk factors
- Added run-compatible offline price-cache reuse and reusable market-evidence
  artifacts with per-company provenance and failure isolation
- Added explicit bounded `--collect-market-risk` collection with cached Yahoo
  price/share history and existing provider safety controls
- Blocked Moonshot watch and priority labels until market-risk coverage is
  sufficient, with unresolved risk bands for incomplete evidence
- Treated zero trading volume as valid maximum liquidity risk instead of a
  missing value
- Added a configurable 99% synchronized-cohort gate with explicit residual
  exclusions for Moonshot calibration
- Added offline Moonshot calibration across baseline, growth, survival, and
  trading-viability weighting scenarios
- Added top-list overlap, rank-sensitivity, factor-distribution, outlier, and
  country/size/sector cohort analytics
- Added hard data-integrity gates and configurable cohort-dispersion review
  gates without imposing country or size quotas
- Added candidate-level upside-driver, risk-driver, warning, and sensitivity
  validation exports
- Added immutable all-cohort forward-test baselines with fixed 1M, 3M, 6M,
  and 1Y eligibility dates
- Added a read-only Moonshot Research Terminal to the local dashboard
- Added Moonshot classification, country, size-tier, and text filters with
  distinct upside, risk-of-ruin, evidence, and market-risk displays
- Added candidate factor anatomy, risk warnings, stress-scenario ranks, and
  immutable forward-horizon status views
- Added dashboard calibration gates, scenario overlap, and country, size-tier,
  and sector selection-rate diagnostics without provider or AI calls

## v0.3.0 Intelligence — 2026-08-09

- Started Sprint 5 batch release validation
- Added Responses API token-usage reporting for uncached research synthesis
- Added candidate-level automated audit and finalized release CSV artifacts
- Added explicit approved, rejected, incomplete, and not-ready batch outcomes
- Added deterministic claim-risk triage and configurable review sampling
- Added mandatory high-risk and per-section review coverage with stable claim IDs
- Added tamper detection between audited samples and finalized review queues
- Added country-balanced ranked research selection
- Added curated, allowlisted Canadian issuer-primary evidence collection
- Added country-aware SEC/Canadian provider routing and provider provenance
- Fixed synthesis cache misses caused only by volatile evidence retrieval times

- Added the v0.3 specification and staged acceptance criteria
- Added configuration-driven structural asset classification
- Exclude obvious acquisition vehicles, shell companies, and non-equities
- Added explainable factor results, score confidence, and JSON breakdowns
- Preserved v0.2 numerical scoring during the framework transition
- Added versioned metadata caching for expanded provider fields
- Added shadow growth, profitability, cash-flow, and balance-sheet factors
- Added fundamental score, normalized score, confidence, and JSON breakdown
- Added curated top-candidate reports with separate fundamental ranking
- Added factor coverage, confidence, exclusion, country, and sector analytics
- Added deterministic interleaved Canadian/U.S. validation sampling
- Added shadow earnings-yield, sales-yield, and EV/EBITDA valuation factors
- Added shadow liquidity, leverage, and earnings-quality risk factors
- Added formal fresh, undated, missing, invalid, and stale data policies
- Added quality-adjusted fundamental confidence and report provenance
- Added officially ordered calibration CSV and aggregate analysis JSON
- Added country, sector, technical, and fundamental candidate percentiles
- Added rank correlation, top-list overlap, disagreement, and outlier analysis
- Added configurable 100/0, 80/20, and 70/30 experimental blend scenarios
- Added configurable cap/invalid policies for extreme fundamental inputs
- Added sector applicability exclusions without artificial confidence loss
- Added confidence-adjusted blends with minimum-confidence eligibility
- Added sector-relative fundamental percentiles and ineligibility explanations
- Added top-20/50/100 overlap, scenario movement, and factor-readiness analysis
- Added dynamically selected cross-market core fundamental factors
- Added country and sufficiently sized country/sector peer percentiles
- Added continuous confidence scaling toward a neutral fundamental percentile
- Constrained fundamental reranking to a top-100 pool and 25-company bands
- Added country-fairness, retention, and movement acceptance gates
- Added automatic experimental-scenario pass/fail recommendations
- Added offline recalibration from complete atomic run checkpoints
- Added decision-ready reports for every passing weighted scenario
- Added rank-movement, core-factor, peer, and data-treatment explanations
- Added combined top-25 scenario review and composition summary artifacts
- Added recalibration provenance to saved run manifests
- Added side-by-side passing-scenario comparison and consensus ranking
- Added rank sensitivity and top-10/25/50/100 agreement measurements
- Added conservative automatic scenario recommendation with gate validation
- Selected the passing 80/20 model as the configured v0.3 research scenario
- Added the final selected v0.3 research-candidate queue and decision record
- Added offline top-N research-packet generation from the selected v0.3 queue
- Added deterministic technical, fundamental, peer, quality, and question data
- Added a pluggable research-provider interface and versioned safe prompt
- Added persistent synthesis caching and per-company provider failure isolation
- Added JSON and Markdown research artifacts with manifest provenance
- Kept AI synthesis disabled by default and unable to change scores or ranks
- Added opt-in, cached primary SEC filing collection for research candidates
- Added curated source manifests with HTTPS allowlists and source priorities
- Added evidence freshness, SHA-256 hashes, deduplication, and failure isolation
- Added citation validation against attached evidence and explicit claim classes
- Added evidence provenance to research packets and saved run manifests
- Added evidence cache hit, miss, expiry, and read-error run statistics
- Added opt-in OpenAI Responses API research synthesis with strict JSON schema
- Added bounded filing excerpts and prompt-injection-resistant source handling
- Required evidence-bound citations for every sourced synthesis claim
- Added interpretation labels and rejection of ranking or recommendation fields
- Added model-aware synthesis caching, failure isolation, and no-evidence skips
- Added validated claim/citation metrics and readable research brief exports
- Added offline saved-research audits with configurable acceptance gates
- Added country evidence, synthesis, citation, section, error, and integrity metrics
- Added claim-level human-review queues with explicit accuracy/support decisions
- Added guarded research-review finalization and manifest acceptance provenance
- Increased synthesis output headroom and constrained briefs for concise completion
- Added explicit diagnostics for Responses API incomplete-output conditions
- Required evidence citations for interpretations as well as sourced facts
- Changed citation coverage to measure all research claims

## v0.2.0 Performance — 2026-07-25

Validated against the complete 6,424-company North American universe:

- 3,158 companies scored successfully
- 2,964 companies removed by explainable pre-filters
- 302 companies rejected for insufficient price history
- Zero provider or pipeline errors

- Added a configuration-driven market-cap pre-filter
- Skip price-history downloads for companies rejected by the pre-filter
- Report filtered companies separately from provider errors
- Corrected nested output-directory configuration loading
- Added persistent metadata and price-history caching with configurable TTLs
- Added per-run cache statistics and recovery from unreadable cache entries
- Added bounded concurrent metadata and price-history collection
- Added deterministic result ordering, benchmark reuse, and failure isolation
- Added configurable transient-provider retries with exponential backoff
- Added retry and exhausted-attempt statistics to run summaries
- Added resumable runs with configuration and universe fingerprints
- Added atomic per-company checkpoints and structured run manifests
- Added reproducible `--tickers` and `--limit` smoke-run options
- Added shared provider pacing and global Yahoo rate-limit cooldowns
- Added separate metadata and price-history worker limits
- Made completed runs with provider errors resumable
- Added a provider circuit breaker for persistent rate limiting
- Split scoring failures from provider errors in run summaries
- Completed the v0.2 Performance milestone

## v0.1 Foundation

- Project architecture established
- JSON configuration
- US universe builder
- Canadian universe builder
- Dynamic universe loading
- Documentation v1

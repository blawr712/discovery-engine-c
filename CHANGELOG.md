# Changelog

## v0.4 Platform (in progress)

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

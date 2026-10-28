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
checkpoints so compatible interrupted runs can resume safely. A compatible run
last updated more than 72 hours ago is not resumed, because its checkpoints
would publish older prices under a new completion time.
Completed runs containing provider errors remain resumable; successful and
analytically rejected rows are reused while only error rows run again.

Scoring Engine: Calculate Discovery Score only. Shadow models run beside it
through one signal registry (`src/scoring_v2.py`) in two pure stages:
per-company point-in-time raw signals (price signals from the price history,
`pit_*` fundamentals from SEC facts), then one cross-sectional pass over the
completed run that applies percentile-based exclusion filters, ranks each
signal across candidates (within sector when the group is large enough), and
blends them into a 0-100 score with explanations and a confidence gate. Score
v2 is the price-only model; Score v3 is the fundamentals-led composite. Neither
alters official scores or ranks, both are written back to checkpoints so saved
runs stay authoritative, and the Backtest Engine reuses the same functions so
historical and live behavior cannot diverge.

Rescoring (`src/rescore.py`, `--rescore-run`): reapply the current shadow
models to a completed run using only local caches. Missing SEC fundamentals
and Form 4 signals are filled as of the run's completion date, checkpoints
and reports are rewritten, and official scores, statuses, and ranks are
asserted unchanged. Weight changes therefore never require a new provider
run. `--refresh-fundamentals` recomputes signals a run already has, so a fix
to how a signal is derived costs no provider calls either; rows whose source
no longer resolves are cleared rather than kept, because a value that cannot
be reproduced from a cached source cannot be checked.

Research Engine: Generate explainable research summaries. Packet selection
follows the research queue (`src/research_ranking.py`), which orders
successful companies by Score v3, then Score v2, then the official Discovery
Score, keeping the official rank alongside for provenance. The queue is a
pure function of a completed run's rows and is exported with every run. Each
row also carries its v3 quintile and a screen status (`SCREEN_STATUSES`),
assigned with the frozen cohorts' quintile formula; the dashboard presents the
queue as a screen filtered on that status rather than as a ranked list.
Investment funds (`investment_fund`, set from EDGAR's filing record by the
Form 4 supplement's submissions listing) form their own tier after the
candidates, and `_apply_bank_lane` passes tradeable banks v3 cannot score in
the cheaper half by earnings yield as `passes_bank_lane`.

Fundamentals (point-in-time): `src/data_sources/sec_xbrl_source.py` fetches
SEC company facts for U.S. filers, keeps only configured concepts with their
filing dates, and caches the compact extract under `data/cache/sec_facts`.
`src/fundamentals_pit.py` turns an extract into quarterly flows (reported
directly or derived by differencing cumulative and annual figures) and
answers "what was knowable on this date" queries, so restated values only
appear from their restating filing onward. The Backtest Engine consumes these
histories, and the orchestration engine collects them per company during live
runs (failure-isolated) when `SEC_USER_AGENT` is configured. `src/sec_mapping.py`
decides which listings have an SEC registrant: U.S. tickers directly, Canadian
listings only when interlisted and the registrant name matches. Listings
without one use `src/statements_extract.py`, which converts provider quarterly
and annual statements into the same extract shape with estimated filing dates
and an explicit `estimated_filing_dates` quality label. Reporting currencies
are tracked per filer (the extract keeps money facts in any ISO currency) and
trading-currency prices are converted at `fundamentals.fx_pairs` rates, with
cross rates through the dollar, before valuation ratios; the backtest, which
has no historical rates, gives a filer reporting in a different currency from
its listing no valuation ratios. Filers whose latest fiscal year has no
quarterly figures get `maximum_annual_report_age_days` (550) instead of 400.
Price bars without a close are dropped wherever histories are read
(`src/data_sources/price_bars.py`).

Market capitalisation for those ratios is the filing's share count times the
traded price, which is the only construction available in historical replay.
The share count is the period-end count when one is fresh, otherwise the
latest quarter's weighted-average basic shares, which multi-class filers
report as one total although their period-end counts are per class
(`share_count_as_of`, recorded as `pit_shares_basis`). The two are
denominated differently in two ways, handled separately.

Splits are corrected (`src/split_adjustment.py`). A price series is adjusted
to the date it was downloaded, so a share count from an earlier filing is in
a different unit; `SplitHistory` reads the split events out of the same frame
as the closes, so cached prices and the events that restate them always
agree, and multiplies each share count by the cumulative ratio of the splits
after it. `pit_split_factor` records the adjustment, and share-count change
is measured on one basis so a reverse split cannot read as dilution. A split
between a share fact's period end and its filing may or may not have been
retroactively restated, which nothing in the extract says, so those facts
are flagged `pit_split_ambiguous` rather than guessed.

ADS ratios and multi-class structures are not splits and leave no event to
read. A live run therefore reconciles the result against the provider's
market cap and uses that figure instead when the two disagree beyond a
configured factor; `pit_market_cap_basis` records which source won.
Historical replay has no contemporaneous provider figure, so this class of
mismatch is uncorrected there. With no price there is no market cap and no
valuation ratios.

Insiders (point-in-time): `src/data_sources/sec_insider_source.py` downloads
the SEC's quarterly insider-transaction data sets once each, keeps only
open-market purchases and sales with filing dates and owner relationships,
and caches the compact extract; `src/insider_signals.py` answers windowed
"what was filed by this date" queries per issuer. The Backtest Engine and the
live orchestration engine both consume these histories for U.S. filers. The
data sets are published months late, so live runs and rescoring append each
company's own Form 4 filings made since the last published quarter
(`src/data_sources/sec_form4_source.py`, `src/insider_supplement.py`), parsed
into the same rows, skipping any filing already held and fetched only for
names above the liquidity floor. Every SEC source in a process shares one
request pacer (`src/data_sources/sec_pacing.py`).

Forward Baselines (`src/research_baseline.py`, `--freeze-research-baseline`):
freeze a completed run's research queue into labelled cohorts with fixed
horizons anchored on the run's completion date, so Score v3 accumulates
evidence on data its weights never saw. Cohorts are carved from the
v3-scored population; names the filters excluded are kept as a control.
Exports refuse to overwrite an existing baseline. Each candidate also carries
the frozen scores of every shadow model, including the v3 challenger, so
models can later be compared on the same outcomes. A baseline is labelled
with the version of the model that produced its scores.

Baseline Resolution (`src/baseline_resolution.py`, `--resolve-baselines`):
read a frozen forward baseline of either kind back and measure each eligible
horizon against realized prices, comparing selected cohorts with control
cohorts. Both prices for a return come from one provider series, and
candidates whose prices stop before a horizon stay visible as unresolved
rather than being dropped. The baseline file is never modified; each
resolution is a separate artifact recorded in the run manifest. `src/ai_review_resolution.py` joins that
artifact to the run's resolved research cohort as an `ai_review_comparison`,
reading but never modifying the baseline. For each
resolved horizon it also measures every frozen score in `compared_scores`:
its rank correlation with realized excess returns and its quintile ladder on
the names it scored, and a head-to-head on the names all of them scored, so a
coverage difference cannot pass for a difference in skill.

Monthly Cycle (`src/monthly.py`, `--monthly`): run the universe rebuild,
discovery, indexing, Moonshot collection and calibration, research-baseline
freeze, and resolution of newly matured baselines as one labelled cycle. Each
step's outcome is persisted, so a rerun continues an interrupted cycle without
repeating finished work; steps run only once their dependencies are done, and
independent steps continue past a failure. The cycle label is mixed into the
discovery fingerprint so that each month's run is distinct from the last, a
provider-error rate above 2% keeps a partly collected run from being frozen,
and a lock prevents two processes running the same cycle. The orchestration
takes its steps as callables and holds no provider code; `main.py` supplies
them. `src/monthly_schedule.py` registers it as a Windows scheduled task that
retries daily for a week, which the resumability makes safe, and
`src/keep_awake.py` holds an idle-sleep exemption while a cycle runs.

Survivorship bounds (`src/survivorship.py`, `src/survivorship_report.py`,
`--survivorship-test`): delisted companies are missing from the backtest
because their prices are not available for free, but the SEC keeps their
filings. `src/data_sources/sec_filings_source.py` reads EDGAR's delisting
notices, each company's exit filings, and its facts by CIK with public float;
the exit is typed from the filings around it, v3's price-free filters place
each dead company in a group, and scenario returns by exit type bound the
backtest's group comparisons with them included.

Backtest Engine: Replay the Scoring Engine's technical factor functions at
historical month-ends using only price data available on each date, then
measure rank correlation, quantile spreads, and top-N outcomes against forward
benchmark-relative returns. It reuses the scoring functions rather than
re-implementing them, reads its universe from a completed saved run, collects
long price histories through the same cached, paced, retried provider stack,
and never changes official scores or ranks. Its configuration lives in
`config/backtest.json`, outside the run fingerprint. `point_in_time_universe`
(on by default) adds the names filtered today only for size and keeps an
observation only when that month-end's market cap was inside the live band,
so the universe is not defined by today's market caps; off is the
`--static-universe` control. Observations below the liquidity floor are
dropped as well (`src/liquidity.py`), matching live research, where shadow
models rank only tradeable names and the research queue puts the rest last.
`src/model_tests.py` (`--test-v3-change`) runs the paired, Newey-West test of a
proposed Score v3 change against saved observations with the decision rule
fixed in advance. `src/screen_research.py` (`--screen-research`) rebuilds the
v3 screen at each month-end with the research queue's quintile formula and
scans signals for ordering power inside it, with a stricter two-window rule
because it tests many signals at once. `src/defensive_order.py` holds the
`defensive-v1` ordering under forward test: the research queue scores names
that pass the screen, cohorts freeze the score, and baseline resolution adds a
`screen_comparison` measured on passing names only, leaving the v3 head to
head untouched. `src/unscored_research.py` (`--unscored-research`) labels
backtest observations by screen group and compares the names v3 never scores
with the screen. `split_adjust_shares`
defaults to on and exists so the split correction can be measured against a
control on the same universe; an unadjusted run writes to its own artifact
names.

AI Review (`src/ai_review.py`, `--ai-review-prepare`; AI_REVIEW_BRIEF.md):
Claude reviews the top names passing the v3 screen from their SEC filings.
Code prepares everything the review may read under `data/ai_review/RUN_ID/`:
per name, a packet joining the run row's `pit_*` figures to the queue row's
screen fields, and the latest annual report, 10-Q and current reports as
plain text with SHA-256 hashes, chosen by form and limited to filings EDGAR
accepted by the run's completion time. EDGAR requests go through the shared
SEC pacer. `src/ai_review_peers.py` writes each name's peer table: closest
peers from the run by SIC code and market cap, and the industry's largest SEC
filers, shortlisted by public float from EDGAR frames and measured on the
anchor date with the same `fundamentals_pit` functions, prices and exchange
rates live runs use. SIC codes come from a persistent index of every SEC
filer's submissions. The review itself is a Claude Code skill
(`.claude/skills/ai-review/`), not engine code: one `ai-reviewer` subagent per
name, restricted to file tools, follows the versioned checklist and writes
`review.json` beside the inputs. `src/ai_review_freeze.py`
(`--ai-review-check`, `--ai-review-freeze`) validates each review against the
schema, its quotes against the hashed filing texts and its peer metrics
against `peers.json`, then writes one immutable `ai_reviews_RUN_ID.json`
recorded in the run manifest. It
never changes scores, ranks, the queue or the dashboard order.

Reporting Engine: Export reports and serve the local dashboard. Moonshot
terminal queries read deterministic analysis, calibration, and immutable
baseline artifacts through a separate bounded store; they never initialize the
market-data or AI layers.

## Configuration

Runtime configuration belongs in:
- config/settings.json
- config/strategy.json
- config/backtest.json (offline validation only; excluded from run fingerprints)

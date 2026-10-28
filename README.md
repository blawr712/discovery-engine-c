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
names without SEC fundamentals, receive no v3 score. Signals listed under
`sector_exclusions` are not applicable to that sector: they neither score,
rank, nor filter those companies and count as unavailable, so Financial
Services names, whose sales yield, net cash, and margins mean something
different, fall back to Score v2 rather than being ranked on them. Rows carry `score_v3`,
`score_v3_rank`, `score_v3_confidence`, `score_v3_excluded`,
`score_v3_exclusion_reasons`, and a per-signal `score_v3_breakdown`.

Reapply the current shadow-model configuration to a completed run without
any provider calls:

```powershell
python main.py --rescore-run RUN_ID
```

Rescoring reads the run's checkpoints, fills missing SEC fundamentals and
refreshes Form 4 insider signals from the local caches as of the run's
completion date (U.S. filers, when `SEC_USER_AGENT` is set), reapplies Score
v2 and Score v3, writes the refreshed rows back to the checkpoints, and
regenerates the score report, candidate report, and research queue. Official
Discovery Scores, statuses, and ranks are asserted unchanged, and the manifest
records the rescore. This is how weight changes reach existing runs without a
new two-hour collection.

Every run now also writes a **research queue**, `research_queue_RUN_ID.csv`
(top N) and `.json` (complete), that orders research attention by the best
validated basis available for each company: Score v3 first, Score v2 for
companies without SEC fundamentals, and the official Discovery Score for the
rest. Each row names its `ranking_basis` and tier, keeps the official
`discovery_rank` for provenance, and lists its strongest and weakest drivers
and any v3 exclusion reasons. `--research-run` builds packets from this queue
by default; set `research_ranking.packet_source` to `calibration_scenario` in
`config/strategy.json` to use the older v0.3 scenario ordering instead. The
queue orders attention; it is not a return forecast or a recommendation.

When `SEC_USER_AGENT` is set (see `.env.example`), live runs collect SEC
company facts for U.S. filers and for interlisted Canadian names whose root
symbol maps to an SEC registrant with a matching name, and record a
`fundamentals_status` on each row. Canadian names without an SEC registrant
fall back to the provider's quarterly and annual statements, which carry no
filing dates; the engine assigns conservative Canadian filing deadlines (60
days quarterly, 120 days annual) and labels those rows
`pit_data_quality = estimated_filing_dates`. Filers reporting in a different
currency than they trade in have prices and market caps converted with a cached
CADUSD rate before valuation ratios are computed; if no rate is available the
ratios are skipped and the status explains why. Statement-based fundamentals
are live-run only and are never used in the backtest. Set
`fundamentals.live_collection_enabled` or `statements_fallback_enabled` to
`false` in `config/settings.json` to turn either off.

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

Each month's universe is chosen by that month's market cap: names filtered
today only for being too big or too small are replayed too, and an observation
counts only if its market cap was inside the band at that month-end.
`--static-universe` replays today's universe at every date instead, as a
control, and writes `*_static_universe` artifacts.

Live runs keep insider signals current. The SEC's quarterly insider data
sets are published months late, so each tradeable U.S. company's own Form 4
filings made since the last published quarter are read from EDGAR, cached for
good, and appended in the same form. The first run after a long gap reads tens
of thousands of filings; later runs read only new ones.

Observations below the liquidity floor, a 30-day average dollar volume of
$250,000 (`universe.minimum_average_dollar_volume`), are dropped too, as they
are from live research: shadow models rank only tradeable names, and the
research queue lists names below the floor last, as `below_liquidity_floor`.
`--no-liquidity-floor` is the backtest control.

Test a proposed Score v3 change the way past changes were tested:

```powershell
python main.py --test-v3-change RUN_ID --change '{"momentum_long": null, "pit_fcf_yield": 20}'
```

A signal mapped to `null` is removed and a number sets its weight. The current
and changed models are compared period by period on the saved backtest
observations, in a 2016-21 fitting window and a 2022-26 holdout, with
Newey-West standard errors. A change is accepted only if it improves both
windows, significantly in at least one, and is significantly worse in
neither. The verdict is saved as `model_test_*.json`.

Ask which signals rank names inside the Score v3 screen:

```powershell
python main.py --screen-research RUN_ID
```

It rebuilds the screen at every backtest month-end (v3 quintiles 3–5 of
tradeable, unrejected names) and measures each signal's rank correlation with
forward excess returns inside it. Because many signals are scanned, a signal
counts as separating winners only when it has the same sign and |t| ≥ 2 in
both the 2017-21 and 2022-26 windows; it also reports how many calendar years
agree and tests a tie-breaker chosen on the first window alone against the
second. It writes `screen_research_*.json` and `screen_research_*.md`.

The scan found no signal that passed, but a defensive combination (near the
52-week high, low volatility, cheap on earnings, less net cash, larger) is
being tested forward as `defensive-v1` (`src/defensive_order.py`). Each
research cohort freezes its score for names that pass the screen, and each
resolution reports an **inside the screen** comparison: its rank correlation
with outcomes against v3's, and outcomes by defensive third. It never orders
the queue; DECISIONS.md fixes how and when it will be judged.

Ask how the names v3 never scores compare with the screen:

```powershell
python main.py --unscored-research RUN_ID
```

It groups every tradeable backtest observation (screen, v3's bottom
quintiles, filter rejections, never scored, the last split into financials,
names with no fundamentals, and the rest), compares each with the screen
month by month on median excess return and win rate, and scans signals inside
the never-scored group. It writes `unscored_research_*.json` and `.md`.

Bound what the remaining survivorship costs, using SEC records of companies
delisted during the backtest window:

```powershell
python main.py --survivorship-test RUN_ID
```

It reads EDGAR's delisting notices, classifies each exit (bankruptcy,
acquisition, delisting for failing listing rules), checks whether v3's filters
would have rejected each dead company, and recomputes the backtest's group
returns with them included under mild, central, and severe return scenarios.
It writes `survivorship_*.json` and `survivorship_*.md`. The results are a
range, not a measurement.

### AI review of the top screen names

Claude reviews the top names that pass the v3 screen by reading their SEC
filings, and gives each a fixed verdict (`clear`, `caution`, `avoid` or
`not_reviewed`) with quoted reasons. The design, guard rails and schema are in
`AI_REVIEW_BRIEF.md`. Reviews never change scores, ranks, the queue order or
the dashboard order. The review runs as a Claude Code skill on your Claude
plan, not through an API key; the shelved OpenAI synthesis stays switched off.

Prepare the inputs (needs `SEC_USER_AGENT` in `.env`):

```powershell
python main.py --ai-review-prepare RUN_ID --top 50
```

It takes the first 50 queue names whose screen status is `passes` (the bank
lane is not mixed in) and writes, under `data/ai_review/RUN_ID/TICKER/`:

- `packet.json`: the research packet, the run row's `pit_*` figures that a
  data check compares with the filings, and the queue row's screen status,
  v3 quintile and defensive score. Net margin is derived from two
  `fundamentals_pit` outputs and labelled as derived.
- `filings/*.txt` and `filings.json`: the latest 10-K (or 10-KT, 20-F,
  40-F) with any EX-13, the latest 10-Q, and up to ten 8-Ks with their EX-99
  exhibits filed since the latest periodic report, as plain text. Filers of
  20-F and 40-F get 6-Ks instead of 8-Ks. Only filings EDGAR accepted on or
  before the run's completion time are used. `filings.json` records each
  document's URL, form, dates, and the SHA-256 of both the raw document and
  the saved text; reviews cite the text's hash.

- `peers.json`: why is the name cheap? Up to eight **closest peers** from the
  same run with the same four-digit SIC code (nearest by market cap, any
  screen status) and the five **industry leaders**, the largest SEC filers
  with that code by market cap on the anchor date. The code widens to three
  digits when fewer than three usable peers share four. Each row carries
  market cap and revenue in dollars, revenue growth, gross, operating and net
  margin, earnings and sales yield, net cash to market cap and share-count
  change, computed with the same `fundamentals_pit` functions v3 uses; a
  summary gives the company's value, both medians and its percentile among
  its closest peers. Peers that cannot be used are listed with the reason,
  and a table with fewer than three usable peers says `insufficient_peers`.

Leaders are found from SIC codes for every SEC filer with a ticker, read once
from each company's EDGAR submissions (about 8,000 filers; the first fill
takes about two hours at the SEC's rate limit) and kept in
`data/cache/ai_review/sic_index.json`. Later runs read only new filers. Each
industry's ten largest filers by SEC public float (four quarterly EDGAR
frames) are then priced through the cached provider stack, and the five
largest by market cap on the anchor date are kept. Filers that report no
public float, often foreign filers, cannot be shortlisted.

Then run the review in Claude Code, in batches (10 by default):

```text
/ai-review RUN_ID 10
```

The skill launches one `ai-reviewer` subagent per company
(`.claude/agents/ai-reviewer.md`), so each judgement starts fresh. The
subagent can only read, search and write files, so it has no web search, and
it reads only that company's folder. It works through the fixed checklist
`review-v1` (`.claude/skills/ai-review/checklist-review-v1.md`): data check,
earnings quality, balance sheet, dilution, pending events, business, peers,
then the bear case before the verdict. Every finding quotes the filing it
comes from, by hash. A name that already has a `review.json` is skipped, so
an interrupted batch carries on, and a review is never re-run. After each
batch the skill validates what was written and asks before starting the
next.

Validate the reviews written so far, without freezing anything:

```powershell
python main.py --ai-review-check RUN_ID
```

When every prepared name has a review, freeze them:

```powershell
python main.py --ai-review-freeze RUN_ID
```

Every review must match the schema, every quote must appear word for word in
the saved filing whose hash it cites, every cited peer metric must exist in
`peers.json`, and every saved filing must still have its prepared hash. A
review that fails is kept in the artifact as failed, never rewritten. The
command writes `data/exports/ai_reviews_RUN_ID.json` (tracked in git, like
the forward baselines) with the model, skill and checklist versions and each
name's `peers.json` hash, refuses to overwrite it, and records it in the run
manifest. `--dry-run` writes `ai_review_dry_run_RUN_ID.json` instead, which
is not part of the forward test.

Canadian names with no SEC registrant get a `not_reviewed` review written by
code, since SEDAR+ may not be read automatically. A name that fails (for
example a network error) is reported and retried by the next run of the
command; finished folders are never rewritten. Nothing is written to the run.

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

Add point-in-time Form 4 insider signals as well:

```powershell
python main.py --backtest-run RUN_ID --with-fundamentals --with-insiders
```

Insider signals come from the SEC's quarterly insider-transaction data sets,
downloaded once per quarter and cached as compact extracts of open-market
purchases and sales. At each month-end the backtest counts purchases, sales,
distinct buyers, officer and director purchases, net purchase value and its
ratio to market cap, cluster buying, and days since the last purchase, using
only filings public on that date. The same signals (`ins_*`) attach to U.S.
rows in live runs when `SEC_USER_AGENT` is set, labeled with an
`ins_data_through` date because the SEC publishes these data sets quarterly
with a lag. `ins_*` signals can be weighted or used as exclusions in
`scoring_v2` and `scoring_v3`.

Freeze a completed run's research queue so Score v3 starts accumulating
out-of-sample evidence:

```powershell
python main.py --freeze-research-baseline RUN_ID
```

v3's weights were chosen from a backtest, so every in-sample result is
suspect. Freezing labels the queue into cohorts carved from the v3-scored
population: the top 25 and the top quintile on the selected side, the
bottom quintile and the names v3's exclusion filters removed on the
control side. Horizons are fixed at 1M, 3M, 6M and 1Y from the run's
completion date, which means freezing an older run can be resolvable
immediately. Baselines are immutable: re-freezing the same run and model
is refused rather than overwritten.

Measure a frozen forward baseline against what actually happened:

```powershell
python main.py --resolve-baselines RUN_ID
python main.py --resolve-baselines RUN_ID --collect-prices
python main.py --resolve-baselines RUN_ID --baseline research
```

Resolution measures every frozen baseline for the run, Moonshot and
research alike, taking each one's cohort split from the baseline itself.
It measures
every horizon that has become eligible, and compares the selected cohorts
with the control cohorts by median, trimmed mean, and win rate. It works
from cached prices alone; `--collect-prices` refreshes only the series that
stop short of a horizon, which is usually just the benchmarks. Candidates
whose prices stop before a horizon are reported as unresolved rather than
dropped, because treating a delisting as missing data would bias results
upward. The frozen baseline is never modified. Results land in
`baseline_resolution_RUN_ID.json` and `.csv`. One baseline resolved on one
date is a single observation, not a backtest.

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

The dashboard opens on the **Research Screen** for the selected run. Score v3
is read as a screen, because in the backtest its bottom two fifths did clearly
worse while its top three fifths did alike: a name *passes* when v3 puts it
in quintile 3–5, v3's filters did not reject it, it clears the liquidity
floor, and it is the kept listing of its company. Banks, which v3 cannot
score, pass in a **bank lane** of their own when their earnings yield ranks in
the cheaper half among tradeable banks. Closed-end funds and business
development companies, identified from EDGAR's filing record, are labelled
investment funds and are not candidates. Every other name carries the
reason it does not pass (low v3 quintile, bank in the dearer half, rejected by
filters, not scored by v3, investment fund, illiquid, duplicate listing). The screen shows each name's v3 quintile and
score, 30-day dollar volume, twelve-month insider purchases with their
data-through date, and key point-in-time ratios, with the backtest's quintile
outcomes beside it, filters by screen status, country, sector, and text, and
sorting by v3 order, insider purchases, dollar volume, or name. Ordering
inside the screen is a convenience, not a ranking. Queues exported before the
screen existed need `--rescore-run RUN_ID` to show statuses. Clicking a
company opens the candidate terminal, which now carries a model-rank strip and
three additional tabs: **Score v3** (each signal's percentile, contribution,
applicability, and plain-language explanation for v3 and v2, plus exclusion
reasons or the confidence gate), **Fundamentals** (point-in-time statement
values with reporting currency, conversion, and data-quality labels), and
**Insiders** (Form 4 activity over 180 and 365 days with its data-through
date). An **Evidence** workspace shows the run's backtest: rank correlations
per signal and horizon, outcomes by score quintile, top-N baskets, exclusion
filter diagnostics, and limitations. Runs recorded before the shadow models
existed say so; rescore them to populate these views.

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

### The monthly cycle

Once a month, after downloading the latest TMX workbook to
`data/raw/canada/tsx-and-tsxv-listed-companies.xlsx`, run:

```powershell
python main.py --monthly
```

That one command rebuilds both universes, runs discovery over the full
universe, indexes the run, collects Moonshot market-risk evidence, calibrates
Moonshot, freezes the Score v3 research queue as a forward baseline, and
resolves every earlier baseline with a horizon that has matured since it was
last measured. It writes `data/exports/monthly_cycle_<cycle>.md` with what each
step did and the selected-versus-control outcome of each newly matured horizon.

A cycle takes most of a day. It can be interrupted at any point: run the same
command again and finished steps are skipped, so it continues where it stopped.
A failed step does not stop steps that do not depend on it, and the command
exits with an error until every step has succeeded. Progress is kept in
`data/runs/monthly/<cycle>.json`.

- The cycle label defaults to the current month, or to an unfinished cycle
  touched in the last seven days, so a cycle that starts on the 30th and
  finishes on the 1st stays one cycle. `--cycle 2026-11` sets it explicitly.
- `--skip-universe` keeps the existing universe files.
- The cycle label is part of the discovery run's fingerprint, so a cycle never
  resumes last month's run, even when the universe and configuration are
  unchanged.
- A run with more than 2% provider errors (typically a rate-limit episode) is
  not frozen. The freeze and Moonshot steps wait, and the next attempt resumes
  the run and retries the failed rows.
- The report warns when the TMX workbook's data is more than 45 days old.

### Rehearsing a cycle

Before a scheduled cycle, and after any change to the code it runs, rehearse
it:

```powershell
python main.py --monthly --rehearsal
```

This runs every step of the real cycle on a balanced sample of 25 names per
country (`--rehearsal-size` changes it) in a fresh folder under
`data/rehearsal/`. Runs, exports, frozen cohorts, and the history database are
all written there, so nothing a rehearsal does can be mistaken for a real
record; caches are shared. Running it through `scripts
un_monthly.cmd
--rehearsal` also tests the scheduled task's wrapper and log. The folders are
safe to delete.

### Scheduled runs

A Windows scheduled task runs the cycle automatically:

```powershell
python -m src.monthly_schedule show        # print the task definition
python -m src.monthly_schedule register    # create it
python -m src.monthly_schedule unregister  # remove it
```

It fires at 02:00 on the 16th of every month and again daily through the
22nd. A finished cycle makes the later days a no-op, so they only matter when
an attempt failed or the PC was off. The 16th follows TMX's release of the
listed-companies workbook, which lands around the 11th to 14th. The task runs
as you, only while you are logged on, with no stored password and no
elevation. A missed start runs when the PC is next available, and an attempt
is stopped after 36 hours. While a cycle runs it asks Windows not to idle-
sleep; whether the PC wakes from sleep to start depends on your power plan's
wake-timer setting, which the task does not change.

Each attempt writes a log to `data/runs/monthly/logs/`, and
`scripts/run_monthly.cmd` runs the same thing by hand.

**The TMX workbook is still downloaded by hand.** TMX's robots.txt disallows
automated fetching of `/resource/`, the path the workbook is served from, so
the pipeline does not fetch it. Download "TSX & TSXV Listed Companies" from
[Current Market Statistics](https://www.tsx.com/en/listings/current-market-statistics)
to `data/raw/canada/tsx-and-tsxv-listed-companies.xlsx` before the 16th. If you
miss it the cycle still runs on the previous workbook, and the cycle report
warns once the data is more than 45 days old.

Every frozen research baseline also records a **challenger** score beside
Score v3. It never orders the research queue. It currently holds Score v3.0,
the model before v3.2 replaced net insider buying with insider purchases, so
unseen outcomes test that change. When a horizon matures, resolution reports
each frozen score's rank correlation with the realized outcome and its
quintile ladder, plus a head-to-head on the names both models scored, and the
cycle report lists it. The two models rank the Oct 19 run 0.988 alike, so
separating them will take many monthly cohorts.

### Individual commands

Run the complete configured universe:

```powershell
python main.py
```

A compatible interrupted run is resumed only if it was updated in the last 72
hours (`run_state.max_resume_age_hours`). An older run's checkpoints hold
older prices, so a new run starts instead.

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

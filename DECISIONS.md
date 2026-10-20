# Decisions

Evidence-backed decisions about how Discovery Engine ranks companies, including
experiments that were run and rejected. Each entry records the date, the
decision, the evidence that drove it, and where the evidence lives. Nothing
here is a return forecast or an investment recommendation; every result comes
from the point-in-time backtest on the Aug 2026 run universe (2016-2026,
survivorship-biased toward companies still listed) unless stated otherwise.

## 2026-09-12: Build measurement before changing scores

**Decision.** Add a point-in-time backtest harness before touching any weight.

**Evidence.** No outcome measurement existed; the Moonshot forward baseline
froze candidates but nothing scored them. Every weight in `strategy.json` was
untested.

**Where.** `src/backtest.py`, `config/backtest.json` (kept outside the run
fingerprint so validation settings never break resume compatibility).

## 2026-09-18: Judge screens by medians, not means

**Decision.** Report median, win rate, and 2%-trimmed mean per score quantile
and lead with them.

**Evidence.** Every quintile of every model has a negative median excess
return versus SPY (the typical micro cap trails by ~20 points a year). Means
are positive only through a thin right tail, up to +850% at the 99th
percentile, concentrated in the lowest-scored bucket of crashed names that
rebounded (2020 most of all).

**Where.** Backtest summaries; `full-backtest-findings` in the changelog.

## 2026-09-18: The official technical score is anti-predictive; v2 replaces it for ranking research

**Decision.** Keep the official Discovery Score for provenance, but rank the
research queue by Score v3, then Score v2, then the official score.

**Evidence.** v1 technical score: 1Y Spearman IC -0.03 (t -3.6), top quintile
has the worst median (-25). Score v2 (continuous, cross-sectional price
signals): IC +0.04 (t 3.7), positive in 79% of months, medians monotone in
the right direction. Volume acceleration carries no information; liquidity
points are strongly negative in this universe.

**Where.** `src/scoring_v2.py`, `src/research_ranking.py`.

## 2026-09-18: Fundamentals dominate; revenue growth is negative

**Decision.** Score v3 is a value and quality composite with exclusion
filters: bottom-quintile operating margin, top-quintile dilution, and
top-quintile revenue growth are excluded, then names rank on sales yield,
net cash, free-cash-flow yield and margin, operating margin, low dilution,
and a price tilt.

**Evidence.** 1Y IC across 2,050 U.S. filers: sales yield 0.24 (t 19),
FCF margin 0.14, operating margin 0.13, net cash / market cap 0.09, share
dilution -0.12, revenue growth **-0.05** (t -7.5; the fastest growers'
quintile has a -33 median). Gross margin, margin trend, cash conversion, and
acceleration carry nothing. Sales yield's top quintile is the only bucket in
the study with a positive trimmed mean (+11). Profitability and dilution work
as filters: their worst quintiles have medians near -40 to -45, but their best
quintiles are no better than the middle.

Result: v3 1Y IC 0.10 (t 10.7), first model where median, trimmed mean, win
rate, and mean all improve from Q1 to Q5; top-25 monthly portfolio 431% vs
267% benchmark over the decade, positive IC in 9 of 10 years (2020 negative).

**Where.** `config/strategy.json` (`scoring_v3`), `src/fundamentals_pit.py`.

## 2026-09-18: Net insider purchases enter v3 at 10 points

**Decision.** Replace medium momentum with net Form 4 purchases (180-day
window) at 10 points.

**Evidence.** Across 1,671 filers with Form 4 history, net purchase count
1Y IC 0.07 (t 13.6), positive in 91% of months; every insider signal positive
at every horizon. Adding it moved v3 1Y IC 0.10 -> 0.11, top-25 median
-14.9 -> -11.8, hit rate 34.8% -> 37.8%, and widened every quantile spread.
Caveat: the SEC publishes these data sets quarterly with a lag, so live
signals are about six months stale until a per-issuer supplement exists.

**Where.** `src/insider_signals.py`, `src/data_sources/sec_insider_source.py`.

## 2026-09-18: Financials are not scored on operating-company signals

**Decision.** Sales yield, net cash, FCF yield and margin, and operating
margin are not applicable to Financial Services in v3; those names fall back
to v2.

**Evidence.** Banks were clearing the v3 gate on net cash, dilution,
insiders, and price alone, with the signals that carry v3's evidence empty
or meaningless for them. Mirrors the existing v1 fundamental policy.

**Where.** `scoring_v3.sector_exclusions` in `config/strategy.json`.

## 2026-09-18: Rejected: leverage and cash-runway exclusions

**Decision.** No leverage or runway exclusion in v3.

**Evidence.** Excluding the bottom 10% or 20% of net cash / market cap raised
1Y IC (0.12 -> 0.14 / 0.16) but left top-25 mean, median, and hit rate
identical (17.6 / -13.3 / 36.8%) and lowered the compounded top-25. The most
indebted decile is one of the best buckets (1Y median -15, win 35%); the weak
pocket is net cash near zero (deciles 4-5: medians -62 and -41), which is
cash-poor, debt-light small caps, not leveraged ones. Cash runway (cash /
annual burn) does not separate that pocket either: every runway bucket among
v3-scored names sits between -19 and -32, and runway exclusions leave the
top 25 unchanged. v3's weights already keep these names out of the top 25.

**Where.** Scratch analysis over `backtest_observations_*.csv.gz`; not in
the repo. Revisit only with delisting-inclusive data.

## 2026-10-19: First resolved forward baseline shows no Moonshot edge at 1M

**Decision.** Record the result; change nothing yet. Moonshot stays a
research queue, not a validated model.

**Evidence.** The Aug 23 cohort was frozen on 2026-09-05 and its one-month
horizon became eligible on 2026-10-05. Resolved against realized prices,
benchmark-relative:

| Cohort | Resolved | Median excess | Win rate |
|---|---|---|---|
| Selected (priority + asymmetric + speculative) | 95 of 96 | -8.05% | 24.2% |
| Control (low upside + insufficient evidence) | 1,166 of 1,182 | -8.75% | 31.1% |

Selected beat control by 0.7 points of median and lost by 6.9 points of win
rate; its trimmed mean was 1.8 points worse. Within the selection,
priority_research (7 names) had the best median at -6.51% and
speculative_watch (10 names) the worst at -18.24%. Both cohorts trailed their
benchmarks by roughly 8%, so the month was broadly bad for micro caps rather
than specifically bad for the selection.

**Why it does not settle anything.** One horizon, one baseline, one month is a
single observation that cannot separate model skill from the regime. Priority
Research is only seven names. The 3M, 6M and 1Y horizons are still pending,
and the Sep 18 baseline adds a second cohort from 2026-10-07.

**Where.** `--resolve-baselines`, artifacts under
`baseline_resolution_20260823T133945Z-4e30d9d9.*`.

## 2026-10-19: First out-of-sample month for Score v3 is encouraging but confounded

**Decision.** Record it; promote nothing. Keep collecting horizons.

**Evidence.** The Sep 18 run's research queue was frozen as a forward
baseline anchored on 2026-09-18, and its one-month horizon (2026-10-18)
resolved against realized prices. v3's weights were chosen from a backtest
ending Aug 2026, so this month is genuinely unseen data. Benchmark-relative:

| Cohort | n | Median excess | Win rate |
|---|---|---|---|
| Queue top 25 | 25 | -8.65% | 28.0% |
| v3 quintile 5 | 131 | -2.04% | 40.8% |
| v3 quintile 4 | 156 | -4.70% | 34.0% |
| v3 quintile 3 | 156 | -8.34% | 22.4% |
| v3 quintile 2 | 156 | -9.98% | 21.8% |
| v3 quintile 1 | 155 | -19.36% | 13.7% |
| v3 excluded | 572 | -19.76% | 11.8% |

The quintile ladder is monotone on both median and win rate, a 17-point
median spread from Q1 to Q5. The exclusion filters also held: excluded names
landed with the worst quintile rather than in the middle.

**Why it is not proof.** One month, one cohort. Micro caps fell roughly 8%
against their benchmarks over the same window (see the Moonshot resolution),
and v3 carries a low-volatility tilt, which outperforms in a falling market
almost mechanically. A monotone ladder in one down month is consistent with
both real skill and a defensive tilt paying off once. The 3M, 6M and 1Y
horizons and future baselines will start to separate those.

Note also that the top 25 underperformed quintile 5 as a whole (-8.65% vs
-2.04%), which is the same thin-tail concentration the backtest showed: the
very top of the queue is noisier than the band beneath it.

**Where.** `--freeze-research-baseline`, `--resolve-baselines --baseline
research`, artifacts under `baseline_resolution_research_*`.

## 2026-10-20: Two data bugs put wrong names at the top of the queue

**Decision.** Reconcile point-in-time market capitalisation against an
independently sourced figure, and never accept a single shared name token as
evidence of a shared issuer. Both fixes are guards on inputs, not changes to
v3's weights.

**Evidence.** Reviewing the Oct 19 run's queue before freezing a baseline on
it, the top two anomalies were both data errors rather than picks.

Valuation ratios divided by the filing's share count times the traded price.
When the two are denominated differently every yield is scaled by that
factor, and the multipliers were exact giveaways: BYND and PSNYW 30.000x,
DDI 0.050x, from reverse splits falling between the filing and the price and
from ADSs bundling ordinary shares. 78 of 2,663 names with fundamentals
(2.9%) were affected. DDI held rank 1 on a sales yield of 10.47 that is
really 0.52; corrected, it sits at 128.

Separately, stopword removal reduces "Q2 Holdings, Inc." to the single token
`q2`, which overlaps "Q2 METALS CORP" at exactly the 0.5 similarity
threshold and passed. Q2 Metals, a pre-revenue Quebec lithium explorer,
inherited Q2 Holdings' $846M revenue and share count and ranked 4th. The
tighter rule was checked against all 33 interlisted names in the run and
flips exactly one decision, the wrong one.

After the fixes, the only names moving more than 25 places in the old top
100 are DDI, QTWO.V, CLPR and ZKIN, the last two the same unit mismatch at
2.6x and 9.2x. The new top 25 derives every market cap from reported shares.

**Why it matters beyond these four names.** Sales yield is v3's heaviest
signal and the one with the strongest evidence behind it (1Y IC 0.24). A bug
in its denominator lands hardest exactly where the model is most confident,
and both of these names reached the top on fundamentals the queue presented
without qualification.

**Where.** `src/fundamentals_pit.py` (`pit_market_cap_basis`),
`src/sec_mapping.py` (`names_match`), `--rescore-run
--refresh-fundamentals`.

## 2026-10-20: Second Score v3 forward baseline frozen on corrected data

**Decision.** Freeze the Oct 19 run's queue as the second v3 forward
baseline, after the fixes above and not before.

**Evidence.** Baselines are immutable by design, so freezing one on a queue
whose top two names were data errors would have put those errors permanently
into the forward-test record. The run was rescored from cached extracts
first (2,989 rows recomputed, 1 cleared), then frozen: 3,023 candidates, 25
top-N and 257 in quintile 5 selected, 281 in quintile 1 and 942 excluded
names as control, anchored 2026-10-20 with the 1M horizon eligible
2026-11-19.

**Where.** `research_forward_baseline_20261019T223433Z-f13f71e5_v3.0-shadow-composite.*`,
baseline ID `e954186803e40841`.

## Standing caveats that gate any promotion to "official"

- **Survivorship.** The universe is companies alive at the source run; the
  rebound effect in low-scored names is inflated. Credible mean-based claims
  need a delisting-inclusive price vendor.
- **In-sample weights.** v3's weights were set from the same decade they are
  evaluated on. They are coarse and evidence-aligned rather than optimized,
  and year-by-year stability is reassuring, but a true out-of-sample test needs
  future runs and the Moonshot-style forward baseline.
- **Coverage.** Backtested fundamentals cover SEC filers only, about 60% of
  U.S. observations plus verified interlisted Canadian names. Other Canadian
  names receive live-run fundamentals from provider statements with estimated
  filing dates; those were never backtested, so their v3 scores rest on the
  assumption that the same signals behave the same way north of the border.
  Insider signals remain U.S. only.
- **Picks versus baskets.** Even v3's top 25 has a negative median and a
  ~38% hit rate; the edge is a fatter right tail within the top group. It
  works as a basket, not as individual conviction.
- **The backtest shares the market-cap bug.** Historical replay has no
  contemporaneous provider market cap to reconcile against, so the guard
  added on 2026-10-20 cannot run there, and every published v3 result was
  fitted on sales yields that were wrong for roughly 3% of names. The
  direction of the distortion is not random: a reverse split between filing
  and price overstates the market cap and makes a distressed name look
  expensive, while an ADS understates it and makes a name look cheap. Both
  correlate with subsequent returns, so sales yield's 1Y IC of 0.24 is to
  some unknown degree an artifact. Closing this needs split-adjusted share
  counts in the extract, not a cross-check, and until then every in-sample
  v3 figure in this file carries that asterisk. The forward baselines are
  unaffected, since they are scored by the live path.

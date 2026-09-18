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

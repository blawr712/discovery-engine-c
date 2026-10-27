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

**Amended 2026-10-23.** Measured on a point-in-time universe, the official
technical score's 1Y IC is +0.014 (t 1.7), not -0.03, and v2's is 0.072, not
0.04. Defining the universe by today's market caps had removed the stocks that
grew past $1B, the high-momentum winners, and understated every price-trend
signal. The decision stands, since v2 and v3 still beat the official score
clearly, but the official score is closer to uninformative than harmful.

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

**Amended 2026-10-20, after the split correction landed.** This baseline was
frozen hours before share counts were split-adjusted, so it records a queue the
corrected code would not reproduce exactly. Measured rather than assumed: the
top 25 overlaps the corrected queue 24 of 25 names (OPAD out, KMDA in) and
95.5% of all 3,023 candidates keep their cohort, with nearly every mover going
one quintile. The selected and control sides are therefore materially intact,
and the baseline is kept rather than re-frozen; discarding an immutable record
for a 4.5% adjacent-bucket reshuffle would cost more than it is worth. The next
baseline will be the first frozen on fully corrected scores.

## 2026-10-20: Split-adjusted share counts; a third of v3's measured edge was the bug

**Decision.** Restate every reported share count into the price series'
split-adjusted basis before deriving valuation ratios. Treat the corrected
numbers below as the evidence of record and every earlier in-sample figure in
this file as superseded.

**Evidence.** A price series is adjusted to the date it was downloaded, so a
share count from an earlier filing is in different units. 682 of 2,071 filers
with price history (32.9%) have at least one split, and 39,193 of 177,856
month-end observations (22.0%) have a split after them. Serial reverse-
splitters compound absurdly: XTIA to 1.5e12, GPUS to 7.8e8, TNXP to 6.4e8.

This was look-ahead bias, not noise. A market cap overstated a million-fold
gives a sales yield of essentially zero, ranking the name maximally
expensive, and chronic reverse-splitters do go on to collapse. "Low sales
yield predicts bad returns" was partly "will reverse-split soon predicts bad
returns", which is a future fact.

Measured as a controlled experiment: the same run universe, the same 256,294
observations, backtested twice with `split_adjust_shares` on and off. 1Y
information coefficients:

| Signal | Unadjusted | Adjusted | Change |
|---|---|---|---|
| Sales yield | 0.251 | 0.133 | -0.119 |
| Net cash / market cap | 0.088 | **-0.018** | -0.106 |
| Earnings yield | 0.046 | **0.142** | +0.096 |
| Free-cash-flow yield | 0.069 | **0.154** | +0.085 |
| Share dilution, 1Y | -0.110 | **-0.175** | -0.065 |
| **Score v3** | **0.129** | **0.083** | **-0.046** |
| Score v2 | 0.039 | 0.039 | 0.000 |
| Technical score | -0.031 | -0.031 | -0.001 |

Every signal with no market capitalisation in it is unchanged to three
decimals: free-cash-flow margin 0.137, operating margin 0.122, OCF margin
0.131, gross margin -0.003, revenue growth -0.041, revenue acceleration
0.004, cash conversion 0.011. Score v2 and the technical score, which never
touch a share count, are identical. Only the ratios that divide by market cap
moved, which is what makes this the fix rather than a different universe.

Score v3's top 25 compounded 993% unadjusted against 496% adjusted, on a 268%
benchmark. Roughly half the flagship basket's apparent decade of outperformance
was the bug.

**The asymmetry is the interesting part.** The distortion inflated sales yield
and net cash while *degrading* earnings yield and free-cash-flow yield. For
sales yield the error pointed the same way as the outcome, so it masqueraded
as skill; for earnings and cash-flow yields it was closer to pure noise, so
removing it recovered signal that was there all along. Corrected, the best
value signals are free-cash-flow yield (0.154) and earnings yield (0.142), not
sales yield (0.133).

**Dilution also changes.** Comparing share counts across a reverse split
understates issuance badly: 1,000 shares becoming 1,100 across a one-for-
twenty reverse split is a 22-fold increase, not 10%. On one basis, dilution is
the strongest single signal in the study at -0.175 (t -12.9), negative in 90%
of months, ahead of every value signal.

**What survives.** Score v3 remains the best model by a wide margin: 1Y IC
0.083 (t 11.7), positive in 90% of months, a monotone quintile ladder on both
median and win rate (Q1 -23.8% / 31.1% win to Q5 -14.2% / 35.7% win), top-25
median -13.3% with a 38.0% hit rate, and top 25 / 50 / 100 compounding to
496% / 434% / 366% against 268%. The top 10 still underperforms the top 25,
the same thin-tail concentration seen before.

**What this does not settle.** v3's weights were chosen from the contaminated
ranking. This entry originally called them demonstrably misaligned; the
re-derivation of 2026-10-21 tested that and found it wrong, because raw-signal
ICs do not describe a filtered composite. The weights stand.

**Where.** `src/split_adjustment.py`, `pit_split_factor`,
`split_adjust_shares` in `config/backtest.json`, artifacts
`backtest_20261019T223433Z-f13f71e5.json` and its `_unadjusted` control.

## 2026-10-21: Re-derived v3's weights; the current weights stand

**Decision.** Keep Score v3's weights exactly as they are. Every change the
corrected evidence appeared to call for made the model significantly worse,
and the one change that did not hurt failed the significance bar set for it
in advance.

**Method.** The saved observations from the split-adjusted backtest were
re-scored offline under candidate configs, using the backtest's own metric
functions; the harness reproduced the published result exactly (1Y IC
0.0832, top 25 compounding 495.91%) before being trusted. Periods were split
into a fitting window (2016-10 to 2021-12) and a holdout (2022-01 to
2026-09). Each change was measured as the per-period IC difference against
the current model, so shared market conditions cancel, with Newey-West
standard errors at eleven lags because monthly 1Y returns overlap by eleven
months and a naive t-statistic overstates significance roughly 3.5x. The
acceptance rule was fixed before running: positive in both windows,
significant (|t| >= 2) in at least one, and significantly negative in
neither.

**Evidence.** Paired 1Y IC difference against current v3 (Newey-West t):

| Change | Fit 2016-21 | Holdout 2022-26 | Verdict |
|---|---|---|---|
| Double dilution weight (10 -> 20) | -0.012 (-2.0) | -0.017 (-6.8) | worse |
| Double FCF yield weight (10 -> 20) | -0.002 (-0.6) | -0.014 (-3.8) | worse |
| Add earnings yield (10) | -0.011 (-1.4) | -0.007 (-2.1) | worse |
| Remove net cash | +0.004 (0.6) | -0.027 (-4.1) | worse |
| Remove long momentum | +0.004 (0.6) | +0.002 (1.4) | neutral |
| Remove FCF margin | +0.002 (0.4) | +0.004 (1.7) | neutral |
| Remove operating-margin weight | +0.001 (0.3) | +0.002 (1.3) | neutral |
| Insiders 10 -> 15 | +0.003 (1.0) | +0.001 (1.0) | neutral |
| Volatility 5 -> 10 | -0.002 (-0.3) | +0.003 (1.3) | neutral |
| Remove all three neutral signals | +0.009 (0.7) | +0.009 (1.9) | not significant |

**Why the corrected raw ICs misled.** They are measured across every name,
but v3 ranks only the roughly 27% of observations that survive its exclusion
filters and confidence gate. Inside that population the picture changes:
dilution falls from -0.175 to -0.070 / -0.030 because the filter has already
removed its worst quintile, and FCF margin (0.000 / 0.021) and operating
margin (-0.029 / 0.005) carry almost nothing once their own filters have run.
Net cash flips sign between regimes, -0.063 in 2016-21 and +0.027 in 2022-26,
plausibly because cash-rich microcaps lagged in the zero-rate years and paid
once rates rose; it earns its weight in the composite even though its raw IC
is near zero. The entry of 2026-10-20 called v3's weights "demonstrably
misaligned" on the strength of the raw table. That was wrong in the way that
matters: the composite's behaviour is not the sum of its signals' raw ICs.

A rule applied mechanically would also have gone wrong. The "minimal"
candidate, which dropped net cash and long momentum and moved their points to
dilution and FCF yield, won the fitting window (IC 0.085 against 0.079) and
then halved the holdout IC (0.043 against 0.089). Fit-window wins of this
size are noise.

The three-signal simplification is positive in both windows and would leave a
seven-signal model performing at least as well. It is deliberately not
adopted, because t 1.9 is below the bar set before the test and moving the bar
after seeing the result is the failure the bar exists to prevent. It is the
natural candidate to run as a forward shadow beside v3.0 if one is added.

**Corrected 2026-10-23: the "larger finding" here was a grouping error.** As
first written, this paragraph said the names v3 *excludes* beat the names it
scores on compounded equal-weight returns in both windows, 299% against 183%
in 2016-21 and 111% against 42% in 2022-26. The script behind those numbers
treated every name without a v3 score as excluded. About 73% of observations
have no v3 score, and most of them were never rejected by a filter: they are
Canadian names without SEC data, financials, and names missing fundamentals.
Split by what v3 actually did, compounded at the 1M rebalance:

| Group | 2016-21 | 2022-26 |
|---|---|---|
| Rejected by v3's filters | 205% | **-5%** |
| Scored by v3 | 183% | 42% |
| Never scored | **324%** | **178%** |
| Whole universe | 268% | 90% |

So the filters did their job on means in the holdout, and gave up only modest
upside in 2016-21. The group that out-compounds everything is the one v3
cannot score at all. Survivorship still inflates every group's mean, the
universe being companies alive at the Oct 2026 run, but the specific worry
drawn from the wrong numbers, that the filters discard real winners, has
little support. The one survivorship-free observation agrees: in the Sep 18
forward baseline's first month the excluded cohort was the worst on mean as
well as median (-18.61%, against -2.84% for quintile 5).

**Consequence, as corrected.** A delisting-inclusive price vendor would still
make the backtest's means trustworthy, and would test why never-scored names
compound so strongly, but it is no longer needed to rescue v3's filters. The
filters' case rests on medians, win rates, the holdout means above, and the
forward cohort.

**Where.** Scratch harness over
`backtest_observations_20261019T223433Z-f13f71e5.csv.gz`; not in the repo.
Forward evidence from `baseline_resolution_research_20260918T144343Z-8ab9f4fc.*`.
Note in passing that v3's configured weights sum to 105 rather than 100;
scores divide by available weight, so rankings are unaffected and only the
explanation points are scaled.

## 2026-10-21: Monthly cycle, and v3.1 as a forward challenger

**Decision.** Run the research cycle as one resumable command,
`--monthly`, and score a Score v3.1 challenger beside v3.0 on every run so
the forward baselines can decide between them.

**Why a pipeline.** Forward baselines are the only evidence here that
survivorship cannot inflate, and they accumulate only if a run is made,
frozen, and resolved every month without gaps. Building it exposed a latent
hazard: the run fingerprint holds no date, and compatible runs that finished
with errors stayed resumable indefinitely, so a plain run in November would
have resumed the Oct 19 run (one provider error), reused about 3,000 rows of
October prices, and stamped them with a November completion time. The cycle
label now enters the fingerprint, runs older than 72 hours are not resumed,
and a run with more than 2% provider errors is not frozen.

**Why v3.1, and why only as a challenger.** Removing long momentum, FCF
margin, and the operating-margin weight was positive in both backtest windows
(+0.009 each) but reached only Newey-West t 1.9 in the holdout, below the bar
set before testing it. It is not adopted, and it never orders the research
queue. Freezing its scores into every baseline lets unseen data settle what
the backtest could not. Expect that to take a long time: on the Oct 19 run the
two scores have a rank correlation of 0.965 and share 20 of their top 25
names, and the backtest gap between them is small.

**First forward rank correlation for v3.0.** Re-resolving the Sep 18 cohort
with the new comparison gives a 1M rank IC of 0.32 over 776 names, with a
clean ladder from quintile 1 (-19.1% median) to quintile 5 (-3.3%). That is
far above the backtest's typical 1M IC of about 0.05, and it is one cross-
section in a month when micro caps fell and v3's low-volatility tilt would
have helped almost mechanically. It is encouraging and settles nothing.

**Where.** `src/monthly.py`, `scoring_v3_1` in `config/strategy.json`,
`compared_scores` in `src/baseline_resolution.py`.

**Amended 2026-10-23.** v3.1 was retired before any cohort froze its scores:
measured on the point-in-time universe it is significantly worse than v3.2 in
the holdout. The challenger slot now holds v3.0; see the entry of that date.

## 2026-10-23: Survivorship test; the backtest's universe is now point in time

**Decision.** Choose each month's backtest universe by that month's market
cap, and make it the default. Record the bounds the SEC's records of delisted
companies put on what survivorship still costs. Change no weights.

**Two biases, not one.** The backtest replayed every name that passed today's
pre-filter over ten years. That misses delisted companies, the survivorship
the test set out to bound, and it also defines the universe by today's market
caps: in the Oct 19 run 2,335 still-listed operating companies were filtered
as too big and 473 as too small, so a company that grew past $1B was missing
in exactly the years it was a winner, and companies inside the band today were
replayed at dates when they were outside it.

**Part A: the point-in-time universe, which is exact.** The size-filtered names
were added and each observation kept only if that month-end's market cap was
inside the band (split-adjusted filed shares times the close, with a constant
per-filer unit factor for bundled ADSs, and a price-scaled fallback for names
without filed shares). 270,831 observations stay and 230,292 are dropped.
1Y rank correlations, today's universe against point in time:

| Signal | Today's universe | Point in time |
|---|---|---|
| Score v3 | 0.083 (t 11.7) | 0.080 (t 10.4) |
| Score v2 | 0.039 (t 3.4) | **0.072 (t 6.1)** |
| Official technical score | **-0.031 (t -3.9)** | +0.014 (t 1.7) |
| Official score with static points | -0.006 (t -0.7) | +0.031 (t 3.7) |
| Net insider buying (in v3) | **0.070 (t 14.3)** | 0.015 (t 3.3) |
| Insider purchases, 12 months | - | 0.046 (t 8.6) |
| FCF yield | 0.154 | 0.176 |
| Operating margin | 0.122 | 0.158 |
| Net cash / market cap | -0.018 | -0.053 |
| Share dilution | -0.175 | -0.186 |

v3 barely moves, which is the most reassuring result in the test. Three
earlier conclusions do not survive:

- *The official score is anti-predictive* (the 2026-09-18 entry) was largely
  produced by the universe. Defining it by today's caps removed the stocks
  that grew past $1B, which are the high-momentum names, and kept fallen
  former large caps, so every price-trend signal was understated. The official
  score is closer to uninformative than harmful, and v2 is nearly twice as
  strong as measured.
- *Net insider buying is v3's steadiest signal* does not survive either. Among
  names outside the band today, mostly companies that grew past $1B, net
  buying is negatively predictive (-0.074, t -7.8), because insiders sell into
  a rally; among names inside the band it is +0.050. Insider purchases alone,
  ignoring sales, are positive in both groups (+0.045 and +0.057). That points
  to replacing v3's insider input, which should go through the same pre-stated
  paired test as any other weight change rather than be made on this evidence.
- v3's quintile ladder flattens at the top: medians run -21.7%, -14.9%, -11.9%,
  -12.9%, -11.3% from Q1 to Q5, and win rates 33.8% then about 38% for Q2-Q5.
  Its value is concentrated in avoiding the bottom fifth.

Compounded equal-weight 1M returns by what v3 did with each name:

| Group | 2016-21 | 2022-26 |
|---|---|---|
| Rejected by v3's filters | 256% | **-38%** |
| Scored by v3 | 187% | 72% |
| v3 top fifth | 245% | 103% |
| v3 top 25 | 522% | 74% |
| Never scored | 260% | 93% |
| Universe | 253% | 51% |
| Benchmark | 102% | 76% |

**Part B: delisted companies, which are bounds.** EDGAR's form index gives
18,237 exchange delisting notices in the window, from 4,411 issuers not in
today's universe. Dropping 1,082 non-operating entities and 635 blank checks
leaves 2,694 companies with an exit to classify: 1,689 acquired, 522 delisted
for failing listing rules, 286 bankrupt, 197 other (counts include companies
without usable facts). 1,898 have months in which their public float places
them inside the market-cap band.

They are a third of the cross-section, not a margin. In 2016-21 delisted
companies would have been 34.7% of the filter-rejected group and 34.3% of the
scored group, and about 16% of each in 2022-26.

The cleanest result needs no return assumption. v3's filters, which use no
price, sort dead companies by how they died:

| Delisted companies v3 would have | Failed | Acquired |
|---|---|---|
| Rejected, 2016-21 | **49.7%** | 49.1% |
| Scored, 2016-21 | 28.5% | **70.2%** |
| Rejected, 2022-26 | **52.6%** | 46.1% |
| Scored, 2022-26 | 33.9% | **65.4%** |

"Failed" is bankruptcy or delisting for failing listing rules. Rejected
companies were about 1.7 times as likely to fail. This is conservative: a dead
company has no price series to read reverse splits from, so its dilution is
understated and the dilution filter catches fewer of them than it should.

With assumed lifetime returns by exit type (central: bankruptcy -95%, rule
delisting -75%, other -50%, acquired +10%), compounded group returns become:

| | Rejected | Scored | Never scored | Universe |
|---|---|---|---|---|
| 2016-21 observed | 256% | 187% | 260% | 253% |
| 2016-21 mild / central / severe | 95% / **40%** / -24% | 77% / **40%** / -8% | 195% / 167% / 124% | 132% / 92% / 36% |
| 2022-26 observed | -38% | 72% | 93% | 51% |
| 2022-26 mild / central / severe | -44% / **-55%** / -70% | 46% / **25%** / -6% | 81% / 69% / 53% | 34% / 17% / -6% |

Under central assumptions the 2016-21 advantage of rejected names disappears
(40% each), and the filters win every 2022-26 scenario. Counting delisted
companies of unknown size as well moves these by a few points and changes no
conclusion. The universe's compounded mean roughly halves or worse: the
survivorship-biased backtest overstated how well this universe did by a wide
margin, which matters for any absolute claim (the top 25's 988% compounded
included) and very little for v3's ranking.

**What it does not settle.** Dead companies have no v3 score, so the bounds
cover groups, not v3's top fifth or top 25. The returns within each exit type
are assumptions, applied equally across groups. The size estimate assumes
insiders hold 25%.

**Consequence for paid data.** The question the paid data was wanted for,
whether v3's filters discard real winners, is answered well enough for free:
they do not, once the missing failures are counted. A delisting-inclusive
price vendor would now buy trustworthy absolute returns and basket-level
numbers, which is worth having but not urgent.

**Where.** `--backtest-run` (point-in-time universe by default,
`--static-universe` for the control), `--survivorship-test`,
`src/survivorship.py`, `src/data_sources/sec_filings_source.py`, artifacts
`survivorship_20261019T223433Z-f13f71e5.*`. The today's-universe backtest is
kept as `backtest_*_static_universe.*`.

## 2026-10-23: Score v3.2 counts insider purchases, not net buying

**Decision.** Replace v3's insider input, net purchases minus sales over 180
days (`ins_net_count_short`), with open-market purchases over 12 months
(`ins_purchase_count_long`), at the same 10 points. Nothing else changes. The
model becomes v3.2. Retire the v3.1 challenger and make v3.0 the challenger,
so the forward cohorts test this change on data it never saw.

**Why it was tested.** On the point-in-time universe net buying fell from
0.070 to 0.015 (1Y IC): among names that grew past the band it is negatively
predictive (-0.074), because insiders sell into a rally, while purchases alone
were positive in every subset (+0.046 overall).

**The test.** The protocol of 2026-10-21, on the point-in-time observations,
with the harness first reproducing the published result exactly (IC 0.0802,
top 25 988.54%). Rule fixed before running: positive in both windows, Newey-
West |t| >= 2 in one, significantly negative in neither, with one decision
test and anything else diagnostic. Paired 1Y IC difference against v3.0:

| Change | 2016-21 | 2022-26 | Verdict |
|---|---|---|---|
| **Net buying 180d -> purchases 12m** | +0.0073 (t 1.8) | +0.0095 (t 5.5) | **accept** |
| Drop insiders entirely (diagnostic) | -0.0028 (t -0.5) | -0.0029 (t -1.0) | - |
| Net buying 180d -> purchases 180d (diagnostic) | +0.0054 (t 1.2) | +0.0091 (t 5.5) | - |

The 3M horizon agrees (+0.0058 and +0.0046). Overall 1Y IC rises from 0.080 to
0.089 and the quintile spread widens (Q1 median -21.7% to -22.1%, Q5 -11.3% to
-10.7%); top-25 median and hit rate are unchanged (-11.2% / 40.1% to -11.1% /
40.2%). The diagnostics say insiders still add something, and that purchases
rather than the window carry it.

**How much to trust it.** Less than a clean out-of-sample result. The idea
came from analysing these same observations, holdout years included, and the
2016-21 difference alone (t 1.8) is not significant. What it can lean on is
independent of this data: insider purchases are widely found to be
informative, while sales are often driven by diversification, liquidity, or
pre-arranged plans. That is why the old model now runs beside the new one.

**The challenger.** v3.1, which dropped long momentum, FCF margin, and the
operating-margin weight, was measured on today's universe. Against v3.2 on
the point-in-time universe it is significantly worse in the holdout (-0.010,
t -2.0), since long momentum is informative once the universe is honest. No
cohort had frozen its scores, so retiring it loses nothing. The challenger
slot is now `score_v3_challenger`, holding v3.0 exactly; its ranking of the Oct
19 run reproduces the old v3.0 queue across all 1,417 scored names. v3.2 and
v3.0 rank that run 0.988 alike, and the queue's top 25 shares 22 names (JCTC,
AUDC, KMDA out; NTWK, JYNT, ACNT in).

**A labelling bug, fixed.** Research baselines took their version from a fixed
`"v3.0-shadow-composite"` in `config/backtest.json`, not from the model that
produced the scores, so cohorts frozen after this change would have been
labelled v3.0. The version now comes from `scoring_v3`. Already frozen cohorts
are unchanged; the Oct 19 baseline file is byte-identical.

**Where.** `scoring_v3` and `scoring_v3_challenger` in `config/strategy.json`;
harness over `backtest_observations_20261019T223433Z-f13f71e5.csv.gz`.

## 2026-10-24: A liquidity floor for research candidates; v3 is a filter more than a picker

**Decision.** Apply the $250,000 30-day average dollar volume floor that
`universe.minimum_average_dollar_volume` always defined and nothing read.
Names below it keep their official score but are not research candidates:
shadow models rank only tradeable names against each other, the research
queue lists illiquid names last as `below_liquidity_floor`, and the backtest
drops them at each month-end. Read v3's ranking as which names to avoid more
than which to buy.

**Evidence.** In the Oct 19 run half of v3.2's top 25 traded below the floor
(12 of 25; 9 below $100k; the top name about $5,300 a day), and about half of
all point-in-time backtest observations sit below it (49% in 2016-21, 46% in
2022-26). The backtest already recorded a liquidity score that is positive
exactly at $250k, so the effect of the floor was measured offline first and
then confirmed by a full rerun:

| v3.2, point-in-time universe | All names | Tradeable only |
|---|---|---|
| 1Y IC | 0.089 (t 11.8) | **0.079 (t 9.1)** |
| Quintile medians, Q1 to Q5 | -22.1, -14.8, -12.3, -12.6, -10.7 | -21.9, -14.9, **-11.4**, -12.0, -12.7 |
| Quintile win rates, Q1 to Q5 | 33.5, 36.7, 38.5, 38.3, 38.7 | 33.0, 36.1, 38.8, 38.6, 36.8 |
| Top-25 median / hit rate | -11.1% / 40.2% | -13.3% / 36.1% |
| Top 25 compounded, 2016-21 / 2022-26 | 498% / 93% | 211% / 104% |

v3 keeps nearly all of its ranking power among tradeable names, and its
holdout correlation barely moves (0.105 to 0.097). But its top fifth is no
better than its middle once illiquid names are gone; what it reliably does is
separate the bottom two fifths from the rest. Illiquid names have the same
median return as liquid ones and a far higher mean (+24.3% against +10.7% in
2016-21), which is what outliers and thin closing prices look like: returns
that could not have been captured. v3's top 25 leaned towards them (41-45%
illiquid, against about 30% in its top fifth), which is where part of its
apparent 2016-21 strength came from.

One guess did not hold: illiquid names do not look low-volatility because
they rarely trade. Their measured volatility is higher (median 0.735 against
0.567), so v3's low-volatility tilt is not what drew it towards them.

The full rerun keeps 142,349 observations, drops 149,366 below the floor and
209,408 outside the size band. v2's 1Y IC is 0.071 and the official technical
score's 0.001.

**Effect on the queue.** On the Oct 19 run 1,823 of 3,023 successful names
clear the floor. Every name in the new top 25 trades at least $298,000 a day.
Frozen cohorts are unchanged; cohorts frozen from now on classify illiquid
names as `below_liquidity_floor` and record the floor.

**How to use v3 now.** As a screen: set aside its bottom two fifths and the
names its filters reject, then research what remains, rather than treating the
top 25 as the best 25.

**Open.** Interlisted companies can appear twice (HITI and HITI.V on Oct 19).
The floor is applied in each listing's trading currency, so it is looser for
Canadian listings by the exchange rate, as the market-cap band already was.

**Where.** `src/liquidity.py`, `minimum_average_dollar_volume` and
`--no-liquidity-floor` for the backtest; `--test-v3-change` now runs the
paired test behind past weight decisions, reproducing the v3.2 decision
exactly.

## 2026-10-26: Live insider data read from companies' own Form 4 filings

**Decision.** Fill the gap after the SEC's quarterly insider data sets with
each company's own Form 4 filings from EDGAR, in the data sets' exact row
shape. The backtest stays on the data sets.

**Why.** In late October the newest published data set covered January to
March 2026; the April to June file still returns 404. Live insider signals
ended at March 31, so Score v3.2's twelve-month purchase window was missing
its most recent seven months.

**Evidence that the rows mean the same thing.** On Q1 2026, which both sources
cover, 40 sampled companies gave 316 transactions from the data set and 316
from their Form 4s. 39 matched transaction for transaction; the fortieth
differed only because the data set rounds fractional share counts to two
decimals.

**Effect on the Oct 19 run.** 1,626 tradeable U.S. companies were refreshed
through 2026-10-26, adding 10,922 transactions filed since April. Of 1,629
tradeable U.S. names, 537 now count more twelve-month purchases (none fewer)
and 257 went from none to some; 200 more companies have any insider history.
The queue's top 25 keeps 20 names (LOVE, SPOK, AMWL, BOLD, OSUR in; AUDC,
KMDA, CRTO, CRNT, CCLD out).

**Design notes.** Filings already in a data set are skipped by accession, a
company whose filings cannot be read keeps its data-set history, and recent
filings are fetched only for names above the liquidity floor, since insider
signals reach the queue only through v3. All SEC sources now share one
request pacer, because a run fetching facts and Form 4s concurrently would
otherwise exceed the SEC's ten-per-second limit. The first fetch read about
21,000 filings; each later run reads only new ones.

**Open.** The backtest's most recent months still see insider data only
through March 2026, which slightly understates insider signals there.

**Where.** `src/data_sources/sec_form4_source.py`, `src/insider_supplement.py`,
`src/data_sources/sec_pacing.py`.

## 2026-10-26: One candidate per company, and a rehearsal before the first scheduled cycle

**Decision.** Count each interlisted company once, and rehearse the monthly
cycle end to end before it runs unattended on Nov 16.

**Duplicates.** The Oct 19 run held 29 companies twice, once per exchange
(HITI and HITI.V, TLRY and TLRY.TO), with identical fundamentals, so each
counted twice in every cross-section and would have weighed twice in frozen
cohorts, which cannot be corrected afterwards. A Canadian listing is now a
duplicate when it is flagged interlisted, the U.S. ticker is its root, and the
names agree by the SEC mapping's rule; that last test kept 35 shared roots
that are different companies apart (VHI.TO VitalHub and VHI Valhi). The U.S.
listing is kept when tradeable; on Oct 19 it was kept in all 29.

**The rehearsal found a bug that would have cost November.** The first
rehearsal, launched through the scheduled task's own wrapper, failed at
discovery: a run with no provider errors ends with status `complete`, and the
monthly step accepted only `completed` or `completed_with_errors`, confusing
the run's status with the cycle's. The October run had one provider error,
which is why nothing had exercised the clean path. On Nov 16 a perfect run
would have blocked indexing, Moonshot, and the cohort freeze for the month.
After the fix a second rehearsal completed all seven steps; real exports,
runs, and the history database were byte-for-byte unchanged, and the
rehearsal's cohort was labelled v3.2 with the challenger's scores and the
liquidity floor recorded.

**Practice.** Rehearse after any change to the code the cycle runs, before the
next scheduled cycle.

**Where.** `src/listings.py`, `--monthly --rehearsal`,
`COMPLETED_RUN_STATUSES` in `src/run_state.py`.

## 2026-10-26: The dashboard shows v3 as a screen, not a top 25

**Decision.** The dashboard's main list is now a research screen. A name
passes when it is ranked by v3 in quintile 3, 4, or 5, its v3 filters did not
reject it, it clears the liquidity floor, and it is the kept listing of its
company. The view opens on passing names. The queue order is still exported
and still feeds research packets; only its presentation changed.

**Why.** The liquidity-floor rerun (2026-10-24) showed that among tradeable
names v3's quintile medians run -21.9, -14.9, -11.4, -12.0, -12.7: the bottom
two fifths are clearly worse, the top three are indistinguishable. A ranked
top 25 implies an ordering inside the top fifths that the evidence does not
support. The screen shows what the evidence does support, and the dashboard
prints those quintile outcomes beside the list so the reason stays visible.

**Details.** Quintiles are assigned over the v3-ranked rows with the formula
frozen cohorts use, so the quintile on screen is the one the forward test
records (checked by a test). The cut-off is `screen_minimum_quintile` in
`research_ranking` settings. Sorting inside the screen (v3 order, insider
purchases, dollar volume, name) is a research convenience, not a ranking.
Queues exported before this change show a notice to rescore.

**Where.** `_apply_screen` and `SCREEN_STATUSES` in
`src/research_ranking.py`; `screen` and `sort` on `/api/research-queue`.

## 2026-10-26: Nothing in the data reliably ranks names inside the screen

**Decision.** Keep the screen unordered. Do not add a tie-breaker to the
queue on this evidence. A defensive ordering is the candidate for a forward
test.

**Method, fixed before results were read.** At each of 110 month-ends
(2017-08 to 2026-09) the backtest population was cut to what the live screen
passes, about 315 names a month, and 43 signals and composites were scanned
for rank correlation with forward excess return inside it. Scanning that many
signals turns up chance winners, so the bar was the same sign in 2017-21 and
2022-26 with |t| ≥ 2 in both. A tie-breaker was also chosen on 2017-21 alone
and judged on 2022-26 alone.

**Result.** No signal passed. v3's own order inside the screen has 1Y IC
+0.009 and -0.001 (positive in 4 of 9 years). The tie-breaker chosen on
2017-21 (falling gross margins) did nothing afterwards (holdout IC +0.008,
t 0.5).

Ten signals were significant in the holdout only, and they agree with each
other: nearness to the 52-week high, lower volatility, larger size, higher
earnings yield, higher FCF yield and margin, and less net cash relative to
market cap. Their 2022-26 ICs reach t 3 to 5 and the five strongest hold
their sign in 7 or 8 of 9 years. All fail in 2020, when the most beaten-down,
volatile names rebounded hardest. Combined equally, the least defensive third
of the screen beat the most defensive third by 41 points of median 1Y excess
from 2020 month-ends, and lost to it by 5 to 30 points in 2018 and in every
year from 2021 to 2025. This is the quality and low-risk tilt that small-cap investors
know, and it was found after looking at the holdout, so it cannot be
validated on this backtest.

**Insider purchases do not help inside the screen.** Twelve-month purchase
counts, which v3.2 uses across all v3-ranked names, correlate negatively with
returns inside the screen in 6 of 9 years (holdout t -4.6). The insider sort
on the dashboard is a research convenience and nothing more.

**A false winner caught.** The official score's static form,
`discovery_score_static`, passed the bar with the wrong sign. Its market-cap
points come from the source run's current market cap, which encodes each
name's later returns; that part alone had a 1Y IC of -0.18 (t -20) inside the
screen. It is now left out of the scan. The backtest already scores the
official model by `technical_score` for this reason, so earlier conclusions
stand.

**Survivorship.** Missing delisted companies are mostly small, volatile, and
far from their highs, so the defensive signals' measured edge is more likely
understated than flattered. That is an argument for testing it, not evidence.

**Open.** Freeze a defensive tie-breaker as a forward shadow ordering in
monthly cohorts, so its within-screen effect is measured on months no one has
seen.

**Where.** `src/screen_research.py`, `--screen-research RUN_ID`.

## 2026-10-26: A forward test of the defensive ordering, rules fixed now

**Decision.** From the Nov 16 cycle on, every frozen research cohort carries
a defensive score for each name that passes the screen, next to v3, and each
resolution measures both inside the screen. The score never orders the queue
or the dashboard.

**The hypothesis, `defensive-v1`.** Equal-weight mean of signed percentile
ranks among passing names: nearness to the 52-week high (higher), volatility
(lower), earnings yield (higher), net cash to market cap (lower), and
market cap (higher), scaled 0 to 100. A name missing a signal is scored on
the rest, and a name missing all sits at 50. Size uses `pit_market_cap`,
which exists live and in the backtest (the scan's `universe_market_cap` gives
the same result: holdout IC 0.099 against 0.098). In the backtest this
combination had a within-screen 1Y IC of +0.05 (t 0.6) in 2017-21 and +0.10
(t 6.0) in 2022-26. Changing any part of it makes a new hypothesis with a new
version, and its record starts again from zero.

**How it will be judged.** No single cohort can decide this; one cohort is
one observation of one market. The rule:

- Primary horizon 6M, pooled over monthly cohorts frozen from Nov 2026, with
  Newey-West standard errors (5 lags, because 6M outcomes of monthly cohorts
  overlap).
- No decision before 12 cohorts have resolved 6M (the Oct 2027 cohort, around
  April 2028). Earlier results are for watching only.
- Adopt it as the screen's sort only if all three hold: the mean
  within-screen IC is positive with t ≥ 2; it beats v3's own within-screen IC
  on average; and the most defensive third beats the least defensive third
  on median excess in at least 7 of the 12 cohorts.
- Retire it if the mean IC after 12 cohorts is negative.
- 1Y results are reported alongside; they cannot rescue a failed 6M test.

**Known differences from the backtest.** The live screen includes about 47
Canadian interlisted names (the backtest's screen was U.S. only), and live
market caps come from each listing's own data. Both are small and recorded,
not adjusted for.

**Effect on Oct 19.** All 469 passing names get a score. The most defensive
are larger, calmer names (INN, ACCO, SGU; volatility 0.12 to 0.29); the least
defensive are small and volatile (MDXH, ACH, FLNA; volatility 1.05 to 2.53).
The Oct 19 cohort is already frozen and stays as it is.

**Where.** `src/defensive_order.py`; `screen_comparison` in each research
resolution; an "Inside the screen" section in the monthly cycle report.

## 2026-10-27: Multi-class companies get a share count

**Decision.** When a filer has no fresh period-end share count, value it with
its latest quarter's weighted-average basic shares. Prefer the period-end
count otherwise, unless it is under half the weighted-average count, which is
what a single class of a multi-class company looks like. `pit_shares_basis`
records which count was used.

**Why.** Looking into the 470 tradeable names v3 did not score on Oct 19
found that 212 were ordinary U.S. companies with SEC filings, missing only a
share count. Companies with several share classes (Movado, Marcus, Hamilton
Beach, AMC Networks, PubMatic) report period-end shares per class, which SEC
company facts leave out. With no share count there is no market cap, so
sales yield, FCF yield, net cash to market cap, and dilution were all empty,
and v3's confidence fell below its 50% floor. 199 U.S. filers with revenue
(13%) had no share count. The same companies report one weighted-average
total every quarter.

**Evidence the counts are right.** On Oct 19, 146 names valued on
weighted-average shares reconciled with the provider's market cap at a
median ratio of 0.994 (75% within 10%, against 84% for period-end counts,
since an average lags). 39 disagreed by more than a factor of two, and the
existing reconciliation replaced them with the provider's figure.

**Effect on Oct 19.** Names not scored by v3 fell from 470 to 382, and 95
names are now scored: 41 pass the screen, 37 sit in its bottom quintiles, and
17 are rejected by its filters. Names passing went from 469 to 511. The top 25
keeps 23 names (GLBS and KELYA in, GTE and UFI out). The frozen Oct 19 cohort
is unchanged; cohorts from Nov 16 use the new counts.

**A hazard found on the way.** `--rescore-run --refresh-fundamentals` read
only cached data and cleared the fundamentals of any name whose cache had
expired. The first rescore after the extract change cleared 970 names, 919 of
them Canadian, whose fundamentals come from provider statements cached for 7
days. Refreshing now fetches SEC extracts first, and a rescore reads the
statements its run collected however old they are. The rerun cleared none.

**Remaining gap.** 382 tradeable names are still not scored. About 250 are
financials, whose structure v3 was never built for (five of its signals do
not apply to them). Village Super Market-style filers that report even the
weighted average per class only stay unscored.

**Where.** `weighted_average_shares` in `src/data_sources/sec_xbrl_source.py`
(extract `sec-facts-3`), `share_count_as_of` in `src/fundamentals_pit.py`,
`SecXbrlSource.prefetch`, `any_age` on cached statements.

**Backtest rerun.** With the corrected counts (and interlisted duplicates
removed), v3's 1Y IC is 0.075 (t 9.0, was 0.079); quintile medians -21.3,
-14.9, -11.6, -11.8, -12.7; the screen holds a median 339 names a month (was
315). `--screen-research` still finds no signal that ranks names inside the
screen.

## 2026-10-27: The names v3 never scores, measured properly

**Decision.** Keep names without fundamentals out of the screen. No change
yet for banks or closed-end funds; both are proposals below.

**Method.** `--unscored-research` labels every tradeable backtest observation
by screen group and compares each group with the screen month by month on
median 1Y excess return and win rate, then scans signals inside the
never-scored group with the two-window rule.

**The earlier "never-scored names out-compound" finding does not survive.**
It came from compounded means, which a few rebounds carry. On medians the
group is mixed (-2.2 points against the screen in 2017-21, +5.8 in 2022-26)
and its win rate is lower in both windows. It is really three groups:

| Never scored, 1Y | Names a month | Median vs screen, fit / holdout | Win rate vs screen | Months ahead of screen |
|---|---|---|---|---|
| No fundamentals | 91-121 | -12.3 / -9.4 | -7.9 / -8.4 | 8/53, 3/45 |
| Banks | 85-107 | +1.1 / +10.5 | -1.3 / +4.4 | 28/53, 31/45 |
| Asset management (mostly closed-end funds) | 67-74 | -0.2 / +7.1 | -8.2 / -12.6 | 35/53, 36/45 |
| Other financials | 31-42 | -4.4 / +7.6 | -4.4 / +3.2 | 22/53, 32/45 |

- **Names without fundamentals** did about as badly as v3's bottom quintiles.
  Excluding them is right.
- **Closed-end funds** are income and bond funds priced against equity
  benchmarks: medians near the screen's, but they rarely beat the benchmark.
  They are not stock picks.
- **Banks** matched the screen in 2017-21 and beat it in 2022-26, and move as
  a block (their cheaper half: -34% median in 2019, -22% in 2022, +16% and
  +11% in 2020-21), so including them is partly a sector call. Inside banks,
  earnings yield passes the two-window rule: IC +0.092 (t 3.0) and +0.089
  (t 2.9), right in 7 of 9 years. The cheaper half by earnings yield had a
  median of -4.7 and -6.7 against the screen's -7.1 and -18.0, and win rates
  of 42.3% and 41.3% against 40.5% and 35.3%.

**A trap avoided.** Across the whole never-scored group, low volatility looked
like a strong separator. Inside financials it does nothing (IC -0.02, +0.03);
it was separating calm financials from volatile names without fundamentals.
Signals must be tested inside each part, not across a mixed group.

**Proposals, not yet built.** (1) Label closed-end funds as funds in the
research layer, as illiquid names and duplicates are, so they stop counting
as candidates. (2) A bank lane: tradeable banks in the cheaper half by
earnings yield pass the screen, labelled as such and tracked separately in
forward cohorts. Earnings yield inside banks was found in a scan of five
signals across three subgroups, which the two-window rule only partly
protects against.

**Where.** `src/unscored_research.py`, `--unscored-research RUN_ID`.

## 2026-10-27: Investment funds are labelled; banks get a lane

**Decision.** Both proposals above are adopted. Closed-end funds and business
development companies are labelled `investment_fund` and listed after the
candidates, like illiquid names and duplicates. Tradeable banks that v3
cannot score pass the screen in a lane of their own, `passes_bank_lane`,
when their earnings yield ranks above the 50th percentile among those banks;
the rest are `bank_dearer_half`. Official scores, the v3 lane, and the
defensive test are unchanged.

**How funds are identified.** From EDGAR's own filing record, not names, which
the price provider truncates or replaces with numbers. Registered funds file
shareholder reports and monthly holdings (N-CSR, N-PORT, N-CEN); BDCs elect
their status on N-54A and withdraw it on N-54C; operating companies file
none. A first version counted any fund form ever filed and mislabelled six
companies that used to be funds: NewtekOne and Medallion (now banks), Red
Cat and Sui Group (former BDCs), NexPoint Diversified and Seven Hills (now
REITs). The rule now needs a fund report within 400 days, or a BDC election
never withdrawn. The record comes from the submissions listing the Form 4
supplement already reads, so it adds no requests. Unknown never means fund.
Known edge: Equus (EQS), whose 1992 election is older than EDGAR's recent
list, counts as a company.

**Effect on Oct 19.** 69 names are labelled funds (53 closed-end, 16 BDCs),
all in asset management; real asset managers (Virtus, Silvercrest, GCM
Grosvenor) stay companies. 56 banks pass in the bank lane (median dollar
volume $2.0M a day; cheapest on earnings: Carter Bankshares, OptimumBank,
Northpointe, OP Bancorp) and 55 are in the dearer half. 511 names still pass
the v3 lane and the top 25 is unchanged.

**How the bank lane will be judged, fixed now.** Banks move as a block, so a
single cohort says little; the record is pooled over monthly cohorts from
Nov 2026, at 6M, and judged after 12 cohorts have resolved 6M (around April
2028), alongside the defensive test.

- Keep the lane if the cheaper half's median excess beats the dearer half's
  on average across the 12 cohorts and in at least 7 of them, and the lane's
  median is not below the v3 lane's on average.
- Otherwise retire it: banks return to `not_scored_by_v3`.
- 1Y results are reported alongside; they cannot rescue a failed 6M test.

Each resolution reports the lane, the dearer half, and the v3 lane (median,
win rate), the two spreads, and the rank correlation of earnings-yield
percentile with outcomes inside banks. The cycle report has a "Bank lane"
section. The lane can be switched off with `research_ranking.bank_lane`.

**Where.** `investment_fund_type` in `src/data_sources/sec_form4_source.py`,
`SupplementedInsiderLookup.investment_fund`, `_apply_bank_lane` in
`src/research_ranking.py`, `bank_lane_comparison` in baseline resolution.

## Standing caveats that gate any promotion to "official"

- **Survivorship.** The backtest's universe is chosen point in time from
  companies alive at the source run, so delisted companies are still missing;
  the SEC bounds of 2026-10-23 say they are about a third of the 2016-21
  cross-section and that counting them roughly halves the universe's
  compounded return or worse. Rankings and group comparisons survive the
  bounds; absolute returns and basket-level means do not, and those need a
  delisting-inclusive price vendor.
- **In-sample weights.** v3's weights were set from the same decade they are
  evaluated on. They are coarse and evidence-aligned rather than optimized,
  and year-by-year stability is reassuring, but a true out-of-sample test needs
  future runs and the Moonshot-style forward baseline.
- **Coverage.** Backtested fundamentals cover SEC filers only, about 60% of
  U.S. observations plus verified interlisted Canadian names. Other Canadian
  names receive live-run fundamentals from provider statements with estimated
  filing dates; those were never backtested, so their v3 scores rest on the
  assumption that the same signals behave the same way north of the border.
  Insider signals remain U.S. only; live ones are current through each
  run's date via companies' own Form 4s, the backtest's through the last
  published quarterly data set.
- **Picks versus baskets.** Even v3's top 25 has a negative median and a
  hit rate near 36% among tradeable names, and among them its top fifth is no
  better than its middle. Its evidence supports avoiding its bottom two
  fifths, not conviction in its top names.
- **Splits: fixed.** Share counts are restated into the price basis in every
  path, measured, and reported above. Superseded figures elsewhere in this
  file are left in place as a record of what was believed at the time.
- **Share-class and ADS mismatches: still open in replay.** An ADS that
  bundles ordinary shares leaves no event to read, and a live run catches it
  only by reconciling against the provider's market cap, which historical
  replay does not have. 54 live names are affected, so call it 2% of filers.
  This residual is far less dangerous than the split bug was, because the
  factor is constant per filer rather than correlated with future events: it
  adds cross-sectional noise to those names' levels instead of leaking the
  future. Closing it needs a per-filer unit factor carried in the extract.
- **Means are not trustworthy in-sample.** The universe is companies alive
  at the source run, which inflates every group's compounded mean, and most
  of all the never-scored names that out-compound the rest. v3's filters hold
  up on holdout means and in the forward cohort; v3's evidence is otherwise
  about medians and win rates until delisting-inclusive prices exist.

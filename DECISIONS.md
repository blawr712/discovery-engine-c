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

**A larger finding than any weight.** Compounded at the 1M rebalance and
equal-weighted, the names v3 *excludes* beat the names it scores in both
windows of the backtest: 299% against 183% in 2016-21 and 111% against 42% in
2022-26, with the full universe at 268% and 90%. v3 improves the median and
the win rate of what it picks, and it leaves out the thin right tail of
crashed names that rebound, which is what drives means here. How much of that
tail is real cannot be known from this data. The universe is companies alive
at the Oct 2026 run, so the junk that went to zero is missing and the junk
that rebounded is overrepresented, which inflates exactly the excluded
bucket's mean. The one survivorship-free observation available contradicts the
backtest: in the Sep 18 forward baseline's first month the excluded cohort was
the worst on mean as well as median (-18.61% mean, against -2.84% for
quintile 5). One month settles nothing, but it is the only unbiased evidence
there is.

**Consequence.** The question that most affects pick quality is no longer how
v3 weights its signals but whether its filters are giving up real upside or
only survivorship-inflated upside, and the in-sample data cannot answer it. A
delisting-inclusive price vendor, the last item on the original roadmap, is
now the most valuable data improvement left. Until then v3's claim is about
typical outcomes, medians and win rates, not means.

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
- **Means are not trustworthy in-sample.** v3's excluded names out-compound
  its scored names in the backtest, very likely inflated by survivorship, and
  the only clean forward month says the opposite. v3's evidence is about
  medians and win rates until delisting-inclusive prices exist.

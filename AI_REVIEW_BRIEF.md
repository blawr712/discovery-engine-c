# AI Review: Design Brief

Status: proposed on 2026-10-28 (peer comparison added the same day), not built. This is the starting point for the
session that builds it.

## Goal

Have Claude review the top names that pass the v3 screen by reading their SEC
filings, and give each name a fixed verdict with cited reasons. The scores stay
the guard rails. The AI never adds names, never changes a score or rank, and
never orders the screen.

Why a review and not a picker: the research found nothing that ranks names
inside the screen (DECISIONS.md, 2026-10 screen research). The numbers have
done what they can. What they cannot see is in the filings: one-off gains,
dilution, going-concern doubt, pending takeovers, restatements, and errors in
our own data.

## What runs where

The analysis runs as a **Claude Code skill**, on the user's Claude plan. It
does not use a pay-per-token API key.

The OpenAI synthesis (`--synthesize`, `src/openai_research.py`) is **shelved**.
Leave the code in place and switched off (`research.ai_synthesis_enabled:
false`), and do not build on it. Its brief schema also forbids a verdict field
by design (`validate_synthesis`), so the new review needs its own schema
anyway.

Reuse these existing parts:

| Part | Where | Use |
|---|---|---|
| Research queue | `src/research_ranking.py` | Pick names: the first N with `screen_status == "passes"`, in queue order |
| Packets | `src/research.py` `build_research_packets` | Base of each name's packet, with more fields added (below) |
| SEC filing fetch | `src/evidence.py` `SecEdgarProvider`, `HttpCache` | Fetch filings with hashes, paced and cached |
| SEC pacer | `src/data_sources/sec_pacing.py` | Shared pacing for every SEC request |
| Frozen cohorts | `src/research_baseline.py` | The reviews attach to these; never modify them |
| Resolution | `src/baseline_resolution.py` | Gains an `ai_review_comparison` |

Gaps found in the existing parts:

- **Excerpts start at the top of each filing.** `_html_excerpt` keeps the first
  12,000 characters, which for a 10-K is the cover page and table of contents.
  The review needs whole filings saved as plain text so the skill can find the
  sections it needs. These are Item 1A, Item 7 (MD&A), the going-concern
  language, the debt and equity notes, and subsequent events.
- **Document choice is "the 5 most recent of any listed form",** so a run of
  8-Ks can push out the 10-K. Choose filings by form instead: the latest
  10-K, 20-F or 40-F; the latest 10-Q; and the 8-Ks filed since that 10-Q,
  capped.
- **Packets lack the figures a data check needs.** Add `pit_revenue_ttm`,
  `pit_shares_outstanding`, `pit_shares_basis`, `pit_market_cap`,
  `pit_market_cap_basis`, `pit_split_factor`, `pit_earnings_yield`, the
  margins, `pit_net_cash_to_market_cap`, `screen_status`, `v3_quintile`,
  `score_defensive` and the reporting currency. These live on the run rows,
  not the queue rows.
- **Canadian names without an SEC registrant have no automatic source.**
  SEDAR+ scraping is not allowed (see README). Such names get the
  `not_reviewed` status unless a curated issuer manifest covers them. On Oct
  19 the top 50 were 45 US and 5 Canadian.

## Flow

1. **Prepare (code, new command), for example `--ai-review-prepare RUN_ID --top 50`.**
   - Write each name's packet and its filings as text to
     `data/ai_review/<run_id>/<ticker>/`, plus a manifest of each filing's
     URL, form, filing date and SHA-256.
   - Include only filings dated on or before the run's completion date (the
     cohort's anchor date). This keeps the review blind to anything after
     the cohort froze.
   - Write each name's peer table as `peers.json` (see "Peer comparison"
     below).
   - No AI is involved and nothing is written to the run.
2. **Review (the skill).**
   - One subagent per name, so each judgement starts fresh and is not
     coloured by the previous company.
   - The subagent reads that name's packet and filings, works through the
     checklist, and writes `review.json`.
   - Work in batches (for example 10 per sitting) to spread out plan usage.
     A review that already exists is skipped, so an interrupted batch
     carries on where it stopped.
3. **Validate and freeze (code, new command), for example `--ai-review-freeze RUN_ID`.**
   - Check every review against the schema.
   - Check every quote appears word for word in the filing it cites, matched
     by hash. A quote that is not found fails that name.
   - Write one combined artifact, `ai_reviews_<run_id>.json`, carrying the
     model name, skill version, checklist version and each name's `peers.json`
     hash. Refuse to overwrite it, the same way baselines are never
     overwritten.
   - Check every metric a review cites from the peer table exists in that
     name's `peers.json`.
   - Record it in the run manifest.
4. **Resolve (code, an extension).** `--resolve-baselines` joins the frozen
   reviews to the cohort by ticker and adds an `ai_review_comparison`. The
   baseline file itself is never modified.

## Peer comparison

The question it answers is why a name is cheap. If it trades well below its
peers and its industry's leaders, either its numbers are clearly worse and the
discount is earned (a likely value trap), or its numbers are close to theirs
and the discount looks like a mispricing. **Code picks the peers and computes
every figure. The AI only interprets the table.** It never fetches peer data
or states a peer figure the table does not hold.

**Industry.** Use the SEC's four-digit SIC code, which every SEC filer has,
including companies outside our universe. It is also the same for the backtest
and live runs. `src/data_sources/sec_filings_source.py` already reads `sic` and
`sic_description` from EDGAR submissions. If fewer than 3 peers share the
four-digit code, widen to the three-digit code and record that it was widened.
The provider's `industry` label is shown for reference only.

**Two peer groups:**
- **Closest peers.** Up to 8 names in the same run with the same SIC code,
  nearest to the company by market cap. Include them whatever their screen
  status, so a company is compared with its industry rather than with other
  winners.
- **Industry leaders.** The 5 largest SEC filers in the same SIC code by market
  cap on the anchor date, whether or not they fall inside our universe's size
  band. Finding them needs SIC codes for all SEC filers. Two options, and the
  build session should pick one:
  - SEC's bulk submissions file;
  - per-company submissions for the tickers in `company_tickers.json`, cached
    and paced through the shared SEC pacer. This takes a long first fill, then
    only small monthly updates.

**Metrics.** Computed as of the anchor date with the same `fundamentals_pit`
functions v3 uses, so peer numbers are built exactly like the company's own:
- market cap;
- revenue (trailing twelve months) and revenue growth;
- gross, operating and net margin;
- earnings yield and sales yield;
- net cash to market cap;
- share-count change.

Leaders' prices come from the same cached provider stack. A peer that reports
in another currency is converted with `fundamentals.fx_pairs`, or loses its
valuation ratios when no rate exists, as live runs already do.

**Table shape (`peers.json`).**
- One row per peer, with `group: closest | leader`.
- A summary with the company's value, the closest-peer median, the leader
  median and the company's percentile among its closest peers, for each
  metric.
- The SIC code used, and whether it was widened.
- Every peer that could not be used (no SEC facts, foreign filer, no price),
  with the reason. Missing peers are listed, never guessed.

When fewer than 3 usable peers remain, the table says `insufficient_peers` and
the review records that rather than reaching for a comparison.

## Review schema (per name)

```json
{
  "ticker": "UTMD",
  "verdict": "clear | caution | avoid | not_reviewed",
  "deciding_reason": "one sentence",
  "data_check": [
    {"field": "pit_revenue_ttm", "ours": 0, "filing": 0, "agrees": true,
     "citation": {"sha256": "...", "quote": "exact text from the filing"}}
  ],
  "findings": [
    {"area": "earnings_quality | balance_sheet | dilution | going_concern | pending_event | business | governance",
     "severity": "info | concern | serious",
     "text": "what was found",
     "citation": {"sha256": "...", "quote": "exact text from the filing"}}
  ],
  "peer_comparison": {
    "discount_explained": "yes | partly | no | not_cheaper | insufficient_peers",
    "text": "how the company compares with its closest peers and leaders",
    "metrics_cited": ["pit_operating_margin", "pit_earnings_yield"]
  },
  "bear_case": "strongest case against owning it",
  "unanswered_questions": ["..."],
  "evidence_through": "YYYY-MM-DD"
}
```

These are forbidden: price targets, expected returns, position sizes, ranks,
and any claim with no citation. Interpretation belongs in `deciding_reason`
and `bear_case`. Every finding must be backed by a quote.

## Checklist (fixed and versioned, for example `review-v1`)

1. **Data check.** Do our revenue, share count, market cap and reporting
   currency match the latest filing? Disagreements are logged even when the
   verdict is `clear`.
2. **Earnings quality.** Is the earnings yield, or are the margins, propped up
   by one-off gains, asset sales, tax benefits or capitalised costs?
3. **Balance sheet.** Debt maturities within 18 months, covenant warnings,
   going-concern language.
4. **Dilution.** At-the-market programmes, convertibles, warrants, shelf
   registrations, or a planned share issue.
5. **Pending events.** An agreed acquisition (the price is pinned to the deal),
   a restatement, an auditor change, a delisting or compliance notice, or
   material litigation.
6. **Business.** What it sells, customer concentration, and where margins sit
   in the cycle.
7. **Peers.** Is the company cheaper than its closest peers and its industry's
   leaders? If so, is the discount explained by weaker margins, growth, balance
   sheet or dilution in the peer table, or by findings from the filings? Answer
   `yes`, `partly`, `no`, `not_cheaper` or `insufficient_peers`, citing the
   table's metrics by name.
8. **Bear case.** Write it before the verdict.

Verdict guide:
- `avoid`: at least one `serious` finding that the scores cannot see, or a data
  check showing that the screen passed the name on wrong numbers.
- `caution`: one or more concerns, none serious.
- `clear`: nothing material found.

The peer answer feeds the verdict, but it does not settle it on its own. An
explained discount points toward `caution`. An unexplained one, with no
concerns in the filings, supports `clear`.

## Guard rails

- Only names with `screen_status == "passes"` are reviewed. The bank lane can
  be added later as its own group, but it is not mixed with the v3 lane.
- Evidence is limited to SEC filings, plus any curated issuer manifest, dated on
  or before the anchor date. No web search, news, analyst views or prices after
  the anchor date. This keeps reviews reproducible and blind to what happened
  next. The model's training cutoff is before the cohorts it reviews, so its
  own knowledge cannot leak the outcome.
- Peers are picked and measured by code, as of the anchor date. The AI may cite
  only figures in `peers.json`, never peer numbers from memory.
- One review per name per cohort. The prompt and checklist are fixed for a
  version, and a changed checklist is a new version with a fresh record. No
  re-running to get a different answer.
- Reviews never change the queue, scores, ranks or the dashboard order. The
  dashboard may show a review as a note.
- The SEC contact email lives only in `.env` as `SEC_USER_AGENT`. Read it with
  `load_dotenv`, and never print, log or commit it.

## Forward test (proposed; to be fixed in DECISIONS.md before the first frozen review)

The rule as fixed is in DECISIONS.md (2026-10-28) and overrides this
proposal. After the Oct 19 dry run gave no `clear` verdicts, its primary
measure became `avoid` against `clear` and `caution` together.

An AI cannot be backtested, because it knows how past periods turned out. Only
forward data counts. The proposal mirrors `defensive-v1`:

- **Population:** reviewed names in each monthly cohort from the first frozen
  review onward.
- **Comparison:** the median 6M excess return of `clear` minus that of
  `caution` and `avoid` together, per cohort, pooled with Newey-West (5 lags).
- **No decision before 12 cohorts** have resolved 6M.
- **Use `avoid` as a veto in the screen only if all of these hold:**
  - the pooled spread is above 0 with t ≥ 2;
  - `clear` wins in at least 7 of 12 cohorts;
  - the result holds within the v3 lane on its own.
- **Retire it if the pooled spread is negative.**
- **Reported but not decided on:**
  - `avoid` against the rest;
  - the spread measured inside defensive thirds;
  - names whose discount was `no` (unexplained) against those marked `yes`.
    This is the peer comparison's own hypothesis. It gets a decision rule of
    its own only after enough cohorts show how often each answer occurs.

The data check is useful straight away and needs no forward test. Each
disagreement it finds is a possible bug to trace, the way the four Oct 27 data
gaps were found.

## Open decisions for the user

- The batch size per sitting, and whether to review 50 names monthly or 25.
- Whether to review only names that are new to the top 50, or whose filings
  changed, and reuse the rest. Reuse saves usage but means some verdicts are
  older than their cohort. If reused, a review must carry its own evidence date.
- Whether the first review runs on the Oct 19 cohort as a dry run (not part of
  the test) or waits for the Nov 16 cycle.

## Later, in the engine itself

Industry-relative versions of v3's signals can be backtested with the data
we already hold, once SIC codes are attached to the backtest's names. v3 already
ranks within sector when a sector has 20 or more names
(`minimum_sector_group`). Many industries are smaller than that, and nothing
has yet separated winners inside the screen, so the expected gain is small.
This comes after the review, not before it.

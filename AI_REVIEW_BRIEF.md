# AI Review: Design Brief

Status: proposed on 2026-10-28, not built. This is the starting point for the
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
     model name, skill version and checklist version. Refuse to overwrite it,
     the same way baselines are never overwritten.
   - Record it in the run manifest.
4. **Resolve (code, an extension).** `--resolve-baselines` joins the frozen
   reviews to the cohort by ticker and adds an `ai_review_comparison`. The
   baseline file itself is never modified.

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
7. **Bear case.** Write it before the verdict.

Verdict guide:
- `avoid`: at least one `serious` finding that the scores cannot see, or a data
  check showing that the screen passed the name on wrong numbers.
- `caution`: one or more concerns, none serious.
- `clear`: nothing material found.

## Guard rails

- Only names with `screen_status == "passes"` are reviewed. The bank lane can
  be added later as its own group, but it is not mixed with the v3 lane.
- Evidence is limited to SEC filings, plus any curated issuer manifest, dated on
  or before the anchor date. No web search, news, analyst views or prices after
  the anchor date. This keeps reviews reproducible and blind to what happened
  next. The model's training cutoff is before the cohorts it reviews, so its
  own knowledge cannot leak the outcome.
- One review per name per cohort. The prompt and checklist are fixed for a
  version, and a changed checklist is a new version with a fresh record. No
  re-running to get a different answer.
- Reviews never change the queue, scores, ranks or the dashboard order. The
  dashboard may show a review as a note.
- The SEC contact email lives only in `.env` as `SEC_USER_AGENT`. Read it with
  `load_dotenv`, and never print, log or commit it.

## Forward test (proposed; to be fixed in DECISIONS.md before the first frozen review)

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
- **Reported but not decided on:** `avoid` against the rest, and the spread
  measured inside defensive thirds.

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

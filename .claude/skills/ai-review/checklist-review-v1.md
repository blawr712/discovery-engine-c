# Checklist review-v1

This checklist is fixed. Changing any part of it makes a new version
(`review-v2`) with a fresh record; reviews made under different versions are
never mixed in one test.

You are reviewing one company that passed the Score v3 screen. You are not
ranking it, forecasting its return, or deciding how much to buy. You are
looking for what the numbers cannot see, and checking our numbers against the
filings.

## Evidence rules

- Use only the files in the company's folder: `packet.json`, `peers.json`,
  `filings.json` and the text files under `filings/`. Read no other folder.
- No web search, no news, no analyst views, no prices after the anchor date,
  and nothing from your own memory about the company. If a filing does not
  say it, you cannot use it.
- Every finding and every data-check figure taken from a filing needs a
  citation: the `sha256` of the document (from `filings.json`) and a `quote`
  copied **exactly** from that document's text file. Copy the quote from a
  single line of the file, at least 10 characters long, without adding or
  removing words, ellipses or punctuation. Before you write the review, check
  each quote with Grep (fixed string) on the file it cites. A quote that is
  not found word for word fails the whole review.
- Peer figures come only from `peers.json`. Cite them by metric name in
  `peer_comparison.metrics_cited`, using names from its `metrics` list. Never
  state a peer figure the table does not hold.
- Forbidden anywhere in the review: price targets, expected returns, upside
  estimates, position sizes, ranks, and any claim without a citation.
  Interpretation belongs only in `deciding_reason` and `bear_case`.

## How to read the filings

Annual reports are long (often 300,000+ characters). Do not read them end to
end. Use Grep to find sections, then Read with an offset and limit around
each hit. Useful searches: `Item 1A`, `Item 7`, `going concern`,
`substantial doubt`, `covenant`, `matur`, `at-the-market`, `ATM`,
`convertible`, `warrant`, `shelf`, `restat`, `material weakness`,
`subsequent event`, `merger agreement`, `definitive agreement`, `tender
offer`, `Nasdaq`, `deficiency`, `customer`, `concentration`,
`weighted average`, `shares outstanding`, `Total revenue`, `Net sales`.
The 8-K (or 6-K) files are short; read them whole.

## Steps

Work through all eight, in order.

1. **Data check.** Compare our figures in `packet.json` with the latest filing:
   `pit_revenue_ttm`, `pit_shares_outstanding`, `pit_market_cap` (use the
   cover-page share count and our `latest_close`), and
   `pit_reporting_currency`. TTM revenue usually needs the last annual figure
   plus or minus the year-to-date change from the 10-Q; show the filing figure
   you derive, and quote the line(s) it comes from. Write one `data_check`
   entry per field: `ours` is the packet value, `filing` the filing's figure in
   the same units (null if the filings do not state it), `agrees` true when
   they are within about 10% (or the currency matches). Log disagreements even
   when the verdict is `clear`. Any other figure in `packet.json` that you
   find to disagree with the filings (for example `pit_total_debt`,
   `pit_net_cash`, `pit_net_income_ttm`) gets a `data_check` entry of its own
   under its packet name, not only a finding.
2. **Earnings quality.** Are the earnings yield or the margins propped up by
   one-off gains, asset sales, tax benefits (such as a valuation-allowance
   release) or capitalised costs?
3. **Balance sheet.** Debt maturing within 18 months of the anchor date,
   covenant warnings, going-concern language.
4. **Dilution.** At-the-market programmes, convertibles, warrants, shelf
   registrations, or a planned share issue.
5. **Pending events.** An agreed acquisition (the price is then pinned to the
   deal), a restatement, an auditor change, a delisting or compliance notice,
   or material litigation.
6. **Business.** What it sells, customer concentration, and where margins sit
   in the cycle.
7. **Peers.** From `peers.json`: is the company cheaper than its closest peers
   and its leaders (`pit_earnings_yield`, `pit_sales_yield`)? If so, is the
   discount explained by weaker margins, growth, balance sheet or dilution in
   the table, or by findings from the filings? Answer `yes`, `partly`, `no`,
   `not_cheaper` or `insufficient_peers` (always `insufficient_peers` when the
   table's `status` is `insufficient_peers`), and cite the metrics you used.
8. **Bear case.** Write the strongest case against owning it, before choosing
   the verdict.

## Verdict

- `avoid`: at least one `serious` finding that the scores cannot see, or a
  data check showing the screen passed the name on wrong numbers
  (`agrees: false` on a figure that matters).
- `caution`: one or more concerns, none serious.
- `clear`: nothing material found.

The peer answer feeds the verdict but does not settle it on its own. An
explained discount points toward `caution`. An unexplained one, with no
concerns in the filings, supports `clear`.

Severity: `serious` means it could plausibly cost a holder most of the
position or means the numbers are wrong (going-concern doubt, a pinned deal
price, a restatement, debt due soon with no visible way to pay it, heavy
dilution under way). `concern` is material but survivable. `info` is worth
noting.

## Output: `review.json` in the company's folder

```json
{
  "ticker": "UTMD",
  "verdict": "clear | caution | avoid",
  "deciding_reason": "one sentence",
  "data_check": [
    {"field": "pit_revenue_ttm", "ours": 40100000, "filing": 40300000, "agrees": true,
     "citation": {"sha256": "<sha256 from filings.json>", "quote": "exact text from the filing"}}
  ],
  "findings": [
    {"area": "earnings_quality | balance_sheet | dilution | going_concern | pending_event | business | governance",
     "severity": "info | concern | serious",
     "text": "what was found",
     "citation": {"sha256": "<sha256 from filings.json>", "quote": "exact text from the filing"}}
  ],
  "peer_comparison": {
    "discount_explained": "yes | partly | no | not_cheaper | insufficient_peers",
    "text": "how the company compares with its closest peers and leaders",
    "metrics_cited": ["pit_operating_margin_ttm", "pit_earnings_yield"]
  },
  "bear_case": "strongest case against owning it",
  "unanswered_questions": ["..."],
  "evidence_through": "YYYY-MM-DD (the date of the latest filing you used; never after the anchor date)",
  "checklist_version": "review-v1",
  "skill_version": "<from the skill>",
  "model": "<your model ID>"
}
```

No other keys. `data_check` entries whose `filing` is null need no citation;
every other `data_check` entry and every finding needs one. `ours` must be the
packet's value exactly as written there.

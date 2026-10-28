---
name: ai-review
description: Review the top names passing the v3 screen from their SEC filings, one subagent per company, writing review.json under data/ai_review/RUN_ID/. Use when the user asks to run, continue or dry-run the AI review for a run. Arguments - RUN_ID, then optionally a batch size (default 10).
---

# AI review (skill version ai-review-skill-1, checklist review-v1)

The design is in `AI_REVIEW_BRIEF.md`. Reviews never change scores, ranks,
the queue order or the dashboard order. The forward-test rule in
`DECISIONS.md` governs how they are judged.

## Before starting

1. Arguments: `RUN_ID` and an optional batch size (default 10). If no run ID
   was given, ask for it.
2. Check that `data/ai_review/RUN_ID/selection.json` exists. If not, tell the
   user to run `python main.py --ai-review-prepare RUN_ID --top 50` first, and
   stop.
3. Read `selection.json`. The names to review are those whose `status` is
   `ready` or `no_periodic_report` and whose folder has no `review.json`, in
   the order listed. Names marked `not_reviewed` already have a review written
   by code. Names marked `error` were not prepared: list them for the user and
   skip them.
4. Check `data/exports/` for `ai_reviews_RUN_ID.json`. If it exists the run is
   frozen: stop and say so.

Do not read any company's filings yourself. Each review must start fresh, so
all reading happens in the subagents.

## Each batch

Take the next `batch size` names. For each, launch one `ai-reviewer`
subagent (Agent tool, `subagent_type: "ai-reviewer"`), all of the batch in a
single message so they run in parallel. Give each this prompt, with the
values filled in:

> Review ticker TICKER. Its folder is `data/ai_review/RUN_ID/FOLDER/`.
> Follow the checklist at `.claude/skills/ai-review/checklist-review-v1.md`.
> Skill version: `ai-review-skill-1`. Checklist version: `review-v1`.
> Write `review.json` in that folder and nowhere else.

When the batch has finished:

1. Run `python main.py --ai-review-check RUN_ID` (Bash, from the repository
   root, with the project's virtual environment) and read the summary. It
   validates every review written so far without freezing anything.
2. A review that fails validation is **not** rewritten or re-run: one review
   per name per cohort. Report the failure to the user; it is recorded as
   failed when the run is frozen.
3. Tell the user, in a few lines: names reviewed in this batch, verdict
   counts, data-check disagreements (ticker and field), validation failures,
   and how many names remain.
4. Ask whether to continue with the next batch. Do not start another batch
   without the user's go-ahead.

## Never

- Never write or edit a `review.json` yourself, and never delete one.
- Never re-run a subagent on a name that already has a review.
- Never use web search, news or prices after the anchor date, or pass any
  such material to a subagent.
- Never run `--ai-review-freeze`; freezing is the user's decision.
- Never print, log or commit the SEC contact email in `.env`.

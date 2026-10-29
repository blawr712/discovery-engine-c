"""A readable report of a run's frozen AI reviews.

Built by code from the frozen artifact (``ai_reviews_RUN_ID.json``, or the
dry-run one), so the same artifact always gives the same report. Peer tables
come from each name's ``peers.json`` only when its hash matches the one frozen
with the review; outcomes come from the run's research resolution once a
horizon has resolved. Names appear in queue order, never sorted by verdict, so
the report cannot read as a ranking. Every quote links to its filing on EDGAR.
"""

from __future__ import annotations

import hashlib
from html import escape
import json
from pathlib import Path

from src.ai_review_peers import METRICS as PEER_METRICS

VERDICT_LABELS = {
    "clear": "Clear",
    "caution": "Caution",
    "avoid": "Avoid",
    "not_reviewed": "Not reviewed",
    None: "Failed validation",
}
DISCOUNT_LABELS = {
    "yes": "Discount explained",
    "partly": "Discount partly explained",
    "no": "Discount not explained",
    "not_cheaper": "Not cheaper than peers",
    "insufficient_peers": "Too few peers to compare",
}
METRIC_LABELS = {
    "market_cap_usd": "Market cap (USD)",
    "revenue_ttm_usd": "Revenue, TTM (USD)",
    "pit_revenue_growth_ttm": "Revenue growth",
    "pit_gross_margin_ttm": "Gross margin",
    "pit_operating_margin_ttm": "Operating margin",
    "net_margin_ttm": "Net margin",
    "pit_earnings_yield": "Earnings yield",
    "pit_sales_yield": "Sales yield",
    "pit_net_cash_to_market_cap": "Net cash / market cap",
    "pit_share_change_1y": "Share count change, 1Y",
}
MONEY_METRICS = {"market_cap_usd", "revenue_ttm_usd"}
SEVERITY_ORDER = {"serious": 0, "concern": 1, "info": 2}
SEC_PREFIX = "https://www.sec.gov/"


def report_names(run_id: str, dry_run: bool) -> tuple[str, str]:
    stem = f"ai_review_dry_run_report_{run_id}" if dry_run else f"ai_review_report_{run_id}"
    return f"{stem}.html", f"{stem}.md"


def load_peer_tables(artifact: dict, review_directory: Path) -> dict[str, dict]:
    """Each name's peers.json, kept only when it is the file frozen with the review."""
    tables = {}
    for entry in artifact.get("names") or []:
        expected = entry.get("peers_sha256")
        path = Path(review_directory) / _folder(entry["ticker"]) / "peers.json"
        if not expected or not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            continue
        try:
            tables[str(entry["ticker"])] = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
    return tables


def build_report(
    artifact: dict,
    peer_tables: dict[str, dict] | None = None,
    comparison: dict | None = None,
) -> tuple[str, str]:
    """The report as (HTML, Markdown)."""
    peer_tables = peer_tables or {}
    model = _model(artifact, peer_tables, comparison)
    return _html(model), _markdown(model)


def _model(artifact: dict, peer_tables: dict[str, dict], comparison: dict | None) -> dict:
    names = []
    for position, entry in enumerate(artifact.get("names") or [], start=1):
        review = entry.get("review") or {}
        documents = {
            str(doc.get("sha256")): doc for doc in entry.get("filings") or [] if doc.get("sha256")
        }
        findings = sorted(
            (item for item in review.get("findings") or [] if isinstance(item, dict)),
            key=lambda item: SEVERITY_ORDER.get(item.get("severity"), 3),
        )
        names.append({
            "position": position,
            "ticker": str(entry["ticker"]),
            "verdict": entry.get("verdict"),
            "status": entry.get("status"),
            "errors": entry.get("errors") or [],
            "review": review,
            "findings": findings,
            "documents": documents,
            "peers": peer_tables.get(str(entry["ticker"])),
            "in_baseline": entry.get("in_research_baseline"),
        })
    return {
        "artifact": artifact,
        "names": names,
        "comparison": comparison,
    }


# ---------------------------------------------------------------- HTML


CSS = """
:root { color-scheme: light dark; --bg:#f7f7f4; --card:#ffffff; --ink:#1d2321; --muted:#5f6a66;
  --line:#dfe3df; --clear:#1f7a4d; --clear-bg:#e3f3ea; --caution:#8a6100; --caution-bg:#fbf0d5;
  --avoid:#a2352a; --avoid-bg:#f8e1dd; --none:#5f6a66; --none-bg:#eceeec; --link:#1f5f8b; }
@media (prefers-color-scheme: dark) { :root { --bg:#121615; --card:#1a201e; --ink:#e7ece9;
  --muted:#9aa6a1; --line:#2b3330; --clear:#6fd3a0; --clear-bg:#173226; --caution:#e7c062;
  --caution-bg:#33290f; --avoid:#f08f82; --avoid-bg:#3a1c18; --none:#9aa6a1; --none-bg:#242b29;
  --link:#7fb8e0; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:980px; margin:0 auto; padding:32px 16px 64px; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:19px; margin:36px 0 12px; }
h3 { font-size:16px; margin:0; } a { color:var(--link); }
.subtle { color:var(--muted); font-size:13px; }
.banner { margin:16px 0; padding:10px 14px; border-radius:8px; background:var(--caution-bg); color:var(--caution); font-weight:600; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; margin:16px 0; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
.tile b { display:block; font-size:24px; } .tile span { color:var(--muted); font-size:13px; }
.chip { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:700; white-space:nowrap; }
.chip.clear { color:var(--clear); background:var(--clear-bg); } .chip.caution { color:var(--caution); background:var(--caution-bg); }
.chip.avoid { color:var(--avoid); background:var(--avoid-bg); } .chip.none { color:var(--none); background:var(--none-bg); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; } td.num { text-align:right; font-variant-numeric:tabular-nums; }
.table-wrap { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px; margin:14px 0; }
.card-head { display:flex; flex-wrap:wrap; gap:8px 12px; align-items:baseline; }
.reason { font-size:16px; margin:10px 0 4px; }
.section-label { margin:16px 0 6px; font-size:12px; font-weight:700; letter-spacing:.05em; text-transform:uppercase; color:var(--muted); }
.finding { margin:8px 0; padding-left:12px; border-left:3px solid var(--line); }
.finding.serious { border-color:var(--avoid); } .finding.concern { border-color:var(--caution); }
blockquote { margin:4px 0 0; padding:6px 10px; background:var(--bg); border-radius:6px; font-size:13px; color:var(--muted); }
.disagree { color:var(--avoid); font-weight:600; }
ul { margin:4px 0; padding-left:20px; }
nav.index { display:flex; flex-wrap:wrap; gap:6px; }
nav.index a { text-decoration:none; }
"""


def _html(model: dict) -> str:
    artifact = model["artifact"]
    run_id = escape(str(artifact.get("run_id")))
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>AI review {run_id}</title><style>{CSS}</style></head><body><main>",
        "<h1>AI review of the top screen names</h1>",
        f"<div class='subtle'>Run {run_id} · anchored {escape(str(artifact.get('anchor_datetime', ''))[:10])}"
        f" · frozen {escape(str(artifact.get('frozen_at', ''))[:10])}</div>",
    ]
    if artifact.get("dry_run"):
        parts.append("<div class='banner'>Dry run: not part of the forward test.</div>")
    parts.append(
        "<p>Claude read each company's SEC filings and its peer table, worked through checklist "
        f"<b>{escape(str(artifact.get('checklist_version')))}</b>, and gave one verdict with quoted reasons. "
        "Verdicts never change scores, ranks or the screen's order. Names are listed in queue order.</p>"
    )
    parts.append(_summary_html(model))
    parts.append(_data_checks_html(model))
    parts.append(_outcomes_html(model))
    parts.append("<h2>Companies</h2><nav class='index'>")
    for name in model["names"]:
        parts.append(f"<a href='#{_anchor(name['ticker'])}'>{_chip(name['verdict'], name['ticker'])}</a>")
    parts.append("</nav>")
    for name in model["names"]:
        parts.append(_card_html(name))
    parts.append(_audit_html(model))
    parts.append("</main></body></html>")
    return "\n".join(parts)


def _summary_html(model: dict) -> str:
    counts: dict[str | None, int] = {}
    for name in model["names"]:
        counts[name["verdict"]] = counts.get(name["verdict"], 0) + 1
    tiles = [f"<div class='tile'><b>{len(model['names'])}</b><span>names</span></div>"]
    for verdict in ("clear", "caution", "avoid", "not_reviewed", None):
        if counts.get(verdict):
            tiles.append(f"<div class='tile'><b>{counts[verdict]}</b><span>{VERDICT_LABELS[verdict]}</span></div>")
    disagreements = len(model["artifact"].get("data_check_disagreements") or [])
    tiles.append(f"<div class='tile'><b>{disagreements}</b><span>data-check disagreements</span></div>")
    return "<h2>Summary</h2><div class='tiles'>" + "".join(tiles) + "</div>"


def _data_checks_html(model: dict) -> str:
    rows = model["artifact"].get("data_check_disagreements") or []
    head = (
        "<h2>Data checks to trace</h2><p class='subtle'>Where our figures disagree with the latest "
        "filing. Each is a possible bug in the engine's data, worth tracing whatever the verdict.</p>"
    )
    if not rows:
        return head + "<p>No disagreements.</p>"
    body = "".join(
        f"<tr><td><a href='#{_anchor(row['ticker'])}'>{escape(str(row['ticker']))}</a></td>"
        f"<td>{escape(str(row.get('field')))}</td><td class='num'>{_figure(row.get('ours'))}</td>"
        f"<td class='num'>{_figure(row.get('filing'))}</td></tr>"
        for row in rows
    )
    return head + (
        "<div class='table-wrap'><table><thead><tr><th>Ticker</th><th>Field</th><th>Ours</th>"
        f"<th>Filing</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def _outcomes_html(model: dict) -> str:
    comparison = model["comparison"]
    head = "<h2>Outcomes</h2>"
    rule = (
        "<p class='subtle'>The forward test (DECISIONS.md, 2026-10-28) compares the median 6M excess "
        "return of <i>clear</i> and <i>caution</i> names together with that of <i>avoid</i> names, pooled "
        "over at least 12 monthly cohorts from Nov 16, 2026. A positive spread means the names the review "
        "would veto did worse. One cohort decides nothing.</p>"
    )
    if not comparison or not comparison.get("horizons"):
        return head + rule + "<p>No horizon has resolved yet.</p>"
    rows = []
    for label, measured in comparison["horizons"].items():
        groups = measured.get("by_verdict") or {}
        cells = "".join(
            f"<td class='num'>{_pct(groups.get(v, {}).get('median_excess_percent'))}"
            f" <span class='subtle'>({groups.get(v, {}).get('resolved', 0)})</span></td>"
            for v in ("clear", "caution", "avoid")
        )
        rows.append(
            f"<tr><td>{escape(label)}</td>{cells}"
            f"<td class='num'>{_pts(measured.get('rest_minus_avoid_median_percent'))}</td></tr>"
        )
    return head + rule + (
        "<div class='table-wrap'><table><thead><tr><th>Horizon</th><th>Clear</th><th>Caution</th>"
        "<th>Avoid</th><th>Others minus avoid</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>"
        "<p class='subtle'>Median excess return, with the number of resolved names in brackets.</p>"
    )


def _card_html(name: dict) -> str:
    review = name["review"]
    ticker = escape(name["ticker"])
    parts = [f"<section class='card' id='{_anchor(name['ticker'])}'><div class='card-head'>",
             f"<h3>{name['position']}. {ticker}</h3>{_chip(name['verdict'])}"]
    if name["in_baseline"] is False:
        parts.append("<span class='subtle'>not in the frozen cohort</span>")
    parts.append("</div>")
    if name["verdict"] is None:
        parts.append("<p class='reason'>This review failed validation and is not counted.</p><ul>")
        parts.extend(f"<li>{escape(str(error))}</li>" for error in name["errors"])
        parts.append("</ul></section>")
        return "".join(parts)
    parts.append(f"<p class='reason'>{escape(str(review.get('deciding_reason', '')))}</p>")
    if name["verdict"] == "not_reviewed":
        parts.append("</section>")
        return "".join(parts)
    if name["findings"]:
        parts.append("<div class='section-label'>Findings</div>")
        for item in name["findings"]:
            severity = escape(str(item.get("severity")))
            parts.append(
                f"<div class='finding {severity}'><b>{severity.capitalize()}</b> · "
                f"{escape(str(item.get('area', '')).replace('_', ' '))}: {escape(str(item.get('text', '')))}"
                f"{_quote_html(item.get('citation'), name['documents'])}</div>"
            )
    checks = [item for item in review.get("data_check") or [] if isinstance(item, dict)]
    if checks:
        parts.append("<div class='section-label'>Data check</div><div class='table-wrap'><table>"
                     "<thead><tr><th>Field</th><th>Ours</th><th>Filing</th><th></th></tr></thead><tbody>")
        for item in checks:
            agrees = item.get("agrees")
            parts.append(
                f"<tr><td>{escape(str(item.get('field')))}</td><td class='num'>{_figure(item.get('ours'))}</td>"
                f"<td class='num'>{_figure(item.get('filing'))}</td>"
                f"<td>{'agrees' if agrees else '<span class=disagree>disagrees</span>'}</td></tr>"
            )
        parts.append("</tbody></table></div>")
    comparison = review.get("peer_comparison") or {}
    parts.append("<div class='section-label'>Peers</div>")
    parts.append(
        f"<p><b>{escape(DISCOUNT_LABELS.get(comparison.get('discount_explained'), str(comparison.get('discount_explained'))))}.</b> "
        f"{escape(str(comparison.get('text', '')))}</p>"
    )
    parts.append(_peer_table_html(name["peers"], comparison.get("metrics_cited") or []))
    parts.append(f"<div class='section-label'>Bear case</div><p>{escape(str(review.get('bear_case', '')))}</p>")
    questions = [q for q in review.get("unanswered_questions") or [] if isinstance(q, str)]
    if questions:
        parts.append("<div class='section-label'>Open questions</div><ul>")
        parts.extend(f"<li>{escape(question)}</li>" for question in questions)
        parts.append("</ul>")
    parts.append(f"<p class='subtle'>Evidence through {escape(str(review.get('evidence_through', '')))}.</p></section>")
    return "".join(parts)


def _peer_table_html(peers: dict | None, cited: list[str]) -> str:
    if not peers:
        return "<p class='subtle'>Peer table not available.</p>"
    summary = peers.get("summary") or {}
    if not summary:
        return f"<p class='subtle'>{escape(str(peers.get('status_reason') or 'No peer comparison.'))}</p>"
    sic = escape(str(peers.get("sic_used") or ""))
    note = f"SIC {sic}" + (" (widened to three digits)" if peers.get("widened") else "")
    rows = []
    for metric in PEER_METRICS:
        values = summary.get(metric) or {}
        mark = " ●" if metric in cited else ""
        rows.append(
            f"<tr><td>{escape(METRIC_LABELS.get(metric, metric))}{mark}</td>"
            f"<td class='num'>{_metric(metric, values.get('company'))}</td>"
            f"<td class='num'>{_metric(metric, values.get('closest_median'))}</td>"
            f"<td class='num'>{_metric(metric, values.get('leader_median'))}</td>"
            f"<td class='num'>{_percentile(values.get('company_percentile_among_closest'))}</td></tr>"
        )
    closest = [p["ticker"] for p in peers.get("peers") or [] if p.get("group") == "closest"]
    leaders = [p["ticker"] for p in peers.get("peers") or [] if p.get("group") == "leader"]
    return (
        "<div class='table-wrap'><table><thead><tr><th>Metric</th><th>Company</th><th>Closest peers (median)</th>"
        "<th>Leaders (median)</th><th>Percentile among closest</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>"
        f"<p class='subtle'>{note}. Closest: {escape(', '.join(map(str, closest)) or 'none')}. "
        f"Leaders: {escape(', '.join(map(str, leaders)) or 'none')}. ● cited by the review.</p>"
    )


def _quote_html(citation: object, documents: dict) -> str:
    if not isinstance(citation, dict):
        return ""
    doc = documents.get(str(citation.get("sha256"))) or {}
    source = f"{doc.get('form', 'filing')} filed {doc.get('filing_date', '?')}"
    url = str(doc.get("url") or "")
    link = (
        f" <a href='{escape(url, quote=True)}' rel='noopener noreferrer'>{escape(source)}</a>"
        if url.startswith(SEC_PREFIX) else f" {escape(source)}"
    )
    return f"<blockquote>“{escape(str(citation.get('quote', '')))}” —{link}</blockquote>"


def _audit_html(model: dict) -> str:
    artifact = model["artifact"]
    failed = [name for name in model["names"] if name["verdict"] is None]
    rows = [
        ("Models", ", ".join(artifact.get("models") or []) or "not recorded"),
        ("Checklist", artifact.get("checklist_version")),
        ("Skill", artifact.get("skill_version")),
        ("Prepared with", f"{artifact.get('prepare_version')}, {artifact.get('peers_version')}"),
        ("Artifact SHA-256", artifact.get("artifact_sha256")),
        ("Failed validation", ", ".join(name["ticker"] for name in failed) or "none"),
    ]
    body = "".join(f"<tr><th>{escape(k)}</th><td>{escape(str(v))}</td></tr>" for k, v in rows)
    return f"<h2>Audit</h2><div class='table-wrap'><table><tbody>{body}</tbody></table></div>"


def _chip(verdict: str | None, label: str | None = None) -> str:
    css = {"clear": "clear", "caution": "caution", "avoid": "avoid"}.get(verdict, "none")
    return f"<span class='chip {css}'>{escape(label or VERDICT_LABELS.get(verdict, str(verdict)))}</span>"


# ---------------------------------------------------------------- Markdown


def _markdown(model: dict) -> str:
    artifact = model["artifact"]
    lines = [
        f"# AI review: run {artifact.get('run_id')}",
        "",
        f"Anchored {str(artifact.get('anchor_datetime', ''))[:10]}, frozen {str(artifact.get('frozen_at', ''))[:10]}, "
        f"checklist {artifact.get('checklist_version')}. Names in queue order; verdicts never change scores or ranks.",
    ]
    if artifact.get("dry_run"):
        lines += ["", "**Dry run: not part of the forward test.**"]
    counts: dict[str | None, int] = {}
    for name in model["names"]:
        counts[name["verdict"]] = counts.get(name["verdict"], 0) + 1
    lines += ["", "## Summary", ""]
    lines += [f"- {VERDICT_LABELS[v]}: {counts[v]}" for v in ("clear", "caution", "avoid", "not_reviewed", None) if counts.get(v)]
    lines += ["", "## Data checks to trace", ""]
    disagreements = artifact.get("data_check_disagreements") or []
    if disagreements:
        lines += ["| Ticker | Field | Ours | Filing |", "|---|---|---:|---:|"]
        lines += [
            f"| {row['ticker']} | {row.get('field')} | {_figure(row.get('ours'))} | {_figure(row.get('filing'))} |"
            for row in disagreements
        ]
    else:
        lines.append("No disagreements.")
    lines += ["", "## Companies", ""]
    for name in model["names"]:
        review = name["review"]
        lines.append(f"### {name['position']}. {name['ticker']}: {VERDICT_LABELS.get(name['verdict'])}")
        lines.append("")
        if name["verdict"] is None:
            lines += [f"- {error}" for error in name["errors"]] + [""]
            continue
        lines += [_md(review.get("deciding_reason", "")), ""]
        if name["verdict"] == "not_reviewed":
            continue
        for item in name["findings"]:
            citation = item.get("citation") or {}
            doc = name["documents"].get(str(citation.get("sha256"))) or {}
            lines.append(
                f"- **{item.get('severity')}** ({str(item.get('area', '')).replace('_', ' ')}): {_md(item.get('text', ''))} "
                f"> \"{_md(citation.get('quote', ''))}\" ({doc.get('form', 'filing')} {doc.get('filing_date', '')})"
            )
        comparison = review.get("peer_comparison") or {}
        lines += [
            "",
            f"Peers: {DISCOUNT_LABELS.get(comparison.get('discount_explained'), comparison.get('discount_explained'))}. "
            f"{_md(comparison.get('text', ''))}",
            "",
            f"Bear case: {_md(review.get('bear_case', ''))}",
            "",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- formatting


def _anchor(ticker: str) -> str:
    return "name-" + "".join(ch if ch.isalnum() else "-" for ch in str(ticker))


def _folder(ticker: str) -> str:
    from src.ai_review import safe_name

    return safe_name(ticker)


def _figure(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return "—" if value is None else str(value)
    if isinstance(value, (int, float)):
        magnitude = abs(float(value))
        if magnitude >= 1e9:
            return f"{value / 1e9:,.2f}B"
        if magnitude >= 1e6:
            return f"{value / 1e6:,.2f}M"
        if magnitude >= 1e3:
            return f"{value:,.0f}"
        return f"{value:,.4g}"
    return escape(str(value))


def _metric(metric: str, value: object) -> str:
    if value is None:
        return "—"
    if metric in MONEY_METRICS:
        return "$" + _figure(value)
    return f"{float(value) * 100:,.1f}%"


def _pct(value: object) -> str:
    return "—" if value is None else f"{float(value):+.1f}%"


def _pts(value: object) -> str:
    return "—" if value is None else f"{float(value):+.1f} pts"


def _percentile(value: object) -> str:
    return "—" if value is None else f"{float(value):.0f}"


def _md(value: object) -> str:
    return " ".join(str(value or "").split()).replace("|", "\\|")

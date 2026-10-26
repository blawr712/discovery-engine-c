"""One research candidate per company, however many listings it has.

A Canadian company interlisted in the United States can appear twice, once
per exchange: High Tide as HITI and HITI.V, Tilray as TLRY and TLRY.TO. Both
listings carry the same fundamentals, so the company is counted twice in
every cross-section it enters and takes two places in the research queue and
in frozen cohorts, where it would weigh twice in the results.

A Canadian listing is a duplicate of a U.S. row in the same run when it is
flagged interlisted, the U.S. ticker is its root symbol, and the two company
names agree by the rule the SEC mapping uses. The name check is what keeps a
shared root from merging different companies: VHI.TO is VitalHub, VHI is
Valhi; LOVE.TO is Cannara Biotech, LOVE is Lovesac.

The listing kept is the U.S. one if it clears the liquidity floor, otherwise
the Canadian one if that does, otherwise the U.S. one.
"""

from __future__ import annotations

from src.liquidity import liquidity_eligible
from src.sec_mapping import names_match


def duplicate_listings(rows: list[dict]) -> dict[str, str]:
    """Secondary listing ticker -> the ticker kept for the same company."""
    successful = [row for row in rows if row.get("status") in (None, "OK")]
    us = {
        str(row.get("ticker") or "").upper(): row
        for row in successful
        if str(row.get("country") or "").upper() == "US" and row.get("ticker")
    }
    secondary: dict[str, str] = {}
    for row in successful:
        if str(row.get("country") or "").upper() != "CA" or not row.get("universe_interlisted"):
            continue
        ticker = str(row.get("ticker") or "").upper()
        root = str(row.get("universe_root_ticker") or ticker.split(".")[0]).strip().upper()
        for candidate in dict.fromkeys((root, root.replace(".", "-"))):
            partner = us.get(candidate)
            if partner is None:
                continue
            name = str(row.get("universe_company_name") or row.get("company_name") or "")
            if not names_match(name, str(partner.get("company_name") or "")):
                break
            if liquidity_eligible(partner) or not liquidity_eligible(row):
                secondary[ticker] = candidate
            else:
                secondary[candidate] = ticker
            break
    return secondary

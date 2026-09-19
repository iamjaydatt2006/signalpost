"""Evidence-grounded company synthesis.

Produces a concise, factual company summary **only** from published (``available``)
claims. Every sentence is tied to the evidence ids that support it. There is no
generative filler: if a fact is not supported by a claim, it is not stated. This is
deterministic string assembly, not an LLM — the evidence system is the source of
truth.
"""
from __future__ import annotations

from typing import Any


def _by_field(claims: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index available claims by field (first available wins)."""
    index: dict[str, dict[str, Any]] = {}
    for claim in claims:
        if claim.get("availability") != "available":
            continue
        field = str(claim.get("field"))
        if field not in index:
            index[field] = claim
    return index


def _fmt_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{int(number):,}".replace(",", " ")
    return f"{number:,.2f}".replace(",", " ")


def synthesize_summary(
    profile: dict[str, Any],
    claims: list[dict[str, Any]],
    *,
    company_name: str | None = None,
) -> dict[str, Any]:
    """Return ``{"summary": str, "sentences": [{text, evidence_ids}], "fact_count": int}``.

    ``claims`` should be the OUTPUT_CONTRACT claims produced by ``claims.extract_claims``.
    """
    by_field = _by_field(claims)
    name = company_name or profile.get("name") or (by_field.get("legal_name") or {}).get("value") or "This entity"
    sentences: list[dict[str, Any]] = []

    def add(text: str, *fields: str) -> None:
        evidence_ids: list[str] = []
        for field in fields:
            claim = by_field.get(field)
            if claim:
                evidence_ids.extend(claim.get("evidence_ids") or [])
        # A sentence is only emitted if at least one supporting field is available.
        if not any(field in by_field for field in fields):
            return
        deduped = list(dict.fromkeys(evidence_ids))
        sentences.append({"text": text, "evidence_ids": deduped})

    # 1. What the company is (identity + form + industry).
    identity_bits = []
    form = (by_field.get("legal_form") or {}).get("value")
    industry = (by_field.get("industry_label") or {}).get("value")
    status = (by_field.get("registration_status") or {}).get("value")
    lead = f"{name} is a registered Norwegian entity"
    if form:
        lead += f" of legal form {form}"
    if industry:
        lead += f", operating in {industry}"
    lead += "."
    if "legal_name" in by_field or "legal_form" in by_field or "industry_label" in by_field:
        add(lead, "legal_name", "legal_form", "industry_label")

    if status and status != "active":
        add(f"The registry records the entity as {status}.", "registration_status")

    # 2. Location.
    municipality = (by_field.get("municipality") or {}).get("value")
    if municipality:
        add(f"It is registered in the municipality of {municipality}.", "municipality")

    # 3. Size / workforce (only when supported; 0 is a real, stated value).
    emp_claim = by_field.get("registered_employees")
    if emp_claim is not None:
        emp = emp_claim.get("value")
        add(f"The registry reports {_fmt_number(emp)} employees.", "registered_employees")

    # 4. Financials (with the reporting period when known).
    revenue = by_field.get("revenue")
    result = by_field.get("annual_result")
    if revenue is not None:
        period = revenue.get("financial_period")
        suffix = f" for {period}" if period else ""
        add(f"Latest reported revenue is NOK {_fmt_number(revenue.get('value'))}{suffix}.", "revenue")
    if result is not None:
        add(f"The latest reported annual result is NOK {_fmt_number(result.get('value'))}.", "annual_result")

    # 5. Roles / leadership.
    roles = by_field.get("role_holders")
    if roles is not None and isinstance(roles.get("value"), list):
        leaders = [str(p.get("name")) for p in roles["value"] if p.get("name")][:3]
        if leaders:
            add(f"Registered role holders include {', '.join(leaders)}.", "role_holders")

    # 6. Website / online presence (verified only).
    site = by_field.get("verified_website")
    if site is not None and site.get("value"):
        add(f"Its verified official website is {site.get('value')}.", "verified_website")
    social_fields = [f for f in by_field if f.startswith("social_profile_")]
    if social_fields:
        platforms = sorted(f.removeprefix("social_profile_") for f in social_fields)
        add(f"Verified social profiles were found on {', '.join(platforms)}.", *social_fields)

    # 7. Locations / subunits.
    subunits = by_field.get("registered_subunits")
    if subunits is not None and isinstance(subunits.get("value"), list) and subunits["value"]:
        add(f"It has {len(subunits['value'])} registered subunit(s).", "registered_subunits")

    # 8. External signals (dated activity / reviews / jobs), verified only.
    for field in sorted(by_field):
        if field.startswith("external_"):
            add(f"An independently verified external signal was recorded ({field.removeprefix('external_')}).", field)

    summary = " ".join(sentence["text"] for sentence in sentences)
    return {
        "summary": summary,
        "sentences": sentences,
        "fact_count": len(sentences),
        "policy": "Every sentence is derived only from available, evidence-linked claims; unsupported statements are omitted.",
    }

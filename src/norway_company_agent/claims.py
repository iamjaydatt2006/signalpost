"""Deterministic OUTPUT_CONTRACT claim and evidence extraction.

This module turns an internal enrichment ``profile`` (BRREG modules + website +
verified external observations) into the two lists the official contract requires:

- ``evidence``: ``{id, source_url, source_class, retrieved_at, content_sha256, claim_span}``
- ``claims``:   ``{field, value, availability, confidence, evidence_ids}``

Design rules (all enforced deterministically, never by an LLM):

- A *missing* source is ``not_available`` — never ``0``. A legitimately zero
  numeric value from an *available* source is preserved as ``0``.
- Every published claim references at least one evidence id that actually exists.
- Availability is derived from evidence status; there is no guessing.
- Website-derived claims are ``ambiguous`` unless the identity gate verified the
  exact legal entity; verified social links only appear when both the site and the
  handle pass identity.
- Conflicts between the bulk registry row and the live registry API are recorded,
  and the more authoritative/current source wins the single canonical value.
"""
from __future__ import annotations

from typing import Any

from .source_policy import source_precedence_rank

# Availability states permitted by OUTPUT_CONTRACT.md.
AVAILABLE = "available"
NOT_AVAILABLE = "not_available"
BLOCKED = "blocked"
NOT_APPLICABLE = "not_applicable"
AMBIGUOUS = "ambiguous"
FAILED = "failed"

# Map internal evidence status -> contract availability.
_STATUS_TO_AVAILABILITY = {
    "available": AVAILABLE,
    "not_found": NOT_AVAILABLE,
    "not_fetched": NOT_AVAILABLE,
    "not_applicable": NOT_APPLICABLE,
    "blocked": BLOCKED,
    "source_error": FAILED,
}

# Deterministic confidence by source authority. Used only when a claim is available.
_CONFIDENCE = {
    "official_registry_bulk": 0.99,
    "official_registry_live": 0.99,
    "official_annual_accounts": 0.98,
    "official_annual_account_copies": 0.97,
    "official_roles": 0.97,
    "official_group_structure": 0.97,
    "official_subunits": 0.97,
    "official_rule_interpretation": 0.9,
    "registry_linked_company_website": 0.9,
    "registry_linked_company_website_scrapy": 0.9,
    "company_linked_social_profile": 0.9,
    "company_reported_claim": 0.85,
}


def _availability_for(record: dict[str, Any] | None) -> str:
    if not record:
        return NOT_AVAILABLE
    return _STATUS_TO_AVAILABILITY.get(str(record.get("status")), FAILED)


def _confidence_for(source_class: str | None, availability: str) -> float | None:
    if availability != AVAILABLE:
        return None
    return _CONFIDENCE.get(str(source_class or ""), 0.75)


def _evidence_id(module: str) -> str:
    return f"ev-{module}"


def _claim_span(record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    value = record.get("value")
    if isinstance(value, dict):
        for key in ("identity_text_excerpt", "title", "reason", "classification", "period"):
            if value.get(key):
                return str(value[key])[:280]
    if record.get("source_row_key"):
        return f"registry row key {record['source_row_key']}"
    if record.get("note"):
        return str(record["note"])[:280]
    return None


def build_evidence_records(profile: dict[str, Any], observations: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Build the contract ``evidence`` list and a module->evidence_id index.

    Only modules that actually produced a record (any status) get an evidence entry,
    so a claim can always cite the real source that was consulted.
    """
    records = profile.get("evidence", {}) or {}
    evidence_list: list[dict[str, Any]] = []
    index: dict[str, str] = {}
    for module in sorted(records):
        record = records[module]
        if not isinstance(record, dict):
            continue
        eid = _evidence_id(module)
        index[module] = eid
        evidence_list.append(
            {
                "id": eid,
                "source_url": record.get("source_url"),
                "source_class": record.get("source_class") or record.get("source_type"),
                "retrieved_at": record.get("retrieved_at"),
                "content_sha256": record.get("content_sha256"),
                "claim_span": _claim_span(record),
            }
        )
    for observation in observations or []:
        oid = str(observation.get("id") or "")
        if not oid:
            continue
        index[f"observation:{oid}"] = oid
        evidence_list.append(
            {
                "id": oid,
                "source_url": observation.get("source_url"),
                "source_class": observation.get("source_class") or observation.get("platform"),
                "retrieved_at": observation.get("retrieved_at"),
                "content_sha256": observation.get("content_sha256"),
                "claim_span": observation.get("evidence_span"),
            }
        )
    return evidence_list, index


def _registry_records(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    records = profile.get("evidence", {}) or {}
    return records.get("registry", {}) or {}, records.get("registry_live", {}) or {}


def _registry_value(profile: dict[str, Any], field: str) -> tuple[Any, str, list[str], dict[str, Any] | None]:
    """Return (value, source_class, evidence_ids, conflict) for a registry field.

    The live API wins over the bulk snapshot when both are available and differ,
    but both evidence ids are cited and the disagreement is recorded.
    """
    bulk, live = _registry_records(profile)
    live_value = None
    if live.get("status") == "available":
        live_value = (live.get("value") or {}).get(field)
    bulk_value = profile.get(field)

    evidence_ids: list[str] = []
    conflict: dict[str, Any] | None = None
    if bulk:
        evidence_ids.append(_evidence_id("registry"))
    if live.get("status") == "available":
        evidence_ids.append(_evidence_id("registry_live"))

    if live_value is not None and bulk_value is not None and live_value != bulk_value:
        conflict = {
            "field": field,
            "values": [
                {"source_class": "official_registry_live", "value": live_value},
                {"source_class": "official_registry_bulk", "value": bulk_value},
            ],
            "resolution": "official_registry_live",
        }
        return live_value, "official_registry_live", evidence_ids, conflict
    if live_value is not None:
        return live_value, "official_registry_live", evidence_ids, None
    return bulk_value, "official_registry_bulk", ([_evidence_id("registry")] if bulk else []), None


def _claim(field: str, value: Any, availability: str, source_class: str | None, evidence_ids: list[str]) -> dict[str, Any]:
    return {
        "field": field,
        "value": value if availability == AVAILABLE else None,
        "availability": availability,
        "confidence": _confidence_for(source_class, availability),
        "evidence_ids": [e for e in evidence_ids if e],
    }


def extract_claims(
    profile: dict[str, Any],
    *,
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return ``{"claims": [...], "evidence": [...], "conflicts": [...]}``.

    ``observations`` must already be resolved to ``verified`` before being passed
    in; this function does not re-run identity, it only publishes what it is given.
    """
    records = profile.get("evidence", {}) or {}
    evidence_list, _index = build_evidence_records(profile, observations)
    claims: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def emit(field: str, value: Any, availability: str, source_class: str | None, evidence_ids: list[str]) -> None:
        key = (field, str(value))
        if key in seen:
            return
        seen.add(key)
        claims.append(_claim(field, value, availability, source_class, evidence_ids))

    # -- registry-anchored identity claims --------------------------------
    bulk, live = _registry_records(profile)
    registry_availability = AVAILABLE if (bulk or live.get("status") == "available") else NOT_AVAILABLE

    for field, contract_field in (
        ("name", "legal_name"),
        ("legal_form", "legal_form"),
        ("municipality", "municipality"),
        ("industry_code", "industry_code"),
        ("industry_label", "industry_label"),
    ):
        value, source_class, ev_ids, conflict = _registry_value(profile, field)
        availability = AVAILABLE if value not in (None, "") else NOT_AVAILABLE
        emit(contract_field, value, availability, source_class, ev_ids)
        if conflict:
            conflicts.append(conflict)

    # Employees: 0 is a real value; None means the registry did not report a count.
    emp_value, emp_source, emp_ev, emp_conflict = _registry_value(profile, "employees")
    emp_availability = AVAILABLE if emp_value is not None else NOT_AVAILABLE
    emit("registered_employees", emp_value, emp_availability, emp_source, emp_ev)
    if emp_conflict:
        conflicts.append(emp_conflict)

    # Registered website URL (registry-declared, distinct from a verified site).
    web_value, web_source, web_ev, _ = _registry_value(profile, "website")
    emit("registered_website", web_value or None, AVAILABLE if web_value else NOT_AVAILABLE, web_source, web_ev)

    # Registration status.
    bankrupt = profile.get("bankrupt")
    liquidating = profile.get("liquidating")
    status_value = "bankrupt" if bankrupt else "liquidating" if liquidating else "active"
    emit(
        "registration_status",
        status_value,
        registry_availability,
        "official_registry_bulk",
        [_evidence_id("registry")] if bulk else [],
    )

    # -- accounting obligation --------------------------------------------
    obligation = records.get("accounting_obligation", {})
    if obligation:
        emit(
            "accounting_obligation",
            (obligation.get("value") or {}).get("classification"),
            _availability_for(obligation),
            obligation.get("source_class"),
            [_evidence_id("accounting_obligation")],
        )

    # -- financials (missing != zero) -------------------------------------
    financials = records.get("financials")
    fin_availability = _availability_for(financials)
    fin_ev = [_evidence_id("financials")] if financials else []
    fin_source = (financials or {}).get("source_class")
    fin_records = ((financials or {}).get("value") or {}).get("records") or []
    latest = fin_records[0] if fin_records else {}
    period = latest.get("period")
    for contract_field, key in (
        ("revenue", "revenue"),
        ("operating_result", "operating_result"),
        ("profit_before_tax", "profit_before_tax"),
        ("annual_result", "annual_result"),
        ("total_assets", "assets"),
        ("equity", "equity"),
        ("debt", "debt"),
    ):
        raw = latest.get(key)
        if fin_availability == AVAILABLE and raw is not None:
            claim = _claim(contract_field, raw, AVAILABLE, fin_source, fin_ev)
            claim["financial_period"] = period
            claims.append(claim)
            seen.add((contract_field, str(raw)))
        else:
            # Source unavailable OR the field was absent from an available filing.
            emit(contract_field, None, fin_availability if fin_availability != AVAILABLE else NOT_AVAILABLE, fin_source, fin_ev)

    # -- financial history (available filing years) ------------------------
    history = records.get("financial_history")
    if history is not None:
        years = ((history.get("value") or {}).get("years")) or []
        emit(
            "available_filing_years",
            years or None,
            _availability_for(history) if years else NOT_AVAILABLE,
            history.get("source_class"),
            [_evidence_id("financial_history")],
        )

    # -- roles -------------------------------------------------------------
    roles = records.get("roles")
    if roles is not None:
        people = [item for item in ((roles.get("value") or {}).get("roles") or []) if not item.get("inactive")]
        role_value = [{"name": p.get("name"), "role": p.get("role") or p.get("group")} for p in people] or None
        emit(
            "role_holders",
            role_value,
            _availability_for(roles) if role_value else NOT_AVAILABLE,
            roles.get("source_class"),
            [_evidence_id("roles")],
        )

    # -- group -------------------------------------------------------------
    group = records.get("group")
    if group is not None:
        emit(
            "group_relationships",
            group.get("value") if group.get("status") == "available" else None,
            _availability_for(group),
            group.get("source_class"),
            [_evidence_id("group")],
        )

    # -- locations / subunits ---------------------------------------------
    locations = records.get("locations")
    if locations is not None:
        loc_avail = _availability_for(locations)
        items = ((locations.get("value") or {}).get("locations")) or []
        loc_ev = [_evidence_id("locations")]
        if loc_avail == AVAILABLE:
            # A checked source returning zero subunits is a real, available value
            # (count 0) — distinct from "not checked". Never emit an available claim
            # with an empty value: the list claim is only published when non-empty.
            emit("registered_subunit_count", len(items), AVAILABLE, locations.get("source_class"), loc_ev)
            if items:
                emit("registered_subunits", items, AVAILABLE, locations.get("source_class"), loc_ev)
        else:
            emit("registered_subunit_count", None, loc_avail, locations.get("source_class"), loc_ev)

    # -- website (only publish identity-verified claims) -------------------
    website = records.get("website")
    if website is not None:
        web_avail = _availability_for(website)
        value = website.get("value") or {}
        assessment = value.get("identity_assessment") or {}
        verified = bool(assessment.get("publishable"))
        web_ev_id = [_evidence_id("website")]
        if web_avail == AVAILABLE and not verified:
            # Fetched but exact entity not established: everything from it is ambiguous.
            emit("verified_website", value.get("final_url"), AMBIGUOUS, website.get("source_class"), web_ev_id)
        elif web_avail == AVAILABLE and verified:
            emit("verified_website", value.get("final_url"), AVAILABLE, website.get("source_class"), web_ev_id)
            if value.get("title"):
                emit("website_title", value.get("title"), AVAILABLE, "company_reported_claim", web_ev_id)
            if value.get("description"):
                emit("website_description", value.get("description"), AVAILABLE, "company_reported_claim", web_ev_id)
            for link in value.get("social_links") or []:
                emit(
                    f"social_profile_{link.get('platform')}",
                    link.get("url"),
                    AVAILABLE,
                    "company_linked_social_profile",
                    web_ev_id,
                )
        else:
            emit("verified_website", None, web_avail, website.get("source_class"), web_ev_id)

    # -- verified external observations -----------------------------------
    for observation in observations or []:
        oid = str(observation.get("id") or "")
        if not oid:
            continue
        signal = str(observation.get("signal_type") or "external_signal")
        claim = _claim(
            f"external_{signal}",
            {k: observation.get(k) for k in ("platform", "signal_type", "metrics", "sentiment_label") if observation.get(k) is not None},
            AVAILABLE,
            observation.get("source_class"),
            [oid],
        )
        claims.append(claim)

    return {"claims": claims, "evidence": evidence_list, "conflicts": conflicts}

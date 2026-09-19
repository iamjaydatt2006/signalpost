from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any


# Query parameters that never represent a material change to a company fact.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "_ga", "ref", "ref_src", "igshid",
}


def _canonical_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    if not parsed.scheme and not parsed.netloc:
        return re.sub(r"\s+", " ", value.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if k.lower() not in _TRACKING_PARAMS]
    query = urllib.parse.urlencode(sorted(kept))
    return urllib.parse.urlunparse((scheme, netloc, path, "", query, ""))


def _looks_like_url(value: str) -> bool:
    return bool(re.match(r"^\s*https?://", value, re.I)) or value.strip().startswith("www.")


def canonicalize(field: str, value: Any) -> Any:
    """Return a comparison-stable form of a tracked value.

    Canonicalization removes *non-material* differences — surrounding/interior
    whitespace, URL tracking parameters, ``www.`` and trailing-slash variants, and
    the ordering of set-like link collections — without collapsing real value
    changes. It is used only to decide equality; emitted change events still carry
    the original raw ``old_value`` / ``new_value``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if field.endswith("website") or field.endswith("social_links") or _looks_like_url(stripped):
            return _canonical_url(stripped)
        return re.sub(r"\s+", " ", stripped)
    if isinstance(value, dict):
        # Canonicalize social-link dicts by their url/platform.
        return {key: canonicalize(f"{field}.{key}", value[key]) for key in sorted(value)}
    if isinstance(value, list):
        canonical_items = [canonicalize(field, item) for item in value]
        if field.endswith("social_links"):
            # Order of declared social links is not a material change.
            return sorted(canonical_items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
        return canonical_items
    return value


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "registry.name": ("name",),
    "registry.legal_form": ("legal_form",),
    "registry.employees": ("employees",),
    "registry.municipality": ("municipality",),
    "registry.website": ("website",),
    "registry.latest_submitted_accounts": ("latest_submitted_accounts",),
    "financials.records": ("evidence", "financials", "value", "records"),
    "financial_history.years": ("evidence", "financial_history", "value", "years"),
    "roles.roles": ("evidence", "roles", "value", "roles"),
    "locations.locations": ("evidence", "locations", "value", "locations"),
    "website.title": ("evidence", "website", "value", "title"),
    "website.description": ("evidence", "website", "value", "description"),
    "website.social_links": ("evidence", "website", "value", "social_links"),
    "external_footprint.review_count": ("external_metrics", "review_count"),
    "external_footprint.active_job_count": ("external_metrics", "active_job_count"),
    "external_footprint.public_item_count": ("external_metrics", "public_item_count"),
}


def _read(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _evidence_for(profile: dict[str, Any], field: str) -> dict[str, Any]:
    module = field.split(".", 1)[0]
    records = profile.get("evidence", {})
    if module == "registry":
        return records.get("registry_live") or records.get("registry", {})
    return records.get(module, {})


def diff_profile(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    for field, path in TRACKED_FIELDS.items():
        old_value = _read(previous, path)
        new_value = _read(current, path)
        # Compare canonical forms so whitespace, URL tracking params, www./slash
        # variants, and link ordering never register as material changes. The
        # emitted event still carries the raw old/new values.
        if canonicalize(field, old_value) == canonicalize(field, new_value):
            continue
        record = _evidence_for(current, field)
        previous_record = _evidence_for(previous, field)
        changes.append({
            "organisation_number": new_org,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "source_url": record.get("source_url"),
            "retrieved_at": record.get("retrieved_at"),
            "effective_at": record.get("effective_at") or record.get("as_of"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "old_content_sha256": previous_record.get("content_sha256"),
            "new_content_sha256": record.get("content_sha256"),
            "status": record.get("status"),
        })
    return changes


def diff_datasets(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_org = {row["organisation_number"]: row for row in previous}
    new_by_org = {row["organisation_number"]: row for row in current}
    if set(old_by_org) != set(new_by_org):
        raise ValueError("Refresh datasets must have identical organisation-number membership")
    return [
        change
        for org in sorted(old_by_org)
        for change in diff_profile(old_by_org[org], new_by_org[org])
    ]

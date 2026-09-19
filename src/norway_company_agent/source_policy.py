"""Central source-rights and host policy.

This module is the single place that answers three questions for any external
source or observation:

1. Is the *source class* one we are allowed to publish from at all?
2. Is the *acquisition mode* (how the bytes were obtained) permitted, experimental,
   or prohibited?
3. Is the *host* a company-owned / neutral candidate, or a directory / social
   platform that cannot itself prove exact-entity identity?

It deliberately reuses the constants that already exist elsewhere in the code base
(``external_footprint`` acquisition modes, ``discovery`` blocked hosts) instead of
re-deriving them, so the policy stays consistent with the publication gates the
tests already enforce.

The competition source rule is strict: open-source connector code does **not**
grant scraping permission. When a source cannot be collected under permitted
terms, callers must return ``blocked`` / ``not_available`` and preserve the reason
rather than bypassing the restriction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .discovery import BLOCKED_DISCOVERY_HOSTS
from .external_footprint import (
    EXPERIMENTAL_ACQUISITION_MODES,
    INDEPENDENT_SENTIMENT_CLASSES,
    PUBLISHABLE_ACQUISITION_MODES,
)

# Source classes that carry official / company-owned / independent authority and
# may back a *published* claim once identity is verified. Ordered loosely by
# authority; see ``SOURCE_PRECEDENCE`` for the canonical ordering.
PUBLISHABLE_SOURCE_CLASSES = frozenset(
    {
        # Official Brønnøysund registry surfaces.
        "official_registry_bulk",
        "official_registry_live",
        "official_rule_interpretation",
        "official_annual_accounts",
        "official_annual_account_copies",
        "official_roles",
        "official_group_structure",
        "official_subunits",
        # Company-owned website (a claim layer, not an official fact).
        "registry_linked_company_website",
        "registry_linked_company_website_scrapy",
        "company_owned",
        "company_site",
        "company_linked_social_profile",
        "company_reported_claim",
        # Independent / licensed third parties.
        "licensed_api",
        "licensed_news",
        "public_news",
        "public_mention",
        "customer_review",
        "employee_review",
    }
)

# Deterministic authority ordering used for conflict resolution. Lower index wins
# when two valid sources disagree on a single canonical value. Provenance for the
# losing source must still be retained (see ``claims`` / conflict handling).
SOURCE_PRECEDENCE: tuple[str, ...] = (
    "official_registry_bulk",
    "official_registry_live",
    "official_annual_accounts",
    "official_annual_account_copies",
    "official_roles",
    "official_group_structure",
    "official_subunits",
    "official_rule_interpretation",
    "company_owned",
    "company_site",
    "registry_linked_company_website",
    "registry_linked_company_website_scrapy",
    "company_reported_claim",
    "company_linked_social_profile",
    "licensed_api",
    "licensed_news",
    "public_news",
    "public_mention",
    "customer_review",
    "employee_review",
)

# Rights states an external source can be in. Only ``approved`` may be published.
RIGHTS_APPROVED = "approved"
RIGHTS_REVIEW_REQUIRED = "review_required"
RIGHTS_BLOCKED = "blocked"
RIGHTS_STATES = frozenset({RIGHTS_APPROVED, RIGHTS_REVIEW_REQUIRED, RIGHTS_BLOCKED, "unknown"})

# Hosts that can nominate a URL but can never *prove* identity themselves. Reused
# from discovery so there is a single blocked-host list in the code base.
DIRECTORY_AND_SOCIAL_HOSTS = frozenset(BLOCKED_DISCOVERY_HOSTS)


def _host(url: str | None) -> str:
    return (urlparse(str(url or "")).hostname or "").casefold().removeprefix("www.")


def source_precedence_rank(source_class: str | None) -> int:
    """Return the authority rank (lower == more authoritative)."""
    try:
        return SOURCE_PRECEDENCE.index(str(source_class or ""))
    except ValueError:
        # Unknown classes are least authoritative but still ordered deterministically.
        return len(SOURCE_PRECEDENCE)


def is_publishable_source_class(source_class: str | None) -> bool:
    return str(source_class or "") in PUBLISHABLE_SOURCE_CLASSES


def is_publishable_acquisition_mode(mode: str | None) -> bool:
    return str(mode or "") in PUBLISHABLE_ACQUISITION_MODES


def is_experimental_acquisition_mode(mode: str | None) -> bool:
    return str(mode or "") in EXPERIMENTAL_ACQUISITION_MODES


def is_independent_sentiment_class(source_class: str | None) -> bool:
    return str(source_class or "") in INDEPENDENT_SENTIMENT_CLASSES


def is_directory_or_social_host(url: str | None) -> bool:
    host = _host(url)
    if not host:
        return False
    return any(host == blocked or host.endswith("." + blocked) for blocked in DIRECTORY_AND_SOCIAL_HOSTS)


@dataclass(frozen=True)
class SourceDecision:
    """Outcome of evaluating a source/candidate against policy."""

    allowed: bool
    availability: str  # one of the OUTPUT_CONTRACT availability states
    rights_status: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "availability": self.availability,
            "rights_status": self.rights_status,
            "reasons": list(self.reasons),
        }


def evaluate_source(
    *,
    source_class: str | None,
    acquisition_mode: str | None = None,
    rights_status: str | None = None,
    source_url: str | None = None,
    is_official: bool = False,
) -> SourceDecision:
    """Decide whether a source may back a *published* claim.

    Official Brønnøysund data does not require an acquisition-mode / rights check:
    it is open data under NLOD 2.0 and is always publishable. External sources must
    pass source-class, acquisition-mode, and rights gates.
    """
    reasons: list[str] = []

    if is_official or (source_class or "").startswith("official_"):
        if not is_publishable_source_class(source_class):
            reasons.append("official source class is not in the permitted set")
            return SourceDecision(False, "blocked", RIGHTS_BLOCKED, tuple(reasons))
        return SourceDecision(True, "available", RIGHTS_APPROVED, ("official open-data source",))

    if source_url and is_directory_or_social_host(source_url):
        reasons.append("directory, aggregator, or social host cannot back a published claim on its own")

    if not is_publishable_source_class(source_class):
        reasons.append(f"source class {source_class!r} is not permitted for publication")

    if acquisition_mode is not None and not is_publishable_acquisition_mode(acquisition_mode):
        if is_experimental_acquisition_mode(acquisition_mode):
            reasons.append("acquisition mode is experimental (benchmark only, not publishable)")
        else:
            reasons.append("acquisition mode is not approved for publication")

    if rights_status is not None and rights_status != RIGHTS_APPROVED:
        reasons.append(f"source rights are {rights_status!r}, not approved")

    if reasons:
        # Distinguish an explicit rights block from a mere "not yet approved" state.
        rights = RIGHTS_BLOCKED if rights_status == RIGHTS_BLOCKED else (rights_status or RIGHTS_REVIEW_REQUIRED)
        availability = "blocked" if rights == RIGHTS_BLOCKED else "not_available"
        return SourceDecision(False, availability, rights, tuple(reasons))

    return SourceDecision(True, "available", RIGHTS_APPROVED, ("permitted external source",))

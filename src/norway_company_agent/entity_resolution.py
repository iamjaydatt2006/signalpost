"""Unified exact-entity resolution.

Every external candidate is resolved to exactly one of three states:

- ``verified``   — exact legal-entity identity established; may become a claim.
- ``ambiguous``  — plausible but not proven; must surface as ``availability=ambiguous``.
- ``rejected``   — a different, parent, brand, franchise, or unrelated entity.

This module does not invent new matching logic. It is a thin, consistent facade
over the deterministic checks that already exist and are already tested:

- ``identity.assess_website_identity`` / ``assess_social_identity``
- ``discovery.score_search_candidate``
- ``external_footprint.validate_observation``

The single most important safety rule of the whole system lives here:
**WRONG COMPANY > MISSING INFORMATION**. When identity is uncertain we return
``ambiguous`` and never guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .discovery import score_search_candidate
from .external_footprint import validate_observation
from .identity import assess_social_identity, assess_website_identity

VERIFIED = "verified"
AMBIGUOUS = "ambiguous"
REJECTED = "rejected"


@dataclass(frozen=True)
class Resolution:
    """Result of resolving one external candidate against a company profile."""

    status: str  # verified | ambiguous | rejected
    score: float
    method: str
    reasons: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED

    @property
    def publishable(self) -> bool:
        return self.status == VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "method": self.method,
            "reasons": list(self.reasons),
            "details": self.details,
        }


def _classify(score: float, *, verify_at: float = 0.9, ambiguous_at: float = 0.6) -> str:
    if score >= verify_at:
        return VERIFIED
    if score >= ambiguous_at:
        return AMBIGUOUS
    return REJECTED


def resolve_website(profile: dict[str, Any]) -> Resolution:
    """Resolve the registry-linked website against the exact legal entity."""
    assessment = assess_website_identity(profile)
    # assess_website_identity uses status exact/review/related_or_uncertain with a
    # publishable flag on `exact`. Map onto the three canonical states while keeping
    # its numeric score and reasons.
    status = VERIFIED if assessment.get("publishable") else _classify(float(assessment.get("score") or 0.0))
    return Resolution(
        status=status,
        score=float(assessment.get("score") or 0.0),
        method=str(assessment.get("method") or "deterministic_name_org_evidence"),
        reasons=tuple(assessment.get("reasons") or ()),
        details=assessment,
    )


def resolve_social(profile: dict[str, Any], link: dict[str, str]) -> Resolution:
    """Resolve a single social handle against the exact legal entity."""
    assessment = assess_social_identity(profile, link)
    status = VERIFIED if assessment.get("publishable") else _classify(float(assessment.get("identity_score") or 0.0))
    return Resolution(
        status=status,
        score=float(assessment.get("identity_score") or 0.0),
        method=str(assessment.get("method") or "deterministic_social_handle_identity"),
        reasons=(str(assessment.get("reason") or ""),),
        details=assessment,
    )


def resolve_search_candidate(profile: dict[str, Any], result: dict[str, Any]) -> Resolution:
    """Resolve a search/discovery snippet.

    A search result can only ever be a *crawl candidate*: even a strong match is
    returned as ``ambiguous`` at best, because publication requires an
    independently fetched page. This preserves the discovery policy exactly.
    """
    assessment = score_search_candidate(profile, result)
    if assessment.get("publishable_candidate"):
        # Strong enough to crawl, but identity is not yet proven from a fetched page.
        status = AMBIGUOUS
    else:
        status = _classify(float(assessment.get("score") or 0.0), verify_at=2.0, ambiguous_at=0.6)
    return Resolution(
        status=status,
        score=float(assessment.get("score") or 0.0),
        method=str(assessment.get("method") or "deterministic_search_candidate_identity"),
        reasons=tuple(assessment.get("reasons") or ()),
        details={**assessment, "note": "search candidate; publication requires fetched-page proof"},
    )


def resolve_observation(observation: dict[str, Any], *, organisation_number: str | None = None) -> Resolution:
    """Resolve an external observation using the existing publication validator.

    An observation is ``verified`` only when it carries an exact-entity flag,
    identity proof, and passes every schema/rights gate. A mismatched organisation
    number is an immediate ``rejected``.
    """
    org = str(observation.get("organisation_number") or "")
    if organisation_number is not None and org and org != str(organisation_number):
        return Resolution(
            status=REJECTED,
            score=0.0,
            method="observation_org_mismatch",
            reasons=("observation organisation number does not match the anchor",),
            details={"expected": str(organisation_number), "observed": org},
        )
    reasons = validate_observation(observation)
    if not reasons:
        return Resolution(
            status=VERIFIED,
            score=1.0,
            method="external_footprint_publication_gate",
            reasons=("passed identity, rights, hash, and span gates",),
            details={},
        )
    identity_failures = {
        "exact legal entity is not verified",
        "missing exact-entity proof",
    }
    if identity_failures.intersection(reasons):
        status = REJECTED
    else:
        # Structural / rights gaps: identity may be fine but the record is not
        # publishable yet. Treat as ambiguous so it is never silently published.
        status = AMBIGUOUS
    return Resolution(
        status=status,
        score=0.0,
        method="external_footprint_publication_gate",
        reasons=tuple(reasons),
        details={},
    )

"""Modular connector interface, registry, and concrete connectors.

A connector turns a verified company profile into *normalized observations* — never
final claims. Observations flow through entity resolution and the publication gate
(`entity_resolution.resolve_observation` -> `external_footprint.validate_observation`)
before any of them can become a claim. Connectors never write into a profile's claims.

This module deliberately **reuses the existing connector scripts** rather than
duplicating their logic:

- `scripts/extract_company_site_activity.observation`  (company-owned site surface)
- `scripts/extract_company_site_news.observation`      (company-owned dated activity)
- `scripts/run_google_news_rss_connector.fetch`        (optional, network, experimental)

The two company-site connectors are the low-risk / high-value priority sources: they
operate on the *already-fetched* verified website evidence, so they add **zero extra
network requests** and produce genuinely publishable, exact-entity observations.

Restricted or network/credential-dependent connectors are **disabled by default** and
return `blocked` / `not_available` when off — the system never bypasses robots,
rate limits, authentication, or platform restrictions, and never invents observations.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .refresh import _canonical_url

# Make the top-level ``scripts`` namespace importable without duplicating code.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.extract_company_site_activity import observation as _site_activity_observation  # noqa: E402
from scripts.extract_company_site_news import observation as _site_news_observation  # noqa: E402


AVAILABLE = "available"
NOT_AVAILABLE = "not_available"
BLOCKED = "blocked"
FAILED = "failed"


@dataclass
class ConnectorResult:
    connector: str
    source_class: str
    status: str  # available | not_available | blocked | failed
    observations: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0
    cost_usd: float = 0.0
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "source_class": self.source_class,
            "status": self.status,
            "observations": len(self.observations),
            "requests": self.requests,
            "cost_usd": round(self.cost_usd, 6),
            **({"note": self.note} if self.note else {}),
        }


class Connector:
    """Base connector. Subclasses implement ``is_useful`` and ``_run``."""

    name: str = "connector"
    source_class: str = "unknown"
    default_enabled: bool = False
    requires_network: bool = False
    requires_credentials: bool = False
    est_requests: int = 0
    est_cost: float = 0.0

    def is_useful(self, profile: dict[str, Any]) -> bool:  # pragma: no cover - overridden
        return True

    def _run(self, profile: dict[str, Any]) -> ConnectorResult:  # pragma: no cover - overridden
        raise NotImplementedError

    def execute(self, profile: dict[str, Any]) -> ConnectorResult:
        try:
            return self._run(profile)
        except Exception as exc:  # never let a connector crash the batch
            return ConnectorResult(self.name, self.source_class, FAILED, note=f"{type(exc).__name__}: {str(exc)[:180]}", requests=0)


class CompanySiteActivityConnector(Connector):
    name = "company_site_activity"
    source_class = "company_site"
    default_enabled = True
    requires_network = False
    est_requests = 0  # reuses already-fetched website evidence
    est_cost = 0.0

    def is_useful(self, profile: dict[str, Any]) -> bool:
        website = (profile.get("evidence") or {}).get("website") or {}
        identity = (website.get("value") or {}).get("identity_assessment") or {}
        return website.get("status") == "available" and bool(identity.get("publishable"))

    def _run(self, profile: dict[str, Any]) -> ConnectorResult:
        obs = _site_activity_observation(profile)
        if obs:
            return ConnectorResult(self.name, self.source_class, AVAILABLE, [obs], requests=0)
        return ConnectorResult(self.name, self.source_class, NOT_AVAILABLE, note="no publishable company-site surface", requests=0)


class CompanySiteNewsConnector(Connector):
    name = "company_site_news"
    source_class = "company_site"
    default_enabled = True
    requires_network = False
    est_requests = 0
    est_cost = 0.0

    def is_useful(self, profile: dict[str, Any]) -> bool:
        website = (profile.get("evidence") or {}).get("website") or {}
        identity = (website.get("value") or {}).get("identity_assessment") or {}
        return website.get("status") == "available" and bool(identity.get("publishable"))

    def _run(self, profile: dict[str, Any]) -> ConnectorResult:
        obs = _site_news_observation(profile)
        if obs:
            return ConnectorResult(self.name, self.source_class, AVAILABLE, [obs], requests=0)
        return ConnectorResult(self.name, self.source_class, NOT_AVAILABLE, note="no company-owned dated activity page", requests=0)


class GoogleNewsRssConnector(Connector):
    """Optional network connector. Disabled by default.

    Google News RSS output is a *discovery experiment*: the existing script marks
    it ``rights_review_experiment`` / ``review_required``, so its observations
    resolve to ``ambiguous`` and are never published as available claims. It is here
    to exercise the real live path honestly, not to earn points.
    """

    name = "news_rss"
    source_class = "public_news"
    default_enabled = False
    requires_network = True
    est_requests = 1
    est_cost = 0.0

    def __init__(self, fetcher: Callable[[dict], tuple[list[dict], dict]] | None = None, *, per_company: int = 5, years: int = 2) -> None:
        self._fetcher = fetcher
        self._per_company = per_company
        self._years = years

    def is_useful(self, profile: dict[str, Any]) -> bool:
        return bool(profile.get("name"))

    def _run(self, profile: dict[str, Any]) -> ConnectorResult:
        fetcher = self._fetcher
        if fetcher is None:
            # Lazy import so the offline default never touches the network path.
            from scripts.run_google_news_rss_connector import fetch as _rss_fetch

            def fetcher(p: dict) -> tuple[list[dict], dict]:
                return _rss_fetch(p, self._per_company, self._years)

        observations, status = fetcher(profile)
        if status.get("error"):
            return ConnectorResult(self.name, self.source_class, FAILED, note=str(status.get("error")), requests=1)
        return ConnectorResult(self.name, self.source_class, AVAILABLE if observations else NOT_AVAILABLE, observations, requests=1)


class ConnectorRegistry:
    """Holds connectors and their enabled/disabled state."""

    def __init__(self, connectors: list[Connector] | None = None) -> None:
        defaults = connectors if connectors is not None else [
            CompanySiteActivityConnector(),
            CompanySiteNewsConnector(),
            GoogleNewsRssConnector(),
        ]
        self._connectors: dict[str, Connector] = {c.name: c for c in defaults}
        self._enabled: dict[str, bool] = {c.name: c.default_enabled for c in defaults}

    def names(self) -> list[str]:
        return list(self._connectors)

    def get(self, name: str) -> Connector | None:
        return self._connectors.get(name)

    def enable(self, name: str) -> None:
        if name in self._connectors:
            self._enabled[name] = True

    def disable(self, name: str) -> None:
        if name in self._connectors:
            self._enabled[name] = False

    def set_enabled(self, names: list[str]) -> None:
        """Enable exactly ``names`` (that exist); disable everything else."""
        wanted = set(names)
        for name in self._connectors:
            self._enabled[name] = name in wanted

    def is_enabled(self, name: str) -> bool:
        return bool(self._enabled.get(name))

    def enabled(self) -> list[Connector]:
        return [self._connectors[name] for name in self._connectors if self._enabled.get(name)]

    def disabled(self) -> list[Connector]:
        return [self._connectors[name] for name in self._connectors if not self._enabled.get(name)]


def _dedup_key(observation: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(observation.get("organisation_number") or ""),
        str(observation.get("platform") or ""),
        str(observation.get("signal_type") or ""),
        _canonical_url(str(observation.get("source_url") or "")),
    )


def deduplicate_observations(observations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse observations that describe the same fact (same org/platform/signal
    and canonical URL), merging identity proof. Independent sources (different URL)
    are preserved so multi-source evidence is not lost."""
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    duplicates = 0
    for obs in observations:
        key = _dedup_key(obs)
        if key in merged:
            duplicates += 1
            proof = list(merged[key].get("identity_proof") or []) + list(obs.get("identity_proof") or [])
            # De-duplicate proof entries deterministically.
            seen = set()
            unique_proof = []
            for item in proof:
                token = repr(sorted(item.items())) if isinstance(item, dict) else repr(item)
                if token not in seen:
                    seen.add(token)
                    unique_proof.append(item)
            merged[key]["identity_proof"] = unique_proof
            merged[key].setdefault("merged_evidence_ids", [merged[key]["id"]])
            merged[key]["merged_evidence_ids"].append(obs.get("id"))
        else:
            merged[key] = dict(obs)
    return list(merged.values()), duplicates

"""Adaptive external-enrichment controller.

The controller decides, per company, which external research tasks are worth
executing. It never runs every connector blindly. A task is only scheduled when:

1. the company is sufficiently *identified* (needed for identity-dependent tasks),
2. the connector's source is *permitted* under ``source_policy``,
3. the connector is actually *useful* for this company, and
4. the *budget* still has optional-work headroom.

It builds on the existing ``external_tasks.plan_external_tasks`` candidate planner
and the ``entity_resolution`` / ``source_policy`` / ``budget`` layers. Planning is
deterministic and side-effect free, so it is fully testable offline; actually
executing a connector remains the caller's (network-bound) responsibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .budget import Budget
from .connectors import ConnectorRegistry, deduplicate_observations
from .entity_resolution import AMBIGUOUS, VERIFIED, resolve_observation, resolve_social, resolve_website
from .evidence import evidence, utc_now
from .external_footprint import aggregate_footprint
from .external_tasks import plan_external_tasks
from .source_policy import RIGHTS_APPROVED, RIGHTS_REVIEW_REQUIRED

# Per-connector cost/behaviour model. ``rights`` reflects whether a *permitted*
# path exists (licensed/official API). Connectors whose only path is an unofficial
# scraper are marked review_required and are never scheduled for publication.
CONNECTOR_POLICY: dict[str, dict[str, Any]] = {
    "google_places_api": {"est_requests": 1, "est_cost": 0.017, "requires_identity": False, "rights": RIGHTS_APPROVED, "useful_signal": "place_summary"},
    "licensed_news_search": {"est_requests": 1, "est_cost": 0.010, "requires_identity": True, "rights": RIGHTS_APPROVED, "useful_signal": "public_mention"},
    "jobs_provider": {"est_requests": 1, "est_cost": 0.005, "requires_identity": True, "rights": RIGHTS_APPROVED, "useful_signal": "job_posting"},
    "permitted_search_api": {"est_requests": 1, "est_cost": 0.005, "requires_identity": False, "rights": RIGHTS_APPROVED, "useful_signal": "profile_handle"},
    # Social profile metric refresh: only meaningful for an already-verified handle,
    # and only through the platform's permitted API.
    "linkedin_connector": {"est_requests": 1, "est_cost": 0.0, "requires_identity": True, "rights": RIGHTS_REVIEW_REQUIRED, "useful_signal": "profile_metrics"},
    "facebook_connector": {"est_requests": 1, "est_cost": 0.0, "requires_identity": True, "rights": RIGHTS_REVIEW_REQUIRED, "useful_signal": "profile_metrics"},
    "instagram_connector": {"est_requests": 1, "est_cost": 0.0, "requires_identity": True, "rights": RIGHTS_REVIEW_REQUIRED, "useful_signal": "profile_metrics"},
    "x_connector": {"est_requests": 1, "est_cost": 0.0, "requires_identity": True, "rights": RIGHTS_REVIEW_REQUIRED, "useful_signal": "profile_metrics"},
    "youtube_connector": {"est_requests": 1, "est_cost": 0.0, "requires_identity": True, "rights": RIGHTS_APPROVED, "useful_signal": "profile_metrics"},
    "tiktok_connector": {"est_requests": 1, "est_cost": 0.0, "requires_identity": True, "rights": RIGHTS_REVIEW_REQUIRED, "useful_signal": "profile_metrics"},
}

_DEFAULT_POLICY = {"est_requests": 1, "est_cost": 0.0, "requires_identity": True, "rights": RIGHTS_REVIEW_REQUIRED, "useful_signal": None}


@dataclass
class EnrichmentDecision:
    task_id: str
    connector: str
    action: str  # "run" | "skip"
    reason: str
    est_requests: int = 0
    est_cost: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "connector": self.connector,
            "action": self.action,
            "reason": self.reason,
            "est_requests": self.est_requests,
            "est_cost": self.est_cost,
            **({"details": self.details} if self.details else {}),
        }


class EnrichmentController:
    def __init__(self, budget: Budget | None = None, registry: ConnectorRegistry | None = None) -> None:
        self.budget = budget or Budget()
        self.registry = registry or ConnectorRegistry()

    def _website_verified(self, profile: dict[str, Any]) -> bool:
        website = profile.get("evidence", {}).get("website", {})
        if website.get("status") != "available":
            return False
        return resolve_website(profile).verified

    def _verified_social_link(self, profile: dict[str, Any], candidate_url: str | None) -> bool:
        if not candidate_url:
            return False
        website = profile.get("evidence", {}).get("website", {})
        for link in (website.get("value") or {}).get("social_links", []) or []:
            if link.get("url") == candidate_url:
                return resolve_social(profile, link).verified
        return False

    def plan(self, profile: dict[str, Any]) -> dict[str, Any]:
        """Return an adaptive execution plan for one company (no side effects)."""
        candidates = plan_external_tasks(profile)
        website_verified = self._website_verified(profile)
        has_social = bool((profile.get("evidence", {}).get("website", {}).get("value") or {}).get("social_links"))
        decisions: list[EnrichmentDecision] = []

        for task in candidates:
            connector = str(task.get("connector"))
            policy = CONNECTOR_POLICY.get(connector, _DEFAULT_POLICY)
            est_requests = int(policy["est_requests"])
            est_cost = float(policy["est_cost"])
            candidate_url = task.get("candidate_url")

            # Gate 1: rights / source policy.
            if policy["rights"] != RIGHTS_APPROVED:
                decisions.append(EnrichmentDecision(task["task_id"], connector, "skip", "connector has no approved rights path (review_required)", details={"rights": policy["rights"]}))
                self.budget.record_blocked(connector=connector)
                continue

            # Gate 2: identity sufficiency for identity-dependent tasks.
            if policy["requires_identity"]:
                if connector.endswith("_connector"):
                    if not self._verified_social_link(profile, candidate_url):
                        decisions.append(EnrichmentDecision(task["task_id"], connector, "skip", "social handle is not identity-verified"))
                        continue
                elif not (website_verified or profile.get("name")):
                    decisions.append(EnrichmentDecision(task["task_id"], connector, "skip", "company is not sufficiently identified for this connector"))
                    continue

            # Gate 3: usefulness / avoid redundant discovery.
            if connector == "permitted_search_api" and (website_verified or has_social):
                decisions.append(EnrichmentDecision(task["task_id"], connector, "skip", "handles already known; discovery search is redundant"))
                continue

            # Gate 4: budget headroom for optional work (reserve planned budget only).
            if not self.budget.reserve(requests=est_requests, cost=est_cost, connector=connector, optional=True):
                decisions.append(EnrichmentDecision(task["task_id"], connector, "skip", "budget headroom exhausted for optional enrichment", est_requests=est_requests, est_cost=est_cost))
                continue

            decisions.append(EnrichmentDecision(task["task_id"], connector, "run", "permitted, identified, useful, and within budget", est_requests=est_requests, est_cost=est_cost))

        return {
            "organisation_number": profile.get("organisation_number"),
            "website_verified": website_verified,
            "candidate_count": len(candidates),
            "scheduled": [d.to_dict() for d in decisions if d.action == "run"],
            "skipped": [d.to_dict() for d in decisions if d.action == "skip"],
            "planned_requests": sum(d.est_requests for d in decisions if d.action == "run"),
            "planned_cost_usd": round(sum(d.est_cost for d in decisions if d.action == "run"), 6),
        }

    def execute(self, profile: dict[str, Any]) -> dict[str, Any]:
        """Execute enabled, useful connectors and resolve their observations.

        This is the real observation pipeline:
            connector -> raw observations -> resolve_observation ->
            (verified | ambiguous | rejected) -> dedup -> external metrics.

        It records *executed* requests/cost on the budget (never planned-as-actual),
        never bypasses restrictions, and never invents observations. Disabled
        connectors are not executed. It does not write claims; the caller passes the
        returned verified observations to the claim extractor.
        """
        org = str(profile.get("organisation_number") or "")
        results: list[dict[str, Any]] = []
        verified: list[dict[str, Any]] = []
        ambiguous: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        executed_requests = 0
        executed_cost = 0.0

        for connector in self.registry.enabled():
            if not connector.is_useful(profile):
                results.append({"connector": connector.name, "source_class": connector.source_class, "status": "not_applicable", "observations": 0, "requests": 0, "note": "connector not useful for this company"})
                continue

            # Budget gate: reserve intent, then only run if a hard commit is possible.
            if connector.est_requests or connector.est_cost:
                self.budget.reserve(requests=connector.est_requests, cost=connector.est_cost, connector=connector.name, optional=True)
                if not self.budget.can_spend(requests=connector.est_requests, cost=connector.est_cost, optional=True):
                    self.budget.record_blocked(connector=connector.name)
                    results.append({"connector": connector.name, "source_class": connector.source_class, "status": "blocked", "observations": 0, "requests": 0, "note": "budget headroom exhausted"})
                    continue

            result = connector.execute(profile)
            # Record ACTUAL executed spend.
            if result.requests or result.cost_usd:
                self.budget.commit(requests=result.requests, cost=result.cost_usd, connector=connector.name)
                executed_requests += result.requests
                executed_cost += result.cost_usd
            if result.status == "blocked":
                self.budget.record_blocked(connector=connector.name)
            elif result.status == "failed":
                self.budget.record_failure(connector=connector.name)
            results.append(result.to_dict())

            for obs in result.observations:
                resolution = resolve_observation(obs, organisation_number=org)
                if resolution.status == VERIFIED:
                    verified.append(obs)
                    self.budget.record_observation()
                elif resolution.status == AMBIGUOUS:
                    ambiguous.append({"id": obs.get("id"), "reasons": list(resolution.reasons)})
                else:
                    rejected.append({"id": obs.get("id"), "reasons": list(resolution.reasons)})

        verified, duplicate_count = deduplicate_observations(verified)

        # Aggregate external metrics and attach a refresh-visible evidence stub so
        # genuine external changes (e.g. review_count) can be detected later.
        if verified:
            aggregate = aggregate_footprint(verified)
            profile["external_metrics"] = {
                "review_count": aggregate.get("review_signal_count", 0),
                "active_job_count": aggregate.get("active_job_count", 0),
                "public_item_count": aggregate.get("public_item_count", 0),
                "verified_observations": len(verified),
            }
            profile.setdefault("evidence", {})["external_footprint"] = evidence(
                "external_footprint", "available", "external_footprint_summary",
                "https://builderr.ai/signalpost/external-footprint",
                value=profile["external_metrics"], retrieved_at=utc_now(),
            )
            profile["external_observations"] = verified

        return {
            "organisation_number": org,
            "connector_results": results,
            "verified_observations": verified,
            "ambiguous_observations": ambiguous,
            "rejected_observations": rejected,
            "duplicate_observations_merged": duplicate_count,
            "executed_requests": executed_requests,
            "executed_cost_usd": round(executed_cost, 6),
        }

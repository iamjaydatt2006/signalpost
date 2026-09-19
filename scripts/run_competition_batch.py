#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.budget import Budget, BudgetLimits  # noqa: E402
from norway_company_agent.connectors import ConnectorRegistry  # noqa: E402
from norway_company_agent.enrichment import EnrichmentController  # noqa: E402
from norway_company_agent.entity_resolution import resolve_observation  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402


# Source metadata for modules skipped because the runtime budget was exhausted.
_MODULE_SOURCE = {
    "registry_live": ("official_registry_live", "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"),
    "financials": ("official_annual_accounts", "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"),
    "financial_history": ("official_annual_account_copies", "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"),
    "roles": ("official_roles", "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"),
    "group": ("official_group_structure", "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"),
    "locations": ("official_subunits", "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}"),
}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website")
    parser.add_argument("--previous-profiles", help="Optional prior profiles JSONL for deterministic change detection")
    parser.add_argument("--observations", help="Optional verified external observations JSONL (one object per line with organisation_number)")
    parser.add_argument("--plan-enrichment", action="store_true", help="Compute an adaptive external-enrichment plan per company (no live connector calls)")
    parser.add_argument("--enable-enrichment", action="store_true", help="Execute enabled enrichment connectors and publish verified observations")
    parser.add_argument("--connectors", help="Comma-separated connector names to enable (default: company-owned site connectors only)")
    parser.add_argument("--max-requests", type=int, default=None, help="Optional hard request budget for the whole run")
    parser.add_argument("--max-cost-usd", type=float, default=None, help="Optional hard third-party cost budget in USD")
    parser.add_argument("--max-runtime-seconds", type=float, default=None, help="Optional hard runtime budget in seconds")
    parser.add_argument("--budget-headroom", type=float, default=0.9, help="Fraction of a limit at which optional enrichment stops")
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}

    budget = Budget(BudgetLimits(
        max_requests=args.max_requests,
        max_cost_usd=args.max_cost_usd,
        max_runtime_seconds=args.max_runtime_seconds,
        headroom=args.budget_headroom,
    ))

    registry = ConnectorRegistry()
    if args.connectors is not None:
        registry.set_enabled([name.strip() for name in args.connectors.split(",") if name.strip()])

    # Optional inputs for change detection and verified external observations.
    previous_by_org: dict[str, dict] = {}
    if args.previous_profiles and Path(args.previous_profiles).exists():
        previous_by_org = {
            row["organisation_number"]: row
            for row in (json.loads(line) for line in Path(args.previous_profiles).read_text(encoding="utf-8").splitlines() if line.strip())
        }
    observations_by_org: dict[str, list[dict]] = {}
    if args.observations and Path(args.observations).exists():
        for line in Path(args.observations).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obs = json.loads(line)
            observations_by_org.setdefault(str(obs.get("organisation_number") or ""), []).append(obs)

    def enrich(profile: dict) -> tuple[dict, dict]:
        org = profile["organisation_number"]
        # Estimate the mandatory request cost and enforce the central hard budget at
        # runtime. No phase bypasses the budget: if the hard request/cost/runtime
        # limit would be breached, official fetches are skipped and the affected
        # modules are marked blocked (budget_exhausted). The company still receives a
        # terminal envelope; it is never silently dropped.
        website_wanted = "website" in requested_modules
        estimate = len(fetch_modules) + (5 if website_wanted else 0)
        if estimate and not budget.can_spend(requests=estimate, optional=False):
            budget.record_blocked(connector="official_brreg")
            for module in fetch_modules:
                source_type, url = _MODULE_SOURCE.get(module, ("official", "https://data.brreg.no/"))
                profile["evidence"][module] = evidence(module, "blocked", source_type, url.format(org=org), note="budget_exhausted")
            if website_wanted:
                profile["evidence"]["website"] = evidence("website", "blocked", "registry_linked_company_website", str(profile.get("website") or "https://data.brreg.no/"), note="budget_exhausted")
            profile["run_metrics"] = {"requests": 0, "bytes": 0, "latencies_ms": []}
            return profile, profile["run_metrics"]

        records, metrics = fetch_official_modules(org, fetch_modules)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if website_wanted:
            website_record, website_metrics = fetch_website(profile.get("website"))
            profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
        actual_requests = len(metrics) + website_metrics["requests"]
        metric = {
            "requests": actual_requests,
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"],
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"],
        }
        # Record actual executed official spend against the central budget.
        budget.record_spend(requests=actual_requests, connector="official_brreg")
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]

    # Official/website spend is recorded per-company inside enrich() against the
    # central budget, so no phase can bypass the hard limit.
    controller = EnrichmentController(budget=budget, registry=registry)
    enrichment_plans: list[dict] = []
    enrichment_runs: list[dict] = []
    verified_observation_count = 0
    quarantined_observation_count = 0
    envelopes = []
    for profile in ordered_profiles:
        org = profile["organisation_number"]

        # Adaptive enrichment plan (deterministic; no live connector calls here).
        if args.plan_enrichment:
            plan = controller.plan(profile)
            profile["enrichment_plan"] = plan
            enrichment_plans.append(plan)

        # Real connector execution -> resolved observations (only when enabled).
        executed_requests = 0
        executed_cost = 0.0
        executed_observations: list[dict] = []
        if args.enable_enrichment:
            run = controller.execute(profile)
            enrichment_runs.append({"organisation_number": org, "connector_results": run["connector_results"], "verified": len(run["verified_observations"]), "ambiguous": len(run["ambiguous_observations"]), "rejected": len(run["rejected_observations"]), "merged": run["duplicate_observations_merged"]})
            executed_requests += run["executed_requests"]
            executed_cost += run["executed_cost_usd"]
            executed_observations = run["verified_observations"]
            verified_observation_count += len(run["verified_observations"])

        # Publish only file-supplied observations that resolve to a verified entity.
        raw_observations = observations_by_org.get(org, [])
        verified_observations = list(executed_observations)
        for obs in raw_observations:
            resolution = resolve_observation(obs, organisation_number=org)
            if resolution.verified:
                verified_observations.append(obs)
                budget.record_observation()
                verified_observation_count += 1
            else:
                quarantined_observation_count += 1

        per_company_ops = dict(profile.get("run_metrics") or {})
        # Official BRREG + registry-linked site are free; enrichment cost is actual.
        per_company_ops["requests"] = int(per_company_ops.get("requests", 0) or 0) + executed_requests
        per_company_ops["third_party_cost_usd"] = round(executed_cost, 6)

        envelopes.append(
            terminal_envelope(
                profile,
                run_id=args.run_id,
                modules=requested_modules,
                started_at=started_at,
                completed_at=completed_at,
                previous_profile=previous_by_org.get(org),
                observations=verified_observations or None,
                operations=per_company_ops,
            )
        )
    validation = validate_envelopes(envelopes, args.expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "budget": budget.snapshot(),
        "change_detection": {
            "previous_profiles_loaded": len(previous_by_org),
            "total_changes": sum(len(item.get("changes", [])) for item in envelopes),
        },
        "external_observations": {
            "verified_published": verified_observation_count,
            "quarantined": quarantined_observation_count,
        },
        "enrichment_planning": {
            "enabled": bool(args.plan_enrichment),
            "companies_planned": len(enrichment_plans),
            "scheduled_tasks": sum(len(item.get("scheduled", [])) for item in enrichment_plans),
            "skipped_tasks": sum(len(item.get("skipped", [])) for item in enrichment_plans),
        },
        "enrichment_execution": {
            "enabled": bool(args.enable_enrichment),
            "connectors_enabled": [c.name for c in registry.enabled()],
            "connectors_disabled": [c.name for c in registry.disabled()],
            "companies_executed": len(enrichment_runs),
            "verified_observations": sum(item["verified"] for item in enrichment_runs),
            "ambiguous_observations": sum(item["ambiguous"] for item in enrichment_runs),
            "rejected_observations": sum(item["rejected"] for item in enrichment_runs),
            "duplicates_merged": sum(item["merged"] for item in enrichment_runs),
        },
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()

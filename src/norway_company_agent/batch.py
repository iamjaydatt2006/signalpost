from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .claims import extract_claims
from .evidence import evidence, utc_now
from .official import accounting_obligation_assessment
from .refresh import diff_profile
from .sampling import iter_bulk
from .synthesis import synthesize_summary


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    values: list[Any]
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    for value in values:
        org = value.get("organisation_number") if isinstance(value, dict) else value
        org = "".join(character for character in str(org or "") if character.isdigit())
        if len(org) != 9:
            raise ValueError(f"Invalid Norwegian organisation number: {value!r}")
        record = {"organisation_number": org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    orgs = [record["organisation_number"] for record in records]
    if len(orgs) != len(set(orgs)):
        raise ValueError("Organisation-number input contains duplicates")
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    snapshot_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    if missing:
        raise ValueError(f"Organisation numbers absent from registry snapshot: {missing[:10]}")
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found),
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "source_error":
        return "source_error"
    return "submission_error"


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
    previous_profile: dict[str, Any] | None = None,
    observations: list[dict[str, Any]] | None = None,
    operations: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Build the terminal envelope for one organisation number.

    The envelope is OUTPUT_CONTRACT.md compliant (``run``, ``claims``, ``evidence``,
    ``changes``, ``errors``, ``operations``) while additively preserving the internal
    ``state`` / ``modules`` / ``profile`` keys the batch validator and existing tests
    rely on.
    """
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"
    terminal_status = "failed" if entity_state == "submission_error" else "completed"

    extracted = extract_claims(profile, observations=observations)
    claims = extracted["claims"]
    contract_evidence = extracted["evidence"]

    # Deterministic change detection against a prior profile snapshot, when supplied.
    changes: list[dict[str, Any]] = []
    if previous_profile is not None:
        try:
            changes = diff_profile(previous_profile, profile)
        except ValueError:
            # Mismatched organisation number: never fabricate a change; record an error.
            changes = []
            errors = list(errors or []) + [{"stage": "change_detection", "error": "previous profile organisation number mismatch"}]

    # Surface source_error / blocked evidence as structured, non-fatal errors.
    derived_errors = list(errors or [])
    for module, record in (profile.get("evidence", {}) or {}).items():
        if isinstance(record, dict) and record.get("status") in {"source_error"}:
            derived_errors.append({"stage": module, "error": record.get("note") or "source error", "source_url": record.get("source_url")})

    operations_block = operations or profile.get("run_metrics") or {}
    contract_operations = {
        "requests": int(operations_block.get("requests", 0) or 0),
        "runtime_ms": int(operations_block.get("runtime_ms") or (sum(operations_block.get("latencies_ms") or []) if operations_block.get("latencies_ms") else 0)),
        "third_party_cost_usd": float(operations_block.get("third_party_cost_usd", 0.0) or 0.0),
    }
    for extra_key in ("bytes", "failures", "blocked_sources", "successful_observations", "degraded", "requests_by_connector"):
        if extra_key in operations_block:
            contract_operations[extra_key] = operations_block[extra_key]

    envelope = {
        # -- OUTPUT_CONTRACT.md primary shape --
        "organisation_number": profile["organisation_number"],
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": terminal_status,
        },
        "claims": claims,
        "evidence": contract_evidence,
        "changes": changes,
        "errors": derived_errors,
        "operations": contract_operations,
        # -- internal keys preserved for the batch validator / tests --
        "run_id": run_id,
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
        "profile": profile,
    }
    if extracted["conflicts"]:
        envelope["conflicts"] = extracted["conflicts"]
    if include_summary:
        envelope["synthesis"] = synthesize_summary(profile, claims)
    return envelope


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.claims import extract_claims, build_evidence_records  # noqa: E402
from norway_company_agent.synthesis import synthesize_summary  # noqa: E402
from norway_company_agent.budget import Budget, BudgetLimits  # noqa: E402
from norway_company_agent.enrichment import EnrichmentController  # noqa: E402
from norway_company_agent.entity_resolution import (  # noqa: E402
    resolve_observation,
    resolve_search_candidate,
    resolve_social,
    resolve_website,
    VERIFIED,
    AMBIGUOUS,
    REJECTED,
)
from norway_company_agent import source_policy as sp  # noqa: E402
from norway_company_agent.refresh import canonicalize, diff_profile  # noqa: E402
from norway_company_agent.batch import terminal_envelope  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def registry_record(org="998877665", **value):
    return evidence(
        "registry", "available", "official_registry_bulk",
        "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
        value=value or {}, content_sha256="a" * 64, source_row_key=org,
    )


def base_profile(**overrides):
    profile = {
        "organisation_number": "998877665",
        "name": "Example",
        "legal_form": "AS",
        "employees": 12,
        "bankrupt": False,
        "liquidating": False,
        "municipality": "OSLO",
        "municipality_number": "0301",
        "industry_code": "62.010",
        "industry_label": "Programmeringstjenester",
        "website": "https://example.no/",
        "latest_submitted_accounts": "2024",
        "evidence": {
            "registry": registry_record(),
            "accounting_obligation": evidence(
                "accounting_obligation", "available", "official_rule_interpretation",
                "https://www.brreg.no/", value={"classification": "required_by_legal_form"},
                content_sha256="b" * 64,
            ),
        },
    }
    profile.update(overrides)
    return profile


def website_value(*, verified, org="998877665", social=None):
    if verified:
        return {
            "requested_url": "https://example.no/",
            "final_url": "https://example.no/",
            "registered_domain": "example.no",
            "title": f"Example AS {org}",
            "description": "We build software.",
            "identity_text_excerpt": f"Example AS organisation number {org}",
            "main_text_excerpt": "Example AS provides substantive company content for identity review. " * 3,
            "social_links": social or [],
            "structured_organisations": [],
            "content_sha256": "c" * 64,
            "extraction_state": "static_complete",
        }
    # Unverified: a generic branded page with no legal-name or org-number evidence.
    return {
        "requested_url": "https://brandsite.no/",
        "final_url": "https://brandsite.no/",
        "registered_domain": "brandsite.no",
        "title": "Corporate Portal",
        "description": "Welcome to our website.",
        "identity_text_excerpt": "Generic corporate information page.",
        "main_text_excerpt": "Generic corporate content providing enough length for the substantive completeness check. " * 3,
        "social_links": social or [],
        "structured_organisations": [],
        "content_sha256": "c" * 64,
        "extraction_state": "static_complete",
    }


def with_website(profile, *, status="available", verified=True, social=None, gate=True):
    from norway_company_agent.identity import apply_website_identity_gate

    record = evidence(
        "website", status, "registry_linked_company_website", "https://example.no/",
        value=website_value(verified=verified, social=social), content_sha256="c" * 64,
    )
    if status == "available" and gate:
        gated = apply_website_identity_gate(profile, record)
        profile["evidence"]["website"] = gated["website"]
    else:
        profile["evidence"]["website"] = record
    return profile


def financials_record(status="available", records=None):
    if status != "available":
        return evidence("financials", status, "official_annual_accounts", "https://data.brreg.no/regnskapsregisteret/regnskap/998877665", note="HTTP 404" if status == "not_found" else None)
    return evidence(
        "financials", "available", "official_annual_accounts",
        "https://data.brreg.no/regnskapsregisteret/regnskap/998877665",
        value={"records": records if records is not None else [{"period": "2024", "revenue": 1000000, "annual_result": 50000, "debt": 0}]},
        content_sha256="d" * 64,
    )


# --------------------------------------------------------------------------- #
# CLAIMS
# --------------------------------------------------------------------------- #
class ClaimsTests(unittest.TestCase):
    def _by_field(self, result):
        return {c["field"]: c for c in result["claims"]}

    def test_missing_financials_is_not_available_not_zero(self):
        profile = base_profile()
        profile["evidence"]["financials"] = financials_record(status="not_found")
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["revenue"]["availability"], "not_available")
        self.assertIsNone(by["revenue"]["value"])
        self.assertNotEqual(by["revenue"]["value"], 0)

    def test_financials_404_maps_to_not_available(self):
        profile = base_profile()
        profile["evidence"]["financials"] = financials_record(status="not_found")
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["annual_result"]["availability"], "not_available")

    def test_financials_source_error_maps_to_failed(self):
        profile = base_profile()
        profile["evidence"]["financials"] = financials_record(status="source_error")
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["revenue"]["availability"], "failed")

    def test_legitimate_zero_financial_value_is_preserved(self):
        profile = base_profile()
        profile["evidence"]["financials"] = financials_record(records=[{"period": "2024", "revenue": 0, "debt": 0}])
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["revenue"]["availability"], "available")
        self.assertEqual(by["revenue"]["value"], 0)
        self.assertEqual(by["revenue"]["financial_period"], "2024")

    def test_missing_account_is_never_confused_with_zero(self):
        # A field absent from an available filing is not_available, not zero.
        profile = base_profile()
        profile["evidence"]["financials"] = financials_record(records=[{"period": "2024", "revenue": 500}])
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["revenue"]["value"], 500)
        self.assertEqual(by["equity"]["availability"], "not_available")
        self.assertIsNone(by["equity"]["value"])

    def test_employee_zero_available_but_none_not_available(self):
        by_zero = self._by_field(extract_claims(base_profile(employees=0)))
        self.assertEqual(by_zero["registered_employees"]["availability"], "available")
        self.assertEqual(by_zero["registered_employees"]["value"], 0)
        by_none = self._by_field(extract_claims(base_profile(employees=None)))
        self.assertEqual(by_none["registered_employees"]["availability"], "not_available")

    def test_multiple_financial_years_use_latest_record(self):
        profile = base_profile()
        profile["evidence"]["financials"] = financials_record(records=[
            {"period": "2024", "revenue": 900},
            {"period": "2023", "revenue": 800},
        ])
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["revenue"]["value"], 900)
        self.assertEqual(by["revenue"]["financial_period"], "2024")

    def test_website_unverified_yields_ambiguous_and_no_social_claims(self):
        profile = with_website(base_profile(), verified=False, social=[{"platform": "youtube", "url": "https://youtube.com/@example"}])
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["verified_website"]["availability"], "ambiguous")
        self.assertFalse(any(f.startswith("social_profile_") for f in by))

    def test_website_verified_publishes_site_and_social_claims(self):
        profile = with_website(base_profile(), verified=True, social=[{"platform": "youtube", "url": "https://youtube.com/@example"}])
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["verified_website"]["availability"], "available")
        self.assertIn("website_title", by)
        self.assertIn("social_profile_youtube", by)

    def test_website_blocked_maps_to_blocked_availability(self):
        profile = with_website(base_profile(), status="blocked", verified=False)
        by = self._by_field(extract_claims(profile))
        self.assertEqual(by["verified_website"]["availability"], "blocked")

    def test_every_available_claim_references_existing_evidence(self):
        profile = with_website(base_profile(), verified=True)
        profile["evidence"]["financials"] = financials_record()
        result = extract_claims(profile)
        evidence_ids = {e["id"] for e in result["evidence"]}
        for claim in result["claims"]:
            if claim["availability"] == "available":
                self.assertTrue(claim["evidence_ids"], f"available claim {claim['field']} has no evidence")
                for eid in claim["evidence_ids"]:
                    self.assertIn(eid, evidence_ids)

    def test_evidence_records_carry_required_contract_fields(self):
        profile = base_profile()
        records, _ = build_evidence_records(profile)
        for record in records:
            for key in ("id", "source_url", "source_class", "retrieved_at", "content_sha256", "claim_span"):
                self.assertIn(key, record)

    def test_registry_conflict_between_bulk_and_live_is_recorded(self):
        profile = base_profile()
        profile["evidence"]["registry_live"] = evidence(
            "registry_live", "available", "official_registry_live",
            "https://data.brreg.no/enhetsregisteret/api/enheter/998877665",
            value={"name": "Example", "employees": 20}, content_sha256="e" * 64,
        )
        result = extract_claims(profile)
        by = {c["field"]: c for c in result["claims"]}
        self.assertEqual(by["registered_employees"]["value"], 20)  # live wins
        self.assertTrue(any(c["field"] == "employees" for c in result["conflicts"]))

    def test_verified_observation_becomes_claim(self):
        profile = base_profile()
        obs = valid_observation()
        result = extract_claims(profile, observations=[obs])
        self.assertTrue(any(c["field"].startswith("external_") for c in result["claims"]))
        self.assertIn(obs["id"], {e["id"] for e in result["evidence"]})

    def test_no_duplicate_claims(self):
        profile = with_website(base_profile(), verified=True)
        claims = extract_claims(profile)["claims"]
        keys = [(c["field"], str(c["value"])) for c in claims]
        self.assertEqual(len(keys), len(set(keys)))

    def test_checked_zero_subunits_is_available_count_not_empty_value(self):
        profile = base_profile()
        profile["evidence"]["locations"] = evidence(
            "locations", "available", "official_subunits",
            "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=998877665",
            value={"locations": []}, content_sha256="9" * 64,
        )
        by = self._by_field(extract_claims(profile))
        self.assertIn("registered_subunit_count", by)
        self.assertEqual(by["registered_subunit_count"]["availability"], "available")
        self.assertEqual(by["registered_subunit_count"]["value"], 0)  # real zero, not None
        self.assertNotIn("registered_subunits", by)  # no empty list claim

    def test_no_available_claim_ever_has_empty_value(self):
        # Rich profile with several checked-but-empty sources.
        profile = with_website(base_profile(), verified=True)
        profile["evidence"]["financials"] = financials_record()
        profile["evidence"]["locations"] = evidence("locations", "available", "official_subunits", "https://data.brreg.no/x", value={"locations": []}, content_sha256="9" * 64)
        profile["evidence"]["roles"] = evidence("roles", "available", "official_roles", "https://data.brreg.no/y", value={"roles": []}, content_sha256="8" * 64)
        for claim in extract_claims(profile)["claims"]:
            if claim["availability"] == "available":
                self.assertNotIn(claim["value"], (None, "", [], {}), f"available claim {claim['field']} has empty value")


# --------------------------------------------------------------------------- #
# SYNTHESIS
# --------------------------------------------------------------------------- #
class SynthesisTests(unittest.TestCase):
    def test_summary_only_uses_available_claims_with_evidence(self):
        profile = with_website(base_profile(), verified=True)
        profile["evidence"]["financials"] = financials_record()
        result = extract_claims(profile)
        summary = synthesize_summary(profile, result["claims"])
        self.assertGreater(summary["fact_count"], 0)
        for sentence in summary["sentences"]:
            self.assertTrue(sentence["evidence_ids"])

    def test_summary_omits_unsupported_facts(self):
        # Bare profile: no financials, no website -> no revenue sentence.
        profile = base_profile()
        summary = synthesize_summary(profile, extract_claims(profile)["claims"])
        self.assertNotIn("revenue", summary["summary"].lower())
        self.assertNotIn("leading company", summary["summary"].lower())

    def test_zero_employees_is_stated_not_dropped(self):
        profile = base_profile(employees=0)
        summary = synthesize_summary(profile, extract_claims(profile)["claims"])
        self.assertIn("0 employees", summary["summary"])

    def test_structured_sections_are_verified_only_with_evidence(self):
        profile = with_website(base_profile(), verified=True)
        profile["evidence"]["financials"] = financials_record()
        result = extract_claims(profile)
        s = synthesize_summary(profile, result["claims"], evidence=result["evidence"])
        self.assertIn("sections", s)
        for key in ("COMPANY", "PEOPLE", "LOCATIONS", "FINANCIALS", "HIRING", "PUBLIC_ACTIVITY", "RECENT_CHANGES", "UNKNOWN", "SOURCES"):
            self.assertIn(key, s["sections"])
        # COMPANY facts must carry evidence ids.
        for item in s["sections"]["COMPANY"]:
            self.assertTrue(item["evidence_ids"])
        # SOURCES lists the real consulted URLs.
        self.assertTrue(s["sections"]["SOURCES"])

    def test_unknown_section_lists_unavailable_fields(self):
        profile = base_profile()
        profile["evidence"]["financials"] = financials_record(status="not_found")
        result = extract_claims(profile)
        s = synthesize_summary(profile, result["claims"], evidence=result["evidence"])
        self.assertIn("revenue", s["sections"]["UNKNOWN"])  # missing financials -> UNKNOWN, not zero

    def test_recent_changes_section_reflects_changes(self):
        profile = with_website(base_profile(), verified=True)
        result = extract_claims(profile)
        changes = [{"field": "registry.employees", "old_value": 5, "new_value": 12,
                    "source_url": "https://data.brreg.no/x", "retrieved_at": "2026-09-01T00:00:00Z"}]
        s = synthesize_summary(profile, result["claims"], changes=changes, evidence=result["evidence"])
        self.assertEqual(len(s["sections"]["RECENT_CHANGES"]), 1)
        self.assertEqual(s["sections"]["RECENT_CHANGES"][0]["field"], "registry.employees")


# --------------------------------------------------------------------------- #
# BUDGET
# --------------------------------------------------------------------------- #
class BudgetTests(unittest.TestCase):
    def test_hard_request_limit_blocks_spend(self):
        b = Budget(BudgetLimits(max_requests=10))
        self.assertTrue(b.try_spend(requests=5, optional=False))
        self.assertTrue(b.try_spend(requests=5, optional=False))
        self.assertFalse(b.try_spend(requests=1, optional=False))
        self.assertEqual(b.requests, 10)

    def test_soft_headroom_stops_optional_but_allows_mandatory(self):
        b = Budget(BudgetLimits(max_requests=10, headroom=0.9))
        self.assertTrue(b.try_spend(requests=5, optional=True))
        # 5 + 5 = 10 > soft cap 9 -> optional refused, degraded flagged.
        self.assertFalse(b.try_spend(requests=5, optional=True))
        self.assertTrue(b.degraded)
        # Mandatory work still fits under the hard cap.
        self.assertTrue(b.can_spend(requests=5, optional=False))

    def test_cost_limit_enforced(self):
        b = Budget(BudgetLimits(max_cost_usd=1.0))
        self.assertTrue(b.try_spend(cost=0.6, optional=False, connector="places"))
        self.assertFalse(b.try_spend(cost=0.6, optional=False, connector="places"))

    def test_snapshot_reports_all_dimensions(self):
        b = Budget(BudgetLimits(max_requests=100))
        b.record_spend(requests=3, connector="official")
        b.record_failure()
        b.record_blocked()
        b.record_observation()
        snap = b.snapshot()
        for key in ("requests", "third_party_cost_usd", "runtime_ms", "failures", "blocked_sources", "successful_observations"):
            self.assertIn(key, snap)
        self.assertEqual(snap["requests"], 3)
        self.assertEqual(snap["failures"], 1)

    def test_exhausted_when_hard_request_cap_reached(self):
        b = Budget(BudgetLimits(max_requests=2))
        b.record_spend(requests=2)
        self.assertTrue(b.exhausted())


# --------------------------------------------------------------------------- #
# ENTITY RESOLUTION
# --------------------------------------------------------------------------- #
def valid_observation(**changes):
    base = {
        "id": "obs-1",
        "organisation_number": "998877665",
        "platform": "google_places",
        "signal_type": "review",
        "source_url": "https://maps.google.com/example",
        "retrieved_at": "2026-08-20T00:00:00Z",
        "content_sha256": "a" * 64,
        "exact_entity": True,
        "identity_proof": [{"type": "address_match", "value": "Oslo"}],
        "acquisition_mode": "official_api",
        "rights_status": "approved",
        "source_class": "customer_review",
        "evidence_span": "Helpful staff",
    }
    return {**base, **changes}


class EntityResolutionTests(unittest.TestCase):
    def test_website_verified_ambiguous_rejected(self):
        self.assertEqual(resolve_website(with_website(base_profile(), verified=True)).status, VERIFIED)
        # Partial-overlap profile -> ambiguous. Name shares one token with content.
        profile = base_profile(name="Example Consulting Group")
        profile = with_website(profile, verified=False)
        profile["evidence"]["website"]["value"]["identity_text_excerpt"] = "Example only"
        profile["evidence"]["website"]["value"]["title"] = "Example only"
        res = resolve_website(profile)
        self.assertIn(res.status, {AMBIGUOUS, REJECTED})
        # Clearly unrelated -> rejected.
        rejected = with_website(base_profile(name="Zzz Totally Different Enterprise"), verified=False)
        self.assertEqual(resolve_website(rejected).status, REJECTED)

    def test_social_verified_and_rejected(self):
        profile = base_profile(name="Example")
        good = resolve_social(profile, {"platform": "youtube", "url": "https://youtube.com/@example"})
        self.assertEqual(good.status, VERIFIED)
        bad = resolve_social(base_profile(name="Zzz Different"), {"platform": "youtube", "url": "https://youtube.com/@example"})
        self.assertEqual(bad.status, REJECTED)

    def test_observation_verified(self):
        self.assertEqual(resolve_observation(valid_observation()).status, VERIFIED)

    def test_observation_org_mismatch_rejected(self):
        res = resolve_observation(valid_observation(organisation_number="123456785"), organisation_number="998877665")
        self.assertEqual(res.status, REJECTED)

    def test_observation_without_identity_proof_rejected(self):
        res = resolve_observation(valid_observation(exact_entity=False, identity_proof=None))
        self.assertEqual(res.status, REJECTED)

    def test_observation_missing_hash_is_ambiguous_not_rejected(self):
        # Identity is fine, but a structural gap means it is not publishable yet.
        res = resolve_observation(valid_observation(content_sha256=None))
        self.assertEqual(res.status, AMBIGUOUS)

    def test_search_candidate_is_never_verified_from_snippet(self):
        profile = base_profile(name="Example", municipality="OSLO")
        result = {"url": "https://example.no/", "title": "Example AS 998877665 Oslo", "snippet": "Example AS Oslo 998877665", "rank": 1}
        res = resolve_search_candidate(profile, result)
        self.assertIn(res.status, {AMBIGUOUS, REJECTED})
        self.assertNotEqual(res.status, VERIFIED)


# --------------------------------------------------------------------------- #
# SOURCE POLICY
# --------------------------------------------------------------------------- #
class SourcePolicyTests(unittest.TestCase):
    def test_official_source_always_allowed(self):
        decision = sp.evaluate_source(source_class="official_registry_bulk", is_official=True)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.availability, "available")

    def test_directory_or_social_host_flagged(self):
        self.assertTrue(sp.is_directory_or_social_host("https://www.linkedin.com/company/x"))
        self.assertTrue(sp.is_directory_or_social_host("https://proff.no/company"))
        self.assertFalse(sp.is_directory_or_social_host("https://example.no/"))

    def test_experimental_acquisition_mode_not_publishable(self):
        decision = sp.evaluate_source(source_class="public_news", acquisition_mode="jobspy_experiment", rights_status="approved")
        self.assertFalse(decision.allowed)

    def test_rights_not_approved_blocks_publication(self):
        decision = sp.evaluate_source(source_class="public_news", acquisition_mode="licensed_api", rights_status="review_required")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.availability, "not_available")

    def test_blocked_rights_maps_to_blocked_availability(self):
        decision = sp.evaluate_source(source_class="public_news", acquisition_mode="licensed_api", rights_status="blocked")
        self.assertEqual(decision.availability, "blocked")

    def test_source_precedence_orders_official_above_third_party(self):
        self.assertLess(sp.source_precedence_rank("official_registry_bulk"), sp.source_precedence_rank("company_owned"))
        self.assertLess(sp.source_precedence_rank("company_owned"), sp.source_precedence_rank("public_news"))


# --------------------------------------------------------------------------- #
# ENRICHMENT CONTROLLER
# --------------------------------------------------------------------------- #
class EnrichmentTests(unittest.TestCase):
    def test_permitted_identified_useful_task_scheduled(self):
        profile = with_website(base_profile(), verified=True)
        plan = EnrichmentController(Budget()).plan(profile)
        scheduled = {t["connector"] for t in plan["scheduled"]}
        self.assertIn("google_places_api", scheduled)
        self.assertIn("jobs_provider", scheduled)

    def test_review_required_connector_is_skipped(self):
        # A LinkedIn social link produces a linkedin_connector task with no
        # approved rights path -> skipped.
        profile = with_website(base_profile(), verified=True, social=[{"platform": "linkedin", "url": "https://linkedin.com/company/example"}])
        plan = EnrichmentController(Budget()).plan(profile)
        skipped = {t["connector"]: t["reason"] for t in plan["skipped"]}
        self.assertIn("linkedin_connector", skipped)
        self.assertIn("rights", skipped["linkedin_connector"])

    def test_unverified_social_handle_task_skipped(self):
        # A social link that survived into the site value but does not match the
        # legal entity must be rejected by the controller's own identity gate.
        profile = with_website(
            base_profile(name="Zzz Different"), verified=True, gate=False,
            social=[{"platform": "youtube", "url": "https://youtube.com/@example"}],
        )
        plan = EnrichmentController(Budget()).plan(profile)
        skipped = {t["connector"]: t["reason"] for t in plan["skipped"]}
        self.assertIn("youtube_connector", skipped)
        self.assertIn("identity", skipped["youtube_connector"])

    def test_redundant_discovery_search_skipped_when_handles_known(self):
        profile = with_website(base_profile(), verified=True, social=[{"platform": "youtube", "url": "https://youtube.com/@example"}])
        plan = EnrichmentController(Budget()).plan(profile)
        self.assertNotIn("permitted_search_api", {t["connector"] for t in plan["scheduled"]})

    def test_budget_exhaustion_degrades_gracefully(self):
        profile = with_website(base_profile(), verified=True)
        tiny = Budget(BudgetLimits(max_requests=0))
        plan = EnrichmentController(tiny).plan(profile)
        self.assertEqual(plan["scheduled"], [])
        self.assertTrue(any("budget" in t["reason"] for t in plan["skipped"]))


# --------------------------------------------------------------------------- #
# REFRESH CANONICALIZATION
# --------------------------------------------------------------------------- #
class RefreshCanonicalizationTests(unittest.TestCase):
    def test_whitespace_only_change_is_not_material(self):
        prev = base_profile(name="Example AS")
        curr = base_profile(name="Example   AS")
        self.assertEqual(diff_profile(prev, curr), [])

    def test_url_tracking_params_and_www_are_not_material(self):
        prev = base_profile(website="https://www.example.no")
        curr = base_profile(website="https://example.no/?utm_source=news&fbclid=1")
        self.assertEqual(diff_profile(prev, curr), [])

    def test_social_link_reordering_is_not_material(self):
        a = {"platform": "youtube", "url": "https://youtube.com/@example"}
        b = {"platform": "linkedin", "url": "https://linkedin.com/company/example"}
        prev = with_website(base_profile(), verified=True, social=[a, b])
        curr = with_website(base_profile(), verified=True, social=[b, a])
        social_changes = [c for c in diff_profile(prev, curr) if c["field"] == "website.social_links"]
        self.assertEqual(social_changes, [])

    def test_real_employee_change_produces_exactly_one_event(self):
        prev = base_profile(employees=5)
        curr = base_profile(employees=6)
        changes = diff_profile(prev, curr)
        emp = [c for c in changes if c["field"] == "registry.employees"]
        self.assertEqual(len(emp), 1)
        self.assertEqual(emp[0]["old_value"], 5)
        self.assertEqual(emp[0]["new_value"], 6)

    def test_refresh_is_idempotent(self):
        curr = base_profile(employees=6)
        self.assertEqual(diff_profile(curr, curr), [])

    def test_canonicalize_preserves_real_difference(self):
        self.assertNotEqual(
            canonicalize("registry.website", "https://example.no"),
            canonicalize("registry.website", "https://other.no"),
        )


# --------------------------------------------------------------------------- #
# CONTRACT ENVELOPE
# --------------------------------------------------------------------------- #
class ContractEnvelopeTests(unittest.TestCase):
    def _envelope(self, **kwargs):
        profile = with_website(base_profile(), verified=True)
        profile["evidence"]["financials"] = financials_record()
        return terminal_envelope(
            profile, run_id="run-1", modules=["registry", "financials", "website"],
            started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:01:00Z", **kwargs
        )

    def test_envelope_has_all_contract_fields(self):
        env = self._envelope()
        for key in ("organisation_number", "run", "claims", "evidence", "changes", "errors", "operations"):
            self.assertIn(key, env)
        for key in ("run_id", "started_at", "completed_at", "terminal_status"):
            self.assertIn(key, env["run"])
        for key in ("requests", "runtime_ms", "third_party_cost_usd"):
            self.assertIn(key, env["operations"])

    def test_envelope_preserves_internal_validator_keys(self):
        env = self._envelope()
        for key in ("state", "modules", "profile", "run_id"):
            self.assertIn(key, env)
        self.assertEqual(env["modules"]["registry"]["state"], "complete")

    def test_change_detection_populated_from_previous_profile(self):
        previous = with_website(base_profile(employees=5), verified=True)
        previous["evidence"]["financials"] = financials_record()
        env = self._envelope(previous_profile=previous)  # current has employees=12
        emp = [c for c in env["changes"] if c["field"] == "registry.employees"]
        self.assertEqual(len(emp), 1)

    def test_source_error_surfaces_as_structured_error(self):
        profile = with_website(base_profile(), verified=True)
        profile["evidence"]["financials"] = financials_record(status="source_error")
        env = terminal_envelope(profile, run_id="r", modules=["registry", "financials"], started_at="s", completed_at="c")
        self.assertTrue(any(e.get("stage") == "financials" for e in env["errors"]))

    def test_synthesis_included_and_grounded(self):
        env = self._envelope()
        self.assertIn("synthesis", env)
        self.assertGreater(env["synthesis"]["fact_count"], 0)


# --------------------------------------------------------------------------- #
# CONNECTOR EXECUTION PIPELINE
# --------------------------------------------------------------------------- #
from norway_company_agent.connectors import (  # noqa: E402
    Connector,
    ConnectorRegistry,
    ConnectorResult,
    GoogleNewsRssConnector,
    deduplicate_observations,
)


def add_news_page(profile):
    v = profile["evidence"]["website"]["value"]
    v["pages"] = [{"url": "https://example.no/news/launch", "title": "We launched a product",
                   "main_text_excerpt": "Company launched something.", "content_sha256": "f" * 64}]
    return profile


def news_fetcher(profile):
    # Mimics the real RSS connector output: experimental/rights_review -> ambiguous.
    obs = {
        "id": "google-news-mock-1", "organisation_number": str(profile["organisation_number"]),
        "platform": "news", "signal_type": "public_mention", "source_url": "https://news.example.no/story",
        "retrieved_at": "2026-09-01T00:00:00Z", "content_sha256": "e" * 64, "exact_entity": True,
        "identity_proof": [{"type": "exact_legal_name_in_news_title", "value": profile.get("name")}],
        "acquisition_mode": "rights_review_experiment", "rights_status": "review_required",
        "source_class": "public_news", "evidence_span": "Headline mentioning the company",
    }
    return [obs], {"accepted": 1}


class _FakeConnector(Connector):
    name = "fake"
    source_class = "company_site"
    default_enabled = True
    est_requests = 1
    est_cost = 0.02

    def __init__(self, observations, status="available", requests=1, cost=0.02):
        self._obs = observations
        self._status = status
        self._requests = requests
        self._cost = cost

    def is_useful(self, profile):
        return True

    def _run(self, profile):
        return ConnectorResult(self.name, self.source_class, self._status, self._obs, requests=self._requests, cost_usd=self._cost)


class ConnectorExecutionTests(unittest.TestCase):
    def test_A_enabled_connector_executes_and_produces_observation(self):
        profile = with_website(base_profile(), verified=True)
        run = EnrichmentController(Budget()).execute(profile)
        statuses = {r["connector"]: r["status"] for r in run["connector_results"]}
        self.assertEqual(statuses.get("company_site_activity"), "available")
        self.assertGreaterEqual(len(run["verified_observations"]), 1)

    def test_B_actual_request_count_increments(self):
        profile = base_profile()
        reg = ConnectorRegistry([GoogleNewsRssConnector(fetcher=news_fetcher)])
        reg.enable("news_rss")
        b = Budget()
        EnrichmentController(b, reg).execute(profile)
        self.assertEqual(b.executed_requests, 1)

    def test_C_actual_cost_increments(self):
        profile = base_profile()
        reg = ConnectorRegistry([_FakeConnector([valid_observation()])])
        b = Budget()
        EnrichmentController(b, reg).execute(profile)
        self.assertAlmostEqual(b.executed_cost_usd, 0.02)

    def test_D_planned_cost_differs_from_executed_cost(self):
        profile = with_website(base_profile(), verified=True)
        b = Budget()
        ctrl = EnrichmentController(b, ConnectorRegistry())
        ctrl.plan(profile)      # reserves planned budget for API-style tasks
        ctrl.execute(profile)   # executes only zero-cost company-site connectors
        snap = b.snapshot()
        self.assertGreater(snap["planned_requests"], 0)
        self.assertEqual(snap["executed_requests"], 0)
        self.assertNotEqual(snap["planned_requests"], snap["executed_requests"])

    def test_E_disabled_connector_does_not_execute(self):
        profile = with_website(base_profile(), verified=True)
        run = EnrichmentController(Budget(), ConnectorRegistry()).execute(profile)
        self.assertNotIn("news_rss", {r["connector"] for r in run["connector_results"]})

    def test_F_blocked_connector_when_budget_exhausted(self):
        profile = base_profile()
        reg = ConnectorRegistry([GoogleNewsRssConnector(fetcher=news_fetcher)])
        reg.enable("news_rss")
        run = EnrichmentController(Budget(BudgetLimits(max_requests=0)), reg).execute(profile)
        statuses = {r["connector"]: r["status"] for r in run["connector_results"]}
        self.assertEqual(statuses.get("news_rss"), "blocked")

    def test_G_ambiguous_observation_never_becomes_available_claim(self):
        profile = base_profile()
        reg = ConnectorRegistry([GoogleNewsRssConnector(fetcher=news_fetcher)])
        reg.enable("news_rss")
        run = EnrichmentController(Budget(), reg).execute(profile)
        self.assertEqual(run["verified_observations"], [])
        self.assertEqual(len(run["ambiguous_observations"]), 1)
        # It never becomes a published claim.
        claims = extract_claims(profile, observations=run["verified_observations"])["claims"]
        self.assertFalse(any(c["field"] == "external_public_mention" for c in claims))

    def test_H_rejected_observation_never_becomes_claim(self):
        profile = base_profile()
        wrong = valid_observation(organisation_number="123456785")  # different org
        reg = ConnectorRegistry([_FakeConnector([wrong], cost=0.0, requests=0)])
        run = EnrichmentController(Budget(), reg).execute(profile)
        self.assertEqual(run["verified_observations"], [])
        self.assertEqual(len(run["rejected_observations"]), 1)

    def test_I_J_verified_observation_becomes_claim_with_evidence(self):
        profile = with_website(base_profile(), verified=True)
        run = EnrichmentController(Budget()).execute(profile)
        result = extract_claims(profile, observations=run["verified_observations"])
        ext = [c for c in result["claims"] if c["field"].startswith("external_")]
        self.assertTrue(ext)
        evidence_ids = {e["id"] for e in result["evidence"]}
        for claim in ext:
            self.assertTrue(claim["evidence_ids"])
            for eid in claim["evidence_ids"]:
                self.assertIn(eid, evidence_ids)

    def test_K_duplicate_observations_merge(self):
        obs = valid_observation()
        deduped, dupes = deduplicate_observations([obs, dict(obs)])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(dupes, 1)

    def test_L_independent_observations_preserved(self):
        a = valid_observation(id="a", source_url="https://maps.google.com/a")
        b = valid_observation(id="b", source_url="https://maps.google.com/b")
        deduped, dupes = deduplicate_observations([a, b])
        self.assertEqual(len(deduped), 2)
        self.assertEqual(dupes, 0)

    def test_M_refresh_detects_genuine_external_change(self):
        prev = base_profile()
        prev["external_metrics"] = {"review_count": 120, "active_job_count": 0, "public_item_count": 0}
        curr = base_profile()
        curr["external_metrics"] = {"review_count": 135, "active_job_count": 0, "public_item_count": 0}
        changes = [c for c in diff_profile(prev, curr) if c["field"] == "external_footprint.review_count"]
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["old_value"], changes[0]["new_value"]), (120, 135))

    def test_N_identical_external_refresh_no_change(self):
        p = base_profile()
        p["external_metrics"] = {"review_count": 135, "active_job_count": 2, "public_item_count": 3}
        self.assertEqual(diff_profile(p, json.loads(json.dumps(p))), [])

    def test_O_terminal_envelope_valid_after_enrichment(self):
        from norway_company_agent.batch import validate_envelopes
        profile = add_news_page(with_website(base_profile(), verified=True))
        run = EnrichmentController(Budget()).execute(profile)
        env = terminal_envelope(profile, run_id="r", modules=["registry", "website"],
                                started_at="s", completed_at="c", observations=run["verified_observations"])
        self.assertTrue(validate_envelopes([env], 1)["passed"])
        for key in ("organisation_number", "run", "claims", "evidence", "changes", "errors", "operations"):
            self.assertIn(key, env)


# --------------------------------------------------------------------------- #
# RUNTIME BUDGET ENFORCEMENT (end-to-end, offline: no network is reached)
# --------------------------------------------------------------------------- #
import csv
import gzip
import subprocess
import tempfile


class BudgetEnforcementTests(unittest.TestCase):
    def _tiny_bulk(self, directory: Path, n: int = 3) -> tuple[Path, Path]:
        cols = ["organisasjonsnummer", "navn", "organisasjonsform.kode", "antallAnsatte", "konkurs",
                "underAvvikling", "forretningsadresse.kommune", "forretningsadresse.kommunenummer",
                "naeringskode1.kode", "naeringskode1.beskrivelse", "hjemmeside", "sisteInnsendteAarsregnskap"]
        rows = []
        for i in range(1, n + 1):
            rows.append({c: "" for c in cols} | {
                "organisasjonsnummer": f"90000000{i}"[:9], "navn": f"Testbedrift {i} AS",
                "organisasjonsform.kode": "AS", "antallAnsatte": str(i), "konkurs": "false",
                "underAvvikling": "false", "forretningsadresse.kommune": "OSLO",
                "naeringskode1.kode": "62.010", "hjemmeside": "https://example.no/",
            })
        bulk = directory / "bulk.csv.gz"
        buf = "\n".join([";".join(cols)] + [";".join(str(r[c]) for c in cols) for r in rows]) + "\n"
        with gzip.open(bulk, "wt", encoding="utf-8") as fh:
            fh.write(buf)
        orgs = directory / "orgs.jsonl"
        orgs.write_text("".join(json.dumps({"organisation_number": r["organisasjonsnummer"]}) + "\n" for r in rows), encoding="utf-8")
        return bulk, orgs

    def test_zero_request_budget_blocks_network_and_still_emits_envelopes(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            bulk, orgs = self._tiny_bulk(directory, 3)
            env_out = directory / "env.jsonl"
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "run_competition_batch.py"),
                 "--organisations", str(orgs), "--bulk", str(bulk),
                 "--profiles-output", str(directory / "profiles.jsonl"),
                 "--output", str(env_out), "--report", str(directory / "report.json"),
                 "--run-id", "budget-test", "--expected-count", "3",
                 "--modules", "registry,accounting_obligation,registry_live,website",
                 "--max-requests", "0"],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
            envelopes = [json.loads(l) for l in env_out.read_text().splitlines() if l.strip()]
            self.assertEqual(len(envelopes), 3)  # exactly one per input, none dropped
            report = json.loads((directory / "report.json").read_text())
            self.assertTrue(report["validation"]["passed"])
            self.assertEqual(report["budget"]["executed_requests"], 0)  # no network reached
            self.assertGreater(report["budget"]["blocked_sources"], 0)
            for env in envelopes:
                rl = env["profile"]["evidence"]["registry_live"]
                web = env["profile"]["evidence"]["website"]
                self.assertEqual(rl["status"], "blocked")
                self.assertEqual(rl["note"], "budget_exhausted")
                self.assertEqual(web["status"], "blocked")
                # Registry + accounting_obligation come from the bulk (0 requests) and remain available.
                self.assertEqual(env["profile"]["evidence"]["registry"]["status"], "available")


if __name__ == "__main__":
    unittest.main()
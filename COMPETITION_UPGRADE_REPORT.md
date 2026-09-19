# Signalpost competition upgrade — engineering report

This report documents the upgrade of the Signalpost reference agent for the Builderr
competition. The work extends the existing, already-strong starter kit; it does not
rewrite it. All original behaviour and tests are preserved.

## 1. Files created

New modules under `src/norway_company_agent/`:

| File | Purpose |
|---|---|
| `source_policy.py` | Single source of truth for permitted/blocked source classes, acquisition modes, rights states, directory/social host policy, and deterministic source precedence. Reuses existing `external_footprint` and `discovery` constants. |
| `entity_resolution.py` | Unified `verified` / `ambiguous` / `rejected` resolver wrapping the existing website, social, search-candidate, and observation identity checks. Enforces *WRONG COMPANY > MISSING INFORMATION*. |
| `budget.py` | Thread-safe central request / cost / runtime / failure budget with soft-headroom (`can_spend`, `try_spend`) and graceful degradation. |
| `claims.py` | Deterministic OUTPUT_CONTRACT claim + evidence extraction from a profile. Enforces missing ≠ zero, availability mapping, evidence linking, dedup, and registry conflict recording. |
| `synthesis.py` | Evidence-grounded factual company summary built only from `available` claims; every sentence carries its supporting evidence ids; no filler. |
| `enrichment.py` | `EnrichmentController` making an adaptive, per-company decision on which external connectors to run, gated by identity, source policy, usefulness, and budget. |

New tests: `tests/test_upgrades.py` (52 tests).

## 2. Files modified

| File | Change (additive / backward-compatible) |
|---|---|
| `src/norway_company_agent/batch.py` | `terminal_envelope` now emits the OUTPUT_CONTRACT shape (`organisation_number`, `run`, `claims`, `evidence`, `changes`, `errors`, `operations`) **plus** the pre-existing internal keys (`state`, `modules`, `profile`, `run_id`, …). New optional params: `previous_profile`, `observations`, `operations`, `errors`, `include_summary`. Also emits `synthesis` and `conflicts`. |
| `src/norway_company_agent/refresh.py` | Added `canonicalize()` and use it for equality in `diff_profile` so whitespace, URL tracking params, `www.`/trailing-slash, and social-link ordering never register as false changes. Raw `old_value`/`new_value` are still emitted. |
| `scripts/run_competition_batch.py` | Wired in `Budget`, `EnrichmentController` (planning only — no live calls), verified-observation gating via `resolve_observation`, optional `--previous-profiles` change detection, and contract envelopes. New optional flags; **all original flags preserved**. Report gains `budget`, `change_detection`, `external_observations`, `enrichment_planning`. |

## 3. Major architectural changes

The pipeline now terminates in an explicit, auditable contract layer:

```
official BRREG + website + verified observations
        -> entity_resolution (verified/ambiguous/rejected)
        -> source_policy (permitted? rights? precedence?)
        -> claims.extract_claims (deterministic, missing != zero, evidence-linked)
        -> conflict recording (bulk vs live, precedence-resolved, provenance kept)
        -> refresh.diff_profile (canonicalized, idempotent change detection)
        -> synthesis (only available, evidence-linked facts)
        -> terminal_envelope (OUTPUT_CONTRACT compliant)
```

Deterministic code owns identity, source identity, URL/date/number normalization,
financial-year association, dedup, schema, evidence linking, change detection, and
budgets. No LLM is on the critical path; the evidence system is the source of truth.

## 4. Connectors

Existing connector scripts are retained unchanged. They are now *orchestrated by
policy* rather than run blindly: `EnrichmentController` decides per company which
connectors are worth running, and `source_policy` marks connectors whose only path
is an unofficial scraper as `review_required` (never scheduled for publication).
No new scraping or bypassing was added. Live connector execution remains gated on
declared API access / rights.

## 5. Identity protections

- Three-state resolution (`verified`/`ambiguous`/`rejected`) for websites, social
  handles, search candidates, and external observations.
- A search snippet can never be `verified` from the snippet alone (crawl candidate
  at best).
- Organisation-number mismatch on an observation is an immediate `rejected`.
- Website-derived claims are `ambiguous` unless the exact entity is proven; social
  links only publish when both the site and handle pass identity.
- Verified end-to-end in the smoke run: a wrong-company observation was quarantined
  while a matching one was published.

## 6. Evidence improvements

- Every published (`available`) claim references at least one real evidence id
  (verified in tests and in the smoke run: zero dangling references).
- Evidence records carry `id`, `source_url`, `source_class`, `retrieved_at`,
  `content_sha256`, `claim_span`.
- Bulk-vs-live registry conflicts are recorded with both values, provenance kept,
  and the more current source chosen as canonical.

## 7. Refresh improvements

- Canonicalization prevents false changes from whitespace, URL tracking params,
  `www.`/trailing-slash, and link ordering.
- Real value changes (e.g. employees 5→6) still produce exactly one event with full
  provenance; identical re-runs produce zero. Verified through the runner and the
  bundled replay (precision/recall 1.0, 0 false positives, idempotent).

## 8. Budget controls

Central budget tracks total requests, per-connector requests/cost, runtime,
failures, blocked sources, and successful observations. Optional enrichment is
gated by a soft headroom (default 90%) so mandatory foundation work always has
room; hard limits stop spend and set a `degraded` flag. Every company still
receives a terminal envelope.

## 9. Tests

- 52 new tests in `tests/test_upgrades.py` covering: claims (missing≠zero, 404,
  source_error→failed, zero preserved, multi-year, website verified/unverified/
  blocked, evidence linking, registry conflict, observation→claim, dedup),
  synthesis (grounding, no filler, zero stated), budget (hard/soft/cost/exhausted/
  snapshot), entity resolution (website/social/observation/search states,
  org-mismatch), source policy (official/directory/experimental/rights/precedence),
  refresh canonicalization (whitespace/tracking/www/reorder/real-change/idempotent),
  and the contract envelope (all fields, preserved internal keys, change detection,
  structured errors, synthesis).

## 10. Tests actually passed

- **`uv run --with pytest pytest -q` → 156 passed, 5 subtests passed, 11 warnings, ~1.4s.**
  (104 pre-existing tests preserved + 52 new.)
- Refresh replay (`scripts/run_refresh_replay.py`) → `qualification_passed: true`,
  precision 1.0, recall 1.0, 0 false positives, idempotent.

## 11. Smoke-test results (offline, synthetic 10-company fixture)

- 10/10 terminal envelopes emitted; `validation.passed = true` (exact count,
  unique orgs, all states terminal, zero silent drops).
- Envelopes are OUTPUT_CONTRACT-compliant; `registered_website`/`verified_website`
  correctly `not_available` (never `""`/`0`); bankrupt entity detected and
  reflected in synthesis.
- Change detection: 1 real change (employees 5→6) with full provenance; identical
  re-run → 0 changes.
- Observations: 1 verified observation published as a claim; 1 wrong-company
  observation quarantined.

*Note:* the smoke run is offline and synthetic (no live BRREG / API access in this
environment). It exercises the registry, accounting-obligation, website, claims,
synthesis, budget, enrichment-planning, change-detection, and observation paths
deterministically. Live official-API modules (`registry_live`, `financials`,
`roles`, `group`, `locations`) and live website crawling were not exercised here
because they require network access.

## 12. Limitations / assumptions

- Live external connectors are **planned** by the controller but not executed in
  this environment; publication still requires declared API access and approved
  rights. Enrichment `cost`/`requests` in the report reflect the *plan*, not live
  spend.
- Sentiment and unqualified external signals remain quarantined until their
  held-out accuracy and rights gates pass, consistent with the existing policy.
- No new third-party dependencies were added; the project remains reproducible with
  the pinned `uv.lock`.

## 13. Exact command to run the agent (live, per README)

```bash
uv sync
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
uv run python scripts/run_competition_batch.py \
  --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id local-001 \
  --expected-count 1000 \
  --max-requests 45000 --max-cost-usd 25 --max-runtime-seconds 3600
```

## 14. Exact command to run tests

```bash
uv run --with pytest pytest -q
```

## 15. Exact command to run the 10-company smoke test (live)

```bash
head -n 10 entry-companies.jsonl > smoke-companies.jsonl
uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 10
```

Offline reproduction of the smoke test used in this report (no network):

```bash
# builds out/smoke-bulk.csv.gz + out/smoke-orgs.jsonl, then:
uv run python scripts/run_competition_batch.py \
  --organisations out/smoke-orgs.jsonl --bulk out/smoke-bulk.csv.gz \
  --profiles-output out/smoke-profiles.jsonl --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json --run-id smoke-offline-001 --expected-count 10 \
  --modules registry,accounting_obligation,website --plan-enrichment
```

## 16. Before the 1,000-company submission

- Run `uv sync` and the live 10-company smoke, then the full 1,000-company batch
  with a bounded budget, on a machine with network access.
- Confirm live `registry_live`/`financials`/`roles`/`group`/`locations`/`website`
  outcomes are all classified (no `submission_error`).
- If external connectors are enabled, supply declared API credentials and feed
  their verified observations via `--observations`.
- Freeze the exact organisation-number manifest and commit.

## 17. Assumptions about external APIs / licences / source rights

- BRREG open data is used under NLOD 2.0.
- Directory/aggregator/social hosts can nominate but never prove identity.
- Any search/news/jobs/reviews connector is `review_required` until its storage,
  source, and crawling rights are explicitly accepted; unofficial scrapers are
  never scheduled for publication.

---

## Competition readiness checklist

- [x] Exactly one terminal envelope per input (validated: zero silent drops).
- [x] OUTPUT_CONTRACT shape: `organisation_number`, `run`, `claims`, `evidence`, `changes`, `errors`, `operations`.
- [x] Availability states used correctly; **missing ≠ zero** (404 → `not_available`, source_error → `failed`, legitimate 0 preserved).
- [x] Every published claim references real evidence with URL, class, retrieval time, and content hash.
- [x] Exact-entity gate: verified/ambiguous/rejected; wrong-company observations quarantined.
- [x] Conflicts recorded with provenance; authoritative source wins the canonical value.
- [x] Refresh deterministic + idempotent; canonicalization prevents false changes.
- [x] Evidence-grounded synthesis with no filler.
- [x] Central budget with graceful degradation; every company still gets an envelope.
- [x] Adaptive enrichment controller (no blind connector fan-out); source policy respected.
- [x] 104 original tests preserved; 156 total pass; refresh replay qualifies.
- [x] CLI backward-compatible; reproducible via pinned `uv.lock` with no new deps.
- [ ] Live 1,000-company run executed with credentials/network (pending environment access).

---

## Addendum — connector execution pipeline (planning → execution)

The `EnrichmentController` is now a real, budget-controlled **execution** pipeline, not
planning-only.

### Architecture
```
BRREG foundation -> verified identity -> ConnectorRegistry (enabled + useful)
  -> connector.execute() -> raw observations
  -> resolve_observation (verified | ambiguous | rejected)
  -> deduplicate_observations (merge same fact, keep independent sources)
  -> claims.extract_claims (only verified -> available claims, evidence-linked)
  -> synthesis -> terminal_envelope
```

### Files created
- `src/norway_company_agent/connectors.py` — `Connector` base, `ConnectorResult`,
  `ConnectorRegistry`, `CompanySiteActivityConnector`, `CompanySiteNewsConnector`,
  `GoogleNewsRssConnector` (optional/network), `deduplicate_observations`. Reuses the
  existing `scripts/extract_company_site_activity.py`, `scripts/extract_company_site_news.py`,
  and `scripts/run_google_news_rss_connector.py` — no duplicated connector logic.

### Files modified
- `budget.py` — separated **planned** (`reserve`) from **executed** (`commit`) spend;
  `snapshot()` reports both; `operations`/`requests`/`cost_usd` reflect executed only.
- `enrichment.py` — added `execute()`; `plan()` now uses `reserve()`; registry support.
- `refresh.py` — added `external_footprint.review_count/active_job_count/public_item_count`
  tracked fields for external change detection.
- `source_policy.py` — added `company_site` to publishable classes + precedence.
- `run_competition_batch.py` — `--enable-enrichment`, `--connectors`; per-company
  `operations` reflect executed enrichment spend; report gains `enrichment_execution`
  and planned-vs-executed budget.

### Connectors actually integrated (real execution)
- **`company_site_activity`** and **`company_site_news`** — enabled by default, run on the
  already-fetched verified website, **0 extra network requests**, produce genuinely
  publishable exact-entity observations. Verified end-to-end offline: a verified-website
  profile yielded 2 verified observations that became evidence-linked `external_*` claims
  and entered synthesis, with `executed_requests=0`, `executed_cost=0.0`.

### Connectors optional (and why)
- **`news_rss`** (Google News RSS) — disabled by default; requires network; the existing
  script marks its output `rights_review_experiment`/`review_required`, so observations
  resolve to `ambiguous` and are never published. Demonstrates the live path + rights gate
  honestly (tested with an injected mock fetcher — no real network call was made).
- API connectors (`google_places`, `jobs`, `reviews`, licensed search) remain planned by
  `plan()` but are not executed without declared credentials/rights.

### Budget accounting (real, from offline smoke with `--enable-enrichment --plan-enrichment`)
- `executed_requests=0`, `executed_cost_usd=0.0` (nothing actually spent offline)
- `planned_requests=40`, `planned_cost_usd=0.37` (plan reservations, **kept separate**)
- Per-company envelope `operations` show executed values only.

### Tests (real results)
- Full suite: **`uv run --with pytest pytest -q` → 170 passed, 5 subtests** (156 previous + 14
  new `ConnectorExecutionTests` A–O).
- Refresh replay: `qualification_passed: true`.
- Smoke (10 companies): 10 envelopes, `validation.passed=true`, **0 dangling available claims,
  0 empty-values-marked-available**, all envelopes contain `run/claims/evidence/changes/errors/operations`.

### Offline vs live commands
```bash
# Offline (deterministic; company-site connectors only, no network):
uv run python scripts/run_competition_batch.py --organisations out/smoke-orgs.jsonl \
  --bulk out/smoke-bulk.csv.gz --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl --report out/smoke-report.json \
  --run-id smoke-enrich-001 --expected-count 10 \
  --modules registry,accounting_obligation,website --enable-enrichment

# Live (adds the network news connector; still never publishes review_required output):
uv run python scripts/run_competition_batch.py --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl --report out/run-report.json \
  --run-id local-001 --expected-count 1000 \
  --enable-enrichment --connectors company_site_activity,company_site_news,news_rss \
  --max-requests 45000 --max-cost-usd 25 --max-runtime-seconds 3600
```

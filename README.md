# Signalpost reference agent

## Start here

You are building an agent that finds public information about Norwegian companies and returns a sourced profile for each one.

1. Run the saved example: `python3 first_run.py`.
2. Open `out/refresh-demo.json` and confirm the two expected changes.
3. Download the public company list and select at least 1,000 organisation numbers.
4. Run your agent on ten companies first, then run the full batch.
5. Submit the repository URL, exact commit, run command and expected cost.

What to optimise: **find more useful facts, attach the right source to each fact, and avoid wrong-company claims**. A missing fact is acceptable when you label it `not_available`; an invented or misattributed fact is not. The daily test runs the same 100-company batch for every frozen agent.

The sections below explain the full contract and optional implementation ideas. You do not need to read them before the saved example runs.

This is a runnable starting point for the Signalpost company-research challenge. It is intentionally a solid baseline, not a winning submission.

The public universe contains 411,160 eligible companies. A valid entry must process at least 1,000; you may process 10,000 or the full universe.

## What it already does

- reads a batch of Norwegian organisation numbers;
- anchors identity in the Brønnøysund bulk registry;
- fetches official financials, roles, group links and registered workplaces;
- visits the registry-listed website and rejects weak entity matches;
- emits one terminal JSONL envelope per input;
- records sources, retrieval times, content hashes, request counts and latency;
- supports checkpoint/resume and a deterministic refresh replay;
- includes examples for external-footprint discovery and an evidence-bounded research agent.

## First run: try one saved example

Requires Python 3.12+. Open a terminal inside this extracted folder.

Before downloading company data or running a full crawl, try the bundled public
sample. It uses saved responses: no API key, registry download or live web requests.

On Windows:

```bash
py first_run.py
```

On macOS or Linux:

```bash
python3 first_run.py
```

If the launcher does not work, run the same check directly:

```bash
python3 scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json --output out/refresh-demo.json
```

Open `out/refresh-demo.json`. The `events` list shows what changed between two
versions of one company profile and the source evidence for each change. The sample
should find two expected changes, no false changes, and no extra changes when the
same data is checked again.

The report's `qualification_passed` field refers only to this public sample check.
It does not qualify an entry for the competition or prove live information coverage.
The printed request counts are reads from saved responses, not network calls.

## Next: research live companies

Requires Python 3.12+ and `uv`. This step downloads data and makes live requests.
The manifest selector requires at least 1,000 companies for a full entry. You can
use its first ten rows for a private smoke test before running the full batch.

```bash
uv sync
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 1000 \
  --output entry-companies.jsonl

# Start with ten companies before the full 1,000-company run.
head -n 10 entry-companies.jsonl > smoke-companies.jsonl

uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 10

# When the smoke output looks right, run your full entry.
uv run python scripts/run_competition_batch.py \
  --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id local-001 \
  --expected-count 1000

uv run --with pytest pytest -q
```

The published archive was clean-room verified on August 24, 2026: 104 tests and 5 subtests passed, followed by a one-company live BRREG smoke run with one terminal envelope, five requests and zero silent drops.

Increase `--count` and `--expected-count` together if you want to publish more than the 1,000-company minimum. The ten-row smoke test above is practice only. Do not set `select_entry_batch.py --count 10`: the selector enforces the 1,000-company entry minimum.

## The improvement loop

1. Treat the organisation number as the anchor.
2. Generate site/profile candidates from official data, the company site, lawful search providers and named people.
3. Save every candidate and the evidence for or against it.
4. Publish only exact-entity matches. Parent, brand, franchise and similarly named companies are not exact.
5. Crawl static HTML first. Escalate to a browser only when a deterministic completeness check fails.
6. Measure added supported coverage, wrong-company claims, runtime, requests and cost.
7. Promote a strategy only when it improves coverage without weakening the accuracy gates.
8. Freeze strategies and thresholds before the daily evaluation run.

The strongest differentiator is external evidence that remains exact and auditable: official company pages, company-owned profiles, jobs, dated activity, ratings/reviews and permitted public signals. Do not trade accuracy for volume.

## Important source rule

Open-source code does not grant permission to scrape a platform. Follow each source's terms, robots policy, rate limits and licence. LinkedIn, Meta and Indeed are useful identity/discovery targets, but direct automated collection may be restricted. Use permitted APIs, licensed providers, company-owned outbound links, or return `blocked`/`not_available`.

Read `docs/competition-control-loop.md`, `docs/external-connectors.md` and the public source policy before adding connectors.

## Submission contract

Submit a repository with:

- at least 1,000 completed company profiles and the exact organisation-number manifest used;
- one documented command that accepts a JSONL batch of organisation numbers;
- exactly one terminal envelope per input;
- pinned dependencies and reproducible setup;
- a previous-snapshot input and material-change output;
- a machine-readable run report with runtime, request count and third-party cost;
- declared models, APIs, licences and source-rights assumptions.

Email the repository URL, run command, models/APIs and expected cost per 100-company run to `submit@builderr.ai`.

## Enhanced runner: budget-safe validation and submission commands

The batch runner enforces the competition constraints as hard limits (requests /
cost / runtime), records planned-vs-executed spend separately, runs adaptive,
identity-gated enrichment connectors, and emits OUTPUT_CONTRACT-compliant envelopes
(`organisation_number`, `run`, `claims`, `evidence`, `changes`, `errors`,
`operations`). Every input yields exactly one terminal envelope; budget-exhausted
modules are marked `blocked` rather than dropped.

Only company-owned, permitted connectors are enabled by default
(`company_site_activity`, `company_site_news`). Network/credentialed connectors
(e.g. `news_rss`, search/places/jobs/reviews) stay disabled until their rights are
accepted.

```bash
uv sync

# 10-company live validation (conservative limits well below the evaluator maximum)
uv run python scripts/run_competition_batch.py \
  --organisations out/live10-orgs.jsonl --bulk out/live10-bulk.csv.gz \
  --profiles-output out/live10-profiles.jsonl --output out/live10-envelopes.jsonl \
  --report out/live10-report.json --run-id live-10 --expected-count 10 \
  --enable-enrichment --connectors company_site_activity,company_site_news \
  --max-requests 300 --max-cost-usd 2 --max-runtime-seconds 600

# 100-company controlled validation
uv run python scripts/run_competition_batch.py \
  --organisations out/live100-orgs.jsonl --bulk out/live100-bulk.csv.gz \
  --profiles-output out/live100-profiles.jsonl --output out/live100-envelopes.jsonl \
  --report out/live100-report.json --run-id live-100 --expected-count 100 \
  --enable-enrichment --connectors company_site_activity,company_site_news \
  --max-requests 1600 --max-cost-usd 5 --max-runtime-seconds 1500

# Reproducible 1,000-company manifest (deterministic given the seed)
uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz --count 1000 --output entry-companies.jsonl

# Tests
uv run --with pytest pytest -q
```

Notes on artifacts: the frozen bulk anchor can be built from the official universe
records when the `enheter/lastned/csv` endpoint is unavailable. Run artifacts live
under `out/` (git-ignored); large downloadable blobs are not committed. See
`COMPETITION_UPGRADE_REPORT.md` for the full engineering report and readiness checklist.

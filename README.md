# Supplier Reliability Scoring Platform

A deterministic, explainable supplier-reliability scoring platform. It generates synthetic
supply-chain data, persists and validates it, scores suppliers on six components with full
lineage, injects and detects five classes of data anomalies, evaluates detection against a
separate ground truth, reconciles every stored score against an independent recomputation,
and exposes everything through a FastAPI backend and a Typer CLI.

Built for the YottaFlex AI candidate project. The original brief and its addendum are in
[`docs/specs/`](docs/specs/), the implementation plan is in
[`docs/superpowers/plans/`](docs/superpowers/plans/), and the AI-usage disclosure and prompt
history are in [`ai-usage/`](ai-usage/).

---

## Status (honest summary)

| Area | State |
|---|---|
| Domain model, migrations (11 core entities + supporting tables) | Done, tested |
| Deterministic synthetic generator (starter + full presets) | Done, tested |
| Scoring engine (6 components, missing-data policy, decisions, explanations, lineage, versioning, restatements) | Done, tested |
| Scoring benchmarks (stable / deteriorating / recovering, hand-calculated) | Done, tested |
| Validation framework (38 rules) | Done, tested |
| Anomaly injection A–E into a raw layer + separate ground truth | Done, tested |
| Content-based anomaly detection + quarantine + corrected scores | Done, tested |
| Evaluation (per-class TP/FP/FN/TN) + independent score reconciliation | Done, tested |
| JSON + Markdown reports | Done, tested |
| End-to-end pipeline + CLI | Done, tested |
| FastAPI (30 routes: health/ready/metadata, suppliers, POs, shipments, scores, validation, exceptions, dataset jobs, manifests, reports, evaluation) | Done, tested |
| Reproducibility workflow (`srp verify-reproducibility`) | **In progress — not yet in this commit** |
| Full documentation set (architecture, data dictionary, business rules, …) | **Not yet written** — design decisions currently live in the plan (`docs/superpowers/plans/…`, sections "Key Domain Decisions" D1–D13) |
| Dockerfile / docker-compose | **Not yet written** |
| Objective acceptance-gate runner (`srp acceptance`) | **Not yet written** |
| Full-scale end-to-end pipeline run recorded in `docs/results.md` | **Not yet executed** (individual stages were measured at full scale, see below) |
| PostgreSQL | **UNVERIFIED** — no PostgreSQL server on the build machine. All tests run on SQLite; PostgreSQL DDL is only compile-checked. |

Last full test run (default suite, run alone): **902 passed, 3 deselected** (the 3 are
full-scale tests marked `slow`), `ruff check` and `ruff format --check` clean.

---

## Requirements

- Python **3.12** (managed by [uv](https://docs.astral.sh/uv/))
- uv ≥ 0.4
- No external services, API keys, LLMs or datasets are needed. SQLite is the default database.

## Setup

```bash
# install uv if you don't have it
pip install uv            # or: see https://docs.astral.sh/uv/getting-started/installation/

uv python install 3.12
uv sync                   # creates .venv from uv.lock (dev tools included)

cp .env.example .env      # optional — defaults work without it
```

All settings use the `SRP_` prefix (see [`.env.example`](.env.example)). Scoring weights,
thresholds, generator presets and anomaly counts live in
[`config/default.yaml`](config/default.yaml).

## Run

```bash
# create the schema (Alembic migrations)
uv run srp init-db

# run the whole pipeline on the starter preset (50 suppliers, 300 POs, 450 shipments, 600 inspections)
uv run srp generate --preset starter

# full preset (1,000 suppliers, 10,000 POs, 15,000 shipments, 20,000 inspections)
uv run srp generate --preset full --replace

# start the API
uv run srp serve --host 127.0.0.1 --port 8000
```

Interactive API docs: **http://127.0.0.1:8000/docs** (OpenAPI JSON at `/openapi.json`).

`srp generate` runs: migrate → generate → load → score (6 trailing-12-month scorecards) →
inject anomalies → detect → validate → export → write ground truth → evaluate + reconcile →
reports → manifest. It refuses to overwrite an existing dataset unless `--replace` is passed.

Other commands: `srp score`, `srp recalculate`, `srp validate`, `srp detect`, `srp evaluate`,
`srp report` (run `uv run srp --help`).

Outputs (recreatable; large ones are git-ignored):

- `data/generated/<preset>/` — CSV + JSONL exports of every table
- `data/ground_truth/<preset>/` — anomaly ground truth + supplier profiles (kept apart from scoring inputs)
- `data/manifests/<dataset_id>.json` — seed, versions, config, counts, checksums, provenance
- `reports/<preset>/` — JSON + Markdown reports

## Test

```bash
uv run pytest -q                  # default suite (~7 min on the build machine)
uv run pytest -q -m slow          # full-scale tests
uv run ruff check . && uv run ruff format --check .
```

## Measured results (from task verification runs; not yet consolidated)

Starter preset, `srp generate --preset starter`: 12.7 s end to end; counts 50/300/450/600;
anomaly detection TP=5, FP=0, FN=0 for each of classes A–E; 300/300 scores reconciled against
the independent recomputation; 0 validation defects (MEDIUM+), 32 informational findings.

Full preset, measured per stage: generation ≈ 4 s, load ≈ 7 s (≈237k rows), scoring
6,000 scores ≈ 30 s, validation (38 rules) ≈ 6.5 s with 0 MEDIUM+ findings, independent
reconciliation 6,000/6,000 exact at tolerance 0, injection 25 anomalies per class.

## How it works (short)

- **Scoring:** delivery 30%, quality 25%, cost 15%, capacity 10%, compliance 10%, disruption
  10% (configurable). Missing evidence is never treated as 0 or 100: weights are renormalised
  and the score is flagged provisional, or the supplier is marked *insufficient evidence*.
  Decision precedence: mandatory compliance failure → open critical data exception →
  insufficient evidence → category.
- **Historical correctness:** a score only uses evidence that occurred *and was recorded* by
  its cutoff; late-arriving data creates a linked *restatement*, never a silent overwrite.
  Every score stores its lineage (`score_evidence`) and can be rebuilt from it exactly.
- **Anomalies:** injected only into a raw-ingestion layer; the canonical tables stay valid.
  Detection is content-based (tests prove renumbering record ids does not change findings).
- **Reconciliation:** an independent pandas implementation recomputes every score from the
  exported CSVs. Its data access and arithmetic are independent of the engine; a documented
  list of rule interpretations is shared with the engine and therefore not cross-checked
  (see `src/supplier_reliability/services/evaluation/independent_metrics/__init__.py`).

## Known limitations

- PostgreSQL is unverified (see Status).
- Dataset generation jobs in the API are process-local (FastAPI background tasks with
  persisted job rows); they are not a durable or distributed queue. Jobs interrupted by a
  restart are marked failed on startup.
- Score writes are serialised with an in-process lock; multiple API workers would need
  database-level locking.
- No authentication — intended for local use; generation and recalculation endpoints should
  be protected before any shared deployment.
- Scorecards use trailing 12-month windows evaluated quarterly (quarterly windows left most
  suppliers with too little evidence at the specified data volumes).

## Repository map

```
config/            scoring + generator configuration
alembic/           database migrations
src/supplier_reliability/
  models/          SQLAlchemy models        services/generator/     synthetic data
  schemas/         API Pydantic schemas     services/scoring/       evidence, components, engine, service
  repositories/    query functions          services/validation/    38 validation rules
  api/             FastAPI routes           services/anomalies/     injection + ground truth
  cli.py           Typer CLI                services/anomaly_detection/
  main.py          app factory              services/evaluation/    metrics + reconciliation
                                            services/reporting/     JSON/Markdown reports
                                            services/pipeline.py    end-to-end orchestration
tests/             unit, integration, api, data_quality, scoring (+ benchmarks)
docs/specs/        original brief + addendum
docs/superpowers/plans/  implementation plan incl. all key domain decisions
ai-usage/          prompt history and AI-usage disclosure
```

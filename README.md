# Supplier Reliability Scoring Platform

A deterministic, explainable supplier-reliability scoring platform built for the **YottaFlex AI candidate project**.

The platform generates synthetic supply-chain data, stores and validates relational records, calculates supplier reliability scores across six components, tracks score lineage, detects injected data anomalies, evaluates detection against independent ground truth, and reconciles persisted scores against an independent recomputation.

It exposes its capabilities through a **FastAPI REST API** and a **Typer command-line interface (CLI)**.

## Key Features

- **Synthetic data generation:** Reproducible datasets with starter and full-scale presets.
- **Relational data model:** 11 core entities plus supporting tables, managed through SQLAlchemy and Alembic migrations.
- **Explainable supplier scoring:** Six configurable scoring components, explicit missing-data handling, decision precedence, and component-level explanations.
- **Historical score correctness:** Cutoff-aware evidence selection, score lineage, versioning, and linked restatements for late-arriving evidence.
- **Data quality validation:** 38 validation rules covering relationships, temporal consistency, completeness, and other integrity constraints.
- **Anomaly injection and detection:** Five classes of seeded anomalies, a separate ground-truth dataset, content-based detection, and quarantine handling.
- **Independent evaluation:** Per-class detection metrics and score reconciliation against an independent pandas implementation.
- **Reporting and exports:** JSON and Markdown reports, CSV and JSONL table exports, and dataset manifests.
- **API and CLI access:** End-to-end pipeline orchestration, scoring, validation, anomaly detection, evaluation, and reporting.

## Architecture Overview

The platform follows a modular-monolith architecture, separating domain models, data generation, scoring, validation, anomaly processing, evaluation, reporting, and API orchestration.

```text
Synthetic Data Generator
          |
          v
Relational Database <----> CSV / JSONL Exports
          |
          v
Supplier Scoring Engine
          |
          +----> Score Evidence and Lineage
          |
          v
Data Validation
          |
          v
Raw Anomaly Injection
          |
          v
Content-Based Detection and Quarantine
          |
          v
Ground-Truth Evaluation
          |
          v
Independent Score Reconciliation
          |
          v
JSON / Markdown Reports and Dataset Manifest

FastAPI REST API + Typer CLI
          |
          v
Application Services
```

The architecture keeps injected anomalies in a raw-ingestion layer rather than corrupting canonical relational records. This allows anomaly detection to be evaluated against known ground truth while preserving the integrity of the underlying dataset.

## Technology Stack

| Area | Technologies |
|---|---|
| Language and runtime | Python 3.12 |
| API | FastAPI, Uvicorn, Pydantic v2 |
| Database | SQLite by default, PostgreSQL intended |
| ORM and migrations | SQLAlchemy 2.x, Alembic |
| Data processing | pandas, NumPy |
| Synthetic data | Faker |
| CLI | Typer |
| Testing | pytest, HTTPX |
| Code quality | Ruff |
| Environment and dependencies | uv |

No external API keys, LLMs, or third-party datasets are required.

## Supplier Scoring Model

Each supplier is evaluated across six configurable components.

| Component | Default weight |
|---|---:|
| Delivery reliability | 30% |
| Quality | 25% |
| Cost | 15% |
| Capacity | 10% |
| Compliance | 10% |
| Disruption risk | 10% |
| **Total** | **100%** |

The engine combines available component evidence using configurable weights and applies an explicit policy for missing evidence.

### Scoring principles

- Missing evidence is not automatically treated as a score of zero or one hundred.
- Available weights are renormalized according to the configured policy.
- Scores can be marked provisional or classified as having insufficient evidence.
- Decision precedence is explicit: mandatory compliance failure, open critical data exception, insufficient evidence, then score category.
- Component explanations and supporting evidence are persisted for auditability.
- Scoring configuration and versions are tracked so results can be reproduced and compared.

### Historical correctness and lineage

A score uses only evidence that both **occurred and was recorded by the score's cutoff**. Evidence received after that cutoff does not silently change a historical result; instead, the platform creates a linked restatement.

Persisted score evidence supports tracing a result back to its underlying records and reconstructing the calculation.

## Synthetic Data and Anomaly Evaluation

The generator supports two dataset presets.

| Entity volume | Starter | Full |
|---|---:|---:|
| Suppliers | 50 | 1,000 |
| Purchase orders | 300 | 10,000 |
| Shipments | 450 | 15,000 |
| Inspections | 600 | 20,000 |

The generator uses a fixed seed and configurable presets to support repeatable experiments. The full pipeline includes database loading, supplier scoring, anomaly injection, detection, validation, exports, evaluation, reconciliation, reporting, and manifest generation.

### Anomaly classes

Five classes of anomalies are injected into the raw layer and evaluated against separately stored ground truth:

- **A:** Shipment without a purchase order.
- **B:** Delivery event occurring before dispatch.
- **C:** Score using evidence from after its cutoff.
- **D:** Risk signal missing required source or timestamp information.
- **E:** Supplier-site identity conflation.

Detection is based on record content and business rules rather than relying solely on generated record IDs.

### Evaluation and reconciliation

The evaluation pipeline calculates true positives, false positives, false negatives, and true negatives for each anomaly class.

Score reconciliation uses a separate pandas implementation to recompute persisted scores from exported CSV data. The implementation has independent data access and arithmetic, although documented rule interpretations are shared with the primary scoring engine and are therefore not independently verified.

## Verified Results

The following results were recorded during project verification runs.

### Starter preset

| Metric | Result |
|---|---:|
| End-to-end runtime | 12.7 seconds |
| Suppliers / POs / shipments / inspections | 50 / 300 / 450 / 600 |
| Detection | TP = 5, FP = 0, FN = 0 for each anomaly class A–E |
| Score reconciliation | 300 / 300 exact matches |
| Medium-or-higher validation findings | 0 |
| Informational validation findings | 32 |

### Full preset — measured by stage

| Stage | Recorded result |
|---|---:|
| Data generation | Approximately 4 seconds |
| Database loading | Approximately 7 seconds |
| Rows loaded | Approximately 237,000 |
| Scoring | 6,000 scorecards in approximately 30 seconds |
| Validation | Approximately 6.5 seconds across 38 rules |
| Medium-or-higher validation findings | 0 |
| Independent score reconciliation | 6,000 / 6,000 exact matches |
| Reconciliation tolerance | 0 |
| Injected anomalies | 25 per class, across five classes |

These full-preset timings are individual stage measurements, not a measured end-to-end runtime for the entire full-scale pipeline.

### Automated tests and code quality

The recorded default test run completed with:

- **902 passed**
- **3 deselected** — full-scale tests marked `slow`
- `ruff check` — clean
- `ruff format --check` — clean

These results describe the recorded verification run and should be regenerated if the code changes before submission.

## Getting Started

### Prerequisites

- Python 3.12
- `uv` 0.4 or later

Install `uv` if needed:

```bash
pip install uv
```

### Installation

Clone the repository and enter the project directory:

```bash
git clone <your-repository-url>
cd <your-repository-directory>
```

Install Python 3.12 and project dependencies:

```bash
uv python install 3.12
uv sync
```

Optionally create a local environment file:

```bash
cp .env.example .env
```

The default configuration works without an `.env` file. Environment variables use the `SRP_` prefix. Scoring weights, thresholds, generator presets, and anomaly counts are configurable in `config/default.yaml`.

### Initialize the database

```bash
uv run srp init-db
```

SQLite is the default database, so a separately managed database server is not required for the standard local workflow.

### Run the pipeline

Generate the starter dataset:

```bash
uv run srp generate --preset starter
```

Generate the full dataset:

```bash
uv run srp generate --preset full --replace
```

The `--replace` option permits replacement of an existing dataset. Without it, the generator refuses to overwrite an existing dataset.

The pipeline runs database migrations, generation, loading, scoring, anomaly injection, detection, validation, export, ground-truth generation, evaluation, score reconciliation, reporting, and manifest creation.

### Start the API

```bash
uv run srp serve --host 127.0.0.1 --port 8000
```

Open the interactive API documentation at:

- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI schema: `http://127.0.0.1:8000/openapi.json`

The API provides 30 routes covering health and readiness, metadata, suppliers, purchase orders, shipments, scores, validation, exceptions, dataset jobs, manifests, reports, and evaluation.

### CLI commands

```bash
uv run srp --help
uv run srp score
uv run srp recalculate
uv run srp validate
uv run srp detect
uv run srp evaluate
uv run srp report
```

Use the CLI help output for command-specific arguments and options.

## Testing

Run the default test suite:

```bash
uv run pytest -q
```

Run full-scale tests separately:

```bash
uv run pytest -q -m slow
```

Check code style and formatting:

```bash
uv run ruff check .
uv run ruff format --check .
```

The default test suite takes approximately seven minutes on the recorded build machine. Full-scale tests are separated so they can be run explicitly.

## Outputs

Generated artifacts can be recreated by rerunning the pipeline. Large generated files are excluded from version control.

| Path | Contents |
|---|---|
| `data/generated/<preset>/` | CSV and JSONL exports of generated tables |
| `data/ground_truth/<preset>/` | Anomaly ground truth and supplier profiles, separated from scoring inputs |
| `data/manifests/<dataset_id>.json` | Seed, versions, configuration, counts, checksums, and provenance |
| `reports/<preset>/` | JSON and Markdown reports |

## Repository Structure

```text
config/
    default.yaml                 # Scoring and generator configuration

alembic/
    migrations                   # Database schema migrations

src/supplier_reliability/
    models/                      # SQLAlchemy models
    schemas/                     # Pydantic API schemas
    repositories/                # Database query functions
    api/                         # FastAPI routes
    cli.py                       # Typer CLI
    main.py                      # Application factory
    services/
        generator/               # Synthetic data generation
        scoring/                 # Evidence, components, engine, service
        validation/              # Validation rules
        anomalies/               # Injection and ground truth
        anomaly_detection/       # Content-based detection
        evaluation/              # Metrics and score reconciliation
        reporting/               # JSON and Markdown reports
        pipeline.py              # End-to-end orchestration

tests/
    unit/
    integration/
    api/
    data_quality/
    scoring/
    benchmarks/

docs/
    specs/                       # Original brief and addendum
    superpowers/plans/            # Implementation plan and domain decisions

ai-usage/                         # AI-usage disclosure and prompt history
```

## Current Limitations and Verification Boundaries

The project is designed and tested primarily for local execution. The following limitations are important when evaluating the current implementation:

- **PostgreSQL:** Not yet verified against a running PostgreSQL server. Automated tests were run on SQLite, and PostgreSQL DDL was compile-checked only.
- **Background jobs:** API dataset-generation jobs use FastAPI background tasks with persisted job records. They are not backed by a durable or distributed queue; jobs interrupted by a restart are marked failed on startup.
- **Concurrent writes:** Score writes use an in-process lock. Multi-worker deployments would require database-level coordination.
- **Authentication:** No authentication is implemented. Administrative endpoints should be protected before shared or public deployment.
- **Full-scale pipeline:** Individual stages have been measured at full scale, but a complete full-preset end-to-end run has not yet been recorded.
- **Reproducibility verification command:** The `srp verify-reproducibility` workflow is not yet included in the current commit.
- **Packaging and acceptance checks:** Docker and Docker Compose files, a dedicated `srp acceptance` runner, and the complete standalone documentation set are not yet included in the current commit.

# MASTER IMPLEMENTATION PROMPT
# Supplier Reliability Scoring Platform — FastAPI, Synthetic Data, Analytics and Validation

> Saved verbatim from the original prompt supplied on 2026-10-27. This is the authoritative spec for
> `docs/superpowers/plans/2026-10-27-supplier-reliability-platform.md`.

## 1. Mission and role

You are a senior software architect, data engineer, backend engineer, analytics engineer, and QA engineer.

Build a complete, working Supplier Reliability Scoring Platform from scratch in a new repository.

The application must generate realistic synthetic supply-chain data, persist and validate interconnected business entities, calculate explainable supplier reliability scores, detect data-quality anomalies, expose the workflow through a FastAPI backend, and produce measurable evaluation reports.

This is an implementation task, not a design-only task.

You are responsible for:
- Architecture and implementation.
- Database schema and migrations.
- Synthetic data generation.
- Business rules and scoring methodology.
- Anomaly injection and detection.
- Ground-truth generation and evaluation.
- FastAPI endpoints and API documentation.
- Automated testing.
- Reproducibility verification.
- Documentation and developer setup.
- Running the project and verifying actual results.

Do not stop after creating a plan, architecture diagram, directory structure, or code skeleton. Implement the features, execute them, run the tests, investigate failures, and fix them.

### Operating instructions

1. Start with an empty project directory and initialize a clean repository.
2. Inspect the runtime environment and available tools before choosing commands.
3. Create an implementation plan and a checklist of acceptance criteria.
4. Implement the project incrementally in dependency order.
5. Run focused tests after each major subsystem.
6. Run the full end-to-end workflow when the subsystems are integrated.
7. Record actual execution results and remaining limitations.
8. Never claim a command or test succeeded unless you executed it and observed the result.

Use reasonable defaults for nonblocking ambiguities. Document assumptions rather than asking unnecessary questions. If a consequential ambiguity prevents correct implementation, ask one concise question.

---

## 2. Business context

Organizations depend on suppliers to deliver the correct materials, in the correct quantity, at the right time, at an acceptable cost, while meeting quality and compliance requirements.

Supplier performance can deteriorate because of:
- Repeated delivery delays.
- Increasing product defects.
- Invoice discrepancies.
- Insufficient production capacity.
- Expired compliance certifications.
- Factory, carrier, or logistics disruptions.
- Missing or unreliable performance data.

Build a system that evaluates these factors and produces supplier reliability scores and operational recommendations.

The primary users are:

- Supplier: supplier and site information, contractual relationships, and performance.
- Planner: purchasing requirements, lead-time risks, and fulfilment status.
- Carrier/Warehouse Operator: shipment and delivery events.
- Logistics Analyst: delivery performance, disruptions, and data-quality exceptions.
- Operations Manager: supplier rankings, reliability trends, risk exposure, and escalation decisions.

The system must answer:
- Which suppliers are reliable?
- Which suppliers are deteriorating?
- Why did a supplier receive its score?
- Which deliveries were late, and by how much?
- Which suppliers have recurring quality problems?
- Which invoices disagree with contractual expectations?
- Which suppliers have capacity or compliance risks?
- Which suppliers lack sufficient evidence for a trustworthy score?
- Which records violate business rules?
- Can an historical score be reproduced using only eligible information?

---

## 3. Technology stack and engineering choices

Use the following default stack.

### Backend
- Python 3.12.
- FastAPI.
- Uvicorn.
- Pydantic v2.
- SQLAlchemy 2.x.
- Alembic for database migrations.
- PostgreSQL for production-style persistence.
- SQLite for a simple local mode and lightweight tests.
- Pandas or Polars for data processing.
- NumPy for numerical operations.
- Faker for controlled synthetic attributes.
- Typer for command-line commands.
- Pytest for automated testing.
- HTTPX for API integration tests.
- Ruff for linting and formatting.
- Standard Python logging or structured JSON logging.

Use compatible, pinned or constrained dependencies and commit a lockfile where supported by the chosen package manager.

### Architecture requirements

Use a modular monolith rather than unnecessary microservices.

Separate:
1. Domain models.
2. Persistence and repositories.
3. Data generation.
4. Scoring logic.
5. Data validation.
6. Anomaly detection.
7. Ground-truth evaluation.
8. Reporting and exports.
9. API schemas and routes.
10. Application configuration.

The core scoring and validation engines must be callable directly from Python without making HTTP requests.

The system must not require an LLM, external API, paid service, or external dataset to function.

### Database strategy

Use PostgreSQL as the recommended application database.

Provide SQLite support for local development and automated tests. Use SQLAlchemy models and portable constraints where possible. Handle database-specific differences explicitly.

Do not rely on SQLite as proof that PostgreSQL-specific migrations or behavior work. If PostgreSQL is available, test against it. If it is unavailable, document which PostgreSQL checks remain unverified.

---

## 4. New repository structure

Create a clean repository with a structure similar to:

```
supplier-reliability-platform/
├── README.md
├── pyproject.toml
├── uv.lock
├── .gitignore
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
├── config/
│   └── default.yaml
├── docs/
│   ├── architecture.md
│   ├── data_dictionary.md
│   ├── entity_relationships.md
│   ├── business_rules.md
│   ├── scoring_methodology.md
│   ├── anomaly_catalog.md
│   ├── api_reference.md
│   └── evaluation_methodology.md
├── alembic/
│   ├── env.py
│   └── versions/
├── src/
│   └── supplier_reliability/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── database.py
│       ├── models/
│       ├── schemas/
│       ├── repositories/
│       ├── services/
│       │   ├── generator/
│       │   ├── scoring/
│       │   ├── validation/
│       │   ├── anomaly_detection/
│       │   ├── evaluation/
│       │   └── reporting/
│       ├── api/
│       │   ├── dependencies.py
│       │   └── routes/
│       └── cli.py
├── scripts/
│   ├── generate_data.py
│   ├── validate_data.py
│   └── verify_reproducibility.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── api/
│   ├── data_quality/
│   ├── scoring/
│   └── reproducibility/
├── data/
│   ├── generated/
│   ├── ground_truth/
│   └── manifests/
├── reports/
└── logs/
```

Adapt the structure when justified, but preserve clear separation of concerns.

Do not commit generated databases, secrets, environment files, or huge generated artifacts unnecessarily. Keep generated outputs available locally and document how to recreate them.

---

## 5. Domain entities and relational schema

Implement eleven core entities:

1. Supplier
2. Site
3. Contract
4. PurchaseOrder
5. POLine
6. Shipment
7. DeliveryEvent
8. Inspection
9. Invoice
10. RiskSignal
11. SupplierScore

Each entity must have a primary key, defined relationships, required fields, allowed values, lifecycle rules, and timestamps as appropriate.

### 5.1 Supplier

Represents a supplier organization.

Fields should include:
- supplier_id
- supplier_name
- supplier_category
- country
- region
- supplier_status
- onboarding_date
- created_at
- updated_at

Use IDs such as `SUP-000001`.

### 5.2 Site

Represents a supplier factory, warehouse, or operating location.

Fields:
- site_id
- supplier_id
- site_name
- site_type
- country
- region
- site_status
- effective_from
- effective_to
- created_at

Use IDs such as `SITE-SUP-000001-01`.

A supplier may have multiple sites. A site must belong to one supplier for a given effective period.

### 5.3 Contract

Represents contractual terms.

Fields:
- contract_id
- supplier_id
- site scope, if applicable
- contract_status
- effective_from
- effective_to
- currency
- payment_terms
- delivery_terms
- agreed pricing or pricing references
- contracted_capacity and unit
- compliance requirements
- created_at

Contract validity must be checked against relevant transactions.

### 5.4 PurchaseOrder

Fields:
- po_id
- supplier_id
- contract_id
- order_date
- required_delivery_date
- po_status
- currency
- created_at

Use IDs such as `PO-2026-000001`.

### 5.5 POLine

Fields:
- po_line_id
- po_id
- line_number
- item_id or product_category
- ordered_quantity
- unit_of_measure
- agreed_unit_price
- line_amount
- created_at

Enforce uniqueness of `(po_id, line_number)`.

Calculate line amounts from quantity and unit price.

### 5.6 Shipment

Fields:
- shipment_id
- po_id
- originating_site_id
- destination_site_id or warehouse identifier
- carrier_id, when modeled
- dispatch_timestamp
- expected_delivery_timestamp
- actual_delivery_timestamp, when known
- shipment_status
- shipped_quantity
- received_quantity
- created_at

Use IDs such as `SHP-2026-000001`.

Support multiple shipments for one PO and partial deliveries.

### 5.7 DeliveryEvent

Fields:
- delivery_event_id
- shipment_id
- event_type
- event_timestamp
- recorded_timestamp
- location_id, if applicable
- source_system
- event_metadata

Define valid event types and lifecycle transitions.

Preserve the difference between when an event occurred and when the system recorded it.

### 5.8 Inspection

Fields:
- inspection_id
- shipment_id and/or po_line_id
- inspection_timestamp
- inspected_quantity
- accepted_quantity
- rejected_quantity
- defect_quantity
- inspection_status
- defect_category
- inspection_source

Document the inspection grain and enforce reconciliation rules. A fully inspected quantity should reconcile with accepted and rejected quantities according to the selected inspection model.

### 5.9 Invoice

Fields:
- invoice_id
- supplier_id
- contract_id
- po_id
- invoice_date
- invoice_status
- currency
- invoiced_amount
- expected_amount
- variance_amount
- variance_percentage
- created_at

Derive expected values and variances from source transactions and contractual terms.

### 5.10 RiskSignal

Fields:
- risk_signal_id
- supplier_id
- site_id, when applicable
- related shipment or contract, when applicable
- signal_type
- severity
- signal_source
- observed_timestamp
- recorded_timestamp
- valid_from
- valid_to
- resolution_status
- evidence_reference

Keep provenance explicit. Synthetic evidence must be identified as synthetic, not represented as a real external report.

### 5.11 SupplierScore

Fields:
- score_id
- supplier_id
- site_id, if site-specific
- evaluation_start
- evaluation_end
- data_cutoff_timestamp
- calculated_at
- delivery_score
- quality_score
- cost_score
- capacity_score
- compliance_score
- disruption_score
- overall_score
- confidence or evidence_coverage
- reliability_category
- decision
- explanation
- scoring_version
- calculation_status

Use IDs such as `SCORE-2026-000001`.

Scores must be versioned and historically traceable.

### 5.12 Relationships and integrity

Implement and document:
- Supplier → Site: one-to-many.
- Supplier → Contract: one-to-many.
- Contract → PurchaseOrder: one-to-many.
- PurchaseOrder → POLine: one-to-many.
- PurchaseOrder → Shipment: one-to-many.
- Shipment → DeliveryEvent: one-to-many.
- Shipment → Inspection: one-to-many.
- PurchaseOrder → Invoice: one-to-many when partial billing is supported.
- Supplier → RiskSignal: one-to-many.
- Supplier → SupplierScore: one-to-many.

Add bridge tables for genuine many-to-many relationships.

Explicitly define how shipment quantities are allocated to PO lines. Do not join shipments, inspections, events, and invoices in a way that multiplies quantities or financial amounts.

Use indexes for frequently filtered fields such as supplier_id, site_id, po_id, shipment_id, evaluation period, event timestamps, and score status.

---

## 6. Data dictionary and lifecycle design

Create a comprehensive data dictionary before completing the generator.

For every field, document:
- Name and meaning.
- Data type.
- Required or optional.
- Primary/foreign key designation.
- Allowed values.
- Units and currency.
- Uniqueness rules.
- Validation logic.
- Derivation formula.
- Timestamp semantics.
- Example value.

Use UTC for persisted timestamps and define timezone conversion rules at API boundaries.

Use decimal-safe arithmetic for money. Document rounding, units, and currency behavior.

Distinguish:
- Missing data from zero.
- Unknown compliance from confirmed noncompliance.
- Unobserved delivery outcomes from late deliveries.
- Zero defects from missing inspection data.
- Absence of risk evidence from confirmed absence of risk.

Preserve historical events and score versions. Correct erroneous records by adding correction or superseding events rather than silently overwriting the original evidence.

---

## 7. Synthetic data generator

Build a configurable, deterministic generator.

### 7.1 Required baseline scale

Generate:
- 1,000 suppliers.
- 10,000 purchase orders.
- 15,000 shipments.
- 20,000 inspections.

Generate all related entities according to documented business rules.

Support a small starter dataset and a full-scale dataset using the same generation logic.

Example starter configuration:
- 50 suppliers.
- 300 POs.
- 450 shipments.
- 600 inspections.

These starter counts are configurable examples, not substitutes for the required full-scale acceptance test.

### 7.2 Fixed seed and reproducibility

Default seed: `20261022`.

Requirements:
- Centralize random-number generation.
- Seed all randomness explicitly.
- Use deterministic ID counters.
- Fix the synthetic reference period.
- Avoid uncontrolled current-time dependencies.
- Keep row ordering and serialization deterministic.
- Record the seed, configuration, generator version, and schema version.
- Make scale, date range, and anomaly counts configurable.

Two runs with identical code version, seed, configuration, and compatible dependencies must produce identical logical records and deterministic exports.

### 7.3 Supplier behavior profiles

Generate meaningful populations such as:
- Stable high performers.
- Average performers.
- Suppliers with deteriorating lead times.
- Suppliers with recurring quality incidents.
- Suppliers with invoice discrepancies.
- Capacity-constrained suppliers.
- Compliance-risk suppliers.
- Disruption-prone suppliers.
- Suppliers with missing historical observations.

Use profile-specific probability distributions and temporal trends.

Do not independently randomize every outcome. Generate transactions conditionally on supplier profiles, site characteristics, contract terms, and historical behavior.

Include enough observations to calculate meaningful component metrics.

### 7.4 Generation order

Generate parent records before dependent records:

1. Suppliers.
2. Sites.
3. Contracts.
4. Purchase orders.
5. PO lines.
6. Shipments.
7. Delivery events.
8. Inspections.
9. Invoices.
10. Risk signals.
11. Supplier scores.

Maintain explicit ID registries and validate every relationship.

### 7.5 Derived values

Derive rather than independently randomize:
- PO line amounts.
- Shipment fulfilment quantities.
- Delivery delays.
- On-time status.
- Inspection rejection and defect rates.
- Expected invoice amounts.
- Invoice variances.
- Supplier performance metrics.
- Component scores.
- Overall scores.
- Reliability categories.
- Decision outcomes.

The generator must produce internally consistent records before anomaly injection.

---

## 8. Anomaly injection and exception handling

Inject the five required negative scenarios only after generating valid baseline records.

### Anomaly A: Shipment with no PO

Expected outcome: flag a missing parent reference and route the record to quarantine.

### Anomaly B: Delivery before dispatch

Expected outcome: detect the invalid event chronology and identify the affected shipment.

### Anomaly C: Historical score uses future data

Expected outcome: detect look-ahead leakage and calculate a corrected score using only eligible evidence.

### Anomaly D: Risk signal lacks source or timestamp

Expected outcome: flag incomplete provenance and exclude the signal from normal scoring until its eligibility is established.

### Anomaly E: Supplier sites are conflated

Expected outcome: detect incorrect supplier-site attribution, including cases where all foreign keys are individually valid.

### Exception architecture

Keep the canonical database valid.

Represent deliberately invalid source records in a raw-ingestion or quarantine layer, with fields such as:
- exception_id
- source_entity_type
- source_record_id
- issue_type
- severity
- detection_status
- detected_at
- disposition
- related_entity_ids
- evidence

Use an explicit exception representation where a malformed source record cannot satisfy the canonical schema.

Do not globally disable foreign-key constraints to inject anomalies.

Keep ground truth in a separate dataset, never in ordinary scoring inputs.

---

## 9. Scoring engine

Implement a deterministic, explainable supplier scoring service.

### 9.1 Initial weights

Use configurable weights:
- Delivery: 30%.
- Quality: 25%.
- Cost: 15%.
- Capacity: 10%.
- Compliance: 10%.
- Disruption: 10%.

Validate that weights are nonnegative and total 100%.

These weights are initial assumptions that must be documented and configurable.

### 9.2 Delivery score

Calculate:

On-time delivery rate = eligible on-time deliveries / eligible deliveries with known outcomes.

Define on-time delivery and grace-period rules explicitly.

Also calculate:
- Average delivery delay.
- Late-delivery frequency.
- Eligible delivery count.
- Delivery evidence coverage.

Exclude deliveries that have not yet become due from the late-delivery denominator.

### 9.3 Quality score

Calculate:

Defect rate = total defective units / total inspected units.

Define how the rate maps to a normalized score between 0 and 100.

Document the inspection grain, duplicate-inspection handling, and treatment of partial inspections.

### 9.4 Cost score

Calculate:

Variance amount = invoiced amount - expected amount.

Variance percentage = variance amount / expected amount, when expected amount is nonzero.

Document the treatment of overbilling, underbilling, credits, taxes, freight, currencies, and rounding.

### 9.5 Capacity score

Calculate demand relative to verified capacity.

Document:
- The time period of capacity.
- Whether capacity is site-specific or supplier-wide.
- How demand is aggregated.
- How utilization maps to the component score.
- What happens when capacity is unknown.

### 9.6 Compliance score

Evaluate explicit requirements using effective dates and statuses.

Distinguish:
- Compliant.
- Noncompliant.
- Expired.
- Pending verification.
- Unknown.

Define mandatory compliance failures and any decision overrides.

### 9.7 Disruption score

Convert eligible risk signals into a documented risk penalty using severity, recency, relevance, scope, and resolution status.

Prevent accidental double-counting of duplicate signals.

Distinguish lack of risk data from verified absence of risk.

### 9.8 Overall score

For fully observed components:

Overall score =
0.30 × Delivery +
0.25 × Quality +
0.15 × Cost +
0.10 × Capacity +
0.10 × Compliance +
0.10 × Disruption.

Use consistent normalization and documented rounding.

For incomplete evidence, use an explicit missing-data policy.

Never silently replace missing components with zero or 100.

If available components are renormalized, expose the effective weights and data coverage. Flag provisional scores as such.

### 9.9 Reliability categories

Use configurable example categories:
- 85–100: High reliability.
- 70–below 85: Acceptable.
- 50–below 70: At risk.
- Below 50: Critical.
- Insufficient evidence: Not confidently assessable.

Ensure thresholds have no gaps or overlaps.

Define explicit decision precedence for severe compliance failures, invalid source data, and other critical exceptions.

### 9.10 Explainability

Every score must provide:
- Overall score.
- Component scores.
- Weights.
- Numerators and denominators.
- Observation counts.
- Evidence coverage.
- Missing-data reasons.
- Contributing source-record references.
- Relevant risk signals.
- Detected exceptions.
- Scoring version.
- Evaluation period.
- Data cutoff timestamp.
- Decision rationale.

A user must be able to inspect why a score was calculated and reproduce it from its source evidence.

### 9.11 Historical scoring

Never allow evidence outside the documented eligible time window to influence a historical score.

Define the distinction between:
- Event occurrence time.
- Event recording time.
- Evaluation cutoff.
- Score calculation time.

Preserve historical score versions and explain differences between evaluations.

---

## 10. Ground truth and evaluation

Generate ground truth from documented deterministic rules.

Store it separately from normal input features.

The ground-truth dataset should include:
- case_id
- entity_type
- entity_id
- issue_type
- expected_detection
- expected_decision
- severity
- rationale
- injection_version

Use separate implementations for generation, anomaly injection, detection, and evaluation.

Evaluate anomaly detection against ground truth and report:
- True positives.
- False positives.
- True negatives, when the negative population is defined.
- False negatives.
- Precision.
- Recall.
- F1 score.

Document the evaluation unit: record-level, anomaly-level, or rule-level.

Do not count every unrelated valid record as a meaningful negative without considering the anomaly class and sampling design.

Keep the known generator profile separate from observable scoring features. Evaluate supplier scoring using independently calculated metrics from the generated source transactions.

---

## 11. Validation framework

Implement reusable validators for:

### Referential integrity
- Unique primary keys.
- Valid foreign keys.
- Consistent cross-file IDs.
- Expected relationship cardinality.
- Correct site attribution.
- Controlled exception records.

### Domain constraints
- Required fields.
- Valid enums.
- Nonnegative quantities where applicable.
- Currency and decimal precision.
- Contract validity.
- Lifecycle states.
- Effective dates.

### Chronology
- Order and delivery dates.
- Dispatch and arrival events.
- Inspection timing.
- Risk-signal timestamps.
- Historical score cutoff compliance.

### Arithmetic
- PO line totals.
- Shipment allocations.
- Inspection quantity reconciliation.
- Invoice variances.
- Component score calculations.
- Overall score reconciliation.

### Duplicate and semantic checks
- Duplicate events.
- Duplicate invoice references.
- Repeated risk signals.
- Site attribution conflicts.
- Unexpected join multiplication.
- Missing performance evidence.

Return structured findings with:
- rule_id
- severity
- entity_type
- entity_id
- field
- observed_value
- expected_condition
- message
- detection_timestamp

Separate expected injected anomalies from unexpected data corruption.

The full baseline canonical dataset should pass applicable integrity rules. Deliberately invalid cases should appear in the designated exception layer and be detected by the validation framework.

---

## 12. FastAPI requirements

Build a documented FastAPI application with dependency injection, Pydantic request/response schemas, database sessions, service-layer separation, and consistent error handling.

### 12.1 API endpoints

Implement endpoints similar to the following.

#### Health and system status
- `GET /health`
- `GET /ready`
- `GET /api/v1/metadata`

Health checks must distinguish application liveness from database readiness.

#### Suppliers
- `GET /api/v1/suppliers`
- `GET /api/v1/suppliers/{supplier_id}`
- `GET /api/v1/suppliers/{supplier_id}/performance`
- `GET /api/v1/suppliers/{supplier_id}/scores`
- `GET /api/v1/suppliers/{supplier_id}/explanation`

Support pagination, sorting, and appropriate filters.

#### Supplier scores
- `GET /api/v1/scores`
- `GET /api/v1/scores/{score_id}`
- `POST /api/v1/scores/calculate`
- `POST /api/v1/scores/recalculate`

Calculation requests must specify or resolve the evaluation period, cutoff, scoring configuration, and target population.

Prevent duplicate score creation through a documented idempotency strategy or unique evaluation key.

Do not silently overwrite historical scores during recalculation.

#### Purchase orders and shipments
- `GET /api/v1/purchase-orders`
- `GET /api/v1/purchase-orders/{po_id}`
- `GET /api/v1/shipments`
- `GET /api/v1/shipments/{shipment_id}`
- `GET /api/v1/shipments/{shipment_id}/events`

#### Validation and exceptions
- `POST /api/v1/validation/run`
- `GET /api/v1/validation/results`
- `GET /api/v1/exceptions`
- `GET /api/v1/exceptions/{exception_id}`

#### Data generation
- `POST /api/v1/datasets/generate`
- `GET /api/v1/datasets/jobs/{job_id}`
- `GET /api/v1/datasets/manifests/{dataset_id}`

For long-running full-scale generation, use a background-job abstraction with persisted job state, status, timestamps, and error details. A process-local background task is acceptable for a local single-instance prototype if the limitation is documented. Do not claim it is a durable distributed job system.

#### Reports and evaluation
- `GET /api/v1/reports/summary`
- `GET /api/v1/reports/supplier-rankings`
- `GET /api/v1/reports/data-quality`
- `GET /api/v1/reports/anomaly-evaluation`
- `POST /api/v1/evaluation/run`

Adjust endpoints when needed, but implement all core capabilities through the API or documented CLI workflows.

### 12.2 API quality

- Use correct HTTP status codes.
- Return consistent JSON responses.
- Use 404 for missing resources.
- Use 409 for documented state conflicts.
- Use 422 for invalid request data.
- Return safe error messages without leaking credentials or stack traces.
- Validate pagination limits.
- Support filters by supplier, site, date range, score category, and exception severity where appropriate.
- Avoid N+1 database queries.
- Use explicit transactions for multi-table writes.
- Include OpenAPI documentation and representative examples.
- Add API integration tests.

Do not expose sensitive operational internals or arbitrary filesystem paths through the API.

### 12.3 API contract

Use Pydantic schemas rather than returning ORM objects directly.

Keep database models, request schemas, response schemas, and service logic separate.

Document response formats and error semantics.

A minimal frontend is not required. The FastAPI API, OpenAPI documentation, CLI, exported datasets, and reports form the primary interface.

---

## 13. Reporting and measurable outputs

Generate reports for:
- Entity row counts.
- Supplier reliability rankings.
- Score distributions.
- Delivery and quality trends.
- Invoice variance distributions.
- Capacity risk.
- Compliance exceptions.
- Disruption exposure.
- Missing-data rates.
- Data-quality findings.
- Anomaly detection performance.
- Score reconciliation.
- Reproducibility.

Calculate metrics from persisted source records and generated outcomes.

Every metric must define its numerator, denominator, filters, time window, and handling of missing data.

Do not invent evaluation results.

Provide machine-readable JSON reports and readable Markdown summaries. CSV exports should include deterministic columns, ordering, encoding, and null conventions.

---

## 14. Reproducibility and provenance

Implement a reproducibility workflow that:

1. Generates the dataset with a specified seed and configuration.
2. Exports deterministic records.
3. Computes row counts and checksums.
4. Regenerates using the same configuration.
5. Compares normalized logical records and checksums.
6. Reports differences.
7. Records execution metadata.

Use a fixed synthetic reference period rather than uncontrolled wall-clock timestamps.

Ensure IDs, row order, derived values, and generated timestamps are deterministic.

Record:
- Seed.
- Generator version.
- Schema version.
- Configuration.
- Dataset version.
- Entity counts.
- File checksums.
- Generation time as execution metadata.
- Software dependency versions.
- Provenance and licensing information.

Exclude volatile execution metadata from deterministic content checksums.

Do not claim byte-for-byte reproducibility unless the exported artifacts were actually compared.

---

## 15. Security and reliability

Even though the dataset is synthetic, follow reasonable application security practices.

- Store secrets in environment variables.
- Provide `.env.example`, not a committed real `.env`.
- Use parameterized SQL through SQLAlchemy.
- Validate API inputs.
- Apply request-size and pagination limits.
- Avoid exposing filesystem paths or arbitrary export locations.
- Use safe exception handling.
- Add configurable CORS only when necessary.
- Document authentication as a future requirement if the application is strictly local and unauthenticated.
- If authentication is implemented, protect administrative generation and recalculation endpoints.
- Use database migrations rather than relying exclusively on automatic table creation.
- Use explicit transaction boundaries.
- Avoid destructive operations without safeguards.
- Do not use real confidential supplier or personal information.

---

## 16. Testing strategy

Create a comprehensive test suite.

### Unit tests
Test:
- Score normalization.
- Weighted score calculations.
- Reliability-category boundaries.
- Missing-data handling.
- Capacity scoring.
- Compliance overrides.
- Invoice variance calculations.
- Date eligibility.
- Risk-signal penalties.

### Data-quality tests
Test:
- Duplicate primary keys.
- Invalid foreign keys.
- Invalid event order.
- Invalid site attribution.
- Missing risk provenance.
- Invalid invoice arithmetic.
- Inspection quantity reconciliation.
- Future-data leakage.
- Unexpected join multiplication.

### Integration tests
Test:
- Database migrations.
- Master-data creation.
- Transaction generation.
- Scoring persistence.
- Historical score retrieval.
- Validation persistence.
- Ground-truth isolation.

### API tests
Test:
- Health and readiness.
- Supplier lookup.
- Pagination and filters.
- Missing-resource responses.
- Invalid input.
- Score calculation.
- Idempotency behavior.
- Validation endpoints.
- Report endpoints.
- Dataset-job lifecycle.

### Reproducibility tests
Run the generator twice using the same seed and configuration. Compare normalized records, row counts, and deterministic exports.

### Required boundary cases
Include:
- Empty datasets.
- Zero denominators.
- Missing delivery dates.
- Missing inspection records.
- Duplicate events.
- Partial shipments.
- Cancelled orders.
- Expired contracts.
- Zero expected invoice amount.
- Missing capacity.
- Unknown compliance.
- Duplicate risk signals.
- Future events.
- All required anomaly scenarios.

Use temporary test databases and isolated output directories. Tests must not depend on previously generated files or developer-specific paths.

---

## 17. Required deliverables

The finished repository must contain:

### Source code
- Complete FastAPI application.
- Database models and migrations.
- Deterministic synthetic-data generator.
- Scoring engine.
- Validation framework.
- Anomaly detection.
- Ground-truth generation.
- Evaluation and reporting.
- CLI.
- API schemas and endpoints.

### Data and reports
- CSV/JSON exports for all eleven core entities.
- Separate ground-truth exports.
- Exception/quarantine records.
- Dataset manifest.
- Validation summary.
- Anomaly evaluation report.
- Score reconciliation report.
- Reproducibility report.

### Documentation
- README with setup and execution commands.
- Architecture document.
- Entity relationship documentation.
- Data dictionary.
- Business rules.
- Scoring methodology.
- API reference.
- Anomaly catalog.
- Evaluation methodology.
- Assumptions and known limitations.

### Runtime and tooling
- pyproject.toml.
- Dependency lockfile.
- .env.example.
- Database migrations.
- Dockerfile.
- Docker Compose configuration, if supported by the environment.
- Automated tests.
- Linting and formatting configuration.

---

## 18. Implementation phases

### Phase 1: Project initialization

Create the repository, Python environment configuration, dependencies, settings, logging, application entry point, and test configuration.

Verify that the application imports and starts.

### Phase 2: Domain and schema

Implement the eleven entities, keys, constraints, relationships, migrations, and data dictionary.

Verify database creation and migration execution.

### Phase 3: Synthetic generator

Implement seeded, configurable master-data and transaction generation.

Generate the starter dataset and validate its joins and arithmetic.

### Phase 4: Scoring

Implement all six components, missing-data policies, decision categories, explanations, and historical cutoff rules.

Write unit tests and validate score reconciliation.

### Phase 5: Anomalies and ground truth

Inject the five required negative scenarios, generate separate ground-truth records, and implement detection and quarantine.

Verify that the ground truth does not leak into scoring inputs.

### Phase 6: Validation and evaluation

Implement integrity, chronology, arithmetic, lifecycle, and semantic validation.

Calculate measurable evaluation metrics and produce reports.

### Phase 7: FastAPI

Implement supplier, scoring, transaction, validation, exception, generation, and reporting endpoints.

Verify OpenAPI documentation and API integration tests.

### Phase 8: Full-scale generation

Generate the complete baseline dataset with 1,000 suppliers, 10,000 POs, 15,000 shipments, and 20,000 inspections.

Validate all canonical data and verify that the injected anomalies are separately recorded and detectable.

### Phase 9: Reproducibility

Execute two complete seeded runs and compare normalized records and deterministic export checksums.

Produce the reproducibility report.

### Phase 10: Final verification

Run the full test suite, linting, migrations, application startup, API checks, generator, scoring engine, validation, and evaluation.

Fix failures before final delivery wherever possible.

Do not stop at the starter dataset. The full baseline must be verified.

---

## 19. Definition of done

The project is complete only when:

1. The application starts successfully.
2. Database migrations work.
3. All eleven entities have documented schemas and relationships.
4. The generator produces the required baseline counts.
5. Canonical records pass applicable referential-integrity checks.
6. All derived values reconcile with their sources.
7. Scores are deterministic and explainable.
8. Missing data is explicitly handled.
9. All five required anomalies are generated and represented in ground truth.
10. Anomaly detection is evaluated against ground truth.
11. Historical scores respect the cutoff policy.
12. FastAPI endpoints work and have integration tests.
13. Reports are generated from actual outputs.
14. Two identical seeded runs are compared.
15. Tests and linting results are reported honestly.
16. Documentation provides working setup and execution commands.
17. No required feature is left as a placeholder or TODO.

If a requirement cannot be fully satisfied in the available environment, implement the strongest practical alternative and document the exact limitation and evidence.

---

## 20. Final execution directive

Start by creating the new repository and implementing Phase 1.

Continue through the phases without waiting for me to approve each routine step.

Use sensible engineering defaults, write the actual code, run the commands, inspect the results, fix defects, and complete the end-to-end workflow.

Do not provide only proposed code or instructions for me to execute. Perform the implementation in your available workspace.

At the end, provide:
- The final repository structure.
- The implemented architecture.
- The exact commands to run the API and generator.
- The local API documentation URL.
- Actual row counts for every entity.
- Validation and anomaly-detection metrics.
- Score reconciliation results.
- Reproducibility comparison results.
- Test and linting results.
- Paths to generated datasets and reports.
- Any unverified requirements or remaining limitations.

Clearly distinguish completed work from work that was not executed or could not be verified.

Your goal is to deliver a working, reproducible, testable, explainable Supplier Reliability Scoring Platform—not a mockup, not a plan, and not a collection of disconnected code snippets.

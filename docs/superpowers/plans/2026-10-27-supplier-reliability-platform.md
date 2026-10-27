# Supplier Reliability Scoring Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, explainable supplier-reliability scoring platform (synthetic data → canonical DB → scoring → anomaly detection → evaluation → reports) exposed through FastAPI and a Typer CLI, verified at full scale (1,000 suppliers / 10,000 POs / 15,000 shipments / 20,000 inspections).

**Architecture:** Modular monolith in one Python package `supplier_reliability`. Pure-function cores (generator, scoring components, validation rules, detectors, evaluation) sit under `services/`. SQLAlchemy repositories sit under `repositories/`. One orchestrator (`services/pipeline.py`) is shared by the CLI and the API's background job. The canonical tables only ever hold valid records. Injected anomalies live in a separate raw-ingestion layer (`raw_ingestion_records`) and a quarantine layer (`data_exceptions`). Ground truth exists only as files under `data/ground_truth/`, and the `services/scoring` package never imports it.

**Tech Stack:** Python 3.12 (via uv), FastAPI, Uvicorn, Pydantic v2 + pydantic-settings, SQLAlchemy 2.x, Alembic, SQLite (verified) / PostgreSQL via psycopg 3 (written, unverified locally), NumPy, pandas, Faker, Typer, PyYAML, pytest, HTTPX, Ruff.

**Spec:** `docs/specs/2026-10-27-master-implementation-prompt.md` (section numbers below like "§9.2" refer to it) plus the addendum `docs/specs/2026-10-27-addendum-domain-completeness.md` (referenced as "Add-A" … "Add-E").

**Repo root:** `C:\Users\Daiwik\Desktop\yottaflexAI\supplier-reliability-platform` (all paths below are relative to it).

## Environment facts (checked 2026-10-27)

- Installed: Python 3.11.9 and 3.14 (`py -0p`), git 2.55, gh 2.101. **Not installed:** uv, Python 3.12, Docker, PostgreSQL.
- Therefore: install uv with `py -3.11 -m pip install --user uv` (if `uv` is not then on PATH, use `py -3.11 -m uv` everywhere `uv` appears), then run `uv python install 3.12`. PostgreSQL, the Dockerfile and docker-compose get written and documented, but are marked **UNVERIFIED** in `docs/results.md` and the README.
- Shell: commands below work in both PowerShell and Git Bash unless marked.

## Global Constraints

- Python `>=3.12,<3.13`; `uv.lock` committed.
- Default seed `20261022`. All randomness comes from `RandomSource` (Task 5). The generator never calls `datetime.now()`, `random`, or an unseeded `np.random`.
- Fixed synthetic reference period (assumption): `2025-01-01T00:00:00Z` → `2026-06-30T23:59:59Z`. Scoring windows are calendar quarters 2025Q1…2026Q2 (6 windows), each with cutoff = window end.
- Full preset: exactly 1,000 suppliers, 10,000 POs, 15,000 shipments, 20,000 inspections. Starter preset: exactly 50 / 300 / 450 / 600.
- ID formats: `SUP-000001`, `SITE-SUP-000001-01`, `CTR-000001`, `PO-2026-000001`, `POL-2026-000001`, `SHP-2026-000001`, `SHL-2026-000001`, `EVT-2026-000001`, `INS-2026-000001`, `INV-2026-000001`, `RSK-2026-000001`, `SCORE-2026-000001`, `RAW-000001`, `EXC-000001`, `CASE-000001`. The year segment is the fixed literal `2026` (the dataset's "edition" year), not the record's date. This keeps counters simple and is documented.
- Weights: delivery 0.30, quality 0.25, cost 0.15, capacity 0.10, compliance 0.10, disruption 0.10. They are validated as nonnegative and must sum to 1 within 1e-9.
- Categories: `HIGH` [85, 100], `ACCEPTABLE` [70, 85), `AT_RISK` [50, 70), `CRITICAL` [0, 50), plus `INSUFFICIENT_EVIDENCE`.
- Timestamps are persisted as timezone-aware UTC. The API accepts ISO-8601 with an offset and converts to UTC. A naive datetime is rejected with 422.
- Money is `Decimal` in `Numeric(18,2)` and is quantized with `ROUND_HALF_UP`. Percentages are `Numeric(12,6)` fractions (0.05 = 5%). Scores are `Numeric(5,2)`. Floats never touch money.
- Missing ≠ zero: a component without enough evidence is `None` with a `missing_reason`, never 0 or 100.
- Scoring configuration is versioned. `scoring.scoring_version` (starting at `"1.0.0"`) must be bumped whenever any scoring parameter changes. Every score stores `scoring_version`, `config_hash` and `code_version` (= package `__version__`). The git SHA goes only into manifest `execution_metadata`, so content checksums stay stable across commits that do not change behaviour.
- Expected values in scoring tests are hand-calculated literals or come from the independent pandas implementation (Task 16). They are never read back from a stored score or from the engine under test (Add-D).
- Canonical tables keep FK constraints enabled. SQLite runs `PRAGMA foreign_keys=ON` on every connection. Nothing disables them.
- No LLM, external API or external dataset at runtime. All risk evidence is labelled synthetic (`signal_source` starts with `SYNTHETIC_`, `evidence_reference` starts with `synthetic://`).
- No secrets are committed. Commit only `.env.example`. `*.db`, `.env`, `logs/*`, and `data/generated/full/` are gitignored.
- `ruff check .` and `ruff format --check .` must be clean before each commit.
- Never record a result in docs unless the command was run and its output observed.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Prompt history is appended to `ai-usage/PROMPT_HISTORY.md` for every prompt given to any AI tool during the project (submission requirement 03).

## Key Domain Decisions (referenced by tasks)

These are the decisions the spec leaves open. They are fixed here so every task agrees, and Task 23 copies them into `docs/`.

**D1. Shipment↔PO-line allocation (Add-A `ShipmentLineAllocation`).** The model class is `ShipmentLineAllocation` and the table is `shipment_lines`. Columns: `shipment_line_id, shipment_id, po_line_id, unit_of_measure, allocated_quantity, shipped_quantity, received_quantity, allocated_at, shipped_at, received_at?, created_at`, unique on `(shipment_id, po_line_id)`.
- Reconciliation rules (each one is a CHECK or validation rule):
  - `received ≤ shipped ≤ allocated`.
  - The allocation's `unit_of_measure` = the PO line's UoM.
  - Σ allocated per PO line ≤ `ordered_quantity`.
  - `shipment.shipped_quantity` = Σ line shipped, and `shipment.received_quantity` = Σ line received.
  - Inspected quantity per line ≤ that line's `received_quantity` (D2).
- **Partial fulfilment is permitted.** A PO line may be split across up to 4 shipments, and a shortfall (Σ received < ordered) leaves the PO `PARTIALLY_RECEIVED`.
- Allocation (not the shipment header) is the only grain used for quantity, defect and invoice aggregation, so no join multiplies quantities.

**D2. Inspection grain.** There is one inspection per `shipment_line`. `inspection_kind ∈ {INITIAL, REINSPECTION}`, and `(shipment_line_id, inspection_kind='INITIAL')` is unique. Reconciliation rules: `accepted + rejected = inspected`; `rejected ≤ defect ≤ inspected`; `inspected ≤ received`. `inspection_type = FULL` means inspected = received; `SAMPLE` means inspected < received. Scoring uses only the **latest eligible** inspection per shipment_line (ordered by inspection_timestamp, then inspection_id), so re-inspections never double-count.

**D3. Invoices.** There is one invoice per delivered shipment (`invoice.shipment_id` FK, plus `po_id`, `contract_id`, `supplier_id`). `expected_amount = Σ(shipment_line.received_quantity × po_line.agreed_unit_price)`, quantized. `variance_amount = invoiced − expected`. `variance_percentage = variance_amount / expected` when expected ≠ 0, else NULL, and such an invoice is excluded from the cost denominator (counted in `missing_reasons`). `supplier_invoice_number` is unique per supplier. There is one currency per contract and no FX conversion; percentages are currency-agnostic. Taxes and freight are out of scope: expected and invoiced amounts are both pre-tax goods value. Credits are not generated, and a negative invoiced amount fails rule `DC-007`.

**D4. Delivery events.** Types are `BOOKED → DISPATCHED → IN_TRANSIT → (DELAYED)* → ARRIVED → RECEIVED`, plus `CORRECTION`, which may follow any event and whose `event_metadata.corrects_event_id` names an earlier event. A shipment's delivery outcome is *known at cutoff C* iff it has an `ARRIVED` event with `event_timestamp ≤ C` **and** `recorded_timestamp ≤ C`. `recorded_timestamp ≥ event_timestamp` always holds. About 5% of events are recorded 1–10 days late, so the cutoff rule matters.

**D5. Evidence eligibility for window [S, E], cutoff C (C ≥ S).** A record counts only if its occurrence time is within [S, min(E, C)] **and** its recording time is ≤ C.
- Delivery: shipments with `expected_delivery_timestamp` in [S, min(E,C)], meaning they are due. A due shipment with a known ARRIVED is on time iff `arrived ≤ expected + 24h grace`. A due shipment with no known ARRIVED by C counts as **late (overdue)** with delay measured as `C − expected`. A shipment not yet due is excluded.
- Quality: inspections with `inspection_timestamp` in window and `recorded_at ≤ C`.
- Cost: invoices with `invoice_date` in window and `recorded_at ≤ C`.
- Capacity: PO lines on POs with `order_date` in window (excluding CANCELLED POs) and `created_at ≤ C`, against `capacity_declarations` that cover the month and have `recorded_at ≤ C` (D12).
- Compliance: `compliance_requirements` effective at C, plus certifications with `recorded_at ≤ C` that were not superseded by a row recorded ≤ C, evaluated as of C (D11).
- Corrections: a `CORRECTION` event recorded ≤ C replaces the corrected field for that cutoff. One recorded after C is ignored (D13).
- Disruption: risk signals with `observed_timestamp ≤ C`, `recorded_timestamp ≤ C`, and `observed_timestamp ≥ C − 180 days`. Signals in quarantine are never eligible.
- Calculation time (`calculated_at`) never affects eligibility. In the pipeline it is set deterministically to `C + 1 day`; in the API it is wall-clock UTC.

**D6. Component formulas** (all results clamped to [0, 100], unrounded internally):
- Delivery (min 5 due shipments): `otr = on_time / due_known_or_overdue`. `penalty = min(delay_penalty_cap, delay_penalty_per_day × mean_late_days)`, where mean_late_days is averaged over late shipments only and the config defaults are cap 20 and 2 per day. `score = 100 × otr − penalty`. **This is a documented deviation from Add-B(1).** The addendum's proposed mapping is plain `100 × otr`, which is exactly what you get with `delay_penalty_per_day: 0`. The default keeps the penalty because a supplier that is 1 day late and one that is 20 days late should not score the same. The rationale goes in `docs/scoring_methodology.md`, and a test pins the penalty-off configuration to the addendum formula.
- Quality (min 3 eligible inspections): `defect_rate = Σ defect / Σ inspected`. `score = 100 × max(0, 1 − defect_rate / 0.10)`. Zero inspected units counts as missing, not 100.
- Cost (min 3 invoices with nonzero expected): `wavp = Σ(w_i × |var_i|) / Σ expected_i`, where `w_i = expected_i` for overbilling and `0.5 × expected_i` for underbilling. `score = 100 × max(0, 1 − wavp / 0.10)`.
- Capacity (needs ≥ 1 VERIFIED capacity declaration covering a window month and ≥ 1 eligible PO line): for each month m, `util_m = required_m / verified_free_m`. Here `required_m` = Σ ordered qty of eligible PO lines ordered in m, and `verified_free_m` = Σ (available − committed) over VERIFIED declarations covering m (D12). The raw metric is `util = max_m util_m`, using only months that have both. The mapping is ≤ 0.80 → 100; 0.80–1.00 linear 100 → 70; 1.00–1.50 linear 70 → 0; > 1.50 → 0. If `verified_free_m ≤ 0` while `required_m > 0`, util is treated as > 1.50 → 0. No VERIFIED declaration is missing ("capacity unverified"). No declaration at all is missing ("capacity unknown").
- Compliance: for each `compliance_requirement` effective at C (D11), take the current certification of that type as of C. State at C uses the D11 mapping (COMPLIANT / NONCOMPLIANT / EXPIRED / PENDING_VERIFICATION / UNKNOWN). Points are COMPLIANT 100, PENDING 50, EXPIRED / NONCOMPLIANT 0, and UNKNOWN is excluded. `score = mean(points of known)`; `coverage = known/required`. The component is missing if known = 0. **A mandatory requirement that is EXPIRED or NONCOMPLIANT sets `mandatory_compliance_failure = True`.**
- Disruption: signals are deduplicated by `(supplier_id, site_id, signal_type, observed date // 7 days)`, keeping the highest severity. `MONITORING_ATTESTATION` signals (severity `NONE`) are proof that monitoring happened and add no penalty. Severity weights are LOW 5, MEDIUM 15, HIGH 30, CRITICAL 50. `penalty = Σ weight × exp(−age_days/90) × (0.5 if RESOLVED else 1) × (1.0 if site_id is None or the site is active else 0.5)`. `score = 100 − penalty`. With zero eligible signals (no attestation either), the component is missing with "no risk monitoring evidence". That is not treated as "no risk".

**D7. Overall, missing-data policy, category, decision.**
- Round each component to 2 dp (ROUND_HALF_UP) first. Then `overall = Σ w_k × c_k / Σ w_k` over available components, rounded to 2 dp. Using rounded inputs makes reconciliation exact.
- `evidence_coverage = Σ w_k(available)`. If coverage < 0.70 **or** delivery is missing, the result is `reliability_category = INSUFFICIENT_EVIDENCE` and `overall_score` is still computed if any component exists. If 0.70 ≤ coverage < 1, `is_provisional = True`. `effective_weights` is always stored.
- Decision precedence (first match wins): (1) mandatory compliance failure → `SUSPEND_PENDING_REVIEW`; (2) an open `CRITICAL` data_exception linked to the supplier → `HOLD_FOR_DATA_REVIEW`; (3) INSUFFICIENT_EVIDENCE → `COLLECT_MORE_DATA`; (4) category map HIGH→`PREFERRED`, ACCEPTABLE→`APPROVED`, AT_RISK→`MONITOR`, CRITICAL→`ESCALATE`.
- Deteriorating means the latest overall score is ≥ 10 points below the previous quarter's (both non-insufficient).

**D8. Score identity, idempotency, versioning.** `evaluation_key = sha256("|".join([supplier_id, site_id or "*", S, E, C (ISO UTC), scoring_version, config_hash]))[:32]`, unique on `(evaluation_key, version_number)`. `calculate` returns the existing current row when the key exists (`created=False`), otherwise inserts version 1. `recalculate` recomputes and compares `evidence_fingerprint`, which is sha256 over the sorted contributing record ids + their row values. If it is equal, it returns the current row (`changed=False`). Otherwise it inserts `version_number+1`, sets the old row's `is_current=False` and `superseded_by_score_id`, and stores `change_reason`. Rows are never updated except for those two supersession fields.
- **Lineage (Add-C):** every score has `score_evidence` rows `(score_id, component, source_entity_type, source_record_id, included: bool, exclusion_reason?, contribution JSON)`. These are written for **every** included record and for records excluded for eligibility reasons (not yet due, recorded after cutoff, superseded, quarantined, zero expected). `contribution` holds the per-record numbers used, e.g. `{"expected":..., "arrived":..., "late_days":...}`. Other stored lineage: `scoring_version`, `config_hash`, `code_version`, `calculated_at`, `explanation.applied_overrides` (the decision rule that fired), `explanation.missing_data_decisions`, and `explanation.exceptions`. A score is therefore rebuilt by reading its `score_evidence` rows plus the config identified by `config_hash`, which Task 12b tests.

**D9. Raw-ingestion layer & anomalies.** The injector writes one raw batch `BATCH-<dataset_id>`. The batch contains the **negatives**, deterministic samples of valid records copied from canonical (full: 500 shipments, 500 delivery-event pairs, 300 risk signals, 300 historical scores; starter: 40 / 40 / 30 / 30). It also contains the **positives** (full: 25 per class; starter: 5 per class):
- A `SHIPMENT_MISSING_PO`: a shipment payload whose `po_id` is absent from canonical.
- B `DELIVERY_BEFORE_DISPATCH`: an ARRIVED event payload for a real shipment with `event_timestamp` before its DISPATCHED event.
- C `SCORE_FUTURE_LEAKAGE`: a score payload computed with evidence through C+90d but labelled with cutoff C. Its `evidence_refs` cite real canonical record ids.
- D `RISK_SIGNAL_MISSING_PROVENANCE`: a risk-signal payload with `signal_source` null/blank (half of cases) or `observed_timestamp` null (the other half).
- E `SITE_ATTRIBUTION_CONFLICT`: a shipment payload whose `originating_site_id` exists but belongs to another supplier (60% of cases), or belongs to the right supplier but is outside its effective period at dispatch (40%). All FKs exist.

Raw records are `(raw_record_id, batch_id, source_entity_type, source_record_id, payload JSON, received_at)`. Ground truth contains **only** the positives (written to `data/ground_truth/anomaly_ground_truth.csv`). The negative population for each class is all raw records of that class's entity type (A, E: SHIPMENT; B: DELIVERY_EVENT; C: SUPPLIER_SCORE; D: RISK_SIGNAL) that are not positives for that class. Evaluation is **record-level per class**.

**D10. Supplier profiles** (shares of suppliers; the remainder after rounding goes to `average`). The profile is stored only in `data/ground_truth/supplier_profiles.csv` and never in the `suppliers` table.

| profile | share | PO weight | on-time p (start → end trend) | mean late days | defect mean | invoice var p / mag | demand/capacity | cert expiry p | risk signals / yr |
|---|---|---|---|---|---|---|---|---|---|
| stable_high | .20 | 1.2 | .97 → .97 | 1 | .005 | .02 / .01 | .55 | .02 | 0.5 |
| average | .25 | 1.0 | .88 → .88 | 3 | .02 | .08 / .02 | .70 | .05 | 1 |
| deteriorating_lead_time | .10 | 1.0 | .92 → .55 | 6 | .02 | .08 / .02 | .75 | .05 | 1.5 |
| quality_issues | .08 | 1.0 | .88 → .85 | 3 | .07 | .08 / .02 | .70 | .05 | 1 |
| invoice_discrepancy | .08 | 1.0 | .88 → .88 | 3 | .02 | .45 / .09 | .70 | .05 | 1 |
| capacity_constrained | .07 | 1.3 | .80 → .75 | 5 | .03 | .08 / .02 | 1.35 | .05 | 1.5 |
| compliance_risk | .06 | 0.9 | .86 → .86 | 3 | .025 | .08 / .02 | .70 | .60 | 1 |
| disruption_prone | .06 | 0.9 | .80 → .70 | 7 | .03 | .08 / .02 | .80 | .05 | 6 |
| sparse_history | .05 | 0.1 | .85 → .85 | 4 | .02 | .08 / .02 | .60 | .10 | 0.2 |
| recovering | .05 | 1.0 | .60 → .93 | 6 → 2 | .06 → .015 | .08 / .02 | .85 | .05 | 1 |

With `recovering` added, `average` drops to .25 so the shares still sum to 1.00. The trend columns (`a → b`) are interpolated linearly over the reference period. These values live in `config/default.yaml` under `generator.profiles` and must not be hard-coded.

**D11. Compliance model (Add-A).**
- `compliance_requirements(requirement_id, contract_id, certification_type, mandatory, scope ∈ {SUPPLIER, SITE}, site_id?, effective_from, effective_to?, created_at)`.
- `supplier_certifications(certification_id, supplier_id, site_id?, certification_type, certificate_number, issuing_authority, verification_status ∈ {VERIFIED, PENDING_VERIFICATION, REJECTED}, compliance_status ∈ {COMPLIANT, NONCOMPLIANT}, valid_from, valid_to, verified_at?, evidence_reference, supersedes_certification_id?, recorded_at)`.
- `issuing_authority` is a clearly synthetic name (`"Synthetic Certification Body 03"`), and `evidence_reference` starts with `synthetic://cert/`.
- **History is append-only.** A status change, renewal or revocation inserts a new row with `supersedes_certification_id` set, and the old row is never edited. The current certification as of C is the latest row recorded ≤ C that is not superseded by another row recorded ≤ C.
- State mapping at C:
  - verification PENDING → `PENDING_VERIFICATION`
  - REJECTED → `NONCOMPLIANT`
  - VERIFIED + NONCOMPLIANT → `NONCOMPLIANT`
  - VERIFIED + COMPLIANT + valid_to < C → `EXPIRED`
  - VERIFIED + COMPLIANT + valid_from ≤ C ≤ valid_to → `COMPLIANT`
  - no row → `UNKNOWN`
- A SITE-scoped requirement is satisfied only by a certification of that site.

**D12. Capacity model (Add-A).**
- `capacity_declarations(capacity_declaration_id, supplier_id, site_id?, period_start (first day of month, UTC), period_end, unit = "units/month", available_capacity, committed_capacity, verification_status ∈ {VERIFIED, UNVERIFIED}, source, recorded_at)`.
- `available_capacity` is the site's total monthly output. `committed_capacity` is output already promised to other customers. Verified free capacity = available − committed.
- **Scope rule (no double counting):** if a supplier has site-level declarations for a month, only those are summed. A supplier-wide (`site_id IS NULL`) declaration is used only for months that have no site-level ones.
- Demand is aggregated per calendar month of `order_date`, over all of the supplier's sites, in the PO line's unit. All generated units are `units/month`, and a UoM mismatch is a validation finding (`DC-008`), not a silent conversion.
- `contract.contracted_capacity` stays as the contractual monthly ceiling. Rule `DC-009` flags months where demand exceeds it (INFO). It is not used by the capacity score, which uses verified capacity per §9.5.

**D13. Late-arriving and corrected records (Add-C).**
- A historical score answers "what was knowable at C". Records recorded after C (late-arriving events, back-dated inserts, corrections) can never change the score at cutoff C. Recomputing at the same C reproduces it exactly: same fingerprint, so `recalculate` returns `changed=False`.
- To take late knowledge into account, request a **restatement**: the same window with a later `data_cutoff` C′. That is a different evaluation key, so it creates a new score row with `restates_score_id` pointing to the C-cutoff score. Its explanation includes `differences_from_restated` (per-component delta + the evidence ids added or removed).
- Corrections are `CORRECTION` delivery events with `event_metadata = {corrects_event_id, field, corrected_value}`, plus superseding certification rows (D11). Evidence collection at C applies corrections recorded ≤ C and ignores later ones. The generator emits about 1% corrections, mostly to ARRIVED timestamps.
- `recalculate` at the same key creates a new version only when the eligible evidence actually changed, e.g. after an operator inserts a record with `recorded_at ≤ C` to repair a load gap. That change is visible as a fingerprint change plus a `change_reason`.

## Review Focus

1. **Late recording around the cutoff.** An ARRIVED event that occurred before cutoff but was recorded after it must make the shipment "overdue/late", not on time, and must never leak into a historical score. → test `test_arrival_recorded_after_cutoff_is_not_known` in Task 9.
2. **Join multiplication.** A shipment with 3 lines, 2 inspections on one line (initial + reinspection) and one invoice must contribute its delivery once, its defects once per line, and its invoice once. → test `test_evidence_counts_do_not_multiply_across_joins` in Task 9.
3. **Supplier with zero activity in a window** (sparse_history, or no POs at all) must produce a persisted `INSUFFICIENT_EVIDENCE` / `COLLECT_MORE_DATA` score, not crash or score 0. → test `test_supplier_without_activity_scores_insufficient` in Task 12.
4. **Destructive regeneration through the API.** `POST /datasets/generate` while data exists and without `replace_existing: true` → 409. A second generate while a job is RUNNING → 409. → tests in Task 21.
5. **Cross-platform checksum drift.** CSV exports written on Windows must use `\n` line endings and fixed decimal/timestamp formatting, so checksums match on Linux. → test `test_export_uses_lf_and_canonical_formats` in Task 8.

---

## File Structure

```
pyproject.toml, uv.lock, .gitignore, .env.example, alembic.ini, Dockerfile, docker-compose.yml, README.md
config/default.yaml                       # scoring weights/thresholds, generator presets & profiles, anomaly counts
alembic/env.py, alembic/versions/0001_initial_schema.py
src/supplier_reliability/
  __init__.py                             # __version__, SCHEMA_VERSION="1", GENERATOR_VERSION="1.0.0", SCORING_VERSION="1.0.0"
  config.py                               # Settings (env), AppConfig (YAML) + loaders
  logging_config.py                       # configure_logging
  database.py                             # engine/session factory, SQLite FK pragma, session_scope
  domain/enums.py                         # every StrEnum + DELIVERY_EVENT_TRANSITIONS
  domain/numeric.py                       # to_money, round_score, safe_ratio, utc
  models/{base,master,transactions,scoring,operations}.py
  repositories/{suppliers,transactions,scores,exceptions,validation,jobs}.py
  services/generator/{config,rng,ids,profiles,master_data,transactions,quality_finance,risk,dataset,loader}.py
  services/export.py                      # deterministic CSV/JSON export + manifest
  services/scoring/{evidence,components,aggregate,engine,service}.py
  services/validation/{framework,rules_integrity,rules_domain,rules_chronology,rules_arithmetic,rules_semantic}.py
  services/anomalies/injector.py          # raw batch + ground truth (separate from detection)
  services/anomaly_detection/detectors.py # A–E detectors + quarantine + leakage correction
  services/evaluation/{anomaly_eval,independent_metrics,reconciliation}.py
  services/reporting/{reports,writers}.py
  services/reproducibility.py
  services/acceptance.py                  # objective acceptance-gate runner (Add-E)
  services/pipeline.py                    # run_pipeline: one orchestration used by CLI + API job
  schemas/{common,suppliers,transactions,scores,validation,datasets,reports}.py
  api/{errors,dependencies}.py, api/routes/{health,suppliers,transactions,scores,validation,datasets,reports}.py
  main.py                                 # create_app()
  cli.py                                  # Typer app
scripts/{generate_data,validate_data,verify_reproducibility}.py   # thin wrappers over cli functions
tests/{conftest.py, unit/, integration/, api/, data_quality/, scoring/, scoring/benchmarks/, reproducibility/, acceptance/}
docs/{architecture,data_dictionary,entity_relationships,business_rules,scoring_methodology,anomaly_catalog,api_reference,evaluation_methodology,assumptions_and_limitations,results}.md
ai-usage/{PROMPT_HISTORY.md, AI_USAGE.md}
data/{generated,ground_truth,manifests}/.gitkeep, reports/, logs/.gitkeep
```

---

### Task 0: Repository, toolchain, AI-usage record

**Files:**
- Create: `.gitignore`, `ai-usage/PROMPT_HISTORY.md`, `ai-usage/AI_USAGE.md`
- Already present: `docs/specs/2026-10-27-master-implementation-prompt.md`, this plan

- [ ] **Step 1:** Install uv and Python 3.12. Run `py -3.11 -m pip install --user uv`, `uv --version` (or `py -3.11 -m uv --version`), and `uv python install 3.12`. Expected: a uv version string, then "Installed Python 3.12.x".
- [ ] **Step 2:** Run `git init -b main` in the repo root. Write `.gitignore` covering Python, `.venv/`, `*.db`, `*.sqlite*`, `.env`, `logs/*.log`, `data/generated/full/`, `data/ground_truth/full/`, `.pytest_cache/`, `.ruff_cache/`, and `reports/tmp/`.
- [ ] **Step 3:** Create `ai-usage/PROMPT_HISTORY.md` with dated entries. Entry 1 is "the master implementation prompt (see docs/specs/…)" plus the user's request "using these details generate me a plan of implementation of this project". Entry 2 is the addendum (`docs/specs/2026-10-27-addendum-domain-completeness.md`) with "check this if it is required or not". Entry 3 is "yeah update the plan". Create `ai-usage/AI_USAGE.md` with three headings to fill in during the project: "Where AI accelerated", "Where I relied heavily on AI", and "What I personally reviewed, changed, validated".
- [ ] **Step 4:** Commit: `git add -A && git commit -m "chore: initialize repository with spec, plan and AI-usage log"`.

### Task 1: Project bootstrap — settings, config, logging, app entry, test harness

**Files:**
- Create: `pyproject.toml`, `.env.example`, `config/default.yaml`, `src/supplier_reliability/{__init__,config,logging_config,main}.py`, `src/supplier_reliability/api/routes/health.py` (liveness only for now), `tests/conftest.py`, `tests/unit/test_config.py`, `tests/api/test_health.py`

**Interfaces:**
- Produces:
  - `Settings(BaseSettings)` with fields `database_url: str = "sqlite:///./data/app.db"`, `app_env: str = "local"`, `log_level: str = "INFO"`, `log_json: bool = False`, `config_path: Path = Path("config/default.yaml")`, `data_dir: Path = Path("data")`, `reports_dir: Path = Path("reports")`, `max_page_size: int = 500`, `cors_origins: list[str] = []`. The env prefix is `SRP_`.
  - `AppConfig` (Pydantic) containing `scoring: ScoringConfig`, `generator: GeneratorSettingsBlock`, and `anomalies: AnomalyConfig`. `ScoringConfig` has `weights: dict[ComponentName, float]`, `category_thresholds`, `grace_hours=24`, `min_observations`, `coverage_threshold=0.70`, `defect_rate_zero_score=0.10`, `variance_zero_score=0.10`, `underbilling_weight=0.5`, `severity_weights`, `recency_days=90`, `lookback_days=180`, and `scoring_version`. It has a validator for nonnegative weights that sum to 1 and a validator for gap-free thresholds. `config_hash()` returns sha256 of the canonical JSON.
  - `load_app_config(path: Path) -> AppConfig`, `get_settings() -> Settings` (lru_cached).
  - `configure_logging(level: str, json_format: bool) -> None`.
  - `create_app(settings: Settings | None = None) -> FastAPI` in `main.py`; module-level `app = create_app()`.

- [ ] **Step 1:** Write `pyproject.toml`. Use a hatchling build with a `src/` layout. Runtime deps (lower-bounded, then locked): `fastapi>=0.115`, `uvicorn[standard]>=0.32`, `pydantic>=2.9`, `pydantic-settings>=2.6`, `sqlalchemy>=2.0.36`, `alembic>=1.14`, `psycopg[binary]>=3.2`, `numpy>=2.1`, `pandas>=2.2`, `faker>=30`, `typer>=0.12`, `pyyaml>=6`. Dev group: `pytest>=8`, `httpx>=0.27`, `ruff>=0.7`. Add the script entry `srp = "supplier_reliability.cli:app"`. Ruff: line-length 100, target py312, rules `E,F,I,UP,B,SIM`. Pytest: `testpaths=["tests"]`, plus a marker `slow` for full-scale tests that are excluded by default via `addopts="-m 'not slow'"`.
- [ ] **Step 2:** Run `uv sync --python 3.12`. Expected: `.venv` is created and `uv.lock` is written.
- [ ] **Step 3:** Write the failing tests:

```python
# tests/unit/test_config.py
def test_default_weights_sum_to_one(app_config):
    assert abs(sum(app_config.scoring.weights.values()) - 1.0) < 1e-9
    assert app_config.scoring.weights["delivery"] == 0.30

def test_negative_weight_rejected(app_config):
    data = app_config.model_dump(); data["scoring"]["weights"]["cost"] = -0.15
    with pytest.raises(ValidationError): AppConfig.model_validate(data)

def test_weights_not_summing_to_one_rejected(app_config): ...  # cost=0.20 → ValidationError

def test_threshold_gap_rejected(app_config): ...  # ACCEPTABLE lower=71 → ValidationError

def test_config_hash_stable(app_config):
    assert app_config.config_hash() == load_app_config(CONFIG_PATH).config_hash()

# tests/api/test_health.py
def test_health_is_live_without_db(client): r = client.get("/health"); assert r.status_code == 200 and r.json()["status"] == "ok"
```

`tests/conftest.py` provides `app_config` (loaded from repo `config/default.yaml`) and `tmp_settings(tmp_path)`, a `Settings` with `database_url=f"sqlite:///{tmp_path}/test.db"` and data/reports dirs under tmp_path. It also provides `client`, a `TestClient(create_app(tmp_settings))`. No test may read `./data` or `./reports`.

- [ ] **Step 4:** Run `uv run pytest tests/unit/test_config.py tests/api/test_health.py -v`. Expected: FAIL (import errors).
- [ ] **Step 5:** Implement the files listed above. `config/default.yaml` holds the D6/D7 parameters, the D10 profile table, presets `starter` and `full` (counts per Global Constraints), the reference period, and the anomaly counts per D9.
- [ ] **Step 6:** Run the tests again. Expected: PASS. Then run `uv run uvicorn supplier_reliability.main:app --port 8000` in the background, request `/health`, and confirm 200, then stop it.
- [ ] **Step 7:** Run `uv run ruff check . && uv run ruff format --check .`, then commit `feat: project bootstrap with settings, config validation, logging and health endpoint`.

### Task 2: Domain enums, numeric helpers, DB layer, master-data models

**Files:**
- Create: `src/supplier_reliability/domain/{__init__,enums,numeric}.py`, `src/supplier_reliability/database.py`, `src/supplier_reliability/models/{__init__,base,master}.py`, `tests/unit/test_numeric.py`, `tests/integration/test_master_models.py`

**Interfaces:**
- Produces:
  - Enums (`StrEnum`):
    - `SupplierStatus{ACTIVE,INACTIVE,SUSPENDED}`
    - `SupplierCategory{RAW_MATERIALS,ELECTRONICS,PACKAGING,CHEMICALS,MACHINED_PARTS,LOGISTICS_SERVICES}`
    - `Region{NA,LATAM,EMEA,APAC}`
    - `SiteType{FACTORY,WAREHOUSE,DISTRIBUTION_CENTER}`
    - `SiteStatus{ACTIVE,INACTIVE}`
    - `ContractStatus{ACTIVE,EXPIRED,TERMINATED,DRAFT}`
    - `CertificationType{ISO9001,ISO14001,IATF16949,REACH,CONFLICT_MINERALS}`
    - `CertificationStatus{COMPLIANT,NONCOMPLIANT}`, `VerificationStatus{VERIFIED,PENDING_VERIFICATION,REJECTED}`, `RequirementScope{SUPPLIER,SITE}`, `CapacityVerification{VERIFIED,UNVERIFIED}`
    - `ComplianceState{COMPLIANT,NONCOMPLIANT,EXPIRED,PENDING_VERIFICATION,UNKNOWN}`
    - `POStatus{OPEN,PARTIALLY_RECEIVED,RECEIVED,CANCELLED,CLOSED}`
    - `ShipmentStatus{BOOKED,IN_TRANSIT,DELIVERED,PARTIALLY_RECEIVED,CANCELLED}`
    - `DeliveryEventType` (D4)
    - `InspectionKind`, `InspectionType{FULL,SAMPLE}`, `InspectionStatus{PASSED,FAILED,CONDITIONAL}`
    - `DefectCategory{NONE,DIMENSIONAL,COSMETIC,FUNCTIONAL,PACKAGING,DOCUMENTATION}`
    - `InvoiceStatus{RECEIVED,APPROVED,DISPUTED,PAID}`
    - `RiskSignalType{LOGISTICS_DISRUPTION,FACTORY_INCIDENT,FINANCIAL_DISTRESS,CERTIFICATION_LAPSE,CARRIER_DELAY,MONITORING_ATTESTATION}`
    - `Severity{NONE,LOW,MEDIUM,HIGH,CRITICAL}`
    - `ResolutionStatus{OPEN,MITIGATING,RESOLVED}`
    - `ReliabilityCategory`, `Decision` (D7)
    - `CalculationStatus{CALCULATED,PROVISIONAL,INSUFFICIENT_EVIDENCE,CORRECTED}`
    - `ComponentName{delivery,quality,cost,capacity,compliance,disruption}`
    - `IssueType` (D9 A–E), `Disposition{QUARANTINED,QUARANTINED_PENDING_PROVENANCE,CORRECTED,REJECTED}`, `DetectionStatus{DETECTED,CONFIRMED,DISMISSED}`
    - `JobStatus{QUEUED,RUNNING,SUCCEEDED,FAILED}`
  - `DELIVERY_EVENT_TRANSITIONS: dict[DeliveryEventType | None, frozenset[DeliveryEventType]]`
  - `to_money(x: Decimal | int | str) -> Decimal` (2 dp, HALF_UP), `round_score(x: float | Decimal) -> Decimal` (2 dp, HALF_UP), `safe_ratio(n, d) -> Decimal | None` (None when d == 0), `ensure_utc(dt: datetime) -> datetime` (raises ValueError for naive)
  - `make_engine(url: str) -> Engine` (adds FK pragma + `check_same_thread=False` for SQLite), `make_session_factory(engine) -> sessionmaker[Session]`, `session_scope(factory) -> ContextManager[Session]` (commit/rollback)
  - `UTCDateTime` TypeDecorator in `models/base.py`. It stores UTC and returns aware UTC on both SQLite and PG, rejects naive datetimes, and is used for every timestamp column.
  - Models `Supplier`, `Site`, `Contract`, `ContractPrice(contract_id, product_category, unit_of_measure, unit_price; unique(contract_id, product_category))`, `ComplianceRequirement` and `SupplierCertification` (D11), and `CapacityDeclaration` (D12). Fields are per §5.1–5.3. `Contract` has `site_id?` (site scope), `contracted_capacity: Numeric(14,2) | None` and `capacity_unit: str` (`"units/month"`), and holds no JSON compliance list; requirements are rows. CHECKs: `committed_capacity >= 0`, `available_capacity >= 0`, `valid_to >= valid_from`. Unique: `(supplier_id, certificate_number)`. `supersedes_certification_id` is a self-FK. Enum columns use `sa.Enum(..., native_enum=False, create_constraint=True)` for portable CHECKs. Add a CHECK for `effective_to IS NULL OR effective_to >= effective_from`. Indexes go on all `supplier_id` FKs.

- [ ] **Step 1:** Write the failing tests: `test_to_money_rounds_half_up` (`to_money("2.345") == Decimal("2.35")`), `test_safe_ratio_zero_denominator_is_none`, `test_ensure_utc_rejects_naive`; plus integration tests on a temp SQLite: `test_fk_enforced_for_site_without_supplier` (insert a Site with unknown supplier → `IntegrityError`), `test_contract_effective_dates_check`, `test_timestamps_roundtrip_as_utc_aware`, `test_invalid_enum_rejected`, `test_requirement_with_unknown_contract_rejected`, `test_negative_committed_capacity_rejected`, `test_certification_supersedes_must_exist`.
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_numeric.py tests/integration/test_master_models.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. (The integration tests use `Base.metadata.create_all` on the temp engine for now; Task 4 switches them to Alembic.)
- [ ] **Step 4:** Run the tests. Expected: PASS. Run ruff, then commit `feat: domain enums, numeric helpers, database layer and master-data models`.

### Task 3: Transaction, scoring and operational models

**Files:**
- Create: `src/supplier_reliability/models/{transactions,scoring,operations}.py`, `tests/integration/test_transaction_models.py`

**Interfaces:**
- Consumes: Task 2 base, enums, `UTCDateTime`.
- Produces:
  - `PurchaseOrder`, `POLine` (unique `(po_id, line_number)`; CHECK `ordered_quantity > 0`; `line_amount` stored), `Shipment` (FKs po_id, originating_site_id → sites, `destination_warehouse_id: str`, `carrier_id: str | None`; CHECK `received_quantity <= shipped_quantity`), `ShipmentLineAllocation` (D1; table `shipment_lines`; CHECK `received_quantity <= shipped_quantity <= allocated_quantity`), `DeliveryEvent` (CHECK `recorded_timestamp >= event_timestamp`, `event_metadata: JSON`), `Inspection` (D2 fields + `recorded_at`; CHECKs for D2 reconciliation), `Invoice` (D3; unique `(supplier_id, supplier_invoice_number)`; `recorded_at`), `RiskSignal` (§5.10 + `related_shipment_id?`, `related_contract_id?`, `dedup_key`)
  - `SupplierScore`: §5.11 fields plus `evaluation_key`, `version_number`, `is_current`, `superseded_by_score_id?`, `change_reason?`, `is_provisional`, `evidence_coverage: Numeric(5,4)`, `effective_weights: JSON`, `explanation: JSON`, `evidence_fingerprint`, `config_hash`, `code_version`, `restates_score_id?` (D13). Unique `(evaluation_key, version_number)`; indexes on `(supplier_id, evaluation_end)`, `reliability_category`, `calculation_status`.
  - `ScoreEvidence` (D8 lineage): PK `(score_id, component, source_entity_type, source_record_id)`, plus `included`, `exclusion_reason?` and `contribution: JSON`. Index on `(source_entity_type, source_record_id)` to answer "which scores used this record?".
  - `ScoringConfigSnapshot(config_hash PK, scoring_version, config JSON, created_at)`: every config used by a score is stored once, so lineage never depends on the YAML file still matching.
  - `RawIngestionRecord`, `DataException` (spec §8 fields + `batch_id`, `rule_id`, `supplier_id?`, `corrected_score_id?`), `ValidationRun(run_id, started_at, finished_at, scope, finding_counts JSON)`, `ValidationFinding` (§11 fields + `run_id`, `is_expected_anomaly: bool`), `DatasetJob(job_id, status, preset, seed, requested_at, started_at?, finished_at?, error_message?, dataset_id?)`, `DatasetManifest(dataset_id PK, manifest JSON, created_at)`.
  - Indexes: `po_id`, `shipment_id`, `site_id`, `(shipment_id, event_timestamp)`, `observed_timestamp`, `invoice_date`, `exception severity`, `issue_type`.

- [ ] **Step 1:** Write the failing tests: `test_po_line_number_unique_per_po`, `test_shipment_line_unique_per_shipment_and_po_line`, `test_allocation_shipped_above_allocated_rejected`, `test_score_evidence_requires_existing_score`, `test_event_recorded_before_occurrence_rejected`, `test_inspection_reconciliation_check` (accepted+rejected≠inspected → IntegrityError), `test_duplicate_supplier_invoice_number_rejected`, `test_score_unique_evaluation_key_and_version`, `test_po_with_unknown_contract_rejected`.
- [ ] **Step 2:** Run `uv run pytest tests/integration/test_transaction_models.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Use `relationship(..., lazy="raise")` by default. Repositories must then load explicitly with `selectinload`/joins, which prevents accidental N+1 queries.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run ruff, then commit `feat: transaction, scoring and operational models with integrity constraints`.

### Task 4: Alembic migration + migration tests

**Files:**
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/0001_initial_schema.py`, `tests/integration/test_migrations.py`
- Modify: `tests/conftest.py` (add `migrated_engine` fixture that runs `alembic upgrade head` against the temp DB; switch Task 2/3 tests to it)

**Interfaces:**
- Produces: `run_migrations(database_url: str) -> None` in `database.py` (programmatic `alembic.command.upgrade(cfg, "head")`). `env.py` reads `SRP_DATABASE_URL`, falling back to `Settings`, and sets `render_as_batch=True` for SQLite.

- [ ] **Step 1:** Write the failing tests: `test_upgrade_creates_all_tables` (table names ⊇ the 11 entities + `contract_prices`, `compliance_requirements`, `supplier_certifications`, `capacity_declarations`, `shipment_lines`, `score_evidence`, `scoring_config_snapshots`, operational tables), `test_migration_matches_models` (use `alembic.autogenerate.compare_metadata` → empty diff), `test_downgrade_to_base_and_upgrade_again`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Generate the revision with `uv run alembic revision --autogenerate -m "initial schema"` against an empty SQLite file, rename it to `0001_initial_schema.py`, and review it by hand. Check the enum CHECK constraints, the JSON types, and the named constraints. Add a naming convention to `Base.metadata` so constraint names are stable across SQLite and PG.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run ruff, then commit `feat: alembic initial migration with migration round-trip tests`.

### Task 5: Generator foundations — config, RNG, IDs, profiles

**Files:**
- Create: `src/supplier_reliability/services/generator/{__init__,config,rng,ids,profiles}.py`, `tests/unit/test_generator_foundations.py`

**Interfaces:**
- Produces:
  - `GeneratorConfig` (Pydantic): `seed: int = 20261022`, `preset: str`, `n_suppliers`, `n_purchase_orders`, `n_shipments`, `n_inspections`, `reference_start`, `reference_end`, `profiles: dict[str, ProfileParams]`, `anomalies: AnomalyConfig`. `from_app_config(app_config, preset: str, seed: int | None = None) -> GeneratorConfig`. `dataset_id` property = `f"DS-{preset}-{seed}-{config_hash[:8]}"`.
  - `RandomSource(seed: int)` with `.stream(name: str) -> np.random.Generator`. Each named stream comes from `np.random.SeedSequence([seed, crc32(name)])`, so adding a new stream never shifts existing ones. It also has `.faker(name) -> Faker`, seeded from that stream, with locale `en_US`.
  - `IdRegistry` with `.next(kind: str) -> str` using the Global Constraints formats, `.register(kind, id)`, `.exists(kind, id) -> bool`, and `.all(kind) -> list[str]`.
  - `assign_profiles(n: int, shares: dict[str, float], rng) -> list[str]`. It computes exact counts by rounding, gives the remainder to `average`, then shuffles deterministically.
  - `allocate_exact(total: int, weights: np.ndarray, minimum: int, rng) -> np.ndarray` (integer allocation summing to `total`, each ≥ minimum; raises `GeneratorCapacityError` if impossible).

- [ ] **Step 1:** Write the failing tests: `test_same_seed_same_stream_values`, `test_stream_independent_of_creation_order`, `test_id_format_and_sequence` (`next("supplier") == "SUP-000001"`, then `"SUP-000002"`; `next("site", parent="SUP-000001") == "SITE-SUP-000001-01"`), `test_assign_profiles_exact_counts` (n=1000 → stable_high 200, sparse_history 50, total 1000), `test_allocate_exact_sums_and_minimum`, `test_allocate_exact_impossible_raises`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run ruff, then commit `feat: deterministic generator foundations (rng streams, id registry, profiles)`.

### Task 6: Generator — master data (suppliers, sites, contracts, prices, certifications)

**Files:**
- Create: `src/supplier_reliability/services/generator/master_data.py`, `src/supplier_reliability/services/generator/dataset.py`, `tests/unit/test_generator_master.py`

**Interfaces:**
- Consumes: Task 5.
- Produces:
  - `GeneratedDataset` dataclass. It has one `list[dict]` per table, named after the table (`suppliers`, `sites`, `contracts`, `contract_prices`, `compliance_requirements`, `supplier_certifications`, `capacity_declarations`, `purchase_orders`, `po_lines`, `shipments`, `shipment_lines`, `delivery_events`, `inspections`, `invoices`, `risk_signals`), plus `supplier_profiles: dict[str, str]`, which is ground truth only, and `config: GeneratorConfig`. Dict keys equal the model column names.
  - `generate_master_data(cfg, rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset) -> None`
- Rules:
  - Each supplier has 1–3 sites (sparse_history has 1).
  - Site effective periods start at onboarding. About 10% of sites have `effective_to` set inside the reference period with status INACTIVE.
  - Contracts: 1–2 per supplier, covering the reference period with no gap. About 15% of suppliers have a first contract that EXPIRED mid-period followed by a renewal.
  - About 5% of contracts have NULL `contracted_capacity`.
  - Capacity declarations: one per site per quarter (expanded to monthly rows). `available − committed` is set so that expected monthly demand (from the PO weight) / free capacity ≈ the profile's demand/capacity ratio. About 10% of declarations are UNVERIFIED, and about 5% of suppliers have no declarations at all (capacity unknown).
  - Each contract has 2–4 `compliance_requirements` rows, at least 1 mandatory, about 15% of them SITE-scoped.
  - Certifications come from the profile's `cert expiry p`. Lapses are an expired `valid_to` inside the period, a later superseding row with NONCOMPLIANT/REJECTED, or a PENDING_VERIFICATION renewal. Each supplier has at least one superseding (history) row with probability 0.3. About 8% of requirements have no certification (UNKNOWN).
  - Each contract has a price per product category.
  - `created_at`/`updated_at` are deterministic: onboarding date + offset.

- [ ] **Step 1:** Write the failing tests (starter preset): `test_supplier_count_exact` (50), `test_every_site_belongs_to_existing_supplier`, `test_contracts_cover_reference_period_without_gap`, `test_some_contracts_have_unknown_capacity`, `test_capacity_declarations_nonnegative_free_mostly_and_some_unverified`, `test_certification_history_supersedes_existing_rows`, `test_site_scoped_requirement_has_site_of_same_supplier`, `test_issuing_authority_and_evidence_are_synthetic`, `test_profiles_not_in_supplier_rows` (no key named `profile` in any supplier dict), `test_master_data_deterministic` (two runs → equal lists).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: generate deterministic supplier master data`.

### Task 7: Generator — POs, lines, shipments, shipment lines, delivery events

**Files:**
- Create: `src/supplier_reliability/services/generator/transactions.py`, `tests/unit/test_generator_transactions.py`

**Interfaces:**
- Produces: `generate_orders_and_shipments(cfg, rs, ids, ds) -> None`.
- Algorithm (decisions the implementer cannot infer):
  1. Allocate exactly `n_purchase_orders` across suppliers with `allocate_exact(weights = profile PO weight, minimum = 0)`. A sparse_history supplier may get 0.
  2. Order dates are uniform over the reference period minus the final 10 days. `required_delivery_date = order_date + lead_time` (14–45 days, by category). The PO uses the contract active at order_date; the PO line's `agreed_unit_price` = the contract price for its category; `line_amount = to_money(qty × price)`. Each PO has 1–5 lines (mean ≈ 2.6). About 2% of POs are CANCELLED, with 0 shipments.
  3. Allocate exactly `n_shipments` across non-cancelled POs (minimum 1, maximum 4, weight ∝ number of lines). Each shipment creates `ShipmentLineAllocation` rows for a subset of the PO's lines. `allocated_quantity` splits the ordered quantity across the PO's shipments, and `shipped_quantity ≤ allocated` (short-shipping happens in about 3% of allocations). `allocated_at` = booking time, `shipped_at` = dispatch, and `received_at` = RECEIVED event time. About 4% of PO lines end with a shortfall (partial fulfilment → PO `PARTIALLY_RECEIVED`).
  4. Dispatch from one of the supplier's sites that is effective on the dispatch date. `expected_delivery = dispatch + transit` (2–10 days). Lateness is drawn from the profile's on-time p, interpolated linearly between start and end over the period (the "deteriorating" trend). The delay in days is drawn from the profile mean. Shipments whose arrival falls after `reference_end` stay `IN_TRANSIT` with no actual delivery.
  5. Events are BOOKED, DISPATCHED, IN_TRANSIT, DELAYED (if late), ARRIVED, and RECEIVED, with `recorded_timestamp = event + lag`, where lag is 0–2 h normally and 1–10 days for about 5% of events. About 1% of shipments get a CORRECTION event. `received_quantity = shipped − (loss draw, usually 0)`.
  6. Statuses: shipment status is derived from its events; PO status is derived from its shipments.

- [ ] **Step 1:** Write the failing tests (starter): `test_po_and_shipment_counts_exact` (300 / 450), `test_cancelled_pos_have_no_shipments`, `test_line_amount_equals_qty_times_price`, `test_shipped_never_exceeds_ordered_per_po_line`, `test_allocations_reconcile_received_le_shipped_le_allocated`, `test_allocation_uom_matches_po_line`, `test_shipment_totals_equal_sum_of_lines`, `test_corrections_reference_earlier_event_of_same_shipment`, `test_recovering_profile_late_rate_decreases`, `test_event_sequence_follows_transitions` (using `DELIVERY_EVENT_TRANSITIONS`), `test_dispatch_site_belongs_to_supplier_and_is_effective`, `test_deteriorating_profile_late_rate_increases` (full-period late rate in 2026H1 > 2025H1 for deteriorating suppliers in aggregate), `test_some_partial_deliveries_exist`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: generate purchase orders, shipments and delivery events`.

### Task 8: Generator — inspections, invoices, risk signals; loader; deterministic export

**Files:**
- Create: `src/supplier_reliability/services/generator/{quality_finance,risk,loader}.py`, `src/supplier_reliability/services/export.py`, `tests/unit/test_generator_quality_finance.py`, `tests/integration/test_load_and_export.py`
- Modify: `services/generator/dataset.py` (add `generate_dataset(cfg) -> GeneratedDataset`, which runs master → orders → quality/finance → risk in §7.4 order)

**Interfaces:**
- Produces:
  - `generate_inspections_and_invoices(cfg, rs, ids, ds) -> None`. The eligible inspection targets are shipment_lines of DELIVERED / PARTIALLY_RECEIVED shipments. Choose exactly `n_inspections` inspections, about 3% of them REINSPECTION of an already-inspected line, and raise `GeneratorCapacityError` if there are not enough targets. Defects are drawn from the profile defect mean (binomial on inspected qty). About 20% of inspections are SAMPLE type. `inspection_timestamp = arrival + 0–3 days`. Invoices follow D3, with variance drawn by the profile's p/mag (over- and under-billing). About 3% of delivered shipments near the end are uninvoiced.
  - `generate_risk_signals(cfg, rs, ids, ds) -> None`. Each supplier gets a Poisson(profile rate × years) count. About 70% of suppliers get ≥ 1 quarterly `MONITORING_ATTESTATION`. About 5% of signals get a duplicate (same type/supplier/site, observed within 2 days, a different `SYNTHETIC_` source). The `dedup_key` is computed per D6.
  - `load_dataset(session: Session, ds: GeneratedDataset) -> dict[str, int]`. Inserts go in §7.4 order with `session.execute(insert(Model), rows)` in chunks of 5,000, inside one transaction. It returns row counts and refuses to run if `suppliers` is non-empty (`DatasetAlreadyLoadedError`).
  - `export_tables(session: Session, out_dir: Path, tables: Sequence[str]) -> dict[str, FileEntry]` with `FileEntry(path: str, rows: int, sha256: str)`. Output is CSV and JSONL per table, UTF-8 without BOM, `\n` newlines, columns in model order, rows ordered by PK. NULL is an empty string in CSV and `null` in JSONL. Decimals are plain fixed-point strings and timestamps are `YYYY-MM-DDTHH:MM:SS.ffffffZ`. JSON columns are canonical JSON (sorted keys, no spaces).
  - `build_manifest(cfg, entries, counts, execution_meta) -> dict`. The manifest's `content_checksum` is sha256 over the sorted `(path, sha256)` pairs only. Volatile fields (`generated_at`, `duration_seconds`, `python_version`, `dependency_versions`, `hostname`) sit under `execution_metadata` and are excluded from the checksum. It also records `provenance: {"synthetic": true, "license": "MIT (synthetic data generated by this repository)"}`.

- [ ] **Step 1:** Write the failing tests: `test_inspection_count_exact` (600), `test_inspection_quantities_reconcile`, `test_reinspections_reference_already_inspected_lines`, `test_invoice_expected_equals_received_times_price`, `test_invoice_variance_derived`, `test_risk_signals_are_synthetic` (all sources start with `SYNTHETIC_`, refs with `synthetic://`), `test_load_counts_match_dataset`, `test_load_twice_refused`, and **`test_export_uses_lf_and_canonical_formats`** (no `\r` bytes, money like `1234.50`, timestamps end with `Z`, NULL is an empty cell), `test_manifest_checksum_ignores_execution_metadata`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: generate inspections, invoices and risk signals; deterministic load and export`.

### Task 9: Scoring evidence collection (eligibility + no join multiplication)

**Files:**
- Create: `src/supplier_reliability/services/scoring/{__init__,evidence}.py`, `tests/scoring/test_evidence.py`, `tests/scoring/factories.py` (tiny helpers that insert minimal valid graphs into a migrated temp DB)

**Interfaces:**
- Produces:
  - `EvaluationWindow(start: datetime, end: datetime, cutoff: datetime)` (frozen; validates `cutoff >= start`, aware UTC). `quarter_windows(reference_start, reference_end) -> list[EvaluationWindow]`.
  - Evidence dataclasses (all frozen, holding plain values + record ids):
    - `DeliveryObs(shipment_id, expected, arrived_known: datetime | None)`
    - `InspectionObs(inspection_id, shipment_line_id, inspected, defect)`, already reduced to the latest eligible per line
    - `InvoiceObs(invoice_id, expected, invoiced)`
    - `DemandObs(po_line_id, month: date, quantity, unit_of_measure)`
    - `CapacityObs(capacity_declaration_id, site_id | None, month: date, available, committed, verified: bool)`, with the D12 scope rule already applied
    - `ComplianceObs(requirement_id, certification_type, mandatory, scope, site_id | None, certification_id | None, state: ComplianceState)`
    - `ExcludedRecord(source_entity_type, source_record_id, component, reason)`, which feeds `score_evidence` rows with `included=False`
    - `RiskObs(risk_signal_id, site_id, site_active: bool, signal_type, severity, observed, resolution, dedup_key)`
  - `EvidenceBundle(supplier_id, window, deliveries, inspections, invoices, demand, capacities, compliance, risks, excluded: list[ExcludedRecord], open_critical_exception_ids: list[str])`
  - `collect_evidence(session, supplier_ids: Sequence[str] | None, window: EvaluationWindow) -> dict[str, EvidenceBundle]`. It runs one set-based query per evidence type over all target suppliers (no per-supplier queries), applies D5 eligibility in SQL where possible, and always includes every requested supplier, even one with no evidence.

- [ ] **Step 1:** Write the failing tests:
  - `test_shipment_not_yet_due_excluded`
  - `test_due_shipment_without_arrival_counts_overdue`
  - **`test_arrival_recorded_after_cutoff_is_not_known`**: ARRIVED event_timestamp = C − 1d, recorded = C + 1d → `arrived_known is None`
  - `test_inspection_recorded_after_cutoff_excluded`
  - `test_latest_reinspection_replaces_initial`
  - **`test_evidence_counts_do_not_multiply_across_joins`**: 1 shipment, 3 lines, line 1 initial+reinspection, 1 invoice → `len(deliveries)==1`, `len(inspections)==3`, `len(invoices)==1`, and Σ inspected equals the line-level sum
  - `test_future_or_late_recorded_risk_signal_excluded` (observed > C, or recorded > C)
  - `test_risk_signal_older_than_lookback_excluded` (observed < C − 180d)
  - `test_unknown_compliance_when_no_certification`
  - `test_certification_superseded_after_cutoff_still_counts_before`: a COMPLIANT row recorded before C and a NONCOMPLIANT superseding row recorded after C → state at C is COMPLIANT; at C′ after the supersession it is NONCOMPLIANT
  - `test_correction_recorded_before_cutoff_applied_after_ignored`
  - `test_capacity_site_declarations_override_supplier_wide`: site rows 100 + 50 free, supplier-wide 500 → 150 used
  - `test_excluded_records_listed_with_reason` (not due / recorded after cutoff)
  - `test_supplier_with_no_activity_returns_empty_bundle`
- [ ] **Step 2:** Run `uv run pytest tests/scoring/test_evidence.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: cutoff-aware scoring evidence collection`.

### Task 10: Component scoring — delivery, quality, cost, capacity, compliance, disruption

**Files:**
- Create: `src/supplier_reliability/services/scoring/components.py`, `tests/scoring/test_components.py`

**Interfaces:**
- Consumes: `EvidenceBundle` (Task 9), `ScoringConfig` (Task 1).
- Produces:
  - `ComponentResult(name: ComponentName, score: Decimal | None, numerator: Decimal | None, denominator: Decimal | None, observations: int, coverage: Decimal, metrics: dict[str, str | int | None], missing_reason: str | None, source_record_ids: list[str], flags: dict[str, bool])`. The score is already `round_score`-ed. When the score is None, `missing_reason` is non-empty.
  - Six pure functions `score_delivery(ev, cfg)`, `score_quality(ev, cfg)`, `score_cost(ev, cfg)`, `score_capacity(ev, cfg)`, `score_compliance(ev, cfg)`, `score_disruption(ev, cfg)`, each `-> ComponentResult`, implementing D6 exactly. Delivery metrics include `on_time_rate`, `avg_delay_days` (all due), `late_frequency`, `eligible_count`, `coverage` (= known outcomes / due). Compliance flags `mandatory_compliance_failure`. Disruption exposes `deduplicated_signal_ids` and `penalty`.

- [ ] **Step 1:** Write the failing tests with exact expected values:
  - Delivery: 10 due, 8 on time, 2 late by 3 and 5 days → otr 0.8, penalty `min(20, 2×4)=8` → score `72.00`. The same input with `delay_penalty_per_day=0` gives `80.00`, which is the Add-B(1) formula. 4 due → None with "insufficient deliveries: 4 < 5". Arrival exactly at expected+24h → on time; +24h+1s → late. 10 late by 30 days → otr 0, penalty capped at 20 → clamped `0.00`.
  - Quality: inspected 1000, defects 20 → rate 0.02 → `80.00`. Defects 150/1000 → `0.00`. Zero inspected units → None (missing, not 100). Zero defects with 3 inspections → `100.00`.
  - Cost: expected 100, 100, 100 and invoiced 105, 95, 100 → wavp = (5 + 0.5×5)/300 = 0.025 → `75.00`. An invoice with expected 0 is excluded and counted in metrics `excluded_zero_expected=1`. Fewer than 3 usable → None.
  - Capacity: util 0.80 → 100; 0.90 → `85.00`; 1.00 → 70; 1.25 → `35.00`; 1.50 → 0; 1.60 → 0. Max-month rule: months at 0.5 and 1.25 → `35.00`. Free capacity 0 with demand > 0 → 0. Only UNVERIFIED declarations → None with "capacity unverified". No declarations → None with "capacity unknown".
  - Compliance: [COMPLIANT, PENDING, UNKNOWN] → `75.00`, coverage `0.6667`. [COMPLIANT, EXPIRED] → `50.00`. Mandatory EXPIRED → flag True. Mandatory REJECTED → NONCOMPLIANT + flag True. Non-mandatory NONCOMPLIANT → flag False. All UNKNOWN → None.
  - **Invalid evidence (Add-D), one test per component.** A component function given invalid evidence must raise `InvalidEvidenceError`, naming the record, rather than score it. Cases: delivery with `arrived < expected − 365d`; quality with `defect > inspected`; cost with negative `expected`; capacity with `committed > available` on a VERIFIED declaration; compliance with `valid_to < valid_from`; disruption with severity `NONE` on a non-attestation signal. In normal flow these never reach scoring because validation and DB CHECKs stop them; the tests prove the engine refuses rather than silently scoring.
  - Disruption: one HIGH open signal age 0 → `70.00`. The same signal resolved → `85.00`. Two duplicates with the same dedup_key → counted once. Attestation only → `100.00`. No signals → None with "no risk monitoring evidence".
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: six explainable scoring components`.

### Task 11: Aggregation, categories, decisions, explanation

**Files:**
- Create: `src/supplier_reliability/services/scoring/{aggregate,engine}.py`, `tests/scoring/test_aggregate.py`

**Interfaces:**
- Produces:
  - `OverallResult(overall: Decimal | None, effective_weights: dict[str, Decimal], evidence_coverage: Decimal, is_provisional: bool, category: ReliabilityCategory, decision: Decision, decision_rationale: list[str])`
  - `categorize(score: Decimal, thresholds) -> ReliabilityCategory`
  - `aggregate(components: list[ComponentResult], cfg: ScoringConfig, open_critical_exceptions: list[str]) -> OverallResult` (D7)
  - `ScoreResult(supplier_id, window, components: dict[str, ComponentResult], overall: OverallResult, explanation: dict, evidence_fingerprint: str)`
  - `score_supplier(bundle: EvidenceBundle, cfg: ScoringConfig) -> ScoreResult`. It is pure, needs no DB, and can be called directly from Python. `explanation` keys: `overall_score`, `components{name:{score,weight,effective_weight,numerator,denominator,observations,coverage,metrics,missing_reason,source_record_ids}}`, `evidence_coverage`, `missing_data_reasons`, `risk_signals`, `detected_exceptions`, `scoring_version`, `evaluation_period{start,end}`, `data_cutoff`, `decision`, `decision_rationale`, `is_provisional`, `formula`. `source_record_ids` lists are capped at 200 per component with a `truncated_count`; the fingerprint always covers all ids.

- [ ] **Step 1:** Write the failing tests:
  - `test_full_weighted_sum`: components 80, 90, 70, 100, 100, 60 → 0.3×80 + 0.25×90 + 0.15×70 + 0.1×100 + 0.1×100 + 0.1×60 = `83.00`, ACCEPTABLE, APPROVED
  - `test_category_boundaries`: 85.00→HIGH, 84.99→ACCEPTABLE, 70.00→ACCEPTABLE, 69.99→AT_RISK, 50.00→AT_RISK, 49.99→CRITICAL, 100→HIGH, 0→CRITICAL
  - `test_renormalized_when_capacity_missing`: coverage 0.90, provisional True, effective delivery weight `0.3333`
  - `test_insufficient_when_delivery_missing`
  - `test_insufficient_when_coverage_below_threshold`
  - `test_compliance_failure_overrides_high_score`: SUSPEND_PENDING_REVIEW
  - `test_open_critical_exception_holds`
  - `test_missing_component_never_zero_or_hundred`: the explanation shows `score: null` plus its reason
  - `test_score_supplier_deterministic_fingerprint`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: overall scoring with missing-data policy, decision precedence and explanations`.

### Task 12: Scoring service — persistence, idempotency, versioning, batch historical scoring

**Files:**
- Create: `src/supplier_reliability/services/scoring/service.py`, `src/supplier_reliability/repositories/scores.py`, `tests/integration/test_scoring_service.py`

**Interfaces:**
- Consumes: Tasks 9–11.
- Produces:
  - `CalculationRequest(window: EvaluationWindow, supplier_ids: list[str] | None, scoring_version: str | None)`
  - `CalculationOutcome(created: list[str], existing: list[str], unchanged: list[str], superseded: dict[str, str])`
  - `ScoringService(session: Session, cfg: ScoringConfig, clock: Callable[[], datetime])`, with:
    - `.calculate(req) -> CalculationOutcome`
    - `.recalculate(req, reason: str) -> CalculationOutcome`
    - `.score_history(windows: list[EvaluationWindow]) -> CalculationOutcome` (pipeline helper; it uses `clock = lambda: window.cutoff + 1 day`)
  - It follows D8 and D13, and uses one transaction per call: the score rows, their `score_evidence` rows and the `ScoringConfigSnapshot` upsert are written together. Score IDs are allocated from `max(existing)+1`.
  - `.restate(window_with_later_cutoff, supplier_ids, reason) -> CalculationOutcome` creates rows with `restates_score_id` and `explanation.differences_from_restated`.
  - `rebuild_from_lineage(session, score_id) -> ScoreResult` recomputes a score using **only** its `score_evidence` rows (loading those source records by id) plus its `ScoringConfigSnapshot`.
  - `calculation_status`: INSUFFICIENT_EVIDENCE when the category is insufficient, PROVISIONAL when provisional, else CALCULATED.
  - Repository functions:
    - `get_score(session, score_id)`
    - `list_scores(session, filters: ScoreFilters, limit, offset, sort) -> tuple[list[SupplierScore], int]`
    - `current_scores_for_supplier(session, supplier_id)`
    - `latest_current_score(session, supplier_id)`

- [ ] **Step 1:** Write the failing tests (seeded starter dataset loaded into a temp DB via a session-scoped fixture `starter_db`; the fixture generates in < 30 s):
  - `test_calculate_persists_one_score_per_supplier`
  - `test_calculate_twice_is_idempotent`: second outcome `created == []`, `len(existing) == 50`, row count unchanged
  - `test_recalculate_unchanged_evidence_no_new_version`
  - `test_recalculate_after_new_evidence_creates_version_2`: insert a late ARRIVED for one supplier, then the old row has `is_current=False` and `superseded_by` set, the new row has `version_number=2`, and the old row's scores are untouched
  - `test_historical_score_ignores_later_evidence`: adding a shipment with expected date after C does not change the fingerprint
  - **`test_supplier_without_activity_scores_insufficient`**
  - `test_score_ids_follow_format`
  - `test_every_score_has_lineage_rows_and_config_snapshot`
  - `test_rebuild_from_lineage_matches_stored_components`, for every starter score
  - `test_late_arriving_event_does_not_change_historical_score`: insert an ARRIVED with event ≤ C and recorded > C; recalculate → `changed=False`
  - `test_restatement_with_later_cutoff_links_and_reports_differences`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: idempotent, versioned score persistence with lineage and restatements`.

### Task 12b: Scoring benchmark scenarios (Add-D)

**Files:**
- Create: `tests/scoring/benchmarks/{__init__,builders,scenarios}.py`, `tests/scoring/benchmarks/expected.yaml`, `tests/scoring/benchmarks/test_benchmarks.py`

**Interfaces:**
- Consumes: Task 9 factories, `ScoringService` (Task 12).
- Produces (test-only):
  - `build_supplier_history(session, supplier_id, quarters: list[QuarterSpec]) -> None`. It inserts real source records (POs, allocations, events, inspections, invoices, capacity declarations, certifications, risk signals) into a migrated temp DB from a `QuarterSpec(on_time, late_days, inspected, defects, invoice_pairs: list[tuple[expected, invoiced]], monthly_demand, free_capacity, cert_states, signals)`.
  - `expected.yaml` holds **hand-calculated** component and overall scores per supplier per quarter, with the arithmetic written out as a comment on each line.
- Scenarios (4 quarters each, Q1–Q4 2025):
  - **stable**: every quarter 20 deliveries / 19 on time / 1 late by 2d; 2,000 inspected / 10 defects; invoices exact; util 0.6; all COMPLIANT; attestation only. Expected each quarter: delivery `95 − min(20, 2×2) = 91.00`, quality `100×(1−0.005/0.10) = 95.00`, cost `100.00`, capacity `100.00`, compliance `100.00`, disruption `100.00`, overall `27.30 + 23.75 + 15.00 + 30.00 = 96.05` → HIGH / PREFERRED.
  - **deteriorating**: on-time 19→16→13→10 of 20 (late 3→5→8→10 days), defects 10→40→80→120 per 2,000, invoice overbilling 0→2→4→6%, util 0.7→0.9→1.1→1.3, a mandatory cert EXPIRED in Q4, and a HIGH open signal in Q3. Expected: Q3→Q4 category drops, Q4 decision `SUSPEND_PENDING_REVIEW`, and the supplier appears in `deteriorating_suppliers`.
  - **recovering**: the mirror of deteriorating (Q1 worst, Q4 best; the signal resolved in Q3). Expected: overall strictly increasing, and the Q4 category ≥ ACCEPTABLE.
- Fill every expected value in `expected.yaml` by hand when writing the test. Never paste them from engine output.

- [ ] **Step 1:** Write the failing tests:
  - `test_benchmark_scores_match_hand_calculation[stable|deteriorating|recovering]`: each component and overall per quarter equals `expected.yaml` (±0.00)
  - `test_one_factor_changes_only_its_component`: parametrized over the six factors. Starting from the stable Q1 evidence, perturb only that factor (+5 late days / +1% defects / +3% overbilling / util 0.6→1.2 / one mandatory cert → EXPIRED / add a HIGH open signal). Assert that only the matching component changes, by the hand-computed delta, and that overall changes by `weight × delta`.
  - `test_identical_evidence_and_config_identical_results`: two separate DBs built from the same spec give equal components, overall and fingerprint
  - `test_evidence_outside_window_cannot_affect_score`: add records dated before S, after E, and recorded after C → same fingerprint and scores
  - (`test_independent_metrics_agree_on_benchmarks` is added in Task 16, once the independent implementation exists.)
- [ ] **Step 2:** Run `uv run pytest tests/scoring/benchmarks -v`. Expected: FAIL (builders missing).
- [ ] **Step 3:** Implement the builders. If a hand calculation and the engine disagree, re-derive by hand from D6/D7 before changing either side, and record which one was wrong in the commit message.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `test: deterministic stable/deteriorating/recovering scoring benchmarks`.

### Task 13: Validation framework and canonical rules

**Files:**
- Create: `src/supplier_reliability/services/validation/{__init__,framework,rules_integrity,rules_domain,rules_chronology,rules_arithmetic,rules_semantic}.py`, `src/supplier_reliability/repositories/validation.py`, `tests/data_quality/test_rules.py`, `tests/data_quality/test_baseline_clean.py`

**Interfaces:**
- Produces:
  - `Finding(rule_id, severity: Severity, entity_type, entity_id, field, observed_value, expected_condition, message, detection_timestamp)`
  - `Rule(rule_id, category, severity, description, check: Callable[[Session], Iterable[Finding]])`, registered with the `@rule(...)` decorator into `REGISTRY: dict[str, Rule]`
  - `run_validation(session, rule_ids: list[str] | None = None, persist: bool = True, clock=...) -> ValidationRunResult(run_id, counts_by_rule, counts_by_severity, findings)`. It persists `ValidationRun` + `ValidationFinding` rows in one transaction. `is_expected_anomaly = True` iff the finding's entity id matches a `data_exceptions.source_record_id` (i.e. an already-quarantined raw record). This marker comes from detection output, never from ground truth, and it is what separates expected injected anomalies from unexpected corruption.
  - Each rule is a set-based SQL query; there are no Python loops over all rows. Rules (IDs are fixed and documented in `docs/business_rules.md`):
    - Integrity: `RI-001` PK uniqueness per table (via GROUP BY on export-level keys), `RI-002` orphan FKs (defence in depth), `RI-003` dispatch site belongs to PO supplier, `RI-004` site effective at dispatch, `RI-005` PO contract belongs to PO supplier, `RI-006` invoice supplier/contract/PO consistency, `RI-007` every non-cancelled PO has ≥ 1 shipment, `RI-008` a SITE-scoped requirement/certification/capacity declaration references a site of the same supplier, `RI-009` an allocation's PO line belongs to the shipment's PO
    - Domain: `DC-001` required fields non-null, `DC-002` enum values, `DC-003` nonnegative quantities, `DC-004` money has ≤ 2 dp, `DC-005` PO order_date within contract effective period, `DC-006` currency = contract currency, `DC-007` invoiced ≥ 0, `DC-008` allocation/demand UoM = PO line UoM = capacity unit, `DC-009` monthly demand > contracted_capacity (INFO), `DC-010` VERIFIED capacity declaration with committed > available
    - Chronology: `CH-001` required_delivery ≥ order_date, `CH-002` dispatch ≥ order_date, `CH-003` ARRIVED ≥ DISPATCHED per shipment, `CH-004` inspection ≥ arrival, `CH-005` score evidence recorded ≤ cutoff (recompute fingerprint check), `CH-006` risk signal observed ≤ recorded, `CH-007` event transition order valid
    - Arithmetic: `AR-001` line_amount, `AR-002` shipment totals = Σ lines, `AR-003` Σ allocated per PO line ≤ ordered and received ≤ shipped ≤ allocated, `AR-004` inspection reconciliation incl. inspected ≤ allocation received, `AR-005` invoice expected/variance recompute, `AR-006` overall score = weighted recompute from stored components & effective_weights (±0.01), `AR-007` each score's `score_evidence` included-record count equals the explanation's observation counts
    - Semantic: `SE-001` duplicate events (same shipment/type/timestamp, non-CORRECTION), `SE-002` duplicate supplier invoice number, `SE-003` repeated risk signals by dedup_key (INFO; expected, handled by scoring), `SE-004` join multiplication (Σ invoice expected per PO vs Σ received × price per PO computed independently), `SE-005` suppliers with no evidence in latest window (INFO)

- [ ] **Step 1:** Write the failing tests. In `tests/data_quality/test_rules.py`, each test builds a minimal valid graph, corrupts exactly one thing in a way the DB constraints allow, and asserts the rule fires on that entity id. Cases: wrong-supplier dispatch site (RI-003), duplicate event (SE-001), invoice with wrong variance (AR-005), overall score tampered (AR-006), PO outside contract (DC-005). Two cases the DB CHECKs already block are tested through the payload validator instead: `validate_payload("INSPECTION", {...accepted+rejected≠inspected})` yields AR-004, and `validate_payload("SHIPMENT", {...status:"BOGUS"})` yields DC-002. In `tests/data_quality/test_baseline_clean.py`, assert `run_validation` on `starter_db` has zero findings with severity ≥ MEDIUM.
- [ ] **Step 2:** Run `uv run pytest tests/data_quality -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Also expose `validate_payload(entity_type, payload) -> list[Finding]` (Pydantic-based) for raw records that cannot reach canonical tables.
- [ ] **Step 4:** Run the tests. Expected: PASS, and the baseline has 0 MEDIUM+ findings. Commit `feat: reusable validation framework with integrity, domain, chronology, arithmetic and semantic rules`.

### Task 14: Anomaly injection and ground truth

**Files:**
- Create: `src/supplier_reliability/services/anomalies/{__init__,injector}.py`, `tests/integration/test_injection.py`

**Interfaces:**
- Consumes: canonical DB (read-only), `ScoringService`/`score_supplier` (to build the leaky and valid score payloads), `RandomSource` stream `"anomalies"`.
- Produces:
  - `GroundTruthCase(case_id, entity_type, entity_id, issue_type, expected_detection: bool, expected_decision: Disposition, severity, rationale, injection_version: str)`, where `entity_id` is the raw_record_id
  - `inject_anomalies(session, cfg: GeneratorConfig, rs: RandomSource) -> InjectionResult(batch_id, raw_counts: dict[str,int], cases: list[GroundTruthCase])`. It writes raw records (positives + negatives per D9) in one transaction and **never** writes canonical tables.
  - `write_ground_truth(cases, profiles: dict[str,str], out_dir: Path) -> dict[str, FileEntry]` writes `anomaly_ground_truth.csv` and `supplier_profiles.csv`.

- [ ] **Step 1:** Write the failing tests:
  - `test_injection_counts_per_class`: starter preset, 5 per class
  - `test_canonical_tables_unchanged_by_injection`: row counts + content checksum before/after
  - `test_missing_po_ids_absent_from_canonical`
  - `test_site_conflation_fks_individually_valid`: both the site and the PO exist; the site's supplier ≠ the PO's supplier, or the site is not effective
  - `test_leaky_score_cites_post_cutoff_evidence`
  - `test_ground_truth_not_importable_from_scoring`: walk `supplier_reliability.services.scoring` modules with `ast` and assert no import of `anomalies`, `evaluation`, `ground_truth`, and no string literal `"ground_truth"`
  - `test_suppliers_table_has_no_profile_column`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: inject anomalies A-E into raw ingestion layer with separate ground truth`.

### Task 15: Anomaly detection, quarantine, and leakage correction

**Files:**
- Create: `src/supplier_reliability/services/anomaly_detection/{__init__,detectors}.py`, `src/supplier_reliability/repositories/exceptions.py`, `tests/integration/test_detection.py`

**Interfaces:**
- Consumes: raw records, canonical tables, `validate_payload` (Task 13), `ScoringService` (Task 12). It **must not** read ground-truth files or `GroundTruthCase`.
- Produces:
  - `detect_raw_batch(session, batch_id: str, scoring: ScoringService, clock) -> DetectionResult(exceptions: list[str], by_issue_type: dict[str,int], corrected_scores: list[str])`
  - Five detectors, each `(session, raw_records) -> list[DetectionHit]`:
    - `detect_missing_parent` (A): the payload `po_id` is not in canonical POs
    - `detect_chronology` (B): the payload ARRIVED is earlier than the canonical DISPATCHED for that shipment; the hit records `related_entity_ids=[shipment_id]`
    - `detect_score_leakage` (C): any cited evidence record whose **canonical** occurrence or recording time > payload cutoff. It then recomputes with `ScoringService.calculate` for that supplier/window, stores the result with `calculation_status=CORRECTED`, and puts its id in `corrected_score_id`
    - `detect_missing_provenance` (D): blank source or null observed/recorded timestamp; disposition `QUARANTINED_PENDING_PROVENANCE`
    - `detect_site_conflation` (E): the site's supplier ≠ the PO's supplier, or the site is not effective at dispatch
  - Each hit becomes a `DataException` (severity: A HIGH, B HIGH, C CRITICAL, D MEDIUM, E HIGH; disposition QUARANTINED unless stated above). The detected supplier is linked when it can be resolved. Detection is idempotent per `(batch_id, raw_record_id, issue_type)`.
  - Repository: `list_exceptions(session, filters: ExceptionFilters, limit, offset)`, `get_exception(session, exception_id)`.

- [ ] **Step 1:** Write the failing tests:
  - `test_each_class_detected_on_starter_batch`: count per issue type == 5
  - `test_valid_raw_negatives_not_flagged`
  - `test_quarantined_signal_not_used_in_scoring`: the supplier's disruption evidence is unchanged
  - `test_leakage_correction_uses_only_eligible_evidence`: corrected fingerprint == fingerprint of a fresh calculation at the same cutoff, and ≠ the leaky payload fingerprint
  - `test_detection_idempotent`
  - `test_detector_module_does_not_import_ground_truth` (AST check as in Task 14)
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: detect and quarantine anomalies A-E with corrected leakage scores`.

### Task 16: Evaluation — anomaly metrics, independent metric recomputation, score reconciliation

**Files:**
- Create: `src/supplier_reliability/services/evaluation/{__init__,anomaly_eval,independent_metrics,reconciliation}.py`, `tests/unit/test_anomaly_eval.py`, `tests/integration/test_reconciliation.py`

**Interfaces:**
- Produces:
  - `ConfusionCounts(tp, fp, fn, tn)` with `.precision`, `.recall`, `.f1`, each returning `float | None` (None on zero denominators, never 0/0 → 1).
  - `evaluate_anomalies(session, batch_id, ground_truth_path: Path) -> AnomalyEvaluation(per_class: dict[IssueType, ConfusionCounts], micro: ConfusionCounts, unit="raw_record_per_issue_class", negative_population: dict[IssueType, int])` (D9 population)
  - `independent_supplier_metrics(export_dir: Path, window: EvaluationWindow, scoring_params: dict) -> pandas.DataFrame`. It recomputes all six raw metrics and component scores **from the exported CSVs with pandas** (incl. `capacity_declarations`, `compliance_requirements`, `supplier_certifications` and correction events), applying D5/D11/D12/D13 independently. The export list in Task 8 therefore includes every table. It is a deliberately separate implementation that imports nothing from `services/scoring` except config values.
  - `reconcile_scores(session, export_dir, windows, tolerance=Decimal("0.01")) -> ReconciliationReport(checked, matched, mismatches: list[dict], component_mismatch_counts)`. It compares the persisted current scores' component metrics and the overall recompute against `independent_supplier_metrics`.
  - `profile_alignment(session, profiles_path) -> dict` gives the category distribution per generator profile for the latest window. It is labelled a **sanity check, not accuracy**.

- [ ] **Step 1:** Write the failing tests: `test_confusion_metrics_math` (tp=8, fp=2, fn=2 → precision 0.8, recall 0.8, f1 0.8), `test_zero_denominators_return_none`, `test_negatives_scoped_to_entity_type` (risk-signal raw rows are not negatives for class A), `test_evaluation_on_starter_batch` (all five classes evaluated, `tp == 5` each), `test_independent_metrics_reconcile_with_engine_on_starter` (mismatches == []), `test_independent_metrics_agree_on_benchmarks` (export the Task 12b benchmark DB; the raw metrics and component scores equal the hand-calculated `tests/scoring/benchmarks/expected.yaml`), `test_reconciliation_detects_tampered_score` (UPDATE one stored delivery_score by +5 in a temp DB → exactly 1 mismatch reported).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: anomaly evaluation and independent score reconciliation`.

### Task 17: Reporting

**Files:**
- Create: `src/supplier_reliability/services/reporting/{__init__,reports,writers}.py`, `tests/integration/test_reports.py`

**Interfaces:**
- Produces: builders, each `(session, ...) -> dict`, where every metric is a dict `{value, numerator, denominator, filters, window, missing_data_handling}`:
  - `entity_counts`
  - `supplier_rankings(window=latest, limit)`: ordered by overall desc, then supplier_id; insufficient listed separately
  - `score_distribution` (per category per window)
  - `delivery_quality_trends` (per quarter: on-time rate, defect rate)
  - `invoice_variance_distribution` (buckets: <−5%, −5..−1, −1..1, 1..5, >5%, zero-expected excluded)
  - `capacity_risk` (suppliers with util > 1.0)
  - `compliance_exceptions`
  - `disruption_exposure`
  - `missing_data_rates` (per component, share of supplier-windows missing)
  - `deteriorating_suppliers` (D7)
  - `data_quality(validation_run_id)`
  - `anomaly_evaluation_report`, `reconciliation_report`
  - `summary(...)` combining headline numbers
  - `write_report(name: str, payload: dict, reports_dir: Path) -> tuple[Path, Path]` writes `<name>.json` (sorted keys, indent 2, LF) and `<name>.md` (rendered tables).

- [ ] **Step 1:** Write the failing tests on `starter_db` (after scoring + injection + detection): `test_entity_counts_match_db`, `test_rankings_sorted_and_exclude_insufficient`, `test_every_metric_has_numerator_and_denominator` (walk the payload), `test_variance_buckets_sum_to_invoice_count_minus_excluded`, `test_writer_produces_json_and_markdown`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: JSON and Markdown reporting from persisted outputs`.

### Task 18: Pipeline orchestration + Typer CLI + scripts

**Files:**
- Create: `src/supplier_reliability/services/pipeline.py`, `src/supplier_reliability/cli.py`, `scripts/{generate_data,validate_data,verify_reproducibility}.py`, `tests/integration/test_pipeline_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `run_pipeline(settings: Settings, app_config: AppConfig, preset: str, seed: int, replace_existing: bool, on_progress: Callable[[str], None] | None = None) -> PipelineResult(dataset_id, counts, manifest_path, validation_run_id, batch_id, evaluation, report_paths)`
  - Steps: migrate → (if data exists: refuse unless `replace_existing`, then drop rows of all tables in reverse FK order inside one transaction) → generate → load → score 6 quarterly windows → export canonical tables to `data/generated/<preset>/` → inject anomalies → detect → validate → write ground truth to `data/ground_truth/<preset>/` → evaluate → reports to `reports/<preset>/` → manifest to `data/manifests/<dataset_id>.json` + `DatasetManifest` row.
  - CLI commands (Typer app `srp`): `init-db`, `generate --preset starter|full --seed 20261022 [--replace]` (runs the full pipeline), `score --start --end [--cutoff] [--supplier ...]`, `recalculate ... --reason`, `validate`, `detect`, `evaluate`, `report`, `verify-reproducibility --preset --seed`, `serve --host 127.0.0.1 --port 8000`. Each prints a short JSON summary to stdout and exits non-zero on failure.

- [ ] **Step 1:** Write the failing tests: use `typer.testing.CliRunner` with `SRP_*` env pointing at tmp_path, then `test_cli_generate_starter_end_to_end` (exit 0, manifest exists, counts 50/300/450/600, reports written), `test_cli_generate_refuses_without_replace` (second run exit code 2 with "dataset already loaded"), `test_cli_score_idempotent`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement. The scripts call the matching CLI functions (`if __name__ == "__main__": app(["generate", *sys.argv[1:]])`-style wrappers).
- [ ] **Step 4:** Run the tests. Expected: PASS. Run `uv run srp generate --preset starter` manually and inspect `reports/starter/summary.md`. Commit `feat: end-to-end pipeline and Typer CLI`.

### Task 19: API foundation — errors, dependencies, schemas, health/ready/metadata, suppliers, POs, shipments

**Files:**
- Create: `src/supplier_reliability/api/{__init__,errors,dependencies}.py`, `src/supplier_reliability/api/routes/{suppliers,transactions}.py`, `src/supplier_reliability/schemas/{common,suppliers,transactions}.py`, `src/supplier_reliability/repositories/{suppliers,transactions}.py`
- Modify: `api/routes/health.py`, `main.py`
- Test: `tests/api/conftest.py` (session-scoped `api_client` over a migrated temp DB with the starter pipeline already run), `tests/api/test_suppliers_transactions.py`

**Interfaces:**
- Produces:
  - Error envelope `{"error": {"code": str, "message": str, "details": list | None}}`. The `code` is one of `not_found` (404), `conflict` (409), `validation_error` (422), or `internal_error` (500, with a generic message and the stack trace only in server logs). `NotFoundError(resource, id)` and `ConflictError(message)` are domain exceptions mapped by handlers. FastAPI's `RequestValidationError` is re-shaped into the envelope.
  - `Page[T]` = `{items: list[T], total: int, limit: int, offset: int}`. `pagination(limit: int = Query(50, ge=1, le=settings.max_page_size), offset: int = Query(0, ge=0))`. Sort params come from a whitelist (`sort=overall_score:desc` style); an unknown field → 422.
  - Dependencies: `get_settings`, `get_db` (yields a session from `app.state.session_factory`), `get_app_config`.
  - Request-size limit: middleware rejects bodies > 1 MB with 413.
  - CORS is enabled only when `settings.cors_origins` is non-empty.
  - Routes:
    - `GET /health` → `{"status":"ok"}` (no DB)
    - `GET /ready` → 200 `{"status":"ready","database":"ok","schema_version":...}`, or 503 `{"status":"not_ready","database":"unavailable"}`. It runs `SELECT 1` and checks the alembic head.
    - `GET /api/v1/metadata` → app/generator/scoring/schema versions, current dataset_id, config_hash, weights. No filesystem paths.
    - `GET /api/v1/suppliers` with filters `region, category, status, reliability_category, q` and sort `supplier_id|supplier_name|overall_score`. The latest current score is joined in one query.
    - `GET /api/v1/suppliers/{id}`, which includes sites and contracts via selectinload
    - `/performance`: per-quarter component metrics from current scores
    - `/scores`: version history, all versions
    - `/explanation`: the latest current score's explanation
    - `GET /api/v1/purchase-orders` with filters `supplier_id, status, order_date_from, order_date_to`, and `/{po_id}` including lines and shipments
    - `GET /api/v1/shipments` with filters `supplier_id, site_id, status, late_only, dispatch_from, dispatch_to`, then `/{id}` and `/{id}/events` ordered by event_timestamp, delivery_event_id

- [ ] **Step 1:** Write the failing tests: `test_health_and_ready`, `test_ready_503_when_db_unreachable` (bad URL app), `test_list_suppliers_paginated` (limit=10 → 10 items, total=50), `test_limit_above_max_is_422`, `test_offset_past_end_returns_empty_page`, `test_unknown_sort_field_422`, `test_supplier_404_envelope`, `test_supplier_explanation_has_components_and_cutoff`, `test_filter_shipments_by_site_and_date`, `test_shipment_events_ordered`, `test_naive_datetime_query_param_422`, `test_metadata_has_no_paths` (no `:\\` or leading `/` path strings in values).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: API foundation with health, suppliers, purchase orders and shipments`.

### Task 20: API — scores, validation, exceptions

**Files:**
- Create: `src/supplier_reliability/api/routes/{scores,validation}.py`, `src/supplier_reliability/schemas/{scores,validation}.py`, `tests/api/test_scores_validation.py`

**Interfaces:**
- Consumes: `ScoringService` (Task 12), `run_validation` (Task 13), exception repository (Task 15).
- Produces:
  - `GET /api/v1/scores`: filters `supplier_id, reliability_category, calculation_status, evaluation_start, evaluation_end, current_only=true`
  - `GET /api/v1/scores/{score_id}`
  - `POST /api/v1/scores/calculate`, body `{evaluation_start, evaluation_end, data_cutoff?, supplier_ids?: list[str] (max 1000), scoring_version?}`. It returns 201 when anything was created, else 200, with body = `CalculationOutcome`. It returns 422 when end < start, cutoff < start, or the datetimes are naive, and 404 for an unknown supplier id. A `scoring_version` that differs from the configured one → 409 ("scoring version not available").
  - `POST /api/v1/scores/recalculate`: same body + required `reason` (1–500 chars) → 200 with the outcome
  - `POST /api/v1/validation/run`, body `{rule_ids?: list[str]}` → 201 with the run summary. An unknown rule id → 422.
  - `GET /api/v1/validation/results?run_id=&severity=&rule_id=` (defaults to the latest run)
  - `GET /api/v1/exceptions` (filters `severity, issue_type, supplier_id, disposition`), `GET /api/v1/exceptions/{id}`
  - OpenAPI examples on every request model (`model_config = {"json_schema_extra": {"examples": [...]}}`).

- [ ] **Step 1:** Write the failing tests: `test_calculate_then_repeat_is_idempotent` (201 then 200, second `created == []`), `test_calculate_invalid_period_422`, `test_calculate_unknown_supplier_404`, `test_recalculate_requires_reason_422`, `test_recalculate_preserves_history` (GET supplier scores shows both versions after the evidence change), `test_filter_scores_by_category`, `test_validation_run_and_results`, `test_exceptions_filter_by_severity`, `test_exception_404`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: score, validation and exception endpoints`.

### Task 21: API — dataset jobs, manifests, reports, evaluation

**Files:**
- Create: `src/supplier_reliability/api/routes/{datasets,reports}.py`, `src/supplier_reliability/schemas/{datasets,reports}.py`, `src/supplier_reliability/repositories/jobs.py`, `src/supplier_reliability/services/jobs.py`, `tests/api/test_datasets_reports.py`

**Interfaces:**
- Produces:
  - `JobRunner` protocol with `submit(job_id: str, fn: Callable[[], None]) -> None`. The default `BackgroundTaskRunner` uses FastAPI `BackgroundTasks`. Tests inject `InlineRunner`, which runs synchronously. Job state is persisted in `dataset_jobs`. On process restart, jobs left `RUNNING` are marked `FAILED` with "interrupted by restart" at startup. This limitation is documented: it is not a durable distributed queue.
  - `POST /api/v1/datasets/generate`, body `{preset: "starter"|"full", seed?: int (default 20261022), replace_existing: bool = false}` → 202 `{job_id, status:"QUEUED"}`. It returns 409 if a job is QUEUED/RUNNING, and 409 if data exists and `replace_existing` is false.
  - `GET /api/v1/datasets/jobs/{job_id}`: status, timestamps, `error_message` (sanitized), `dataset_id`
  - `GET /api/v1/datasets/manifests/{dataset_id}`: the manifest JSON, with file paths given relative to `data/` only
  - `GET /api/v1/reports/{summary|supplier-rankings|data-quality|anomaly-evaluation}`. These are computed live from the DB via the Task 17 builders; anomaly-evaluation returns 404 if no evaluation has been run yet.
  - `POST /api/v1/evaluation/run` → 200 with the anomaly evaluation + reconciliation summary

- [ ] **Step 1:** Write the failing tests (fresh empty DB fixture for job tests):
  - `test_generate_job_lifecycle`: 202 → GET job SUCCEEDED (InlineRunner) → manifest retrievable
  - **`test_generate_conflict_when_data_exists_without_replace`**: 409
  - **`test_generate_conflict_when_job_running`**: seed a RUNNING job row → 409
  - `test_failed_job_reports_sanitized_error`: inject a runner that raises → FAILED, and the message has no traceback/paths
  - `test_reports_endpoints_return_metrics`
  - `test_anomaly_evaluation_404_before_run`
  - `test_evaluation_run_returns_per_class_metrics`
  - `test_openapi_lists_all_spec_endpoints`: assert each §12.1 path is in `/openapi.json`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite with `uv run pytest -v`. Expected: all PASS. Commit `feat: dataset job, manifest, report and evaluation endpoints`.

### Task 22: Reproducibility workflow

**Files:**
- Create: `src/supplier_reliability/services/reproducibility.py`, `tests/reproducibility/test_reproducibility.py`

**Interfaces:**
- Produces: `verify_reproducibility(app_config, preset: str, seed: int, work_dir: Path) -> ReproReport(run_a: manifest, run_b: manifest, identical_content_checksum: bool, file_diffs: list[{path, sha_a, sha_b, rows_a, rows_b, first_differing_row}], record_diffs_by_table: dict[str,int], compared_bytes: bool=True, execution_metadata)`. It runs `run_pipeline` twice into two separate SQLite DBs and output dirs, compares every exported file (canonical + raw batch + ground truth + exceptions export) byte-by-byte and as normalized records (pandas, sorted by PK), and writes `reports/<preset>/reproducibility.{json,md}`.

- [ ] **Step 1:** Write the failing tests: `test_starter_runs_identical` (checksums equal, no diffs), `test_different_seed_differs` (seed 20261023 → content checksum differs), `test_volatile_metadata_excluded` (manifests differ only under `execution_metadata`).
- [ ] **Step 2:** Run `uv run pytest tests/reproducibility -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Also add exports for `raw_ingestion_records` and `data_exceptions` (with `detected_at` = deterministic batch time in the pipeline) so the quarantine layer is covered.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit `feat: reproducibility verification with byte and record-level comparison`.

### Task 23: Packaging, Docker, documentation

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.env.example` (finalize), `README.md`, `docs/{architecture,data_dictionary,entity_relationships,business_rules,scoring_methodology,anomaly_catalog,api_reference,evaluation_methodology,assumptions_and_limitations}.md`, `tests/unit/test_docs.py`

- [ ] **Step 1:** Write the failing test `test_data_dictionary_covers_every_column`. It parses `docs/data_dictionary.md` tables (`| table | column | type | required | key | allowed values | units | uniqueness | validation | derivation | timestamp semantics | example |`) and asserts that every column of every table in `Base.metadata` appears. Add `test_business_rules_lists_every_registered_rule` (each `REGISTRY` id appears in `docs/business_rules.md`).
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Write the docs. Content comes from the decisions above (D1–D10), the rule registry and the model metadata. `entity_relationships.md` includes a Mermaid ER diagram. `api_reference.md` lists each endpoint, its params, its status codes and an example response, taken from a real call against the starter DB. `scoring_methodology.md` gives, for each component, the Add-B checklist as a table: raw metric, numerator/denominator, eligibility, normalization, boundary behaviour, missing-data policy, minimum evidence, a worked example (taken from the Task 10 hand cases), the consequence of each threshold, and the delivery-penalty deviation (D6). `assumptions_and_limitations.md` covers the reference period, ID year segment, single currency, no taxes/freight, supplier-level (not site-level) scores, process-local jobs, no auth (future: API-key/OAuth on generate/recalculate), and PostgreSQL/Docker unverified on the build machine.
- [ ] **Step 4:** Write the `Dockerfile` (python:3.12-slim, uv, non-root user, `srp serve --host 0.0.0.0`) and `docker-compose.yml` (`postgres:16` + app with `SRP_DATABASE_URL=postgresql+psycopg://srp:${POSTGRES_PASSWORD}@db:5432/srp`, healthcheck, `alembic upgrade head` on start). The README covers prerequisites, uv install, `uv sync`, `uv run alembic upgrade head`, `uv run srp generate --preset starter`, `uv run srp generate --preset full --replace`, `uv run srp serve` → `http://127.0.0.1:8000/docs`, tests, lint, reproducibility, PostgreSQL via compose (marked unverified), and the repo map and AI-usage pointer.
- [ ] **Step 5:** Run the tests. Expected: PASS. Commit `docs: architecture, data dictionary, rules, scoring, anomalies, API and evaluation docs; docker setup`.

### Task 23b: Objective acceptance-gate runner (Add-E)

**Files:**
- Create: `src/supplier_reliability/services/acceptance.py`, `tests/acceptance/test_acceptance_runner.py`
- Modify: `src/supplier_reliability/cli.py` (add the `acceptance` command)

**Interfaces:**
- Consumes: `run_validation` (Task 13), `detect_raw_batch` results / `data_exceptions`, `evaluate_anomalies` + `reconcile_scores` (Task 16), `verify_reproducibility` (Task 22), and the ground-truth AST check (shared helper `assert_no_ground_truth_imports(package) -> list[str]`, moved from the Task 14/15 tests into `services/acceptance.py`).
- Produces:
  - `GateResult(gate_id, name, status: Literal["PASS","FAIL","UNVERIFIED"], checked: int, failures: int, evidence: dict, reason: str | None)`
  - `run_acceptance_gates(settings, app_config, preset: str, seed: int, run_pytest: bool = True) -> AcceptanceReport(gates: list[GateResult], overall: Literal["PASS","FAIL"], generated_at)`. It writes `reports/<preset>/acceptance_gates.{json,md}`.
  - CLI: `srp acceptance --preset full --seed 20261022`. Exit code 0 only if no gate FAILs; UNVERIFIED gates are listed but do not fail the run.
- Gates. Every gate **executes its check now**; none reads a pre-existing report or checks for a file's existence.

| id | gate | how it is checked | evidence recorded |
|---|---|---|---|
| G01 | Baseline entity counts | `SELECT COUNT(*)` per table vs preset targets | expected vs actual per table |
| G02 | PK uniqueness | `RI-001` executed | rows checked, duplicates |
| G03 | Canonical FK integrity | `RI-002` + `RI-003…009` executed | orphan count per FK |
| G04 | Shipment↔PO-line quantity reconciliation | `AR-002`, `AR-003` | allocations checked, failures |
| G05 | Inspection arithmetic | `AR-004` | inspections checked, failures |
| G06 | Invoice reconciliation | `AR-005` + `SE-004` | invoices checked, failures |
| G07 | Component & overall score reconciliation | `reconcile_scores` (independent pandas) + `AR-006` + a `rebuild_from_lineage` sample of 200 scores, chosen deterministically (every k-th score_id) | checked, mismatches, sample ids |
| G08 | Historical cutoff enforcement | `CH-005` on all scores + `score_evidence` check: no included record with recorded/occurred > cutoff | scores checked, violations |
| G09 | All five anomalies detected | `evaluate_anomalies`: recall = 1.0 for each class A–E, and every ground-truth case has a matching exception | per-class TP/FP/FN/TN, precision, recall, F1 |
| G10 | Ground-truth isolation | `assert_no_ground_truth_imports` over `services.scoring`, `services.anomaly_detection`, `services.validation` + no profile column in `suppliers` + no ground-truth table in the DB | offending modules (none) |
| G11 | Repeatability | runs `verify_reproducibility` now (two fresh seeded runs) | content checksums A/B, differing files |
| G12 | API integration tests | `pytest tests/api --junitxml=<tmp>` subprocess, then parse the XML | tests, failures, errors, skipped |
| G13 | Migration tests | `pytest tests/integration/test_migrations.py --junitxml=...` | same |
| G14 | Full automated suite | `pytest -m "not slow" --junitxml=...` | same |
| G15 | PostgreSQL migrations/runtime | runs only if `SRP_PG_TEST_URL` is set; otherwise **UNVERIFIED** with the reason "PostgreSQL not available on build machine" | — |

- [ ] **Step 1:** Write the failing tests (starter preset, `run_pytest=False` so tests don't recurse; G12–G14 are then UNVERIFIED with the reason "pytest gates disabled in this invocation"):
  - `test_all_data_gates_pass_on_clean_starter`
  - `test_gate_fails_when_count_wrong`: delete one inspection → G01 FAIL with actual 599
  - `test_gate_fails_on_tampered_score`: G07 FAIL, failures == 1
  - `test_gate_fails_when_detection_missing`: delete one `data_exceptions` row → G09 FAIL
  - `test_gate_unverified_without_postgres`
  - `test_report_does_not_pass_from_file_presence`: write a fake passing `acceptance_gates.json` beforehand, corrupt the DB, run → FAIL
  - `test_junit_parser_counts`: on a fixture XML
- [ ] **Step 2:** Run `uv run pytest tests/acceptance -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run `uv run srp acceptance --preset starter` and inspect `reports/starter/acceptance_gates.md`. Commit `feat: objective acceptance-gate runner`.

### Task 24: Full-scale run, final verification, results record

**Files:**
- Create: `docs/results.md`, `tests/integration/test_full_scale.py` (marked `slow`)
- Commit generated: `reports/starter/**`, `reports/full/**`, `data/manifests/*.json`, `data/generated/starter/**`, `data/ground_truth/starter/**`, `data/ground_truth/full/*.csv` (small). Full canonical CSVs stay gitignored and are recreatable.

- [ ] **Step 1:** Write `test_full_scale_counts_and_validation` (slow). It asserts exact counts of 1000 / 10000 / 15000 / 20000, zero MEDIUM+ canonical findings, 25 detections per class, and reconciliation mismatches == 0.
- [ ] **Step 2:** Run `uv run srp generate --preset full --replace`, timing it with `Measure-Command` in PowerShell. Expected: it completes, `reports/full/summary.md` exists, and the counts match. If it takes more than 10 min, profile and fix the slowest step (likely evidence collection or bulk inserts) before continuing.
- [ ] **Step 3:** Run `uv run pytest -m slow -v`, then `uv run srp acceptance --preset full --seed 20261022`. Expected: PASS for G01–G14 and UNVERIFIED for G15, with `reports/full/acceptance_gates.md` written. Any FAIL is debugged (superpowers:systematic-debugging) and fixed, then the full acceptance run is repeated. A gate is never hand-edited to PASS.
- [ ] **Step 4:** Final gate: `uv run ruff check . && uv run ruff format --check . && uv run pytest -v`, plus `uv run alembic downgrade base && uv run alembic upgrade head` on a scratch DB, plus `uv run srp serve` with manual curls of `/ready`, `/api/v1/suppliers?limit=3`, `/api/v1/reports/summary` and `/docs`. Every command's real output gets pasted (abridged) into `docs/results.md`.
- [ ] **Step 5:** Write `docs/results.md`. It starts with the acceptance-gate table copied from `reports/full/acceptance_gates.md` (status, checked, failures per gate), then covers the actual row counts for every table (from `reports/full/entity_counts.json`), validation findings by severity, per-class TP/FP/FN/TN, precision/recall/F1, reconciliation results, reproducibility results, the test count and pass/fail, the lint result, and timing. It ends with a **Verified / Not verified** table, where Not verified includes PostgreSQL migrations & runtime, the Docker image build, and docker-compose.
- [ ] **Step 6:** Update `ai-usage/AI_USAGE.md` and `ai-usage/PROMPT_HISTORY.md` with all prompts used. Commit `chore: full-scale verification results and generated reports`.
- [ ] **Step 7 (ask the user first; this step is outward-facing):** create the public GitHub repo with `gh repo create supplier-reliability-platform --public --source . --push`, then confirm the README renders.

---

## Acceptance Checklist (spec §19 → task)

| # | Definition-of-done item | Proven by |
|---|---|---|
| 1 | App starts | Task 1 Step 6, Task 24 Step 4 |
| 2 | Migrations work | Task 4 tests, Task 24 Step 4 |
| 3 | 11 entities documented | Tasks 2–3, 23 (`test_data_dictionary_covers_every_column`) |
| 4 | Baseline counts | Task 24 `test_full_scale_counts_and_validation` |
| 5 | Referential integrity | Task 13 baseline-clean, Task 24 |
| 6 | Derived values reconcile | AR-001…AR-006 (Task 13), Task 16 reconciliation |
| 7 | Deterministic & explainable scores | Tasks 11, 12, 22 |
| 8 | Missing data explicit | Tasks 10, 11 tests |
| 9 | Anomalies A–E + ground truth | Task 14 |
| 10 | Detection evaluated | Task 16 |
| 11 | Cutoff policy | Tasks 9, 12, 15 |
| 12 | API + integration tests | Tasks 19–21 |
| 13 | Reports from real outputs | Tasks 17, 24 |
| 14 | Two seeded runs compared | Tasks 22, 24 |
| 15 | Honest test/lint reporting | Task 24 `docs/results.md` |
| 16 | Working setup docs | Task 23 README |
| 17 | No placeholders | Task 24 Step 4: `rg -n "TODO\|FIXME\|NotImplementedError" src` → no matches |

| Addendum | Requirement | Proven by |
|---|---|---|
| Add-A | Allocation bridge, partial fulfilment, no double counting | D1; Tasks 3, 7, 9 (join-multiplication test); AR-002/003/004; gate G04 |
| Add-A | Normalized compliance requirements + certification history | D11; Tasks 2, 6, 9 (supersession-at-cutoff test) |
| Add-A | Capacity model (units, period, site scope, available/committed) | D12; Tasks 2, 6, 9, 10 |
| Add-B | Fully specified formulas, versioned config, worked examples | D6/D7; Task 10 hand cases; Task 23 `scoring_methodology.md` |
| Add-C | Score lineage, versions, late-arriving & corrected data | D8/D13; Task 12 (`rebuild_from_lineage`, late-arrival, restatement tests); gate G08 |
| Add-D | Stable/deteriorating/recovering benchmarks, one-factor changes, invalid evidence, independent recomputation | Tasks 10, 12b, 16 |
| Add-E | Executed pass/fail gates with counts and evidence | Task 23b; Task 24 Step 3 |

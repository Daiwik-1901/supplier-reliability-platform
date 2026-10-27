## ADDITIONAL REQUIREMENTS — DOMAIN COMPLETENESS AND SCORING ACCEPTANCE

> Saved verbatim from the addendum prompt supplied on 2026-10-27. It extends
> `2026-10-27-master-implementation-prompt.md`; sections are referenced in the plan as "Add-A" … "Add-E".

### A. Explicit relationship and allocation model

Add a `ShipmentLineAllocation` entity or equivalent bridge table to represent the allocation of shipment quantities to purchase-order lines.

Each allocation must reference a valid shipment and PO line and include the allocated quantity, shipped quantity, received quantity, unit of measure, and relevant timestamps.

Define whether partial fulfilment is permitted and how allocation quantities reconcile with PO-line demand, shipment totals, and inspection records.

Prevent double-counting when aggregating shipment events, inspections, and invoices.

Add dedicated compliance requirement and supplier certification entities, or an equally normalized model, to represent certification type, issuing authority, verification status, effective dates, expiry, evidence references, and historical changes.

Define the capacity model with explicit units, time periods, site scope, available capacity, committed capacity, and demand aggregation rules.

### B. Fully specified scoring formulas

Do not leave the component-score normalization functions to undocumented implementation discretion.

For each component, specify:

- Raw business metric.
- Numerator and denominator.
- Eligibility conditions.
- Normalization formula or threshold table.
- Boundary behavior.
- Missing-data policy.
- Minimum evidence requirements.
- Unit tests and worked examples.

Use a versioned scoring configuration.

As an initial configurable baseline, consider these deterministic mappings:

1. Delivery: component score = 100 × on-time eligible deliveries / eligible deliveries with known outcomes.
2. Quality: component score = max(0, 100 × (1 − observed defect rate / configured maximum acceptable defect rate)).
3. Cost: component score = max(0, 100 × (1 − configured cost penalty based on the absolute invoice variance rate)).
4. Capacity: define a documented utilization-based penalty using required capacity and verified available capacity.
5. Compliance: define a documented rule-based mapping for verified compliant, noncompliant, expired, pending, and unknown requirements.
6. Disruption: define a documented penalty function using eligible signal severity, recency, scope, and resolution status.

These mappings are starting proposals, not universal business standards. Configure the parameters explicitly, handle zero denominators, and document the consequences of every threshold.

Define a deterministic policy for missing or insufficiently observed components. Do not silently equate missing evidence with zero risk.

### C. Historical score lineage

Every SupplierScore must be reproducible from its eligible source evidence.

Store or reference:

- Evaluation period and data cutoff.
- Source-record identifiers or a versioned source snapshot.
- Scoring configuration and version.
- Component-level metrics, denominators, and evidence coverage.
- Calculation timestamp and code/version identifier.
- Applied overrides, exceptions, and missing-data decisions.

Preserve historical score versions. A recalculation must create a new version or evaluation record rather than silently overwrite a previous result.

Define how late-arriving events and corrected source records affect historical scores.

### D. Independent scoring evaluation

Create deterministic benchmark cases for stable, deteriorating, and recovering suppliers.

Use controlled changes to delivery delays, defect rates, invoice variances, capacity, compliance status, and disruption exposure to verify that the relevant metrics and scores change according to the configured rules.

For every component, include hand-calculated test cases covering normal conditions, boundaries, missing data, and invalid evidence.

Test that identical eligible evidence and identical scoring configurations produce identical results.

Test that evidence outside the historical eligibility window cannot affect the score.

Do not evaluate a score against the same stored score or the same calculation output that generated it. Recalculate expected results independently from the source records and documented rules.

### E. Objective acceptance gates

The final report must include pass/fail outcomes for:

- Required baseline entity counts.
- Primary-key uniqueness.
- Canonical foreign-key integrity.
- Shipment-to-PO-line quantity reconciliation.
- Inspection arithmetic.
- Invoice reconciliation.
- Component-score and overall-score reconciliation.
- Historical cutoff enforcement.
- Detection of all five seeded anomaly scenarios.
- Ground-truth isolation.
- Repeatability across two identical seeded runs.
- API integration tests.
- Database migration tests.
- Full automated test suite.

Report actual counts, failure counts, and evidence for each gate.

A required gate must not be marked as passed merely because a corresponding report or test file exists.

Where a gate cannot be executed, mark it unverified and explain why.

"""Pydantic validation of raw payloads that cannot (yet) reach canonical tables (Task 13; reused
by the Task 15 detectors).

`validate_payload(entity_type, payload, *, detected_at)` returns Findings under the canonical
rule ids, so a raw record and a canonical row with the same defect are reported alike:

- DC-001: a required field is missing, null, blank, unparseable or a naive timestamp (it is
  unusable either way).
- DC-002: a value outside its enum.
- DC-003: a negative quantity, or a score/coverage outside its range.
- DC-004: more than 2 decimal places on a money/quantity/score value.
- Cross-field checks, run only when every field parsed: AR-003 (shipment received <= shipped),
  CH-003 (shipment arrival >= dispatch), CH-007 (delivery event recorded >= event time, D4;
  the event-chronology rule), AR-004 (inspection: accepted + rejected = inspected,
  rejected <= defect <= inspected), CH-006 (risk signal observed <= recorded), AR-006 (score:
  overall = sum ew_k c_k / sum ew_k over the non-null components, +/- 0.01, effective_weights
  keys = those components).

Payload shape: the canonical row's columns as JSON (timestamps ISO-8601 with an offset, decimals
as strings or numbers); unknown keys are ignored (e.g. a score payload's `evidence_refs`).
Database-dependent checks (does the PO exist, did the shipment dispatch earlier, ...) are the
detectors' job. Findings use the table name as entity_type and the payload's id as entity_id.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from supplier_reliability.domain.enums import (
    Decision,
    DefectCategory,
    DeliveryEventType,
    InspectionKind,
    InspectionStatus,
    InspectionType,
    ReliabilityCategory,
    ResolutionStatus,
    RiskSignalType,
    Severity,
    ShipmentStatus,
)
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.services.validation.framework import Finding, Violation, to_finding
from supplier_reliability.services.validation.rules_arithmetic import (
    SCORE_TOLERANCE,
    recompute_overall,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Quantity = Annotated[Decimal, Field(ge=0, decimal_places=2)]
Score = Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ShipmentPayload(_Payload):
    shipment_id: Text
    po_id: Text
    originating_site_id: Text
    destination_warehouse_id: Text
    carrier_id: str | None = None
    dispatch_timestamp: AwareDatetime
    expected_delivery_timestamp: AwareDatetime
    actual_delivery_timestamp: AwareDatetime | None = None
    shipment_status: ShipmentStatus
    shipped_quantity: Quantity
    received_quantity: Quantity
    created_at: AwareDatetime


class DeliveryEventPayload(_Payload):
    delivery_event_id: Text
    shipment_id: Text
    event_type: DeliveryEventType
    event_timestamp: AwareDatetime
    recorded_timestamp: AwareDatetime
    location_id: str | None = None
    source_system: Text
    event_metadata: dict[str, Any] | None = None


class InspectionPayload(_Payload):
    inspection_id: Text
    shipment_line_id: Text
    inspection_kind: InspectionKind
    inspection_type: InspectionType
    inspection_timestamp: AwareDatetime
    recorded_at: AwareDatetime
    inspected_quantity: Quantity
    accepted_quantity: Quantity
    rejected_quantity: Quantity
    defect_quantity: Quantity
    inspection_status: InspectionStatus
    defect_category: DefectCategory
    inspection_source: Text


class RiskSignalPayload(_Payload):
    risk_signal_id: Text
    supplier_id: Text
    site_id: str | None = None
    related_shipment_id: str | None = None
    related_contract_id: str | None = None
    signal_type: RiskSignalType
    severity: Severity
    signal_source: Text
    observed_timestamp: AwareDatetime
    recorded_timestamp: AwareDatetime
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    resolution_status: ResolutionStatus
    evidence_reference: Text
    dedup_key: Text


class SupplierScorePayload(_Payload):
    score_id: Text
    supplier_id: Text
    site_id: str | None = None
    evaluation_start: AwareDatetime
    evaluation_end: AwareDatetime
    data_cutoff_timestamp: AwareDatetime
    calculated_at: AwareDatetime
    delivery_score: Score | None = None
    quality_score: Score | None = None
    cost_score: Score | None = None
    capacity_score: Score | None = None
    compliance_score: Score | None = None
    disruption_score: Score | None = None
    overall_score: Score | None = None
    evidence_coverage: Annotated[Decimal, Field(ge=0, le=1)] | None = None
    effective_weights: dict[str, Decimal] | None = None
    reliability_category: ReliabilityCategory
    decision: Decision


# entity type -> (model, table name, id field)
_MODELS: dict[str, tuple[type[_Payload], str, str]] = {
    "SHIPMENT": (ShipmentPayload, "shipments", "shipment_id"),
    "DELIVERY_EVENT": (DeliveryEventPayload, "delivery_events", "delivery_event_id"),
    "INSPECTION": (InspectionPayload, "inspections", "inspection_id"),
    "RISK_SIGNAL": (RiskSignalPayload, "risk_signals", "risk_signal_id"),
    "SUPPLIER_SCORE": (SupplierScorePayload, "supplier_scores", "score_id"),
}
PAYLOAD_ENTITY_TYPES = frozenset(_MODELS)
_COMPONENTS = ("delivery", "quality", "cost", "capacity", "compliance", "disruption")


def _rule_for(error: dict[str, Any]) -> str:
    kind = error["type"]
    if kind == "enum":
        return "DC-002"
    if kind in {"greater_than_equal", "less_than_equal", "greater_than", "less_than"}:
        return "DC-003"
    if kind in {"decimal_max_places", "decimal_max_digits"}:
        return "DC-004"
    return "DC-001"  # missing, null, blank, unparseable, naive timestamp


def _field_violations(table: str, entity_id: str | None, err: ValidationError) -> list[tuple]:
    out, seen = [], set()
    for error in err.errors():
        field = str(error["loc"][0]) if error["loc"] else None
        if field in seen:  # one finding per field (the first problem)
            continue
        seen.add(field)
        rule_id = _rule_for(error)
        value = error.get("input")
        out.append(
            (
                rule_id,
                Violation(
                    table,
                    entity_id,
                    field,
                    None if isinstance(value, dict) else value,
                    error["msg"],
                    f"{table} payload field {field}: {error['msg']}",
                ),
            )
        )
    return out


def _cross_field(table: str, entity_id: str | None, p: _Payload) -> list[tuple]:
    out: list[tuple] = []

    def add(rule_id: str, field: str, observed: Any, expected: str, message: str) -> None:
        out.append((rule_id, Violation(table, entity_id, field, observed, expected, message)))

    if isinstance(p, ShipmentPayload):
        if p.received_quantity > p.shipped_quantity:
            add(
                "AR-003",
                "received_quantity",
                p.received_quantity,
                f"<= shipped {p.shipped_quantity}",
                "shipment received more than shipped",
            )
        arrived = p.actual_delivery_timestamp
        if arrived is not None and arrived < p.dispatch_timestamp:
            add(
                "CH-003",
                "actual_delivery_timestamp",
                arrived.isoformat(),
                f">= dispatch {p.dispatch_timestamp.isoformat()}",
                "shipment arrived before it was dispatched",
            )
    elif isinstance(p, DeliveryEventPayload):
        if p.recorded_timestamp < p.event_timestamp:
            add(
                "CH-007",
                "recorded_timestamp",
                p.recorded_timestamp.isoformat(),
                f">= event_timestamp {p.event_timestamp.isoformat()}",
                "delivery event recorded before it happened (D4)",
            )
    elif isinstance(p, InspectionPayload):
        if p.accepted_quantity + p.rejected_quantity != p.inspected_quantity:
            add(
                "AR-004",
                "inspected_quantity",
                f"accepted {p.accepted_quantity} + rejected {p.rejected_quantity} vs inspected "
                f"{p.inspected_quantity}",
                "accepted + rejected = inspected",
                "inspection quantities do not reconcile",
            )
        if not p.rejected_quantity <= p.defect_quantity <= p.inspected_quantity:
            add(
                "AR-004",
                "defect_quantity",
                p.defect_quantity,
                "rejected <= defect <= inspected",
                "inspection defect count out of bounds",
            )
    elif isinstance(p, RiskSignalPayload):
        if p.observed_timestamp > p.recorded_timestamp:
            add(
                "CH-006",
                "recorded_timestamp",
                p.recorded_timestamp.isoformat(),
                f">= observed {p.observed_timestamp.isoformat()}",
                "risk signal recorded before it was observed",
            )
    elif isinstance(p, SupplierScorePayload) and p.effective_weights is not None:
        comps = {
            c: getattr(p, f"{c}_score") for c in _COMPONENTS if getattr(p, f"{c}_score") is not None
        }
        if set(p.effective_weights) != set(comps):
            add(
                "AR-006",
                "effective_weights",
                sorted(p.effective_weights),
                f"keys = available components {sorted(comps)}",
                "score effective weights do not match its components",
            )
        else:
            recomputed = recompute_overall(comps, p.effective_weights)
            overall = p.overall_score
            if (overall is None) != (recomputed is None) or (
                overall is not None and abs(overall - recomputed) > SCORE_TOLERANCE
            ):
                add(
                    "AR-006",
                    "overall_score",
                    overall,
                    f"= {recomputed} (+/- {SCORE_TOLERANCE})",
                    "score overall does not reconcile with its components",
                )
    return out


def validate_payload(entity_type: str, payload: Any, *, detected_at: datetime) -> list[Finding]:
    """Findings for one raw payload of `entity_type` (one of PAYLOAD_ENTITY_TYPES).

    `detected_at` is required (keyword-only) and stamped on every finding: there is no
    wall-clock fallback, so a pipeline run with an injected clock stays deterministic."""
    if entity_type not in _MODELS:
        raise ValueError(
            f"unsupported payload entity type {entity_type!r}; known: {sorted(_MODELS)}"
        )
    model, table, id_field = _MODELS[entity_type]
    when = ensure_utc(detected_at)
    if not isinstance(payload, dict):
        v = Violation(table, None, None, type(payload).__name__, "a JSON object", "not an object")
        return [to_finding("DC-001", v, when)]
    raw_id = payload.get(id_field)
    entity_id = raw_id.strip() or None if isinstance(raw_id, str) else None
    try:
        parsed = model.model_validate(payload)
    except ValidationError as err:
        found = _field_violations(table, entity_id, err)
    else:
        found = _cross_field(table, entity_id, parsed)
    return [to_finding(rule_id, v, when) for rule_id, v in found]

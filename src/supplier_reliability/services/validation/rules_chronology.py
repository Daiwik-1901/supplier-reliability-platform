"""Chronology rules CH-001..CH-007 (spec section 11; D4, D5, D8, D13).

All are HIGH (time order violated), except CH-005, historical cutoff compliance, which is
CRITICAL: a score that used a record not knowable at its data cutoff leaks the future.

DATE vs DATETIME: a DATE column compared with a cutoff DATETIME in SQL ("invoice_date > C")
is exact on both backends (SQLite compares the ISO texts, where a date sorts before any time on
the same day; PostgreSQL casts the date to midnight), and means date > date(C), the D5 day
granularity. The opposite direction (a datetime against a date's end) is evaluated in Python.
"""

from collections import defaultdict
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased

from supplier_reliability.domain.enums import (
    ComponentName,
    DeliveryEventType,
    Severity,
    validate_sequence,
)
from supplier_reliability.models import (
    CapacityDeclaration,
    ComplianceRequirement,
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RiskSignal,
    ScoreEvidence,
    Shipment,
    ShipmentLineAllocation,
    SupplierCertification,
    SupplierScore,
)
from supplier_reliability.services.validation.framework import RuleCheck, Violation, rule
from supplier_reliability.services.validation.rules_integrity import count

_HIGH = {"category": "chronology", "severity": Severity.HIGH}
E = DeliveryEventType


@rule(
    "CH-001",
    **_HIGH,
    description="A PO's required_delivery_date is on or after its order_date. HIGH.",
)
def ch_001(session: Session) -> RuleCheck:
    po = PurchaseOrder
    stmt = (
        sa.select(po.po_id, po.order_date, po.required_delivery_date)
        .where(po.required_delivery_date < po.order_date)
        .order_by(po.po_id)
    )
    violations = [
        Violation(
            "purchase_orders",
            r.po_id,
            "required_delivery_date",
            r.required_delivery_date.isoformat(),
            f">= order_date {r.order_date}",
            f"PO {r.po_id} is required before it was ordered",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, po), violations)


@rule(
    "CH-002",
    **_HIGH,
    description=(
        "A shipment is dispatched on or after its PO's order date (UTC date of "
        "dispatch_timestamp >= order_date). HIGH."
    ),
)
def ch_002(session: Session) -> RuleCheck:
    stmt = (
        sa.select(Shipment.shipment_id, Shipment.dispatch_timestamp, PurchaseOrder.order_date)
        .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
        .order_by(Shipment.shipment_id)
    )
    checked, violations = 0, []
    for r in session.execute(stmt):
        checked += 1
        if r.dispatch_timestamp.date() < r.order_date:
            violations.append(
                Violation(
                    "shipments",
                    r.shipment_id,
                    "dispatch_timestamp",
                    r.dispatch_timestamp.isoformat(),
                    f"on or after PO order_date {r.order_date}",
                    f"shipment {r.shipment_id} dispatched before its PO was ordered",
                )
            )
    return RuleCheck(checked, violations)


@rule(
    "CH-003",
    **_HIGH,
    description=(
        "Every ARRIVED event of a shipment is at or after its earliest DISPATCHED event (D4; "
        "anomaly B in canonical form). Compared with min(DISPATCHED) per shipment, so one "
        "ARRIVED event yields at most one finding; the finding names the ARRIVED event. HIGH."
    ),
)
def ch_003(session: Session) -> RuleCheck:
    arrived = aliased(DeliveryEvent)
    dispatched = (
        sa.select(
            DeliveryEvent.shipment_id,
            sa.func.min(DeliveryEvent.event_timestamp).label("dispatched_at"),
        )
        .where(DeliveryEvent.event_type == E.DISPATCHED)
        .group_by(DeliveryEvent.shipment_id)
        .subquery()
    )
    stmt = (
        sa.select(
            arrived.delivery_event_id,
            arrived.shipment_id,
            arrived.event_timestamp,
            dispatched.c.dispatched_at,
        )
        .join(dispatched, dispatched.c.shipment_id == arrived.shipment_id)
        .where(
            arrived.event_type == E.ARRIVED,
            arrived.event_timestamp < dispatched.c.dispatched_at,
        )
        .order_by(arrived.delivery_event_id)
    )
    violations = [
        Violation(
            "delivery_events",
            r.delivery_event_id,
            "event_timestamp",
            r.event_timestamp.isoformat(),
            f">= DISPATCHED at {r.dispatched_at.isoformat()}",
            f"shipment {r.shipment_id} arrived before it was dispatched",
        )
        for r in session.execute(stmt)
    ]
    checked = count(session, DeliveryEvent, DeliveryEvent.event_type == E.ARRIVED)
    return RuleCheck(checked, violations)


@rule(
    "CH-004",
    **_HIGH,
    description=(
        "An inspection happens at or after its shipment's arrival: inspection_timestamp >= the "
        "earliest ARRIVED event (the original arrival, which inspections follow; a later "
        "correction does not move the physical inspection). An inspection of a shipment with no "
        "ARRIVED event is also reported. HIGH."
    ),
)
def ch_004(session: Session) -> RuleCheck:
    arrivals = (
        sa.select(
            DeliveryEvent.shipment_id,
            sa.func.min(DeliveryEvent.event_timestamp).label("arrived_at"),
        )
        .where(DeliveryEvent.event_type == E.ARRIVED)
        .group_by(DeliveryEvent.shipment_id)
        .subquery()
    )
    a = ShipmentLineAllocation
    stmt = (
        sa.select(
            Inspection.inspection_id,
            Inspection.inspection_timestamp,
            a.shipment_id,
            arrivals.c.arrived_at,
        )
        .join(a, a.shipment_line_id == Inspection.shipment_line_id)
        .outerjoin(arrivals, arrivals.c.shipment_id == a.shipment_id)
        .where(
            sa.or_(
                arrivals.c.arrived_at.is_(None),
                Inspection.inspection_timestamp < arrivals.c.arrived_at,
            )
        )
        .order_by(Inspection.inspection_id)
    )
    violations = []
    for r in session.execute(stmt):
        arrived = r.arrived_at
        violations.append(
            Violation(
                "inspections",
                r.inspection_id,
                "inspection_timestamp",
                r.inspection_timestamp.isoformat(),
                "after an ARRIVED event" if arrived is None else f">= {arrived.isoformat()}",
                f"inspection {r.inspection_id} of shipment {r.shipment_id} "
                + ("has no arrival" if arrived is None else "precedes the arrival"),
            )
        )
    return RuleCheck(count(session, Inspection), violations)


# --- CH-005 -------------------------------------------------------------------------------------

# lineage entity type -> (model, pk column, [(label, time column)], extra outer joins)
_PO = aliased(PurchaseOrder)
_SOURCES: dict[str, tuple[Any, Any, list[tuple[str, Any]], list[tuple[Any, Any]]]] = {
    "shipment": (
        Shipment,
        Shipment.shipment_id,
        [
            ("expected_delivery_timestamp", Shipment.expected_delivery_timestamp),
            ("created_at", Shipment.created_at),
        ],
        [],
    ),
    "delivery_event": (
        DeliveryEvent,
        DeliveryEvent.delivery_event_id,
        [
            ("event_timestamp", DeliveryEvent.event_timestamp),
            ("recorded_timestamp", DeliveryEvent.recorded_timestamp),
        ],
        [],
    ),
    "inspection": (
        Inspection,
        Inspection.inspection_id,
        [
            ("inspection_timestamp", Inspection.inspection_timestamp),
            ("recorded_at", Inspection.recorded_at),
        ],
        [],
    ),
    "invoice": (
        Invoice,
        Invoice.invoice_id,
        [("invoice_date", Invoice.invoice_date), ("recorded_at", Invoice.recorded_at)],
        [],
    ),
    "po_line": (
        POLine,
        POLine.po_line_id,
        [
            ("created_at", POLine.created_at),
            ("purchase_order.order_date", _PO.order_date),
            ("purchase_order.created_at", _PO.created_at),
        ],
        [(_PO, _PO.po_id == POLine.po_id)],
    ),
    "capacity_declaration": (
        CapacityDeclaration,
        CapacityDeclaration.capacity_declaration_id,
        [("recorded_at", CapacityDeclaration.recorded_at)],
        [],
    ),
    "compliance_requirement": (
        ComplianceRequirement,
        ComplianceRequirement.requirement_id,
        [
            ("created_at", ComplianceRequirement.created_at),
            ("effective_from", ComplianceRequirement.effective_from),  # DATE: day granularity
        ],
        [],
    ),
    "supplier_certification": (
        SupplierCertification,
        SupplierCertification.certification_id,
        [("recorded_at", SupplierCertification.recorded_at)],
        [],
    ),
    "risk_signal": (
        RiskSignal,
        RiskSignal.risk_signal_id,
        [
            ("observed_timestamp", RiskSignal.observed_timestamp),
            ("recorded_timestamp", RiskSignal.recorded_timestamp),
        ],
        [],
    ),
}


def _leak(score_id: str, entity: str, record_id: str, what: str, value: Any, cutoff) -> Violation:
    shown = value.isoformat() if hasattr(value, "isoformat") else value
    return Violation(
        "supplier_scores",
        score_id,
        f"{entity}.{what}",
        f"{record_id} {what}={shown}",
        f"<= data cutoff {cutoff.isoformat()}",
        f"score {score_id} includes {entity} {record_id} whose {what} is after its data cutoff",
    )


@rule(
    "CH-005",
    category="chronology",
    severity=Severity.CRITICAL,
    description=(
        "Historical cutoff compliance (D5, D8, D13): no INCLUDED score_evidence row names a "
        "source record whose occurrence or recording time is after the score's "
        "data_cutoff_timestamp (shipment expected/created, event time/recorded, inspection "
        "time/recorded, invoice date/recorded, PO line created and its PO's order date/created, "
        "capacity/certification recorded, requirement created and effective_from (date), risk "
        "signal observed/recorded; a compliance row's certification from its contribution). "
        "An included row whose source "
        "record is missing, or of an unknown entity type, cannot be proven compliant and is "
        "reported too. One set-based join per entity type. CRITICAL: future leakage."
    ),
)
def ch_005(session: Session) -> RuleCheck:
    se, s = ScoreEvidence, SupplierScore
    cutoff = s.data_cutoff_timestamp
    included = se.included.is_(True)
    violations: list[Violation] = []
    for entity, (model, pk, times, joins) in _SOURCES.items():
        stmt = (
            sa.select(
                se.score_id,
                se.source_record_id,
                pk.label("found"),
                cutoff,
                *[col.label(f"t{i}") for i, (_, col) in enumerate(times)],
            )
            .join(s, s.score_id == se.score_id)
            .outerjoin(model, pk == se.source_record_id)
        )
        for target, on in joins:
            stmt = stmt.outerjoin(target, on)
        late = [col > cutoff for _, col in times]
        stmt = stmt.where(
            included, se.source_entity_type == entity, sa.or_(pk.is_(None), *late)
        ).order_by(se.score_id, se.source_record_id)
        for r in session.execute(stmt):
            if r.found is None:
                violations.append(
                    _leak(
                        r.score_id,
                        entity,
                        r.source_record_id,
                        "source_record",
                        "missing",
                        r.data_cutoff_timestamp,
                    )
                )
                continue
            for i, (what, _) in enumerate(times):
                value = getattr(r, f"t{i}")
                if value is not None and _after(value, r.data_cutoff_timestamp):
                    violations.append(
                        _leak(
                            r.score_id,
                            entity,
                            r.source_record_id,
                            what,
                            value,
                            r.data_cutoff_timestamp,
                        )
                    )
    # the certification a compliance requirement was evaluated against (contribution JSON)
    cert = SupplierCertification
    cert_id = se.contribution["certification_id"].as_string()
    stmt = (
        sa.select(se.score_id, cert.certification_id, cert.recorded_at, cutoff)
        .join(s, s.score_id == se.score_id)
        .join(cert, cert.certification_id == cert_id)
        .where(included, se.component == ComponentName.compliance, cert.recorded_at > cutoff)
        .order_by(se.score_id, cert.certification_id)
    )
    for r in session.execute(stmt):
        violations.append(
            _leak(
                r.score_id,
                "supplier_certification",
                r.certification_id,
                "recorded_at",
                r.recorded_at,
                r.data_cutoff_timestamp,
            )
        )
    unknown = (
        sa.select(se.score_id, se.source_entity_type, se.source_record_id)
        .where(included, se.source_entity_type.not_in(list(_SOURCES)))
        .order_by(se.score_id, se.source_record_id)
    )
    for r in session.execute(unknown):
        violations.append(
            Violation(
                "supplier_scores",
                r.score_id,
                "score_evidence.source_entity_type",
                f"{r.source_entity_type}:{r.source_record_id}",
                f"one of {sorted(_SOURCES)}",
                f"score {r.score_id} includes evidence of unknown type {r.source_entity_type}",
            )
        )
    return RuleCheck(count(session, se, included), violations)


def _after(value: Any, cutoff) -> bool:
    """value > cutoff with D5 day granularity for DATE values."""
    if hasattr(value, "hour"):
        return value > cutoff
    return value > cutoff.date()


@rule(
    "CH-006",
    **_HIGH,
    description="A risk signal is observed at or before it is recorded. HIGH.",
)
def ch_006(session: Session) -> RuleCheck:
    rs = RiskSignal
    stmt = (
        sa.select(rs.risk_signal_id, rs.observed_timestamp, rs.recorded_timestamp)
        .where(rs.observed_timestamp > rs.recorded_timestamp)
        .order_by(rs.risk_signal_id)
    )
    violations = [
        Violation(
            "risk_signals",
            r.risk_signal_id,
            "recorded_timestamp",
            r.recorded_timestamp.isoformat(),
            f">= observed_timestamp {r.observed_timestamp.isoformat()}",
            f"risk signal {r.risk_signal_id} was recorded before it was observed",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, rs), violations)


_RANK = {t: i for i, t in enumerate(DeliveryEventType)}  # lifecycle order breaks timestamp ties


@rule(
    "CH-007",
    **_HIGH,
    description=(
        "Delivery-event transitions (D4): each shipment's events ordered by event_timestamp "
        "(ties by lifecycle order, then id) follow BOOKED -> DISPATCHED -> IN_TRANSIT -> "
        "(DELAYED)* -> ARRIVED -> RECEIVED; CORRECTION may follow any event but never starts a "
        "shipment. One bulk read, validated in Python. The finding names the shipment. HIGH."
    ),
)
def ch_007(session: Session) -> RuleCheck:
    ev = DeliveryEvent
    stmt = sa.select(ev.shipment_id, ev.event_type, ev.event_timestamp, ev.delivery_event_id)
    by_shipment: dict[str, list[tuple]] = defaultdict(list)
    for r in session.execute(stmt):
        by_shipment[r.shipment_id].append(
            (r.event_timestamp, _RANK[r.event_type], r.delivery_event_id, r.event_type)
        )
    violations = []
    for shipment_id in sorted(by_shipment):
        events = sorted(by_shipment[shipment_id])
        sequence = [e[3] for e in events]
        if not validate_sequence(sequence):
            violations.append(
                Violation(
                    "shipments",
                    shipment_id,
                    "delivery_events",
                    " -> ".join(t.value for t in sequence),
                    "BOOKED -> DISPATCHED -> IN_TRANSIT -> (DELAYED)* -> ARRIVED -> RECEIVED",
                    f"shipment {shipment_id} has an invalid delivery-event sequence",
                )
            )
    return RuleCheck(len(by_shipment), violations)

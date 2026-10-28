"""Occurrence and recording times of lineage source records (D5, D8, D13).

ONE mapping, shared by CH-005 (historical cutoff compliance of stored scores) and the Task 15
score-leakage detector (cited evidence of raw score payloads), so both judge "knowable at the
cutoff" identically: lineage entity type (lowercase, as in score_evidence.source_entity_type) ->
(model, pk column, [(label, time column)], extra outer joins).

A DATE time (invoice_date, order_date, effective_from) has day granularity: it is after a cutoff
C iff its date > date(C) (`after_cutoff`).
"""

from collections import defaultdict
from collections.abc import Collection, Mapping
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased

from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import (
    CapacityDeclaration,
    ComplianceRequirement,
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RiskSignal,
    Shipment,
    SupplierCertification,
)

__all__ = ["SOURCE_TIMES", "after_cutoff", "load_source_times"]

_PO = aliased(PurchaseOrder)
SOURCE_TIMES: dict[str, tuple[Any, Any, list[tuple[str, Any]], list[tuple[Any, Any]]]] = {
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

_CHUNK = 500  # ids per IN (...) list: stays far below SQLite's bound-parameter limit


def after_cutoff(value: datetime | date, cutoff: datetime) -> bool:
    """value > cutoff, with D5 day granularity for DATE values."""
    if isinstance(value, datetime):
        return ensure_utc(value) > ensure_utc(cutoff)
    return value > ensure_utc(cutoff).date()


def load_source_times(
    session: Session, ids_by_entity: Mapping[str, Collection[str]]
) -> dict[tuple[str, str], list[tuple[str, datetime | date | None]]]:
    """(entity, record id) -> [(label, time)] in SOURCE_TIMES order, for every requested record
    that exists. One set-based statement per entity type and chunk of ids; unknown entity types
    raise ValueError."""
    unknown = set(ids_by_entity) - set(SOURCE_TIMES)
    if unknown:
        raise ValueError(f"unknown lineage entity types {sorted(unknown)}")
    out: dict[tuple[str, str], list[tuple[str, Any]]] = defaultdict(list)
    for entity, wanted in ids_by_entity.items():
        _, pk, times, joins = SOURCE_TIMES[entity]
        ids = sorted(set(wanted))
        for i in range(0, len(ids), _CHUNK):
            stmt = sa.select(pk, *[col for _, col in times]).where(pk.in_(ids[i : i + _CHUNK]))
            for target, on in joins:
                stmt = stmt.outerjoin(target, on)
            for row in session.execute(stmt):
                out[(entity, row[0])] = [(label, row[j + 1]) for j, (label, _) in enumerate(times)]
    return dict(out)

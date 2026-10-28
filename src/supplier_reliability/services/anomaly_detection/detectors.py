"""Anomaly detectors A, B, D, E over one batch of raw-ingestion records (Task 15, D9, spec 8).

Each detector is `(session, raw_records) -> list[DetectionHit]`: set-based over the records of
its entity type, with a fixed number of bulk canonical lookups (chunked IN lists), never a
query per record. Detection is CONTENT-BASED (Ruling R38): a detector reads the payload's
fields and the canonical rows they reference; it never looks at the raw record's own natural
id (its format, prefix or number) nor at whether that id exists in canonical. The leakage
detector C lives in detectors_leakage.py.

- A SHIPMENT_MISSING_PO (DET-A, HIGH, QUARANTINED): a SHIPMENT payload whose po_id is null,
  blank or absent from canonical purchase_orders (the parent reference is missing). The
  supplier is resolved through the payload's originating_site_id when that site exists
  (the only real reference left); related_entity_ids = [po_id] when one is given.
- B DELIVERY_BEFORE_DISPATCH (DET-B, HIGH, QUARANTINED): an ARRIVED DELIVERY_EVENT payload
  whose event_timestamp is before the EARLIEST canonical DISPATCHED event_timestamp of its
  shipment_id (as CH-003). A canonical shipment with no DISPATCHED event falls back to its
  own dispatch fact, shipments.dispatch_timestamp; evidence["dispatch_source"] says which was
  used. related_entity_ids = [shipment_id]; supplier via shipment -> PO.
- D RISK_SIGNAL_MISSING_PROVENANCE (DET-D, MEDIUM, QUARANTINED_PENDING_PROVENANCE): the
  DC-001 findings of `validate_payload` on signal_source, observed_timestamp or
  recorded_timestamp (null, blank, unparseable or naive), mapped rather than re-implemented.
  Supplier = the payload's supplier_id when it exists in canonical.
- E SITE_ATTRIBUTION_CONFLICT (DET-E, HIGH, QUARANTINED): a SHIPMENT payload whose
  originating_site_id exists in canonical but belongs to another supplier than the payload
  PO's supplier, or is not effective at the dispatch date (effective_from <= date(dispatch) <=
  effective_to, open-ended when null). A site absent from canonical is a missing reference,
  not an attribution conflict, and is not flagged here. related_entity_ids = [po_id, site_id];
  supplier = the PO's supplier.

Classes are independent, so one raw record may carry several exceptions (one per issue type),
e.g. a shipment whose PO is missing (A) and whose site is not effective at dispatch (E: the
effective-period check needs no PO; the supplier check needs one, so it is skipped then).

Malformed fields a detector needs (unparseable timestamps, non-object payloads) make it skip
the record: those are validate_payload findings, not this class.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import (
    DeliveryEventType,
    Disposition,
    IssueType,
    Severity,
)
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import (
    DeliveryEvent,
    PurchaseOrder,
    RawIngestionRecord,
    Shipment,
    Site,
    Supplier,
)
from supplier_reliability.services.scoring.evidence import EvaluationWindow
from supplier_reliability.services.validation.payloads import validate_payload

SHIPMENT, DELIVERY_EVENT, RISK_SIGNAL, SUPPLIER_SCORE = (
    "SHIPMENT",
    "DELIVERY_EVENT",
    "RISK_SIGNAL",
    "SUPPLIER_SCORE",
)
# issue type -> (detector rule id, severity, disposition)
OUTCOMES: dict[IssueType, tuple[str, Severity, Disposition]] = {
    IssueType.SHIPMENT_MISSING_PO: ("DET-A", Severity.HIGH, Disposition.QUARANTINED),
    IssueType.DELIVERY_BEFORE_DISPATCH: ("DET-B", Severity.HIGH, Disposition.QUARANTINED),
    IssueType.SCORE_FUTURE_LEAKAGE: ("DET-C", Severity.CRITICAL, Disposition.CORRECTED),
    IssueType.RISK_SIGNAL_MISSING_PROVENANCE: (
        "DET-D",
        Severity.MEDIUM,
        Disposition.QUARANTINED_PENDING_PROVENANCE,
    ),
    IssueType.SITE_ATTRIBUTION_CONFLICT: ("DET-E", Severity.HIGH, Disposition.QUARANTINED),
}
PROVENANCE_FIELDS = ("signal_source", "observed_timestamp", "recorded_timestamp")
_CHUNK = 500


@dataclass(frozen=True)
class DetectionHit:
    raw_record_id: str
    source_entity_type: str
    source_record_id: str | None
    issue_type: IssueType
    rule_id: str
    severity: Severity
    disposition: Disposition
    supplier_id: str | None
    related_entity_ids: list[str]
    evidence: dict[str, Any]
    corrected_score_id: str | None = None
    # C only: the window to correct (from the payload); not persisted
    window: EvaluationWindow | None = field(default=None, compare=False)


def hit(
    record: RawIngestionRecord,
    issue: IssueType,
    supplier_id: str | None,
    related: Iterable[str | None],
    evidence: dict[str, Any],
    window: EvaluationWindow | None = None,
) -> DetectionHit:
    rule_id, severity, disposition = OUTCOMES[issue]
    return DetectionHit(
        raw_record_id=record.raw_record_id,
        source_entity_type=record.source_entity_type,
        source_record_id=record.source_record_id,
        issue_type=issue,
        rule_id=rule_id,
        severity=severity,
        disposition=disposition,
        supplier_id=supplier_id,
        related_entity_ids=[r for r in related if r is not None],
        evidence=evidence,
        window=window,
    )


# --- payload helpers --------------------------------------------------------------------------


def of_type(records: Sequence[RawIngestionRecord], entity_type: str) -> list[RawIngestionRecord]:
    return [r for r in records if r.source_entity_type == entity_type]


def payload(record: RawIngestionRecord) -> dict[str, Any]:
    return record.payload if isinstance(record.payload, dict) else {}


def text(value: Any) -> str | None:
    """A non-blank string, stripped; anything else is None."""
    return value.strip() or None if isinstance(value, str) else None


def parse_ts(value: Any) -> datetime | None:
    """An ISO-8601 timestamp WITH offset as aware UTC; None if missing, naive or unparseable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if parsed.tzinfo is None else ensure_utc(parsed)


def iso(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat().replace("+00:00", "Z")
    return value.isoformat()


# --- bulk lookups -----------------------------------------------------------------------------


def lookup(session: Session, key: Any, columns: Sequence[Any], keys: Iterable[str]) -> dict:
    """key value -> row (key, *columns) for every existing key; one statement per chunk."""
    wanted = sorted({k for k in keys if k is not None})
    out: dict[str, Any] = {}
    for i in range(0, len(wanted), _CHUNK):
        stmt = sa.select(key, *columns).where(key.in_(wanted[i : i + _CHUNK]))
        out.update({row[0]: row for row in session.execute(stmt)})
    return out


def existing_suppliers(session: Session, ids: Iterable[str | None]) -> set[str]:
    return set(lookup(session, Supplier.supplier_id, [], ids))


def _po_suppliers(session: Session, po_ids: Iterable[str | None]) -> dict[str, str]:
    rows = lookup(session, PurchaseOrder.po_id, [PurchaseOrder.supplier_id], po_ids)
    return {k: row.supplier_id for k, row in rows.items()}


# --- A ----------------------------------------------------------------------------------------


def detect_missing_parent(
    session: Session, raw_records: Sequence[RawIngestionRecord]
) -> list[DetectionHit]:
    shipments = of_type(raw_records, SHIPMENT)
    po_ids = {text(payload(r).get("po_id")) for r in shipments}
    known = _po_suppliers(session, po_ids)
    sites = lookup(
        session,
        Site.site_id,
        [Site.supplier_id],
        {text(payload(r).get("originating_site_id")) for r in shipments},
    )
    hits = []
    for r in shipments:
        p = payload(r)
        po_id = text(p.get("po_id"))
        if po_id is not None and po_id in known:
            continue
        site = sites.get(text(p.get("originating_site_id")))
        evidence = {
            "po_id": p.get("po_id"),
            "observed": "po_id is missing" if po_id is None else "po_id not in purchase_orders",
            "expected_condition": "the shipment's po_id references an existing purchase order",
            "supplier_resolved_via": None if site is None else "originating_site_id",
        }
        hits.append(
            hit(
                r,
                IssueType.SHIPMENT_MISSING_PO,
                None if site is None else site.supplier_id,
                [po_id],
                evidence,
            )
        )
    return hits


# --- B ----------------------------------------------------------------------------------------


def detect_chronology(
    session: Session, raw_records: Sequence[RawIngestionRecord]
) -> list[DetectionHit]:
    arrivals = [
        r
        for r in of_type(raw_records, DELIVERY_EVENT)
        if payload(r).get("event_type") == DeliveryEventType.ARRIVED.value
    ]
    shipment_ids = sorted({s for r in arrivals if (s := text(payload(r).get("shipment_id")))})
    dispatched: dict[str, datetime] = {}
    for i in range(0, len(shipment_ids), _CHUNK):
        stmt = (
            sa.select(DeliveryEvent.shipment_id, sa.func.min(DeliveryEvent.event_timestamp))
            .where(
                DeliveryEvent.event_type == DeliveryEventType.DISPATCHED,
                DeliveryEvent.shipment_id.in_(shipment_ids[i : i + _CHUNK]),
            )
            .group_by(DeliveryEvent.shipment_id)
        )
        dispatched.update({sid: ensure_utc(ts) for sid, ts in session.execute(stmt)})
    supplier_of: dict[str, str] = {}
    header_dispatch: dict[str, datetime] = {}  # fallback: the shipment's own dispatch fact
    for i in range(0, len(shipment_ids), _CHUNK):
        stmt = (
            sa.select(Shipment.shipment_id, Shipment.dispatch_timestamp, PurchaseOrder.supplier_id)
            .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
            .where(Shipment.shipment_id.in_(shipment_ids[i : i + _CHUNK]))
        )
        for sid, dispatch_ts, supplier in session.execute(stmt):
            supplier_of[sid] = supplier
            header_dispatch[sid] = ensure_utc(dispatch_ts)

    hits = []
    for r in arrivals:
        p = payload(r)
        shipment_id = text(p.get("shipment_id"))
        arrived = parse_ts(p.get("event_timestamp"))
        if shipment_id in dispatched:
            first_dispatch, source = dispatched[shipment_id], "delivery_events.DISPATCHED"
        else:
            first_dispatch = header_dispatch.get(shipment_id or "")
            source = "shipments.dispatch_timestamp"
        if arrived is None or first_dispatch is None or arrived >= first_dispatch:
            continue
        evidence = {
            "shipment_id": shipment_id,
            "event_type": DeliveryEventType.ARRIVED.value,
            "event_timestamp": iso(arrived),
            "canonical_dispatched_timestamp": iso(first_dispatch),
            "dispatch_source": source,
            "arrived_before_dispatch_by_seconds": int((first_dispatch - arrived).total_seconds()),
            "expected_condition": (
                "ARRIVED event_timestamp >= the shipment's earliest canonical DISPATCHED "
                "event_timestamp, or its shipments.dispatch_timestamp when it has no DISPATCHED "
                "event (D4)"
            ),
        }
        hits.append(
            hit(
                r,
                IssueType.DELIVERY_BEFORE_DISPATCH,
                supplier_of.get(shipment_id or ""),
                [shipment_id],
                evidence,
            )
        )
    return hits


# --- D ----------------------------------------------------------------------------------------


def detect_missing_provenance(
    session: Session, raw_records: Sequence[RawIngestionRecord]
) -> list[DetectionHit]:
    """DC-001 findings of validate_payload on the provenance fields. validate_payload requires a
    detection time for its findings; the record's received_at is passed (deterministic, and
    the findings' timestamps are not kept: the exception gets the run's detected_at)."""
    signals = of_type(raw_records, RISK_SIGNAL)
    suppliers = existing_suppliers(session, (text(payload(r).get("supplier_id")) for r in signals))
    hits = []
    for r in signals:
        findings = [
            f
            for f in validate_payload(RISK_SIGNAL, r.payload, detected_at=r.received_at)
            if f.rule_id == "DC-001" and f.field in PROVENANCE_FIELDS
        ]
        if not findings:
            continue
        supplier = text(payload(r).get("supplier_id"))
        evidence = {
            "findings": [
                {
                    "rule_id": f.rule_id,
                    "field": f.field,
                    "observed_value": f.observed_value,
                    "expected_condition": f.expected_condition,
                    "message": f.message,
                }
                for f in findings
            ],
            "expected_condition": (
                "signal_source non-blank and observed_timestamp / recorded_timestamp present "
                "(aware ISO-8601); excluded from scoring until provenance is established"
            ),
        }
        hits.append(
            hit(
                r,
                IssueType.RISK_SIGNAL_MISSING_PROVENANCE,
                supplier if supplier in suppliers else None,
                [supplier if supplier in suppliers else None],
                evidence,
            )
        )
    return hits


# --- E ----------------------------------------------------------------------------------------


def _effective(row: Any, day: date) -> bool:
    return row.effective_from <= day and (row.effective_to is None or day <= row.effective_to)


def detect_site_conflation(
    session: Session, raw_records: Sequence[RawIngestionRecord]
) -> list[DetectionHit]:
    shipments = of_type(raw_records, SHIPMENT)
    po_supplier = _po_suppliers(session, (text(payload(r).get("po_id")) for r in shipments))
    sites = lookup(
        session,
        Site.site_id,
        [Site.supplier_id, Site.effective_from, Site.effective_to],
        (text(payload(r).get("originating_site_id")) for r in shipments),
    )
    hits = []
    for r in shipments:
        p = payload(r)
        po_id, site_id = text(p.get("po_id")), text(p.get("originating_site_id"))
        site = sites.get(site_id or "")
        if site is None:
            continue
        expected_supplier = po_supplier.get(po_id or "")
        dispatch = parse_ts(p.get("dispatch_timestamp"))
        conflicts = []
        if expected_supplier is not None and site.supplier_id != expected_supplier:
            conflicts.append("site_belongs_to_another_supplier")
        if dispatch is not None and not _effective(site, dispatch.date()):
            conflicts.append("site_not_effective_at_dispatch")
        if not conflicts:
            continue
        evidence = {
            "conflicts": conflicts,
            "po_id": po_id,
            "po_supplier_id": expected_supplier,
            "originating_site_id": site_id,
            "site_supplier_id": site.supplier_id,
            "site_effective_from": iso(site.effective_from),
            "site_effective_to": None if site.effective_to is None else iso(site.effective_to),
            "dispatch_date": None if dispatch is None else dispatch.date().isoformat(),
            "expected_condition": (
                "the originating site belongs to the PO's supplier and effective_from <= "
                "date(dispatch_timestamp) <= effective_to (open-ended when null)"
            ),
        }
        hits.append(
            hit(
                r,
                IssueType.SITE_ATTRIBUTION_CONFLICT,
                expected_supplier,
                [po_id, site_id],
                evidence,
            )
        )
    return hits

"""Set-based evidence queries (Task 9). One statement per evidence type over all target
suppliers; no per-supplier queries. Time windows are applied in SQL; the recorded-after-cutoff
split happens in Python (evidence.py) because those rows are reported as excluded evidence.

Every function takes `ids` (the supplier ids to restrict to, or None for every supplier) and
returns plain `Row`s. Money and quantities come back as Decimal (SQLAlchemy Numeric). The
keyword `only` (record ids, or None for no restriction) limits a query to the named records of
its own entity; `rebuild_from_lineage` (Task 12) uses it to load exactly a score's lineage.
"""

from collections.abc import Collection, Sequence
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import (
    ContractStatus,
    DeliveryEventType,
    DetectionStatus,
    Disposition,
    POStatus,
    Severity,
)
from supplier_reliability.models import (
    CapacityDeclaration,
    ComplianceRequirement,
    Contract,
    DataException,
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RiskSignal,
    Shipment,
    ShipmentLineAllocation,
    Site,
    Supplier,
    SupplierCertification,
)

Ids = Sequence[str] | None
Only = Collection[str] | None


def _of(column: sa.ColumnElement, ids: Ids | Only) -> sa.ColumnElement[bool]:
    return sa.true() if ids is None else column.in_(sorted(ids))


def _effective_at(model, day: date) -> sa.ColumnElement[bool]:
    """effective_from <= day <= effective_to (open-ended when effective_to is NULL)."""
    return sa.and_(
        model.effective_from <= day,
        sa.or_(model.effective_to.is_(None), model.effective_to >= day),
    )


def existing_supplier_ids(session: Session, ids: Ids) -> list[str]:
    stmt = sa.select(Supplier.supplier_id).where(_of(Supplier.supplier_id, ids))
    return list(session.scalars(stmt.order_by(Supplier.supplier_id)))


def shipments(
    session: Session, ids: Ids, start: datetime, end: datetime, *, only: Only = None
) -> Sequence[Row]:
    """Shipments with expected delivery in [start, end] (due and not-yet-due alike, any
    created_at: rows created after the cutoff are reported as excluded)."""
    stmt = (
        sa.select(
            Shipment.shipment_id,
            PurchaseOrder.supplier_id,
            Shipment.expected_delivery_timestamp,
            Shipment.shipment_status,
            Shipment.created_at,
        )
        .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
        .where(
            _of(PurchaseOrder.supplier_id, ids),
            Shipment.expected_delivery_timestamp.between(start, end),
            _of(Shipment.shipment_id, only),
        )
    )
    return session.execute(stmt).all()


def arrival_events(
    session: Session,
    ids: Ids,
    start: datetime,
    end: datetime,
    *,
    only: Only = None,
    only_events: Only = None,
) -> Sequence[Row]:
    """ARRIVED and CORRECTION events of shipments with expected delivery in [start, end]
    (`only`: shipment ids, `only_events`: delivery event ids)."""
    stmt = (
        sa.select(
            DeliveryEvent.delivery_event_id,
            DeliveryEvent.shipment_id,
            DeliveryEvent.event_type,
            DeliveryEvent.event_timestamp,
            DeliveryEvent.recorded_timestamp,
            DeliveryEvent.event_metadata,
        )
        .join(Shipment, Shipment.shipment_id == DeliveryEvent.shipment_id)
        .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
        .where(
            _of(PurchaseOrder.supplier_id, ids),
            Shipment.expected_delivery_timestamp.between(start, end),
            DeliveryEvent.event_type.in_([DeliveryEventType.ARRIVED, DeliveryEventType.CORRECTION]),
            _of(Shipment.shipment_id, only),
            _of(DeliveryEvent.delivery_event_id, only_events),
        )
    )
    return session.execute(stmt).all()


def inspections(
    session: Session, ids: Ids, start: datetime, end: datetime, *, only: Only = None
) -> Sequence[Row]:
    """Inspections with inspection_timestamp in [start, end]; supplier via allocation ->
    shipment -> PO (D1/D2)."""
    stmt = (
        sa.select(
            Inspection.inspection_id,
            Inspection.shipment_line_id,
            PurchaseOrder.supplier_id,
            Inspection.inspection_timestamp,
            Inspection.recorded_at,
            Inspection.inspected_quantity,
            Inspection.defect_quantity,
        )
        .join(
            ShipmentLineAllocation,
            ShipmentLineAllocation.shipment_line_id == Inspection.shipment_line_id,
        )
        .join(Shipment, Shipment.shipment_id == ShipmentLineAllocation.shipment_id)
        .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
        .where(
            _of(PurchaseOrder.supplier_id, ids),
            Inspection.inspection_timestamp.between(start, end),
            _of(Inspection.inspection_id, only),
        )
    )
    return session.execute(stmt).all()


def invoices(
    session: Session, ids: Ids, first: date, last: date, *, only: Only = None
) -> Sequence[Row]:
    """Invoices with invoice_date in [first, last]."""
    stmt = sa.select(
        Invoice.invoice_id,
        Invoice.supplier_id,
        Invoice.expected_amount,
        Invoice.invoiced_amount,
        Invoice.recorded_at,
    ).where(
        _of(Invoice.supplier_id, ids),
        Invoice.invoice_date.between(first, last),
        _of(Invoice.invoice_id, only),
    )
    return session.execute(stmt).all()


def demand_lines(
    session: Session,
    ids: Ids,
    first: date,
    last: date,
    cutoff: datetime,
    *,
    only: Only = None,
) -> Sequence[Row]:
    """PO lines of non-CANCELLED POs ordered in [first, last], PO and line created <= cutoff."""
    stmt = (
        sa.select(
            POLine.po_line_id,
            PurchaseOrder.supplier_id,
            PurchaseOrder.order_date,
            POLine.ordered_quantity,
            POLine.unit_of_measure,
        )
        .join(PurchaseOrder, PurchaseOrder.po_id == POLine.po_id)
        .where(
            _of(PurchaseOrder.supplier_id, ids),
            PurchaseOrder.po_status != POStatus.CANCELLED,
            PurchaseOrder.order_date.between(first, last),
            PurchaseOrder.created_at <= cutoff,
            POLine.created_at <= cutoff,
            _of(POLine.po_line_id, only),
        )
    )
    return session.execute(stmt).all()


def capacity_declarations(
    session: Session, ids: Ids, first: date, last: date, *, only: Only = None
) -> Sequence[Row]:
    """Declarations whose period overlaps [first, last]."""
    cd = CapacityDeclaration
    stmt = sa.select(
        cd.capacity_declaration_id,
        cd.supplier_id,
        cd.site_id,
        cd.period_start,
        cd.period_end,
        cd.available_capacity,
        cd.committed_capacity,
        cd.verification_status,
        cd.recorded_at,
    ).where(
        _of(cd.supplier_id, ids),
        cd.period_start <= last,
        cd.period_end >= first,
        _of(cd.capacity_declaration_id, only),
    )
    return session.execute(stmt).all()


def requirements(
    session: Session, ids: Ids, cutoff: datetime, *, only: Only = None
) -> Sequence[Row]:
    """Requirements effective at the cutoff date, created <= cutoff, on (non-DRAFT) contracts
    whose effective period contains the cutoff date (D11)."""
    day = cutoff.date()
    req = ComplianceRequirement
    stmt = (
        sa.select(
            req.requirement_id,
            Contract.supplier_id,
            req.certification_type,
            req.mandatory,
            req.scope,
            req.site_id,
        )
        .join(Contract, Contract.contract_id == req.contract_id)
        .where(
            _of(Contract.supplier_id, ids),
            _effective_at(req, day),
            req.created_at <= cutoff,
            _effective_at(Contract, day),
            Contract.contract_status != ContractStatus.DRAFT,
            _of(req.requirement_id, only),
        )
    )
    return session.execute(stmt).all()


def certifications(
    session: Session, ids: Ids, cutoff: datetime, *, only: Only = None
) -> Sequence[Row]:
    """Every certification row recorded <= cutoff (supersession is resolved in Python)."""
    sc = SupplierCertification
    stmt = sa.select(
        sc.certification_id,
        sc.supplier_id,
        sc.site_id,
        sc.certification_type,
        sc.verification_status,
        sc.compliance_status,
        sc.valid_from,
        sc.valid_to,
        sc.supersedes_certification_id,
        sc.recorded_at,
    ).where(_of(sc.supplier_id, ids), sc.recorded_at <= cutoff, _of(sc.certification_id, only))
    return session.execute(stmt).all()


def risk_signals(
    session: Session, ids: Ids, start: datetime, cutoff: datetime, *, only: Only = None
) -> Sequence[Row]:
    """Signals observed in [start, cutoff], with their site's status/effective dates."""
    rs = RiskSignal
    stmt = (
        sa.select(
            rs.risk_signal_id,
            rs.supplier_id,
            rs.site_id,
            rs.signal_type,
            rs.severity,
            rs.observed_timestamp,
            rs.recorded_timestamp,
            rs.valid_to,
            rs.resolution_status,
            rs.dedup_key,
            Site.site_status,
            Site.effective_from.label("site_effective_from"),
            Site.effective_to.label("site_effective_to"),
        )
        .outerjoin(Site, Site.site_id == rs.site_id)
        .where(
            _of(rs.supplier_id, ids),
            rs.observed_timestamp.between(start, cutoff),
            _of(rs.risk_signal_id, only),
        )
    )
    return session.execute(stmt).all()


def open_critical_exceptions(session: Session, ids: Ids) -> Sequence[Row]:
    """CRITICAL data exceptions linked to a supplier that are neither DISMISSED (detection
    status) nor CORRECTED (disposition), at any detection time (ruling R17)."""
    de = DataException
    stmt = sa.select(de.exception_id, de.supplier_id).where(
        de.supplier_id.is_not(None),
        _of(de.supplier_id, ids),
        de.severity == Severity.CRITICAL,
        de.detection_status != DetectionStatus.DISMISSED,
        sa.or_(de.disposition.is_(None), de.disposition != Disposition.CORRECTED),
    )
    return session.execute(stmt).all()

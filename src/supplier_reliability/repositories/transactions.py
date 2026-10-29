"""Purchase-order and shipment read queries (set-based; details use selectinload)."""

from dataclasses import dataclass
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from supplier_reliability.domain.enums import POStatus, ShipmentStatus
from supplier_reliability.models import DeliveryEvent, PurchaseOrder, Shipment

PO_SORT_FIELDS = ("po_id", "order_date", "required_delivery_date")
SHIPMENT_SORT_FIELDS = ("shipment_id", "dispatch_timestamp", "expected_delivery_timestamp")


@dataclass(frozen=True)
class PurchaseOrderFilters:
    supplier_id: str | None = None
    status: POStatus | None = None
    order_date_from: date | None = None
    order_date_to: date | None = None


@dataclass(frozen=True)
class ShipmentFilters:
    supplier_id: str | None = None
    site_id: str | None = None
    status: ShipmentStatus | None = None
    late_only: bool | None = None
    dispatch_from: datetime | None = None
    dispatch_to: datetime | None = None
    grace_hours: int = 24


def _ordering(column, descending: bool, tiebreak):
    """Requested order, then the primary key ascending so paging is deterministic."""
    primary = column.desc() if descending else column.asc()
    return [primary] if column is tiebreak else [primary, tiebreak.asc()]


def list_purchase_orders(
    session: Session,
    filters: PurchaseOrderFilters,
    sort_field: str,
    descending: bool,
    limit: int,
    offset: int,
) -> tuple[list[PurchaseOrder], int]:
    po = PurchaseOrder
    conditions: list[sa.ColumnElement[bool]] = []
    if filters.supplier_id is not None:
        conditions.append(po.supplier_id == filters.supplier_id)
    if filters.status is not None:
        conditions.append(po.po_status == filters.status)
    if filters.order_date_from is not None:
        conditions.append(po.order_date >= filters.order_date_from)
    if filters.order_date_to is not None:
        conditions.append(po.order_date <= filters.order_date_to)
    total = session.scalar(sa.select(sa.func.count()).select_from(po).where(*conditions))
    column = getattr(po, sort_field)
    stmt = (
        sa.select(po)
        .where(*conditions)
        .order_by(*_ordering(column, descending, po.po_id))
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt)), int(total or 0)


def get_purchase_order(session: Session, po_id: str) -> PurchaseOrder | None:
    """PO with lines (by line number) and shipments (by id) loaded: 3 statements."""
    stmt = (
        sa.select(PurchaseOrder)
        .where(PurchaseOrder.po_id == po_id)
        .options(selectinload(PurchaseOrder.lines), selectinload(PurchaseOrder.shipments))
    )
    po = session.scalars(stmt).first()
    if po is not None:
        po.lines.sort(key=lambda line: line.line_number)
        po.shipments.sort(key=lambda s: s.shipment_id)
    return po


def _seconds_between(session: Session, later, earlier):
    """Whole seconds from `earlier` to `later` as an integer expression (exact at boundaries)."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return sa.cast(sa.func.strftime("%s", later), sa.Integer) - sa.cast(
            sa.func.strftime("%s", earlier), sa.Integer
        )
    if dialect == "postgresql":
        return sa.func.extract("epoch", later - earlier)
    raise NotImplementedError(f"late_only is not implemented for {dialect}")


def list_shipments(
    session: Session,
    filters: ShipmentFilters,
    sort_field: str,
    descending: bool,
    limit: int,
    offset: int,
) -> tuple[list[tuple[Shipment, str]], int]:
    """(page of (shipment, supplier_id), total): the supplier comes from the PO in the same query.

    late_only compares the shipment row's actual_delivery_timestamp (the current, corrected
    arrival) with expected + grace. A shipment with no recorded arrival is never "late" here:
    this is a current-state view without a clock, unlike the cutoff-aware scoring evidence."""
    sh, po = Shipment, PurchaseOrder
    conditions: list[sa.ColumnElement[bool]] = []
    if filters.supplier_id is not None:
        conditions.append(po.supplier_id == filters.supplier_id)
    if filters.site_id is not None:
        conditions.append(sh.originating_site_id == filters.site_id)
    if filters.status is not None:
        conditions.append(sh.shipment_status == filters.status)
    if filters.dispatch_from is not None:
        conditions.append(sh.dispatch_timestamp >= filters.dispatch_from)
    if filters.dispatch_to is not None:
        conditions.append(sh.dispatch_timestamp <= filters.dispatch_to)
    if filters.late_only is not None:
        delay = _seconds_between(
            session, sh.actual_delivery_timestamp, sh.expected_delivery_timestamp
        )
        late = sa.and_(
            sh.actual_delivery_timestamp.is_not(None), delay > filters.grace_hours * 3600
        )
        conditions.append(late if filters.late_only else sa.not_(late))

    def joined(stmt: sa.Select) -> sa.Select:
        return stmt.select_from(sh).join(po, po.po_id == sh.po_id).where(*conditions)

    total = session.scalar(joined(sa.select(sa.func.count(sh.shipment_id))))
    column = getattr(sh, sort_field)
    stmt = (
        joined(sa.select(sh, po.supplier_id))
        .order_by(*_ordering(column, descending, sh.shipment_id))
        .limit(limit)
        .offset(offset)
    )
    return [(row[0], row[1]) for row in session.execute(stmt)], int(total or 0)


def get_shipment(session: Session, shipment_id: str) -> tuple[Shipment, str] | None:
    """Shipment with its allocation lines and the owning supplier id: 2 statements."""
    stmt = (
        sa.select(Shipment, PurchaseOrder.supplier_id)
        .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
        .where(Shipment.shipment_id == shipment_id)
        .options(selectinload(Shipment.lines))
    )
    row = session.execute(stmt).first()
    if row is None:
        return None
    shipment = row[0]
    shipment.lines.sort(key=lambda line: line.shipment_line_id)
    return shipment, row[1]


def shipment_exists(session: Session, shipment_id: str) -> bool:
    stmt = sa.select(sa.literal(1)).where(sa.exists().where(Shipment.shipment_id == shipment_id))
    return session.scalar(stmt) is not None


def list_shipment_events(
    session: Session, shipment_id: str, limit: int, offset: int
) -> tuple[list[DeliveryEvent], int]:
    """Events ordered by event_timestamp, then delivery_event_id."""
    where = DeliveryEvent.shipment_id == shipment_id
    total = session.scalar(sa.select(sa.func.count()).select_from(DeliveryEvent).where(where))
    stmt = (
        sa.select(DeliveryEvent)
        .where(where)
        .order_by(DeliveryEvent.event_timestamp, DeliveryEvent.delivery_event_id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt)), int(total or 0)

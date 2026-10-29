"""Purchase-order and shipment read queries (set-based; details use selectinload)."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

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
    # generator reference-period end (aware UTC, from config, never the wall clock)
    reference_end: datetime | None = None


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


def lateness(
    shipment: Shipment, grace_hours: int, reference_end: datetime
) -> tuple[bool, Decimal | None]:
    """(is_late, delay_hours) by the R48 rule; the SQL predicate in `_is_late` is the same.

    Arrived: late iff actual > expected + grace, delay = actual - expected. Not arrived and
    not CANCELLED: late iff expected + grace < reference_end (overdue), delay = reference_end -
    expected. Otherwise on time / not due / cancelled: (False, None). Hours have 2 dp."""
    grace = timedelta(hours=grace_hours)
    expected, actual = shipment.expected_delivery_timestamp, shipment.actual_delivery_timestamp
    if actual is not None:
        late, delay = actual > expected + grace, actual - expected
    else:
        cancelled = shipment.shipment_status == ShipmentStatus.CANCELLED
        late, delay = (not cancelled and expected + grace < reference_end), reference_end - expected
    if not late:
        return False, None
    hours = Decimal(delay.total_seconds()) / Decimal(3600)
    return True, hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _is_late(session: Session, filters: ShipmentFilters) -> sa.ColumnElement[bool]:
    """SQL form of `lateness`. Never NULL, so NOT(...) is the exact complement.

    The arrived branch compares two columns. SQLite cannot add an interval to a column, so
    there the difference is taken in whole epoch seconds (sub-second parts are truncated; the
    generator emits whole seconds). PostgreSQL adds an interval, which keeps full precision
    (PostgreSQL behaviour UNVERIFIED: no PostgreSQL available). The overdue branch compares
    the column with a bound parameter on both dialects."""
    sh = Shipment
    if filters.reference_end is None:
        raise ValueError("late_only needs reference_end")
    grace = timedelta(hours=filters.grace_hours)
    if session.get_bind().dialect.name == "sqlite":
        seconds = sa.cast(
            sa.func.strftime("%s", sh.actual_delivery_timestamp), sa.Integer
        ) - sa.cast(sa.func.strftime("%s", sh.expected_delivery_timestamp), sa.Integer)
        arrived_late = seconds > int(grace.total_seconds())
    else:
        arrived_late = sh.actual_delivery_timestamp > sh.expected_delivery_timestamp + grace
    arrived = sa.and_(sh.actual_delivery_timestamp.is_not(None), arrived_late)
    overdue = sa.and_(
        sh.actual_delivery_timestamp.is_(None),
        sh.shipment_status != ShipmentStatus.CANCELLED,
        sh.expected_delivery_timestamp < filters.reference_end - grace,
    )
    return sa.or_(arrived, overdue)


def list_shipments(
    session: Session,
    filters: ShipmentFilters,
    sort_field: str,
    descending: bool,
    limit: int,
    offset: int,
) -> tuple[list[tuple[Shipment, str]], int]:
    """(page of (shipment, supplier_id), total): the supplier comes from the PO in the same query.

    late_only follows Ruling R48 (see `lateness`): the shipment row's actual_delivery_timestamp
    (the current, corrected arrival) is compared with expected + grace, and a shipment with no
    arrival that is not CANCELLED is late once expected + grace < reference_end (the fixed
    reference-period end, not the wall clock). This is a current-state view, unlike the
    cutoff-aware scoring evidence. late_only=false is the exact complement."""
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
        late = _is_late(session, filters)
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

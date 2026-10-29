"""Purchase-order and shipment endpoints."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from supplier_reliability.api.dependencies import (
    AppConfigDep,
    DbDep,
    PageParams,
    Sort,
    sort_dependency,
)
from supplier_reliability.api.errors import NotFoundError, error_responses
from supplier_reliability.domain.enums import POStatus, ShipmentStatus
from supplier_reliability.repositories import transactions as repo
from supplier_reliability.schemas.common import Page
from supplier_reliability.schemas.transactions import (
    DeliveryEventOut,
    PurchaseOrderDetail,
    PurchaseOrderSummary,
    ShipmentBase,
    ShipmentDetail,
    ShipmentLineOut,
    ShipmentSummary,
)

router = APIRouter(prefix="/api/v1", tags=["purchase orders and shipments"])

PoSort = Annotated[Sort, Depends(sort_dependency(repo.PO_SORT_FIELDS, default="po_id:asc"))]
ShipmentSort = Annotated[
    Sort, Depends(sort_dependency(repo.SHIPMENT_SORT_FIELDS, default="shipment_id:asc"))
]

AWARE_NOTE = "ISO-8601 with an offset (`Z` or `+02:00`), converted to UTC; a naive value is a 422."


@router.get(
    "/purchase-orders",
    response_model=Page[PurchaseOrderSummary],
    summary="List purchase orders",
    description="Paginated purchase-order headers. Filters are ANDed; order dates are inclusive.",
    responses=error_responses(422),
)
def list_purchase_orders(
    db: DbDep,
    page: PageParams,
    sort: PoSort,
    supplier_id: Annotated[str | None, Query(examples=["SUP-000001"])] = None,
    status: Annotated[POStatus | None, Query(examples=["OPEN"])] = None,
    order_date_from: Annotated[date | None, Query(examples=["2025-01-01"])] = None,
    order_date_to: Annotated[date | None, Query(examples=["2025-06-30"])] = None,
) -> Page[PurchaseOrderSummary]:
    filters = repo.PurchaseOrderFilters(
        supplier_id=supplier_id,
        status=status,
        order_date_from=order_date_from,
        order_date_to=order_date_to,
    )
    rows, total = repo.list_purchase_orders(
        db, filters, sort.field, sort.descending, page.limit, page.offset
    )
    items = [PurchaseOrderSummary.model_validate(r) for r in rows]
    return Page[PurchaseOrderSummary](
        items=items, total=total, limit=page.limit, offset=page.offset
    )


@router.get(
    "/purchase-orders/{po_id}",
    response_model=PurchaseOrderDetail,
    summary="Get a purchase order",
    description="Purchase order with its lines and shipments.",
    responses=error_responses(404, 422),
)
def get_purchase_order(po_id: str, db: DbDep) -> PurchaseOrderDetail:
    po = repo.get_purchase_order(db, po_id)
    if po is None:
        raise NotFoundError("Purchase order", po_id)
    return PurchaseOrderDetail.model_validate(po)


@router.get(
    "/shipments",
    response_model=Page[ShipmentSummary],
    summary="List shipments",
    description="Paginated shipments. `late_only=true` keeps shipments whose "
    "`actual_delivery_timestamp` is later than `expected_delivery_timestamp` plus the configured "
    "grace period; `false` keeps the rest. This is a current-state view of the shipment row (its "
    "corrected arrival), so a shipment without an arrival is never late; scoring uses "
    f"cutoff-aware evidence instead. Datetime filters: {AWARE_NOTE}",
    responses=error_responses(422),
)
def list_shipments(
    db: DbDep,
    config: AppConfigDep,
    page: PageParams,
    sort: ShipmentSort,
    supplier_id: Annotated[str | None, Query(examples=["SUP-000001"])] = None,
    site_id: Annotated[str | None, Query(examples=["SITE-SUP-000001-01"])] = None,
    status: Annotated[ShipmentStatus | None, Query(examples=["DELIVERED"])] = None,
    late_only: Annotated[bool | None, Query(examples=[True])] = None,
    dispatch_from: Annotated[
        AwareDatetime | None, Query(description=AWARE_NOTE, examples=["2025-03-01T00:00:00Z"])
    ] = None,
    dispatch_to: Annotated[
        AwareDatetime | None, Query(description=AWARE_NOTE, examples=["2025-03-31T23:59:59Z"])
    ] = None,
) -> Page[ShipmentSummary]:
    filters = repo.ShipmentFilters(
        supplier_id=supplier_id,
        site_id=site_id,
        status=status,
        late_only=late_only,
        dispatch_from=dispatch_from,
        dispatch_to=dispatch_to,
        grace_hours=config.scoring.grace_hours,
    )
    rows, total = repo.list_shipments(
        db, filters, sort.field, sort.descending, page.limit, page.offset
    )
    items = [_summary(shipment, supplier) for shipment, supplier in rows]
    return Page[ShipmentSummary](items=items, total=total, limit=page.limit, offset=page.offset)


def _summary(shipment, supplier_id: str) -> ShipmentSummary:
    base = ShipmentBase.model_validate(shipment)
    return ShipmentSummary(**dict(base), supplier_id=supplier_id)


@router.get(
    "/shipments/{shipment_id}",
    response_model=ShipmentDetail,
    summary="Get a shipment",
    description="Shipment with its PO-line allocations.",
    responses=error_responses(404, 422),
)
def get_shipment(shipment_id: str, db: DbDep) -> ShipmentDetail:
    found = repo.get_shipment(db, shipment_id)
    if found is None:
        raise NotFoundError("Shipment", shipment_id)
    shipment, supplier_id = found
    return ShipmentDetail(
        **dict(ShipmentBase.model_validate(shipment)),
        supplier_id=supplier_id,
        lines=[ShipmentLineOut.model_validate(line) for line in shipment.lines],
    )


@router.get(
    "/shipments/{shipment_id}/events",
    response_model=Page[DeliveryEventOut],
    summary="List a shipment's delivery events",
    description="Events ordered by `event_timestamp`, then `delivery_event_id`. "
    "`recorded_timestamp` shows when each event became known.",
    responses=error_responses(404, 422),
)
def list_events(shipment_id: str, db: DbDep, page: PageParams) -> Page[DeliveryEventOut]:
    if not repo.shipment_exists(db, shipment_id):
        raise NotFoundError("Shipment", shipment_id)
    rows, total = repo.list_shipment_events(db, shipment_id, page.limit, page.offset)
    items = [DeliveryEventOut.model_validate(r) for r in rows]
    return Page[DeliveryEventOut](items=items, total=total, limit=page.limit, offset=page.offset)

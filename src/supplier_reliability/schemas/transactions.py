"""Purchase-order and shipment response schemas."""

from datetime import date
from typing import Any

from supplier_reliability.domain.enums import DeliveryEventType, POStatus, ShipmentStatus
from supplier_reliability.schemas.common import Money, OrmModel, Quantity, UtcDatetime


class PurchaseOrderSummary(OrmModel):
    po_id: str
    supplier_id: str
    contract_id: str
    order_date: date
    required_delivery_date: date
    po_status: POStatus
    currency: str
    created_at: UtcDatetime


class POLineOut(OrmModel):
    po_line_id: str
    po_id: str
    line_number: int
    product_category: str
    ordered_quantity: Quantity
    unit_of_measure: str
    agreed_unit_price: Money
    line_amount: Money


class ShipmentBase(OrmModel):
    shipment_id: str
    po_id: str
    originating_site_id: str
    destination_warehouse_id: str
    carrier_id: str | None
    dispatch_timestamp: UtcDatetime
    expected_delivery_timestamp: UtcDatetime
    actual_delivery_timestamp: UtcDatetime | None
    shipment_status: ShipmentStatus
    shipped_quantity: Quantity
    received_quantity: Quantity


class ShipmentSummary(ShipmentBase):
    supplier_id: str
    is_late: bool
    delay_hours: Quantity | None  # hours late when is_late, else null


class ShipmentLineOut(OrmModel):
    shipment_line_id: str
    po_line_id: str
    unit_of_measure: str
    allocated_quantity: Quantity
    shipped_quantity: Quantity
    received_quantity: Quantity
    received_at: UtcDatetime | None


class ShipmentDetail(ShipmentSummary):
    lines: list[ShipmentLineOut]


class PurchaseOrderDetail(PurchaseOrderSummary):
    lines: list[POLineOut]
    shipments: list[ShipmentBase]


class DeliveryEventOut(OrmModel):
    delivery_event_id: str
    shipment_id: str
    event_type: DeliveryEventType
    event_timestamp: UtcDatetime
    recorded_timestamp: UtcDatetime
    location_id: str | None
    source_system: str
    event_metadata: dict[str, Any] | None

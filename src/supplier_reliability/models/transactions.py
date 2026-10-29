"""Transactions: POs, shipments, allocations (D1), events (D4), inspections (D2), invoices (D3),
risk signals. Relationships are lazy="raise": load explicitly with selectinload/joins (R7)."""

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supplier_reliability.domain.enums import (
    DefectCategory,
    DeliveryEventType,
    InspectionKind,
    InspectionStatus,
    InspectionType,
    InvoiceStatus,
    POStatus,
    ResolutionStatus,
    RiskSignalType,
    Severity,
    ShipmentStatus,
)
from supplier_reliability.models.base import Base, UTCDateTime, enum_type
from supplier_reliability.models.master import MONEY

if TYPE_CHECKING:
    from supplier_reliability.models.master import Contract, Site, Supplier

QUANTITY = sa.Numeric(14, 2)
PERCENTAGE = sa.Numeric(12, 6)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    po_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(sa.ForeignKey("suppliers.supplier_id"), index=True)
    contract_id: Mapped[str] = mapped_column(sa.ForeignKey("contracts.contract_id"), index=True)
    order_date: Mapped[date] = mapped_column(sa.Date, index=True)
    required_delivery_date: Mapped[date] = mapped_column(sa.Date)
    po_status: Mapped[POStatus] = mapped_column(enum_type(POStatus))
    currency: Mapped[str] = mapped_column(sa.String(3))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    supplier: Mapped["Supplier"] = relationship(back_populates="purchase_orders", lazy="raise")
    contract: Mapped["Contract"] = relationship(back_populates="purchase_orders", lazy="raise")
    lines: Mapped[list["POLine"]] = relationship(
        back_populates="po", lazy="raise", order_by="POLine.line_number"
    )
    shipments: Mapped[list["Shipment"]] = relationship(
        back_populates="po", lazy="raise", order_by="Shipment.shipment_id"
    )
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="po", lazy="raise")


class POLine(Base):
    __tablename__ = "po_lines"
    __table_args__ = (
        sa.UniqueConstraint("po_id", "line_number"),
        sa.CheckConstraint("ordered_quantity > 0", name="ordered_quantity_positive"),
        sa.CheckConstraint("agreed_unit_price >= 0", name="unit_price_nonneg"),
        sa.CheckConstraint("line_number >= 1", name="line_number_positive"),
    )

    po_line_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    po_id: Mapped[str] = mapped_column(sa.ForeignKey("purchase_orders.po_id"))
    line_number: Mapped[int] = mapped_column(sa.Integer)
    product_category: Mapped[str] = mapped_column(sa.String(64))
    ordered_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit_of_measure: Mapped[str] = mapped_column(sa.String(16))
    agreed_unit_price: Mapped[Decimal] = mapped_column(MONEY)
    line_amount: Mapped[Decimal] = mapped_column(MONEY)  # stored: quantity x price, quantized
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    po: Mapped["PurchaseOrder"] = relationship(back_populates="lines", lazy="raise")
    allocations: Mapped[list["ShipmentLineAllocation"]] = relationship(
        back_populates="po_line", lazy="raise"
    )


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (
        sa.CheckConstraint("shipped_quantity >= 0", name="shipped_nonneg"),
        sa.CheckConstraint("received_quantity >= 0", name="received_nonneg"),
        sa.CheckConstraint("received_quantity <= shipped_quantity", name="received_le_shipped"),
    )

    shipment_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    po_id: Mapped[str] = mapped_column(sa.ForeignKey("purchase_orders.po_id"), index=True)
    originating_site_id: Mapped[str] = mapped_column(sa.ForeignKey("sites.site_id"), index=True)
    destination_warehouse_id: Mapped[str] = mapped_column(sa.String(40))
    carrier_id: Mapped[str | None] = mapped_column(sa.String(40))
    dispatch_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    expected_delivery_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actual_delivery_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    shipment_status: Mapped[ShipmentStatus] = mapped_column(enum_type(ShipmentStatus))
    shipped_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    received_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    po: Mapped["PurchaseOrder"] = relationship(back_populates="shipments", lazy="raise")
    originating_site: Mapped["Site"] = relationship(lazy="raise")
    lines: Mapped[list["ShipmentLineAllocation"]] = relationship(
        back_populates="shipment", lazy="raise", order_by="ShipmentLineAllocation.shipment_line_id"
    )
    events: Mapped[list["DeliveryEvent"]] = relationship(back_populates="shipment", lazy="raise")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="shipment", lazy="raise")


class ShipmentLineAllocation(Base):
    """D1 bridge: the only grain for quantity, defect and invoice aggregation."""

    __tablename__ = "shipment_lines"
    __table_args__ = (
        sa.UniqueConstraint("shipment_id", "po_line_id"),
        sa.CheckConstraint(
            "received_quantity <= shipped_quantity AND shipped_quantity <= allocated_quantity",
            name="quantity_order",
        ),
        sa.CheckConstraint(
            "received_quantity >= 0 AND shipped_quantity >= 0 AND allocated_quantity >= 0",
            name="quantities_nonneg",
        ),
    )

    shipment_line_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(sa.ForeignKey("shipments.shipment_id"))
    po_line_id: Mapped[str] = mapped_column(sa.ForeignKey("po_lines.po_line_id"), index=True)
    unit_of_measure: Mapped[str] = mapped_column(sa.String(16))
    allocated_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    shipped_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    received_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    allocated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    shipped_at: Mapped[datetime] = mapped_column(UTCDateTime)
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    shipment: Mapped["Shipment"] = relationship(back_populates="lines", lazy="raise")
    po_line: Mapped["POLine"] = relationship(back_populates="allocations", lazy="raise")
    inspections: Mapped[list["Inspection"]] = relationship(
        back_populates="shipment_line", lazy="raise"
    )


class DeliveryEvent(Base):
    __tablename__ = "delivery_events"
    __table_args__ = (
        sa.CheckConstraint("recorded_timestamp >= event_timestamp", name="recorded_after_event"),
        sa.Index(
            "ix_delivery_events_shipment_id_event_timestamp", "shipment_id", "event_timestamp"
        ),
    )

    delivery_event_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(sa.ForeignKey("shipments.shipment_id"))
    event_type: Mapped[DeliveryEventType] = mapped_column(enum_type(DeliveryEventType))
    event_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    recorded_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    location_id: Mapped[str | None] = mapped_column(sa.String(40))
    source_system: Mapped[str] = mapped_column(sa.String(64))
    # CORRECTION events carry {corrects_event_id, field, corrected_value} (D4, D13).
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)

    shipment: Mapped["Shipment"] = relationship(back_populates="events", lazy="raise")


class Inspection(Base):
    """D2: one row per inspection of a shipment line; one INITIAL per line, any number of
    REINSPECTIONs. shipment/po-line are reached through shipment_line_id only."""

    __tablename__ = "inspections"
    __table_args__ = (
        sa.CheckConstraint(
            "inspected_quantity >= 0 AND accepted_quantity >= 0 AND rejected_quantity >= 0"
            " AND defect_quantity >= 0",
            name="quantities_nonneg",
        ),
        # round(): Numeric is stored as float on SQLite, so 0.1 + 0.2 must still equal 0.3.
        sa.CheckConstraint(
            "round(accepted_quantity + rejected_quantity, 2) = round(inspected_quantity, 2)",
            name="reconciliation",
        ),
        sa.CheckConstraint(
            "rejected_quantity <= defect_quantity AND defect_quantity <= inspected_quantity",
            name="defect_bounds",
        ),
        sa.Index(
            "uq_inspections_one_initial_per_shipment_line",
            "shipment_line_id",
            unique=True,
            sqlite_where=sa.text("inspection_kind = 'INITIAL'"),
            postgresql_where=sa.text("inspection_kind = 'INITIAL'"),
        ),
    )

    inspection_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    shipment_line_id: Mapped[str] = mapped_column(
        sa.ForeignKey("shipment_lines.shipment_line_id"), index=True
    )
    inspection_kind: Mapped[InspectionKind] = mapped_column(enum_type(InspectionKind))
    inspection_type: Mapped[InspectionType] = mapped_column(enum_type(InspectionType))
    inspection_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)
    inspected_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    accepted_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    rejected_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    defect_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    inspection_status: Mapped[InspectionStatus] = mapped_column(enum_type(InspectionStatus))
    defect_category: Mapped[DefectCategory] = mapped_column(enum_type(DefectCategory))
    inspection_source: Mapped[str] = mapped_column(sa.String(64))

    shipment_line: Mapped["ShipmentLineAllocation"] = relationship(
        back_populates="inspections", lazy="raise"
    )


class Invoice(Base):
    """D3: one invoice per delivered shipment; amounts are pre-tax goods value."""

    __tablename__ = "invoices"
    __table_args__ = (sa.UniqueConstraint("supplier_id", "supplier_invoice_number"),)

    invoice_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(sa.ForeignKey("suppliers.supplier_id"), index=True)
    contract_id: Mapped[str] = mapped_column(sa.ForeignKey("contracts.contract_id"), index=True)
    po_id: Mapped[str] = mapped_column(sa.ForeignKey("purchase_orders.po_id"), index=True)
    shipment_id: Mapped[str] = mapped_column(sa.ForeignKey("shipments.shipment_id"), index=True)
    supplier_invoice_number: Mapped[str] = mapped_column(sa.String(64))
    invoice_date: Mapped[date] = mapped_column(sa.Date, index=True)
    invoice_status: Mapped[InvoiceStatus] = mapped_column(enum_type(InvoiceStatus))
    currency: Mapped[str] = mapped_column(sa.String(3))
    invoiced_amount: Mapped[Decimal] = mapped_column(MONEY)
    expected_amount: Mapped[Decimal] = mapped_column(MONEY)
    variance_amount: Mapped[Decimal] = mapped_column(MONEY)
    variance_percentage: Mapped[Decimal | None] = mapped_column(PERCENTAGE)  # NULL if expected = 0
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)

    supplier: Mapped["Supplier"] = relationship(lazy="raise")
    contract: Mapped["Contract"] = relationship(lazy="raise")
    po: Mapped["PurchaseOrder"] = relationship(back_populates="invoices", lazy="raise")
    shipment: Mapped["Shipment"] = relationship(back_populates="invoices", lazy="raise")


class RiskSignal(Base):
    """Canonical signals always carry provenance; provenance-less ones live only in the raw
    layer (D9-D)."""

    __tablename__ = "risk_signals"
    __table_args__ = (
        sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="validity_dates"),
    )

    risk_signal_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(sa.ForeignKey("suppliers.supplier_id"), index=True)
    site_id: Mapped[str | None] = mapped_column(sa.ForeignKey("sites.site_id"), index=True)
    related_shipment_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("shipments.shipment_id"), index=True
    )
    related_contract_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("contracts.contract_id"), index=True
    )
    signal_type: Mapped[RiskSignalType] = mapped_column(enum_type(RiskSignalType))
    severity: Mapped[Severity] = mapped_column(enum_type(Severity))
    signal_source: Mapped[str] = mapped_column(sa.String(100))
    observed_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    recorded_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution_status: Mapped[ResolutionStatus] = mapped_column(enum_type(ResolutionStatus))
    evidence_reference: Mapped[str] = mapped_column(sa.String(500))
    dedup_key: Mapped[str] = mapped_column(sa.String(128))

    supplier: Mapped["Supplier"] = relationship(back_populates="risk_signals", lazy="raise")
    site: Mapped["Site | None"] = relationship(lazy="raise")
    related_shipment: Mapped["Shipment | None"] = relationship(lazy="raise")
    related_contract: Mapped["Contract | None"] = relationship(lazy="raise")

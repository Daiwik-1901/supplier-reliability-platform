"""Bulk-load a GeneratedDataset into the canonical tables.

Tables are inserted parents first in the fixed ``LOAD_ORDER`` (spec section 7.4), never by
relationship-based unit-of-work ordering, with ``session.execute(insert(Model), rows)`` in chunks
of ``CHUNK_SIZE``. Everything runs in the caller's single transaction: the loader never commits,
so ``session_scope`` commits all tables together or rolls all of them back.
"""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.models import (
    CapacityDeclaration,
    ComplianceRequirement,
    Contract,
    ContractPrice,
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
from supplier_reliability.services.generator.dataset import GeneratedDataset

__all__ = ["CHUNK_SIZE", "LOAD_ORDER", "DatasetAlreadyLoadedError", "load_dataset"]

CHUNK_SIZE = 5_000

# table name (= GeneratedDataset attribute) -> model, parents before children.
LOAD_ORDER: dict[str, type[Any]] = {
    "suppliers": Supplier,
    "sites": Site,
    "contracts": Contract,
    "contract_prices": ContractPrice,
    "compliance_requirements": ComplianceRequirement,
    "supplier_certifications": SupplierCertification,  # self-FK: superseded rows come first
    "capacity_declarations": CapacityDeclaration,
    "purchase_orders": PurchaseOrder,
    "po_lines": POLine,
    "shipments": Shipment,
    "shipment_lines": ShipmentLineAllocation,
    "delivery_events": DeliveryEvent,
    "inspections": Inspection,
    "invoices": Invoice,
    "risk_signals": RiskSignal,
}


class DatasetAlreadyLoadedError(RuntimeError):
    """The target database already holds suppliers; loading again would mix datasets."""


def load_dataset(session: Session, ds: GeneratedDataset) -> dict[str, int]:
    """Insert every table of ``ds``; returns rows inserted per table (in load order)."""
    if session.scalar(sa.select(sa.func.count()).select_from(Supplier)):
        raise DatasetAlreadyLoadedError("suppliers is not empty: refusing to load a dataset")
    counts: dict[str, int] = {}
    for table, model in LOAD_ORDER.items():
        rows = getattr(ds, table)
        for start in range(0, len(rows), CHUNK_SIZE):
            session.execute(sa.insert(model), rows[start : start + CHUNK_SIZE])
        counts[table] = len(rows)
        if table == "contract_prices" and rows:
            _reset_contract_price_sequence(session)
    return counts


def _reset_contract_price_sequence(session: Session) -> None:
    """contract_prices is loaded with explicit integer ids; on PostgreSQL the serial sequence
    must then move past them or the next default insert collides. UNVERIFIED: no PostgreSQL is
    available in this environment. SQLite needs nothing (it uses max(rowid) + 1)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        sa.text(
            "SELECT setval(pg_get_serial_sequence('contract_prices', 'contract_price_id'),"
            " (SELECT MAX(contract_price_id) FROM contract_prices))"
        )
    )

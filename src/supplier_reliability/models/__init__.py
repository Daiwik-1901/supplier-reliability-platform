"""ORM models. Import every model module so Base.metadata is complete."""

from supplier_reliability.models.base import Base
from supplier_reliability.models.master import (
    CapacityDeclaration,
    ComplianceRequirement,
    Contract,
    ContractPrice,
    Site,
    Supplier,
    SupplierCertification,
)
from supplier_reliability.models.operations import (
    DataException,
    DatasetJob,
    DatasetManifest,
    RawIngestionRecord,
    ValidationFinding,
    ValidationRun,
)
from supplier_reliability.models.scoring import ScoreEvidence, ScoringConfigSnapshot, SupplierScore
from supplier_reliability.models.transactions import (
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RiskSignal,
    Shipment,
    ShipmentLineAllocation,
)

__all__ = [
    "Base",
    "CapacityDeclaration",
    "ComplianceRequirement",
    "Contract",
    "ContractPrice",
    "DataException",
    "DatasetJob",
    "DatasetManifest",
    "DeliveryEvent",
    "Inspection",
    "Invoice",
    "POLine",
    "PurchaseOrder",
    "RawIngestionRecord",
    "RiskSignal",
    "ScoreEvidence",
    "ScoringConfigSnapshot",
    "Shipment",
    "ShipmentLineAllocation",
    "Site",
    "Supplier",
    "SupplierCertification",
    "SupplierScore",
    "ValidationFinding",
    "ValidationRun",
]

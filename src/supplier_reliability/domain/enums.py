"""Domain enumerations. Every member's value equals its name (portable CHECK constraints)."""

from enum import StrEnum


class SupplierStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    SUSPENDED = "SUSPENDED"


class SupplierCategory(StrEnum):
    RAW_MATERIALS = "RAW_MATERIALS"
    ELECTRONICS = "ELECTRONICS"
    PACKAGING = "PACKAGING"
    CHEMICALS = "CHEMICALS"
    MACHINED_PARTS = "MACHINED_PARTS"
    LOGISTICS_SERVICES = "LOGISTICS_SERVICES"


class Region(StrEnum):
    NA = "NA"
    LATAM = "LATAM"
    EMEA = "EMEA"
    APAC = "APAC"


class SiteType(StrEnum):
    FACTORY = "FACTORY"
    WAREHOUSE = "WAREHOUSE"
    DISTRIBUTION_CENTER = "DISTRIBUTION_CENTER"


class SiteStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ContractStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    TERMINATED = "TERMINATED"
    DRAFT = "DRAFT"


class CertificationType(StrEnum):
    ISO9001 = "ISO9001"
    ISO14001 = "ISO14001"
    IATF16949 = "IATF16949"
    REACH = "REACH"
    CONFLICT_MINERALS = "CONFLICT_MINERALS"


class CertificationStatus(StrEnum):
    COMPLIANT = "COMPLIANT"
    NONCOMPLIANT = "NONCOMPLIANT"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    REJECTED = "REJECTED"


class RequirementScope(StrEnum):
    SUPPLIER = "SUPPLIER"
    SITE = "SITE"


class CapacityVerification(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class ComplianceState(StrEnum):
    COMPLIANT = "COMPLIANT"
    NONCOMPLIANT = "NONCOMPLIANT"
    EXPIRED = "EXPIRED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    UNKNOWN = "UNKNOWN"


class POStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class ShipmentStatus(StrEnum):
    BOOKED = "BOOKED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    CANCELLED = "CANCELLED"


class DeliveryEventType(StrEnum):
    BOOKED = "BOOKED"
    DISPATCHED = "DISPATCHED"
    IN_TRANSIT = "IN_TRANSIT"
    DELAYED = "DELAYED"
    ARRIVED = "ARRIVED"
    RECEIVED = "RECEIVED"
    CORRECTION = "CORRECTION"


_E = DeliveryEventType

# D4: BOOKED -> DISPATCHED -> IN_TRANSIT -> (DELAYED)* -> ARRIVED -> RECEIVED.
# CORRECTION is deliberately absent from this table: it is allowed after ANY event
# (but never as the first event) and does not advance the shipment state, so the
# previous state stays the one before the correction. is_valid_transition encodes this.
DELIVERY_EVENT_TRANSITIONS: dict[DeliveryEventType | None, frozenset[DeliveryEventType]] = {
    None: frozenset({_E.BOOKED}),
    _E.BOOKED: frozenset({_E.DISPATCHED}),
    _E.DISPATCHED: frozenset({_E.IN_TRANSIT}),
    _E.IN_TRANSIT: frozenset({_E.DELAYED, _E.ARRIVED}),
    _E.DELAYED: frozenset({_E.DELAYED, _E.ARRIVED}),
    _E.ARRIVED: frozenset({_E.RECEIVED}),
    _E.RECEIVED: frozenset(),
}


def is_valid_transition(prev: DeliveryEventType | None, nxt: DeliveryEventType) -> bool:
    """Whether `nxt` may follow `prev` (the last non-CORRECTION event, None for a new shipment)."""
    if nxt is DeliveryEventType.CORRECTION:
        return prev is not None
    return nxt in DELIVERY_EVENT_TRANSITIONS[prev]


class InspectionKind(StrEnum):
    INITIAL = "INITIAL"
    REINSPECTION = "REINSPECTION"


class InspectionType(StrEnum):
    FULL = "FULL"
    SAMPLE = "SAMPLE"


class InspectionStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    CONDITIONAL = "CONDITIONAL"


class DefectCategory(StrEnum):
    NONE = "NONE"
    DIMENSIONAL = "DIMENSIONAL"
    COSMETIC = "COSMETIC"
    FUNCTIONAL = "FUNCTIONAL"
    PACKAGING = "PACKAGING"
    DOCUMENTATION = "DOCUMENTATION"


class InvoiceStatus(StrEnum):
    RECEIVED = "RECEIVED"
    APPROVED = "APPROVED"
    DISPUTED = "DISPUTED"
    PAID = "PAID"


class RiskSignalType(StrEnum):
    LOGISTICS_DISRUPTION = "LOGISTICS_DISRUPTION"
    FACTORY_INCIDENT = "FACTORY_INCIDENT"
    FINANCIAL_DISTRESS = "FINANCIAL_DISTRESS"
    CERTIFICATION_LAPSE = "CERTIFICATION_LAPSE"
    CARRIER_DELAY = "CARRIER_DELAY"
    MONITORING_ATTESTATION = "MONITORING_ATTESTATION"


class Severity(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ResolutionStatus(StrEnum):
    OPEN = "OPEN"
    MITIGATING = "MITIGATING"
    RESOLVED = "RESOLVED"


class ReliabilityCategory(StrEnum):
    HIGH = "HIGH"
    ACCEPTABLE = "ACCEPTABLE"
    AT_RISK = "AT_RISK"
    CRITICAL = "CRITICAL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class Decision(StrEnum):
    SUSPEND_PENDING_REVIEW = "SUSPEND_PENDING_REVIEW"
    HOLD_FOR_DATA_REVIEW = "HOLD_FOR_DATA_REVIEW"
    COLLECT_MORE_DATA = "COLLECT_MORE_DATA"
    PREFERRED = "PREFERRED"
    APPROVED = "APPROVED"
    MONITOR = "MONITOR"
    ESCALATE = "ESCALATE"


class CalculationStatus(StrEnum):
    CALCULATED = "CALCULATED"
    PROVISIONAL = "PROVISIONAL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CORRECTED = "CORRECTED"


class ComponentName(StrEnum):
    delivery = "delivery"
    quality = "quality"
    cost = "cost"
    capacity = "capacity"
    compliance = "compliance"
    disruption = "disruption"


class IssueType(StrEnum):
    SHIPMENT_MISSING_PO = "SHIPMENT_MISSING_PO"
    DELIVERY_BEFORE_DISPATCH = "DELIVERY_BEFORE_DISPATCH"
    SCORE_FUTURE_LEAKAGE = "SCORE_FUTURE_LEAKAGE"
    RISK_SIGNAL_MISSING_PROVENANCE = "RISK_SIGNAL_MISSING_PROVENANCE"
    SITE_ATTRIBUTION_CONFLICT = "SITE_ATTRIBUTION_CONFLICT"


class Disposition(StrEnum):
    QUARANTINED = "QUARANTINED"
    QUARANTINED_PENDING_PROVENANCE = "QUARANTINED_PENDING_PROVENANCE"
    CORRECTED = "CORRECTED"
    REJECTED = "REJECTED"


class DetectionStatus(StrEnum):
    DETECTED = "DETECTED"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

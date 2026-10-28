"""Builders that insert minimal valid record graphs with explicit timestamps (Tasks 9, 12b).

Every method takes keyword arguments, inserts one row (flushing so constraint errors surface at
the call site) and returns the new primary key. Ids are sequential per entity unless given.
Typical graph::

    g = GraphBuilder(session)
    sup = g.supplier_graph()                      # supplier + ACTIVE site + contract
    po = g.purchase_order(supplier_id=sup.supplier_id, contract_id=sup.contract_id,
                          order_date=date(2025, 1, 5))
    line = g.po_line(po_id=po)
    shp = g.shipment(po_id=po, site_id=sup.site_id, expected=utc(2025, 2, 1))
    sl = g.allocation(shipment_id=shp, po_line_id=line)
    arrived = g.event(shipment_id=shp, event_type=DeliveryEventType.ARRIVED,
                      event_timestamp=utc(2025, 2, 1, 6))
"""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import (
    CapacityVerification,
    CertificationStatus,
    CertificationType,
    ContractStatus,
    DefectCategory,
    DeliveryEventType,
    DetectionStatus,
    Disposition,
    InspectionKind,
    InspectionStatus,
    InspectionType,
    InvoiceStatus,
    IssueType,
    POStatus,
    Region,
    RequirementScope,
    ResolutionStatus,
    RiskSignalType,
    Severity,
    ShipmentStatus,
    SiteStatus,
    SiteType,
    SupplierCategory,
    SupplierStatus,
    VerificationStatus,
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
    RawIngestionRecord,
    RiskSignal,
    Shipment,
    ShipmentLineAllocation,
    Site,
    Supplier,
    SupplierCertification,
)
from supplier_reliability.services.generator.risk import risk_dedup_key

EPOCH = datetime(2024, 1, 1, tzinfo=UTC)  # default creation time: before every test window


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _at_hour(d: date, hour: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


@dataclass(frozen=True)
class SupplierGraph:
    supplier_id: str
    site_id: str
    contract_id: str


class GraphBuilder:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._seq: Counter[str] = Counter()
        self._lines_per_po: Counter[str] = Counter()

    # -- plumbing --------------------------------------------------------------------------------

    def _next(self, prefix: str) -> str:
        self._seq[prefix] += 1
        return f"{prefix}-{self._seq[prefix]:06d}"

    def _add(self, row: Any) -> None:
        self.session.add(row)
        self.session.flush()

    # -- master data -----------------------------------------------------------------------------

    def supplier(
        self, *, supplier_id: str | None = None, onboarding_date: date = date(2024, 1, 1)
    ) -> str:
        sid = supplier_id or self._next("SUP")
        self._add(
            Supplier(
                supplier_id=sid,
                supplier_name=f"Synthetic Supplier {sid}",
                supplier_category=SupplierCategory.ELECTRONICS,
                country="Germany",
                region=Region.EMEA,
                supplier_status=SupplierStatus.ACTIVE,
                onboarding_date=onboarding_date,
                created_at=EPOCH,
                updated_at=EPOCH,
            )
        )
        return sid

    def site(
        self,
        *,
        supplier_id: str,
        site_id: str | None = None,
        status: SiteStatus = SiteStatus.ACTIVE,
        effective_from: date = date(2024, 1, 1),
        effective_to: date | None = None,
    ) -> str:
        site_id = site_id or self._next(f"SITE-{supplier_id}")
        self._add(
            Site(
                site_id=site_id,
                supplier_id=supplier_id,
                site_name=f"Site {site_id}",
                site_type=SiteType.FACTORY,
                country="Germany",
                region=Region.EMEA,
                site_status=status,
                effective_from=effective_from,
                effective_to=effective_to,
                created_at=EPOCH,
            )
        )
        return site_id

    def contract(
        self,
        *,
        supplier_id: str,
        site_id: str | None = None,
        status: ContractStatus = ContractStatus.ACTIVE,
        effective_from: date = date(2024, 1, 1),
        effective_to: date | None = None,
        created_at: datetime = EPOCH,
    ) -> str:
        cid = self._next("CTR")
        self._add(
            Contract(
                contract_id=cid,
                supplier_id=supplier_id,
                site_id=site_id,
                contract_status=status,
                effective_from=effective_from,
                effective_to=effective_to,
                currency="EUR",
                payment_terms="NET30",
                delivery_terms="DAP",
                contracted_capacity=Decimal("10000.00"),
                capacity_unit="units/month",
                created_at=created_at,
            )
        )
        return cid

    def supplier_graph(self, *, supplier_id: str | None = None) -> SupplierGraph:
        """Supplier + one ACTIVE site + one open-ended ACTIVE supplier-level contract."""
        sid = self.supplier(supplier_id=supplier_id)
        return SupplierGraph(sid, self.site(supplier_id=sid), self.contract(supplier_id=sid))

    def requirement(
        self,
        *,
        contract_id: str,
        certification_type: CertificationType = CertificationType.ISO9001,
        mandatory: bool = True,
        site_id: str | None = None,
        effective_from: date = date(2024, 1, 1),
        effective_to: date | None = None,
        created_at: datetime = EPOCH,
    ) -> str:
        """SITE scope when `site_id` is given, else SUPPLIER scope."""
        rid = self._next("REQ")
        self._add(
            ComplianceRequirement(
                requirement_id=rid,
                contract_id=contract_id,
                certification_type=certification_type,
                mandatory=mandatory,
                scope=RequirementScope.SITE if site_id else RequirementScope.SUPPLIER,
                site_id=site_id,
                effective_from=effective_from,
                effective_to=effective_to,
                created_at=created_at,
            )
        )
        return rid

    def certification(
        self,
        *,
        supplier_id: str,
        certification_type: CertificationType = CertificationType.ISO9001,
        site_id: str | None = None,
        verification: VerificationStatus = VerificationStatus.VERIFIED,
        compliance: CertificationStatus = CertificationStatus.COMPLIANT,
        valid_from: date = date(2024, 1, 1),
        valid_to: date = date(2027, 1, 1),
        recorded_at: datetime = EPOCH,
        supersedes: str | None = None,
        certificate_number: str | None = None,
    ) -> str:
        cid = self._next("CERT")
        pending = verification is VerificationStatus.PENDING_VERIFICATION
        self._add(
            SupplierCertification(
                certification_id=cid,
                supplier_id=supplier_id,
                site_id=site_id,
                certification_type=certification_type,
                certificate_number=certificate_number or f"SYN-{cid}",
                issuing_authority="Synthetic Certification Body 01",
                verification_status=verification,
                compliance_status=compliance,
                valid_from=valid_from,
                valid_to=valid_to,
                verified_at=None if pending else recorded_at,
                evidence_reference=f"synthetic://cert/{cid}",
                supersedes_certification_id=supersedes,
                recorded_at=recorded_at,
            )
        )
        return cid

    def capacity(
        self,
        *,
        supplier_id: str,
        month: date,
        available: Decimal | str,
        committed: Decimal | str = Decimal("0"),
        site_id: str | None = None,
        verified: bool = True,
        recorded_at: datetime = EPOCH,
        period_end: date | None = None,
    ) -> str:
        """Declaration for `month` (first day); `period_end` defaults to the month's last day."""
        cid = self._next("CAP")
        nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        self._add(
            CapacityDeclaration(
                capacity_declaration_id=cid,
                supplier_id=supplier_id,
                site_id=site_id,
                period_start=month,
                period_end=period_end or nxt - timedelta(days=1),
                unit="units/month",
                available_capacity=Decimal(available),
                committed_capacity=Decimal(committed),
                verification_status=(
                    CapacityVerification.VERIFIED if verified else CapacityVerification.UNVERIFIED
                ),
                source="SYNTHETIC_SUPPLIER_AUDIT" if verified else "SYNTHETIC_SELF_DECLARED",
                recorded_at=recorded_at,
            )
        )
        return cid

    # -- transactions ----------------------------------------------------------------------------

    def purchase_order(
        self,
        *,
        supplier_id: str,
        contract_id: str,
        order_date: date,
        created_at: datetime | None = None,
        status: POStatus = POStatus.OPEN,
    ) -> str:
        """`created_at` defaults to 09:00 UTC on the order date."""
        pid = self._next("PO")
        self._add(
            PurchaseOrder(
                po_id=pid,
                supplier_id=supplier_id,
                contract_id=contract_id,
                order_date=order_date,
                required_delivery_date=order_date + timedelta(days=30),
                po_status=status,
                currency="EUR",
                created_at=created_at or _at_hour(order_date, 9),
            )
        )
        return pid

    def po_line(
        self,
        *,
        po_id: str,
        ordered_quantity: Decimal | str = Decimal("100"),
        unit_price: Decimal | str = Decimal("10.00"),
        unit_of_measure: str = "EA",
        created_at: datetime | None = None,
    ) -> str:
        """`created_at` defaults to the PO's created_at."""
        po = self.session.get(PurchaseOrder, po_id)
        lid = self._next("POL")
        qty, price = Decimal(ordered_quantity), Decimal(unit_price)
        self._lines_per_po[po_id] += 1
        line_number = self._lines_per_po[po_id]
        self._add(
            POLine(
                po_line_id=lid,
                po_id=po_id,
                line_number=line_number,
                product_category="WIDGET",
                ordered_quantity=qty,
                unit_of_measure=unit_of_measure,
                agreed_unit_price=price,
                line_amount=(qty * price).quantize(Decimal("0.01")),
                created_at=created_at or po.created_at,
            )
        )
        return lid

    def shipment(
        self,
        *,
        po_id: str,
        site_id: str,
        expected: datetime,
        dispatch: datetime | None = None,
        status: ShipmentStatus = ShipmentStatus.IN_TRANSIT,
        created_at: datetime | None = None,
    ) -> str:
        """`dispatch` defaults to expected - 5 days; `created_at` to dispatch - 1 day."""
        sid = self._next("SHP")
        dispatch = dispatch or expected - timedelta(days=5)
        self._add(
            Shipment(
                shipment_id=sid,
                po_id=po_id,
                originating_site_id=site_id,
                destination_warehouse_id="WH-01",
                carrier_id="CARRIER-01",
                dispatch_timestamp=dispatch,
                expected_delivery_timestamp=expected,
                actual_delivery_timestamp=None,
                shipment_status=status,
                shipped_quantity=Decimal("0"),
                received_quantity=Decimal("0"),
                created_at=created_at or dispatch - timedelta(days=1),
            )
        )
        return sid

    def allocation(
        self,
        *,
        shipment_id: str,
        po_line_id: str,
        quantity: Decimal | str = Decimal("100"),
        received: Decimal | str | None = None,
    ) -> str:
        """Allocated = shipped = `quantity`; received defaults to `quantity`."""
        shipment = self.session.get(Shipment, shipment_id)
        uom = self.session.get(POLine, po_line_id).unit_of_measure
        qty = Decimal(quantity)
        rec = qty if received is None else Decimal(received)
        lid = self._next("SHL")
        self._add(
            ShipmentLineAllocation(
                shipment_line_id=lid,
                shipment_id=shipment_id,
                po_line_id=po_line_id,
                unit_of_measure=uom,
                allocated_quantity=qty,
                shipped_quantity=qty,
                received_quantity=rec,
                allocated_at=shipment.created_at,
                shipped_at=shipment.dispatch_timestamp,
                received_at=None,
                created_at=shipment.created_at,
            )
        )
        shipment.shipped_quantity += qty
        shipment.received_quantity += rec
        self.session.flush()
        return lid

    def event(
        self,
        *,
        shipment_id: str,
        event_type: DeliveryEventType,
        event_timestamp: datetime,
        recorded_timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """`recorded_timestamp` defaults to event_timestamp + 1 h."""
        eid = self._next("EVT")
        self._add(
            DeliveryEvent(
                delivery_event_id=eid,
                shipment_id=shipment_id,
                event_type=event_type,
                event_timestamp=event_timestamp,
                recorded_timestamp=recorded_timestamp or event_timestamp + timedelta(hours=1),
                location_id=None,
                source_system="SYNTHETIC_TMS",
                event_metadata=metadata,
            )
        )
        return eid

    def correction(
        self,
        *,
        shipment_id: str,
        corrects_event_id: str,
        corrected_value: datetime,
        recorded_timestamp: datetime,
        event_timestamp: datetime | None = None,
    ) -> str:
        """CORRECTION of an event's event_timestamp (D13 metadata shape, ISO-8601 UTC value)."""
        return self.event(
            shipment_id=shipment_id,
            event_type=DeliveryEventType.CORRECTION,
            event_timestamp=event_timestamp or recorded_timestamp,
            recorded_timestamp=recorded_timestamp,
            metadata={
                "corrects_event_id": corrects_event_id,
                "field": "event_timestamp",
                "corrected_value": corrected_value.isoformat(),
            },
        )

    def inspection(
        self,
        *,
        shipment_line_id: str,
        inspection_timestamp: datetime,
        recorded_at: datetime | None = None,
        inspected: Decimal | str = Decimal("10"),
        defect: Decimal | str = Decimal("0"),
        kind: InspectionKind = InspectionKind.INITIAL,
    ) -> str:
        """All defects are rejected (rejected = defect); `recorded_at` defaults to +2 h."""
        iid = self._next("INS")
        inspected, defect = Decimal(inspected), Decimal(defect)
        self._add(
            Inspection(
                inspection_id=iid,
                shipment_line_id=shipment_line_id,
                inspection_kind=kind,
                inspection_type=InspectionType.SAMPLE,
                inspection_timestamp=inspection_timestamp,
                recorded_at=recorded_at or inspection_timestamp + timedelta(hours=2),
                inspected_quantity=inspected,
                accepted_quantity=inspected - defect,
                rejected_quantity=defect,
                defect_quantity=defect,
                inspection_status=InspectionStatus.FAILED if defect else InspectionStatus.PASSED,
                defect_category=DefectCategory.FUNCTIONAL if defect else DefectCategory.NONE,
                inspection_source="SYNTHETIC_QMS",
            )
        )
        return iid

    def invoice(
        self,
        *,
        shipment_id: str,
        invoice_date: date,
        expected: Decimal | str = Decimal("1000.00"),
        invoiced: Decimal | str = Decimal("1000.00"),
        recorded_at: datetime | None = None,
    ) -> str:
        """Supplier/PO/contract come from the shipment; `recorded_at` defaults to 10:00 on the
        invoice date."""
        po = self.session.get(PurchaseOrder, self.session.get(Shipment, shipment_id).po_id)
        iid = self._next("INV")
        expected, invoiced = Decimal(expected), Decimal(invoiced)
        variance = invoiced - expected
        recorded = recorded_at or _at_hour(invoice_date, 10)
        self._add(
            Invoice(
                invoice_id=iid,
                supplier_id=po.supplier_id,
                contract_id=po.contract_id,
                po_id=po.po_id,
                shipment_id=shipment_id,
                supplier_invoice_number=f"SINV-{iid}",
                invoice_date=invoice_date,
                invoice_status=InvoiceStatus.RECEIVED,
                currency="EUR",
                invoiced_amount=invoiced,
                expected_amount=expected,
                variance_amount=variance,
                variance_percentage=(variance / expected).quantize(Decimal("0.000001"))
                if expected
                else None,
                created_at=recorded,
                recorded_at=recorded,
            )
        )
        return iid

    def risk_signal(
        self,
        *,
        supplier_id: str,
        observed: datetime,
        recorded: datetime | None = None,
        site_id: str | None = None,
        signal_type: RiskSignalType = RiskSignalType.FACTORY_INCIDENT,
        severity: Severity = Severity.HIGH,
        resolution: ResolutionStatus = ResolutionStatus.OPEN,
        valid_to: datetime | None = None,
    ) -> str:
        """`recorded` defaults to observed + 1 h."""
        rid = self._next("RSK")
        self._add(
            RiskSignal(
                risk_signal_id=rid,
                supplier_id=supplier_id,
                site_id=site_id,
                related_shipment_id=None,
                related_contract_id=None,
                signal_type=signal_type,
                severity=severity,
                signal_source="SYNTHETIC_RISK_FEED",
                observed_timestamp=observed,
                recorded_timestamp=recorded or observed + timedelta(hours=1),
                valid_from=observed,
                valid_to=valid_to,
                resolution_status=resolution,
                evidence_reference=f"synthetic://risk/{rid}",
                dedup_key=risk_dedup_key(supplier_id, site_id, signal_type, observed),
            )
        )
        return rid

    # -- operations ------------------------------------------------------------------------------

    def data_exception(
        self,
        *,
        supplier_id: str | None,
        severity: Severity = Severity.CRITICAL,
        detection_status: DetectionStatus = DetectionStatus.DETECTED,
        disposition: Disposition | None = Disposition.QUARANTINED,
        detected_at: datetime = EPOCH,
        issue_type: IssueType = IssueType.SITE_ATTRIBUTION_CONFLICT,
    ) -> str:
        """Exception (plus the raw record it points at) linked to `supplier_id`."""
        raw_id = self._next("RAW")
        self._add(
            RawIngestionRecord(
                raw_record_id=raw_id,
                batch_id="BATCH-TEST",
                source_entity_type="SHIPMENT",
                source_record_id=None,
                payload={},
                received_at=detected_at,
            )
        )
        eid = self._next("EXC")
        self._add(
            DataException(
                exception_id=eid,
                batch_id="BATCH-TEST",
                raw_record_id=raw_id,
                source_entity_type="SHIPMENT",
                source_record_id=None,
                issue_type=issue_type,
                rule_id="TEST-001",
                severity=severity,
                detection_status=detection_status,
                detected_at=detected_at,
                disposition=disposition,
                related_entity_ids={},
                evidence={},
                supplier_id=supplier_id,
                corrected_score_id=None,
            )
        )
        return eid

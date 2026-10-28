"""Shared hand-built EvidenceBundle builders for scoring tests (no DB)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from supplier_reliability.domain.enums import (
    CertificationType,
    ComplianceState,
    RequirementScope,
    ResolutionStatus,
    RiskSignalType,
    Severity,
)
from supplier_reliability.services.scoring.components import ComponentResult
from supplier_reliability.services.scoring.evidence import (
    CapacityObs,
    ComplianceObs,
    DeliveryObs,
    DemandObs,
    EvaluationWindow,
    EvidenceBundle,
    InspectionObs,
    InvoiceObs,
    RiskObs,
)

D = Decimal
START = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)
WINDOW = EvaluationWindow(START, CUTOFF, CUTOFF)


def bundle(**kw) -> EvidenceBundle:
    return EvidenceBundle(supplier_id="SUP-000001", window=WINDOW, **kw)


# --- builders ---------------------------------------------------------------------------------


def deliveries(on_time: int = 0, late_days: tuple = (), overdue: int = 0) -> list[DeliveryObs]:
    out, n = [], 0
    for _ in range(on_time):
        n += 1
        expected = START + timedelta(days=n)
        out.append(DeliveryObs(f"SHP-{n:03d}", expected, expected))
    for days in late_days:
        n += 1
        expected = START + timedelta(days=n)
        arrived = expected + (days if isinstance(days, timedelta) else timedelta(days=days))
        out.append(DeliveryObs(f"SHP-{n:03d}", expected, arrived))
    for _ in range(overdue):
        n += 1
        out.append(DeliveryObs(f"SHP-{n:03d}", CUTOFF - timedelta(days=2), None))
    return out


def inspections(*pairs: tuple[int, int]) -> list[InspectionObs]:
    return [
        InspectionObs(f"INS-{i:03d}", f"SHL-{i:03d}", D(insp), D(defect))
        for i, (insp, defect) in enumerate(pairs, 1)
    ]


def invoices(*pairs: tuple[str, str]) -> list[InvoiceObs]:
    return [InvoiceObs(f"INV-{i:03d}", D(e), D(v)) for i, (e, v) in enumerate(pairs, 1)]


def demand(month: date, qty: int, line: str = "POL-1") -> DemandObs:
    return DemandObs(line, month, D(qty), "units/month")


def cap(month: date, avail: int, committed: int = 0, verified: bool = True, cid: str = "CAP-1"):
    return CapacityObs(cid, None, month, D(avail), D(committed), verified)


JAN, FEB = date(2026, 1, 1), date(2026, 2, 1)


def comp(state: ComplianceState, mandatory: bool = False, rid: str = "REQ-1", **kw):
    return ComplianceObs(
        rid,
        CertificationType.ISO9001,
        mandatory,
        RequirementScope.SUPPLIER,
        None,
        None,
        state,
        **kw,
    )


def risk(
    rid: str = "RSK-1",
    severity: Severity = Severity.HIGH,
    age_days: float = 0,
    resolved_at_cutoff: bool = False,
    site_active: bool = True,
    key: str | None = None,
    signal_type: RiskSignalType = RiskSignalType.FACTORY_INCIDENT,
    resolution: ResolutionStatus | None = None,
) -> RiskObs:
    return RiskObs(
        rid,
        None,
        site_active,
        signal_type,
        severity,
        CUTOFF - timedelta(days=age_days),
        resolution or (ResolutionStatus.RESOLVED if resolved_at_cutoff else ResolutionStatus.OPEN),
        key or rid,
        resolved_at_cutoff,
    )


def assert_missing(r: ComponentResult, reason: str) -> None:
    assert r.score is None
    assert reason in (r.missing_reason or "")

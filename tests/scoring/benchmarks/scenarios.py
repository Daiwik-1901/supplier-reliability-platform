"""Benchmark scenarios (Task 12b): controlled quarterly evidence for Q1-Q4 2025.

Each scenario is four QuarterSpecs, one per calendar quarter of 2025 (Ruling R30: scored with
explicit quarter windows so each quarter's evidence maps 1:1 to its expected score). The
hand-calculated expectations live in expected.yaml.
"""

from dataclasses import replace
from datetime import UTC, datetime

from supplier_reliability.domain.enums import CertificationType, ComplianceState
from supplier_reliability.services.scoring.evidence import quarter_windows

from .builders import QuarterSpec, SignalSpec

SUPPLIER = "SUP-000001"
WINDOWS = quarter_windows(
    datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
)
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]
FREE_CAPACITY = 2000  # verified free units per month


def invoices(overbilling_pct: int) -> tuple[tuple[str, str], ...]:
    """20 invoices of expected 1000.00, each overbilled by `overbilling_pct` percent."""
    return (("1000.00", f"{1000 + 10 * overbilling_pct}.00"),) * 20


def certs(iso9001: ComplianceState = ComplianceState.COMPLIANT):
    return {
        CertificationType.ISO9001: iso9001,
        CertificationType.ISO14001: ComplianceState.COMPLIANT,
    }


def quarter(
    on_time: int,
    late_days: int,
    defects: int,
    overbilling_pct: int,
    util_tenths: int,
    iso9001: ComplianceState = ComplianceState.COMPLIANT,
    signals: tuple[SignalSpec, ...] = (),
) -> QuarterSpec:
    """20 deliveries, 2,000 inspected; demand = util x FREE_CAPACITY per month."""
    return QuarterSpec(
        on_time=on_time,
        late_days=late_days,
        inspected=2000,
        defects=defects,
        invoice_pairs=invoices(overbilling_pct),
        monthly_demand=FREE_CAPACITY * util_tenths // 10,
        free_capacity=FREE_CAPACITY,
        cert_states=certs(iso9001),
        signals=signals,
    )


EXPIRED = ComplianceState.EXPIRED
# HIGH open signal for deteriorating: 45 days old at the Q3 cutoff, 137 at Q4's.
DETERIORATING_SIGNAL = SignalSpec(observed=datetime(2025, 8, 16, 23, 59, 59, tzinfo=UTC))
# HIGH signal for recovering: observed in Q2 (45 days old at Q2's cutoff), resolved in Q3.
RECOVERING_SIGNAL = SignalSpec(
    observed=datetime(2025, 5, 16, 23, 59, 59, tzinfo=UTC),
    resolved_at=datetime(2025, 8, 15, 12, tzinfo=UTC),
)

STABLE_Q = quarter(on_time=19, late_days=2, defects=10, overbilling_pct=0, util_tenths=6)

SCENARIOS: dict[str, list[QuarterSpec]] = {
    "stable": [STABLE_Q] * 4,
    "deteriorating": [
        quarter(19, 3, 10, 0, 7),
        quarter(16, 5, 40, 2, 9),
        quarter(13, 8, 80, 4, 11, signals=(DETERIORATING_SIGNAL,)),
        quarter(10, 10, 120, 6, 13, iso9001=EXPIRED),
    ],
    "recovering": [
        quarter(10, 10, 120, 6, 13, iso9001=EXPIRED),
        quarter(13, 8, 80, 4, 11, signals=(RECOVERING_SIGNAL,)),
        quarter(16, 5, 40, 2, 9),
        quarter(19, 3, 10, 0, 7),
    ],
}

# One-factor perturbations of the stable Q1 evidence (component name -> perturbed spec).
ONE_FACTOR: dict[str, QuarterSpec] = {
    "delivery": replace(STABLE_Q, late_days=STABLE_Q.late_days + 5),
    "quality": replace(STABLE_Q, defects=STABLE_Q.defects + 20),  # +1 % of 2,000
    "cost": replace(STABLE_Q, invoice_pairs=invoices(3)),
    "capacity": replace(STABLE_Q, monthly_demand=FREE_CAPACITY * 12 // 10),  # util 1.2
    "compliance": replace(STABLE_Q, cert_states=certs(EXPIRED)),
    "disruption": replace(
        STABLE_Q, signals=(SignalSpec(observed=datetime(2025, 2, 14, 23, 59, 59, tzinfo=UTC)),)
    ),
}

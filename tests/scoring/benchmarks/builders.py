"""Build a controlled quarterly supplier history from QuarterSpecs (Task 12b).

`build_supplier_history(session, supplier_id, quarters)` inserts real source records through
the Task 9 GraphBuilder for consecutive calendar quarters of BENCHMARK_YEAR (quarters[0] = Q1).
It flushes but does not commit (the caller owns the transaction). Layout per quarter, chosen so
the hand calculation is exact:

- Deliveries: `deliveries` shipments split over the quarter's three months (n*3//N rule: 7/7/6
  for 20), shipment j of a month expected on day 10 + j at 12:00 UTC. The first `on_time` arrive
  exactly on time; the rest arrive exactly `late_days` whole days late (>= 2, beyond the 24 h
  grace). ARRIVED events are recorded 1 h after arrival.
- Each shipment has one line of `inspected / deliveries` units (ordered = allocated = received)
  on that month's PO (ordered day 2); one INITIAL inspection per line 6 h after arrival, all
  units inspected, defects spread as evenly as possible (the first `defects % N` lines get one
  extra).
- invoice_pairs[i] (expected, invoiced) is the invoice of shipment i, dated the day after arrival.
- Capacity: per month a top-up PO line makes the month's ordered demand exactly
  `monthly_demand`; one VERIFIED supplier-wide declaration per month with available =
  `free_capacity`, committed 0, recorded on the 1st. util = monthly_demand / free_capacity.
- Compliance: one mandatory SUPPLIER requirement per certification type in any cert_states
  (on the open-ended contract). States are COMPLIANT or EXPIRED. A COMPLIANT run followed by an
  EXPIRED run is one row whose valid_to is day 45 of the first EXPIRED quarter (so COMPLIANT at
  the earlier cutoffs, EXPIRED at later ones). An EXPIRED first quarter is a row valid until day
  45 of that quarter; a later COMPLIANT run is a renewal row recorded (and valid from) day 15 of
  its first quarter that supersedes the previous row.
- Disruption: one MONITORING_ATTESTATION (severity NONE) on day 21 of the quarter at 12:00, plus
  the spec's signals (supplier-wide, recorded 1 h after observed; resolved_at set = RESOLVED with
  valid_to = resolved_at).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import (
    CertificationType,
    ComplianceState,
    DeliveryEventType,
    ResolutionStatus,
    RiskSignalType,
    Severity,
)

from ..factories import GraphBuilder

BENCHMARK_YEAR = 2025
UNIT = "units/month"
UNIT_PRICE = Decimal("10.00")
_FAR_FUTURE = date(2026, 12, 31)


@dataclass(frozen=True)
class SignalSpec:
    observed: datetime
    severity: Severity = Severity.HIGH
    resolved_at: datetime | None = None  # None: OPEN; else RESOLVED with valid_to = resolved_at
    signal_type: RiskSignalType = RiskSignalType.FACTORY_INCIDENT


@dataclass(frozen=True)
class QuarterSpec:
    on_time: int
    late_days: int
    inspected: int
    defects: int
    invoice_pairs: Sequence[tuple[str, str]]
    monthly_demand: int
    free_capacity: int
    cert_states: Mapping[CertificationType, ComplianceState]
    signals: Sequence[SignalSpec] = field(default_factory=tuple)
    deliveries: int = 20


def _quarter_start(k: int) -> date:
    return date(BENCHMARK_YEAR, 3 * k + 1, 1)


def _month(k: int, offset: int) -> date:
    return date(BENCHMARK_YEAR, 3 * k + 1 + offset, 1)


def _validate(spec: QuarterSpec) -> None:
    n = spec.deliveries
    if not 0 <= spec.on_time <= n:
        raise ValueError(f"on_time {spec.on_time} outside [0, {n}]")
    if spec.on_time < n and spec.late_days < 2:
        raise ValueError("late_days must be >= 2 (beyond the 24 h grace)")
    if spec.inspected % n:
        raise ValueError(f"inspected {spec.inspected} is not divisible by {n} deliveries")
    if len(spec.invoice_pairs) > n:
        raise ValueError("more invoices than shipments")


def build_supplier_history(
    session: Session,
    supplier_id: str,
    quarters: list[QuarterSpec],
    *,
    graph: GraphBuilder | None = None,
) -> None:
    """Insert the supplier, its site/contract and every quarter's records (see module doc).
    Pass `graph` to keep using the same GraphBuilder (id sequences) afterwards."""
    if not 1 <= len(quarters) <= 4:
        raise ValueError("between 1 and 4 quarters (Q1..Q4 of the benchmark year)")
    for spec in quarters:
        _validate(spec)
    g = graph or GraphBuilder(session)
    sup = g.supplier_graph(supplier_id=supplier_id)
    for k, spec in enumerate(quarters):
        _quarter(g, sup.supplier_id, sup.site_id, sup.contract_id, k, spec)
    _compliance(g, sup.supplier_id, sup.contract_id, quarters)


def _quarter(g: GraphBuilder, supplier_id: str, site_id: str, contract_id: str, k: int,
             spec: QuarterSpec) -> None:  # fmt: skip
    n = spec.deliveries
    per_line = Decimal(spec.inspected // n)
    month_of = [i * 3 // n for i in range(n)]
    pos = [
        g.purchase_order(
            supplier_id=supplier_id, contract_id=contract_id, order_date=_month(k, m) + timedelta(1)
        )
        for m in range(3)
    ]
    seen = [0, 0, 0]
    for i in range(n):
        m = month_of[i]
        first = _month(k, m)
        expected = datetime(first.year, first.month, 10 + seen[m], 12, tzinfo=UTC)
        seen[m] += 1
        late = i >= spec.on_time
        arrival = expected + timedelta(days=spec.late_days if late else 0)
        line = g.po_line(po_id=pos[m], ordered_quantity=per_line, unit_price=UNIT_PRICE,
                         unit_of_measure=UNIT)  # fmt: skip
        shipment = g.shipment(po_id=pos[m], site_id=site_id, expected=expected)
        shipment_line = g.allocation(shipment_id=shipment, po_line_id=line, quantity=per_line)
        g.event(shipment_id=shipment, event_type=DeliveryEventType.ARRIVED, event_timestamp=arrival)
        g.inspection(
            shipment_line_id=shipment_line,
            inspection_timestamp=arrival + timedelta(hours=6),
            inspected=per_line,
            defect=Decimal(spec.defects // n + (i < spec.defects % n)),
        )
        if i < len(spec.invoice_pairs):
            expected_amount, invoiced = spec.invoice_pairs[i]
            g.invoice(shipment_id=shipment, invoice_date=(arrival + timedelta(days=1)).date(),
                      expected=expected_amount, invoiced=invoiced)  # fmt: skip

    for m in range(3):
        top_up = Decimal(spec.monthly_demand) - per_line * seen[m]
        if top_up < 0:
            raise ValueError(f"monthly_demand {spec.monthly_demand} below the shipped lines")
        if top_up:
            g.po_line(po_id=pos[m], ordered_quantity=top_up, unit_price=UNIT_PRICE,
                      unit_of_measure=UNIT)  # fmt: skip
        first = _month(k, m)
        g.capacity(supplier_id=supplier_id, month=first, available=Decimal(spec.free_capacity),
                   recorded_at=datetime(first.year, first.month, 1, tzinfo=UTC))  # fmt: skip

    q_start = _quarter_start(k)
    g.risk_signal(
        supplier_id=supplier_id,
        observed=datetime(q_start.year, q_start.month, 21, 12, tzinfo=UTC),
        signal_type=RiskSignalType.MONITORING_ATTESTATION,
        severity=Severity.NONE,
    )
    for s in spec.signals:
        g.risk_signal(
            supplier_id=supplier_id,
            observed=s.observed,
            signal_type=s.signal_type,
            severity=s.severity,
            resolution=ResolutionStatus.OPEN
            if s.resolved_at is None
            else ResolutionStatus.RESOLVED,
            valid_to=s.resolved_at,
        )


def _compliance(g: GraphBuilder, supplier_id: str, contract_id: str,
                quarters: list[QuarterSpec]) -> None:  # fmt: skip
    types = sorted({t for spec in quarters for t in spec.cert_states})
    for cert_type in types:
        g.requirement(contract_id=contract_id, certification_type=cert_type, mandatory=True)
        states = [spec.cert_states.get(cert_type) for spec in quarters]
        if None in states:
            raise ValueError(f"{cert_type}: a state is needed in every quarter")
        if set(states) - {ComplianceState.COMPLIANT, ComplianceState.EXPIRED}:
            raise ValueError(f"{cert_type}: only COMPLIANT / EXPIRED are supported")
        previous: str | None = None
        for k, state in enumerate(states):
            if k and state == states[k - 1]:
                continue
            if state == ComplianceState.EXPIRED and k:
                continue  # the previous COMPLIANT row's valid_to already falls in this quarter
            expiry = next(
                (
                    _quarter_start(j) + timedelta(days=44)
                    for j in range(k, len(states))
                    if states[j] == ComplianceState.EXPIRED
                ),
                _FAR_FUTURE,
            )
            renewal = _quarter_start(k) + timedelta(days=14)
            previous = g.certification(
                supplier_id=supplier_id,
                certification_type=cert_type,
                valid_from=date(2024, 1, 1) if k == 0 else renewal,
                valid_to=expiry,
                recorded_at=(
                    datetime(2024, 1, 1, tzinfo=UTC)
                    if k == 0
                    else datetime(renewal.year, renewal.month, renewal.day, tzinfo=UTC)
                ),
                supersedes=previous,
            )

"""Cutoff-aware scoring evidence (Task 9): what was knowable at cutoff C for window [S, E].

Eligibility (D5): a record counts only if its occurrence time is in [S, min(E, C)] and its
recording time is <= C. Interpretations fixed here (documented, tested):

- Delivery (D4, D13, R12): due = expected_delivery_timestamp in [S, min(E, C)]; CANCELLED
  shipments are excluded ("cancelled"); shipments expected in (min(E, C), E] are "not_yet_due".
  The arrival known at C is derived from DeliveryEvent rows only (never
  Shipment.actual_delivery_timestamp): the ARRIVED event with event_timestamp <= C and
  recorded_timestamp <= C (the first recorded, if several), its timestamp replaced by the
  corrected_value of the latest CORRECTION (recorded <= C) whose corrects_event_id names it.
  If that correction moves the arrival after C, the arrival is unknown at C (overdue). ARRIVED
  and CORRECTION events of due shipments with event_timestamp <= C but recorded after C are
  listed as excluded ("recorded_after_cutoff").
- Quality (D2): inspections with inspection_timestamp in [S, min(E, C)] and recorded_at <= C,
  reduced to the latest per shipment_line by (inspection_timestamp, inspection_id).
- Cost: invoice_date is a DATE, so it is compared with the dates of S and min(E, C);
  recorded_at <= C is still required.
- Demand (D12): lines of non-CANCELLED POs with order_date (a DATE) in [date(S),
  date(min(E, C))], PO and line created_at <= C; month = first day of the order month.
- Capacity (D12): declarations recorded <= C overlapping the window's months, one CapacityObs
  per declaration per covered window month. Per supplier and month, site-level rows (any
  verification status) override supplier-wide rows ("capacity_scope_overridden"). UNVERIFIED
  rows are kept with verified=False; the component decides.
- Compliance (D11): requirements with effective_from <= date(C) <= effective_to (or open) and
  created_at <= C, on non-DRAFT contracts effective at date(C). The current certification per
  (supplier, type, site-or-None) is the latest row recorded <= C (by recorded_at, then id) not
  superseded by another row recorded <= C. SITE requirements match only that site's
  certification; SUPPLIER requirements match only a supplier-wide one (site_id NULL). A
  VERIFIED+COMPLIANT certification whose valid_from is after date(C) is not yet valid: UNKNOWN.
- Disruption (R14): signals with observed in [C - lookback_days, C] and recorded <= C. Older
  signals observed inside [S, C - lookback) are listed "signal_outside_lookback".
  site_active = the site's effective period contains date(C) (an open-ended period also
  requires site_status ACTIVE); True when site_id is NULL. resolved_at_cutoff = RESOLVED and
  valid_to <= C. Quarantined signals never reach canonical tables, so nothing to filter.
- open_critical_exception_ids: CRITICAL data_exceptions linked to the supplier whose
  detection_status is not DISMISSED, regardless of C (detected_at is processing time; they flag
  data trust for the supplier).
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import (
    CapacityVerification,
    CertificationStatus,
    CertificationType,
    ComplianceState,
    ComponentName,
    DeliveryEventType,
    RequirementScope,
    ResolutionStatus,
    RiskSignalType,
    Severity,
    ShipmentStatus,
    SiteStatus,
    VerificationStatus,
)
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.services.scoring import evidence_queries as q

# --- window ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationWindow:
    """Window [start, end] evaluated with data cutoff `cutoff` (all aware, normalised to UTC)."""

    start: datetime
    end: datetime
    cutoff: datetime

    def __post_init__(self) -> None:
        for name in ("start", "end", "cutoff"):
            object.__setattr__(self, name, ensure_utc(getattr(self, name)))
        if self.end < self.start:
            raise ValueError(f"window end {self.end} is before start {self.start}")
        if self.cutoff < self.start:
            raise ValueError(f"cutoff {self.cutoff} is before window start {self.start}")

    @property
    def occurrence_end(self) -> datetime:
        """min(end, cutoff): the last instant an eligible record may have occurred."""
        return min(self.end, self.cutoff)


def quarter_windows(reference_start: datetime, reference_end: datetime) -> list[EvaluationWindow]:
    """Calendar quarters overlapping [reference_start, reference_end]; each ends at its last
    second (clipped to reference_end) and has cutoff = end."""
    start, end = ensure_utc(reference_start), ensure_utc(reference_end)
    out: list[EvaluationWindow] = []
    q_start = datetime(start.year, 3 * ((start.month - 1) // 3) + 1, 1, tzinfo=UTC)
    while q_start <= end:
        year, month = q_start.year + (q_start.month == 10), (q_start.month + 2) % 12 + 1
        nxt = datetime(year, month, 1, tzinfo=UTC)
        q_end = min(nxt - timedelta(seconds=1), end)
        out.append(EvaluationWindow(start=max(q_start, start), end=q_end, cutoff=q_end))
        q_start = nxt
    return out


# --- evidence records -----------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryObs:
    shipment_id: str
    expected: datetime
    arrived_known: datetime | None  # effective arrival known at C, or None (overdue)


@dataclass(frozen=True)
class InspectionObs:
    inspection_id: str
    shipment_line_id: str
    inspected: Decimal
    defect: Decimal


@dataclass(frozen=True)
class InvoiceObs:
    invoice_id: str
    expected: Decimal
    invoiced: Decimal


@dataclass(frozen=True)
class DemandObs:
    po_line_id: str
    month: date
    quantity: Decimal
    unit_of_measure: str


@dataclass(frozen=True)
class CapacityObs:
    capacity_declaration_id: str
    site_id: str | None
    month: date
    available: Decimal
    committed: Decimal
    verified: bool


@dataclass(frozen=True)
class ComplianceObs:
    requirement_id: str
    certification_type: CertificationType
    mandatory: bool
    scope: RequirementScope
    site_id: str | None
    certification_id: str | None
    state: ComplianceState


@dataclass(frozen=True)
class RiskObs:
    risk_signal_id: str
    site_id: str | None
    site_active: bool
    signal_type: RiskSignalType
    severity: Severity
    observed: datetime
    resolution: ResolutionStatus
    dedup_key: str
    resolved_at_cutoff: bool


@dataclass(frozen=True)
class ExcludedRecord:
    source_entity_type: str
    source_record_id: str
    component: ComponentName
    reason: str


@dataclass(frozen=True)
class EvidenceBundle:
    supplier_id: str
    window: EvaluationWindow
    deliveries: list[DeliveryObs] = field(default_factory=list)
    inspections: list[InspectionObs] = field(default_factory=list)
    invoices: list[InvoiceObs] = field(default_factory=list)
    demand: list[DemandObs] = field(default_factory=list)
    capacities: list[CapacityObs] = field(default_factory=list)
    compliance: list[ComplianceObs] = field(default_factory=list)
    risks: list[RiskObs] = field(default_factory=list)
    excluded: list[ExcludedRecord] = field(default_factory=list)
    open_critical_exception_ids: list[str] = field(default_factory=list)


# --- collection -----------------------------------------------------------------------------

_C = ComponentName
_Lists = dict[str, list[Any]]


def collect_evidence(
    session: Session,
    supplier_ids: Sequence[str] | None,
    window: EvaluationWindow,
    *,
    lookback_days: int,
) -> dict[str, EvidenceBundle]:
    """Eligible evidence per supplier at `window.cutoff` (see module docstring).

    `supplier_ids=None` means every supplier. Unknown ids raise ValueError. Every requested
    supplier gets a bundle, possibly empty. `lookback_days` is `scoring.lookback_days`.
    Runs a fixed number of set-based statements regardless of the number of suppliers.
    """
    ids = None if supplier_ids is None else sorted(set(supplier_ids))
    known = q.existing_supplier_ids(session, ids)
    if ids is not None and len(known) != len(ids):
        raise ValueError(f"unknown supplier ids: {sorted(set(ids) - set(known))}")
    acc: dict[str, _Lists] = {sid: defaultdict(list) for sid in known}

    _deliveries(session, ids, window, acc)
    _inspections(session, ids, window, acc)
    _invoices(session, ids, window, acc)
    _demand(session, ids, window, acc)
    _capacities(session, ids, window, acc)
    _compliance(session, ids, window, acc)
    _risks(session, ids, window, lookback_days, acc)
    for row in q.open_critical_exceptions(session, ids):
        acc[row.supplier_id]["open_critical_exception_ids"].append(row.exception_id)

    return {sid: _bundle(sid, window, lists) for sid, lists in acc.items()}


def _bundle(sid: str, window: EvaluationWindow, lists: _Lists) -> EvidenceBundle:
    def by(key, name: str) -> list[Any]:
        return sorted(lists[name], key=key)

    return EvidenceBundle(
        supplier_id=sid,
        window=window,
        deliveries=by(lambda o: o.shipment_id, "deliveries"),
        inspections=by(lambda o: o.inspection_id, "inspections"),
        invoices=by(lambda o: o.invoice_id, "invoices"),
        demand=by(lambda o: o.po_line_id, "demand"),
        capacities=by(lambda o: (o.capacity_declaration_id, o.month), "capacities"),
        compliance=by(lambda o: o.requirement_id, "compliance"),
        risks=by(lambda o: o.risk_signal_id, "risks"),
        excluded=sorted(
            set(lists["excluded"]),
            key=lambda x: (x.component, x.source_entity_type, x.source_record_id, x.reason),
        ),
        open_critical_exception_ids=sorted(lists["open_critical_exception_ids"]),
    )


def _exclude(acc: _Lists, entity: str, record_id: str, component: ComponentName, reason: str):
    acc["excluded"].append(ExcludedRecord(entity, record_id, component, reason))


def _deliveries(session: Session, ids, w: EvaluationWindow, acc: dict[str, _Lists]) -> None:
    c, top = w.cutoff, w.occurrence_end
    due: dict[str, tuple[str, datetime]] = {}  # shipment_id -> (supplier_id, expected)
    for r in q.shipments(session, ids, w.start, w.end):
        if r.shipment_status == ShipmentStatus.CANCELLED:
            _exclude(acc[r.supplier_id], "shipment", r.shipment_id, _C.delivery, "cancelled")
        elif r.expected_delivery_timestamp > top:
            _exclude(acc[r.supplier_id], "shipment", r.shipment_id, _C.delivery, "not_yet_due")
        else:
            due[r.shipment_id] = (r.supplier_id, r.expected_delivery_timestamp)

    events: dict[str, list[Any]] = defaultdict(list)
    for e in q.arrival_events(session, ids, w.start, top):
        if e.shipment_id not in due:
            continue  # cancelled
        if e.recorded_timestamp <= c:
            events[e.shipment_id].append(e)
        elif e.event_timestamp <= c:  # happened by C, but not known at C
            sup = due[e.shipment_id][0]
            _exclude(acc[sup], "delivery_event", e.delivery_event_id, _C.delivery,
                     "recorded_after_cutoff")  # fmt: skip

    for shipment_id, (sup, expected) in due.items():
        arrived = _known_arrival(events.get(shipment_id, []), c)
        acc[sup]["deliveries"].append(DeliveryObs(shipment_id, expected, arrived))


def _known_arrival(events: list[Any], cutoff: datetime) -> datetime | None:
    """R12: effective ARRIVED timestamp at the cutoff from events all recorded <= cutoff."""
    key = lambda e: (e.recorded_timestamp, e.delivery_event_id)  # noqa: E731
    arrivals = sorted(
        (
            e
            for e in events
            if e.event_type == DeliveryEventType.ARRIVED and e.event_timestamp <= cutoff
        ),
        key=key,
    )
    if not arrivals:
        return None
    arrived = arrivals[0]
    corrections = sorted(
        (
            e
            for e in events
            if e.event_type == DeliveryEventType.CORRECTION
            and (e.event_metadata or {}).get("corrects_event_id") == arrived.delivery_event_id
            and (e.event_metadata or {}).get("field") == "event_timestamp"
        ),
        key=key,
    )
    when = arrived.event_timestamp
    if corrections:
        when = ensure_utc(datetime.fromisoformat(corrections[-1].event_metadata["corrected_value"]))
    return when if when <= cutoff else None


def _inspections(session: Session, ids, w: EvaluationWindow, acc: dict[str, _Lists]) -> None:
    latest: dict[str, Any] = {}  # shipment_line_id -> row
    for r in sorted(
        q.inspections(session, ids, w.start, w.occurrence_end),
        key=lambda r: (r.inspection_timestamp, r.inspection_id),
    ):
        if r.recorded_at > w.cutoff:
            _exclude(acc[r.supplier_id], "inspection", r.inspection_id, _C.quality,
                     "inspection_recorded_after_cutoff")  # fmt: skip
            continue
        previous = latest.get(r.shipment_line_id)
        if previous is not None:
            _exclude(acc[r.supplier_id], "inspection", previous.inspection_id, _C.quality,
                     "superseded_inspection")  # fmt: skip
        latest[r.shipment_line_id] = r
    for r in latest.values():
        acc[r.supplier_id]["inspections"].append(
            InspectionObs(
                r.inspection_id, r.shipment_line_id, r.inspected_quantity, r.defect_quantity
            )
        )


def _invoices(session: Session, ids, w: EvaluationWindow, acc: dict[str, _Lists]) -> None:
    for r in q.invoices(session, ids, w.start.date(), w.occurrence_end.date()):
        if r.recorded_at > w.cutoff:
            _exclude(acc[r.supplier_id], "invoice", r.invoice_id, _C.cost,
                     "invoice_recorded_after_cutoff")  # fmt: skip
        else:
            acc[r.supplier_id]["invoices"].append(
                InvoiceObs(r.invoice_id, r.expected_amount, r.invoiced_amount)
            )


def _demand(session: Session, ids, w: EvaluationWindow, acc: dict[str, _Lists]) -> None:
    for r in q.demand_lines(session, ids, w.start.date(), w.occurrence_end.date(), w.cutoff):
        acc[r.supplier_id]["demand"].append(
            DemandObs(
                r.po_line_id, r.order_date.replace(day=1), r.ordered_quantity, r.unit_of_measure
            )
        )


def _months(first: date, last: date) -> list[date]:
    """First days of every month from first's month to last's month inclusive."""
    out, m = [], first.replace(day=1)
    while m <= last:
        out.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def _capacities(session: Session, ids, w: EvaluationWindow, acc: dict[str, _Lists]) -> None:
    first, last = w.start.date().replace(day=1), w.occurrence_end.date()
    window_months = _months(first, last)
    # (supplier, month) -> {"site": [obs], "wide": [obs]}
    cells: dict[tuple[str, date], dict[str, list[CapacityObs]]] = defaultdict(
        lambda: {"site": [], "wide": []}
    )
    for r in q.capacity_declarations(session, ids, first, last):
        if r.recorded_at > w.cutoff:
            _exclude(acc[r.supplier_id], "capacity_declaration", r.capacity_declaration_id,
                     _C.capacity, "capacity_recorded_after_cutoff")  # fmt: skip
            continue
        for month in window_months:
            if r.period_start.replace(day=1) <= month <= r.period_end:
                obs = CapacityObs(
                    r.capacity_declaration_id,
                    r.site_id,
                    month,
                    r.available_capacity,
                    r.committed_capacity,
                    r.verification_status == CapacityVerification.VERIFIED,
                )
                cells[(r.supplier_id, month)]["site" if r.site_id else "wide"].append(obs)
    for (sup, _month), cell in cells.items():
        if cell["site"]:
            acc[sup]["capacities"].extend(cell["site"])
            for obs in cell["wide"]:
                _exclude(acc[sup], "capacity_declaration", obs.capacity_declaration_id,
                         _C.capacity, "capacity_scope_overridden")  # fmt: skip
        else:
            acc[sup]["capacities"].extend(cell["wide"])


def _compliance_state(cert: Any, day: date) -> ComplianceState:
    if cert is None:
        return ComplianceState.UNKNOWN
    if cert.verification_status == VerificationStatus.PENDING_VERIFICATION:
        return ComplianceState.PENDING_VERIFICATION
    if cert.verification_status == VerificationStatus.REJECTED:
        return ComplianceState.NONCOMPLIANT
    if cert.compliance_status == CertificationStatus.NONCOMPLIANT:
        return ComplianceState.NONCOMPLIANT
    if cert.valid_to < day:
        return ComplianceState.EXPIRED
    if cert.valid_from <= day:
        return ComplianceState.COMPLIANT
    return ComplianceState.UNKNOWN  # not yet valid at C


def _compliance(session: Session, ids, w: EvaluationWindow, acc: dict[str, _Lists]) -> None:
    rows = q.certifications(session, ids, w.cutoff)  # all recorded <= C
    superseded = {r.supersedes_certification_id for r in rows if r.supersedes_certification_id}
    current: dict[tuple[str, str, str | None], Any] = {}
    for r in sorted(rows, key=lambda r: (r.recorded_at, r.certification_id)):
        key = (r.supplier_id, r.certification_type, r.site_id)
        if r.certification_id in superseded:
            _exclude(acc[r.supplier_id], "supplier_certification", r.certification_id,
                     _C.compliance, "certification_superseded")  # fmt: skip
            continue
        if key in current:  # an older, never-superseded row of the same key
            _exclude(acc[r.supplier_id], "supplier_certification",
                     current[key].certification_id, _C.compliance,
                     "certification_superseded")  # fmt: skip
        current[key] = r
    day = w.cutoff.date()
    for req in q.requirements(session, ids, w.cutoff):
        site = req.site_id if req.scope == RequirementScope.SITE else None
        cert = current.get((req.supplier_id, req.certification_type, site))
        acc[req.supplier_id]["compliance"].append(
            ComplianceObs(
                req.requirement_id,
                req.certification_type,
                req.mandatory,
                req.scope,
                req.site_id,
                None if cert is None else cert.certification_id,
                _compliance_state(cert, day),
            )
        )


def _site_active(r: Any, day: date) -> bool:
    if r.site_id is None:
        return True
    if r.site_effective_from > day:
        return False
    if r.site_effective_to is None:
        return r.site_status == SiteStatus.ACTIVE
    return day <= r.site_effective_to


def _risks(
    session: Session, ids, w: EvaluationWindow, lookback_days: int, acc: dict[str, _Lists]
) -> None:
    c = w.cutoff
    horizon = c - timedelta(days=lookback_days)
    for r in q.risk_signals(session, ids, min(w.start, horizon), c):
        sup = acc[r.supplier_id]
        if r.observed_timestamp < horizon:
            if r.observed_timestamp >= w.start:
                _exclude(sup, "risk_signal", r.risk_signal_id, _C.disruption,
                         "signal_outside_lookback")  # fmt: skip
            continue
        if r.recorded_timestamp > c:
            _exclude(sup, "risk_signal", r.risk_signal_id, _C.disruption,
                     "signal_recorded_after_cutoff")  # fmt: skip
            continue
        resolved = (
            r.resolution_status == ResolutionStatus.RESOLVED
            and r.valid_to is not None
            and r.valid_to <= c
        )
        sup["risks"].append(
            RiskObs(
                r.risk_signal_id,
                r.site_id,
                _site_active(r, c.date()),
                r.signal_type,
                r.severity,
                r.observed_timestamp,
                r.resolution_status,
                r.dedup_key,
                resolved,
            )
        )

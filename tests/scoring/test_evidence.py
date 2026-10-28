"""Task 9: cutoff-aware evidence collection (D2, D5, D11, D12, D13; rulings R12, R14)."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from supplier_reliability.database import make_engine, make_session_factory, session_scope
from supplier_reliability.domain.enums import (
    CertificationStatus,
    CertificationType,
    ComplianceState,
    ComponentName,
    DeliveryEventType,
    DetectionStatus,
    InspectionKind,
    POStatus,
    RequirementScope,
    ResolutionStatus,
    RiskSignalType,
    Severity,
    ShipmentStatus,
    SiteStatus,
    VerificationStatus,
)
from supplier_reliability.models import (
    CapacityDeclaration,
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RiskSignal,
    Shipment,
    ShipmentLineAllocation,
    Supplier,
    SupplierCertification,
)
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import load_dataset
from supplier_reliability.services.scoring.evidence import (
    CapacityObs,
    EvaluationWindow,
    EvidenceBundle,
    ExcludedRecord,
    collect_evidence,
    quarter_windows,
)

from .factories import GraphBuilder, utc

LOOKBACK = 180  # = scoring.lookback_days in config/default.yaml (asserted below)
S = utc(2025, 1, 1)
E = utc(2025, 3, 31, 23, 59, 59)
ARRIVED = DeliveryEventType.ARRIVED


def q1(cutoff: datetime = E) -> EvaluationWindow:
    return EvaluationWindow(start=S, end=E, cutoff=cutoff)


def collect(session, ids, window) -> dict[str, EvidenceBundle]:
    return collect_evidence(session, ids, window, lookback_days=LOOKBACK)


def one(session, sid, window=None) -> EvidenceBundle:
    return collect(session, [sid], window or q1())[sid]


def _shipment(g: GraphBuilder, sup, expected: datetime, **kw) -> str:
    po = g.purchase_order(
        supplier_id=sup.supplier_id, contract_id=sup.contract_id, order_date=date(2024, 12, 1)
    )
    line = g.po_line(po_id=po)
    shp = g.shipment(po_id=po, site_id=sup.site_id, expected=expected, **kw)
    g.allocation(shipment_id=shp, po_line_id=line)
    return shp


def _line(g: GraphBuilder, sup) -> str:
    """A shipment line (fresh PO + shipment due 2025-02-01); returns the shipment_line_id."""
    po = g.purchase_order(
        supplier_id=sup.supplier_id, contract_id=sup.contract_id, order_date=date(2024, 12, 1)
    )
    line = g.po_line(po_id=po)
    shp = g.shipment(po_id=po, site_id=sup.site_id, expected=utc(2025, 2, 1))
    return g.allocation(shipment_id=shp, po_line_id=line)


def _excluded(bundle: EvidenceBundle) -> set[tuple[str, str, str]]:
    return {(x.source_record_id, x.component, x.reason) for x in bundle.excluded}


def test_lookback_constant_matches_config(app_config):
    assert app_config.scoring.lookback_days == LOOKBACK


# --- window ---------------------------------------------------------------------------------


def test_window_validates_cutoff_and_timezone():
    with pytest.raises(ValueError):
        EvaluationWindow(start=S, end=E, cutoff=S - timedelta(seconds=1))
    with pytest.raises(ValueError):
        EvaluationWindow(start=datetime(2025, 1, 1), end=E, cutoff=E)
    with pytest.raises(ValueError):
        EvaluationWindow(start=E, end=S, cutoff=E)
    plus2 = timezone(timedelta(hours=2))
    w = EvaluationWindow(start=datetime(2025, 1, 1, 2, tzinfo=plus2), end=E, cutoff=E)
    assert w.start == S and w.start.tzinfo == UTC


def test_quarter_windows_cover_reference_period():
    ws = quarter_windows(utc(2025, 1, 1), utc(2026, 6, 30, 23, 59, 59))
    assert len(ws) == 6
    assert ws[0] == EvaluationWindow(start=S, end=E, cutoff=E)
    assert ws[-1].start == utc(2026, 4, 1) and ws[-1].end == utc(2026, 6, 30, 23, 59, 59)
    assert all(w.cutoff == w.end for w in ws)
    assert all(a.end + timedelta(seconds=1) == b.start for a, b in zip(ws, ws[1:], strict=False))


# --- delivery -------------------------------------------------------------------------------


def test_shipment_not_yet_due_excluded(graph, db_session):
    sup = graph.supplier_graph()
    due = _shipment(graph, sup, utc(2025, 2, 1))
    later = _shipment(graph, sup, utc(2025, 3, 1))
    b = one(db_session, sup.supplier_id, q1(cutoff=utc(2025, 2, 15)))
    assert [d.shipment_id for d in b.deliveries] == [due]
    assert (later, "delivery", "not_yet_due") in _excluded(b)


def test_due_shipment_without_arrival_counts_overdue(graph, db_session):
    sup = graph.supplier_graph()
    shp = _shipment(graph, sup, utc(2025, 2, 1))
    graph.event(shipment_id=shp, event_type=DeliveryEventType.DISPATCHED,
                event_timestamp=utc(2025, 1, 27))  # fmt: skip
    (obs,) = one(db_session, sup.supplier_id).deliveries
    assert obs.shipment_id == shp
    assert obs.expected == utc(2025, 2, 1)
    assert obs.arrived_known is None


def test_shipments_outside_window_and_cancelled_are_not_deliveries(graph, db_session):
    sup = graph.supplier_graph()
    _shipment(graph, sup, utc(2024, 12, 31, 23, 59, 59))
    _shipment(graph, sup, utc(2025, 4, 1))
    edge = _shipment(graph, sup, E)
    cancelled = _shipment(graph, sup, utc(2025, 2, 1), status=ShipmentStatus.CANCELLED)
    b = one(db_session, sup.supplier_id)
    assert [d.shipment_id for d in b.deliveries] == [edge]
    assert (cancelled, "delivery", "cancelled") in _excluded(b)


def test_arrival_recorded_after_cutoff_is_not_known(graph, db_session):
    sup = graph.supplier_graph()
    c = utc(2025, 3, 15)
    shp = _shipment(graph, sup, utc(2025, 3, 1))
    ev = graph.event(shipment_id=shp, event_type=ARRIVED, event_timestamp=c - timedelta(days=1),
                     recorded_timestamp=c + timedelta(days=1))  # fmt: skip
    b = one(db_session, sup.supplier_id, q1(cutoff=c))
    (obs,) = b.deliveries
    assert obs.arrived_known is None  # overdue at C, never on time
    assert (ev, "delivery", "recorded_after_cutoff") in _excluded(b)
    # restatement with a later cutoff sees the arrival
    (later,) = one(db_session, sup.supplier_id, q1(cutoff=c + timedelta(days=2))).deliveries
    assert later.arrived_known == c - timedelta(days=1)


def test_correction_recorded_before_cutoff_applied_after_ignored(graph, db_session):
    sup = graph.supplier_graph()
    shp = _shipment(graph, sup, utc(2025, 2, 1))
    ev = graph.event(shipment_id=shp, event_type=ARRIVED, event_timestamp=utc(2025, 2, 1, 6))
    graph.correction(shipment_id=shp, corrects_event_id=ev, corrected_value=utc(2025, 2, 3),
                     recorded_timestamp=utc(2025, 2, 10))  # fmt: skip
    late = graph.correction(shipment_id=shp, corrects_event_id=ev,
                            corrected_value=utc(2025, 2, 1, 1),
                            event_timestamp=utc(2025, 2, 14),
                            recorded_timestamp=utc(2025, 2, 20))  # fmt: skip
    b = one(db_session, sup.supplier_id, q1(cutoff=utc(2025, 2, 15)))
    assert b.deliveries[0].arrived_known == utc(2025, 2, 3)  # first correction applied
    assert (late, "delivery", "recorded_after_cutoff") in _excluded(b)
    assert one(db_session, sup.supplier_id).deliveries[0].arrived_known == utc(2025, 2, 1, 1)
    before = one(db_session, sup.supplier_id, q1(cutoff=utc(2025, 2, 5)))
    assert before.deliveries[0].arrived_known == utc(2025, 2, 1, 6)  # no correction known yet


def test_correction_moving_arrival_after_cutoff_makes_arrival_unknown(graph, db_session):
    sup = graph.supplier_graph()
    shp = _shipment(graph, sup, utc(2025, 2, 1))
    ev = graph.event(shipment_id=shp, event_type=ARRIVED, event_timestamp=utc(2025, 2, 1, 6))
    graph.correction(shipment_id=shp, corrects_event_id=ev, corrected_value=utc(2025, 2, 20),
                     recorded_timestamp=utc(2025, 2, 10))  # fmt: skip
    b = one(db_session, sup.supplier_id, q1(cutoff=utc(2025, 2, 15)))
    assert b.deliveries[0].arrived_known is None
    assert one(db_session, sup.supplier_id).deliveries[0].arrived_known == utc(2025, 2, 20)


def test_shipment_row_actual_delivery_is_ignored(graph, db_session):
    sup = graph.supplier_graph()
    shp = _shipment(graph, sup, utc(2025, 2, 1))
    db_session.get(Shipment, shp).actual_delivery_timestamp = utc(2025, 2, 1, 3)
    db_session.flush()
    assert one(db_session, sup.supplier_id).deliveries[0].arrived_known is None


# --- quality --------------------------------------------------------------------------------


def test_inspection_recorded_after_cutoff_excluded(graph, db_session):
    sup = graph.supplier_graph()
    sl = _line(graph, sup)
    c = utc(2025, 3, 1)
    ins = graph.inspection(shipment_line_id=sl, inspection_timestamp=utc(2025, 2, 27),
                           recorded_at=c + timedelta(hours=1))  # fmt: skip
    graph.inspection(shipment_line_id=_line(graph, sup), inspection_timestamp=utc(2025, 3, 2))
    b = one(db_session, sup.supplier_id, q1(cutoff=c))
    assert b.inspections == []
    assert (ins, "quality", "inspection_recorded_after_cutoff") in _excluded(b)
    assert [i.inspection_id for i in one(db_session, sup.supplier_id).inspections] != []


def test_latest_reinspection_replaces_initial(graph, db_session):
    sup = graph.supplier_graph()
    sl = _line(graph, sup)
    first = graph.inspection(shipment_line_id=sl, inspection_timestamp=utc(2025, 2, 2),
                             inspected="10", defect="4")  # fmt: skip
    re_ = graph.inspection(shipment_line_id=sl, inspection_timestamp=utc(2025, 2, 5),
                           inspected="10", defect="1", kind=InspectionKind.REINSPECTION,
                           recorded_at=utc(2025, 2, 20))  # fmt: skip
    (obs,) = one(db_session, sup.supplier_id).inspections
    assert (obs.inspection_id, obs.shipment_line_id) == (re_, sl)
    assert (obs.inspected, obs.defect) == (Decimal("10"), Decimal("1"))
    assert (first, "quality", "superseded_inspection") in _excluded(
        one(db_session, sup.supplier_id)
    )
    # before the re-inspection was recorded, the initial one is the latest eligible
    (early,) = one(db_session, sup.supplier_id, q1(cutoff=utc(2025, 2, 10))).inspections
    assert (early.inspection_id, early.defect) == (first, Decimal("4"))


def test_evidence_counts_do_not_multiply_across_joins(graph, db_session):
    sup = graph.supplier_graph()
    po = graph.purchase_order(supplier_id=sup.supplier_id, contract_id=sup.contract_id,
                              order_date=date(2025, 1, 5))  # fmt: skip
    pls = [graph.po_line(po_id=po, ordered_quantity="100") for _ in range(3)]
    shp = graph.shipment(po_id=po, site_id=sup.site_id, expected=utc(2025, 2, 1))
    sls = [graph.allocation(shipment_id=shp, po_line_id=pl, quantity="50") for pl in pls]
    graph.event(shipment_id=shp, event_type=ARRIVED, event_timestamp=utc(2025, 2, 1, 2))
    t = utc(2025, 2, 2)
    graph.inspection(shipment_line_id=sls[0], inspection_timestamp=t, inspected="20", defect="5")
    graph.inspection(shipment_line_id=sls[0], inspection_timestamp=t + timedelta(days=1),
                     inspected="30", defect="2", kind=InspectionKind.REINSPECTION)  # fmt: skip
    graph.inspection(shipment_line_id=sls[1], inspection_timestamp=t, inspected="40", defect="0")
    graph.inspection(shipment_line_id=sls[2], inspection_timestamp=t, inspected="50", defect="3")
    graph.invoice(shipment_id=shp, invoice_date=date(2025, 2, 3), expected="1500.00")
    b = one(db_session, sup.supplier_id)
    assert len(b.deliveries) == 1
    assert len(b.inspections) == 3
    assert len(b.invoices) == 1
    assert len(b.demand) == 3
    assert sum(i.inspected for i in b.inspections) == Decimal("120")  # 30 + 40 + 50
    assert sum(i.defect for i in b.inspections) == Decimal("5")
    assert sum(d.quantity for d in b.demand) == Decimal("300")
    assert b.invoices[0].expected == Decimal("1500.00")


# --- cost -----------------------------------------------------------------------------------


def test_invoice_window_and_recording(graph, db_session):
    sup = graph.supplier_graph()
    shp = _shipment(graph, sup, utc(2025, 2, 1))
    inside = graph.invoice(shipment_id=shp, invoice_date=date(2025, 3, 31),
                           expected="100.00", invoiced="110.00")  # fmt: skip
    graph.invoice(shipment_id=shp, invoice_date=date(2025, 4, 1))
    graph.invoice(shipment_id=shp, invoice_date=date(2024, 12, 31))
    late = graph.invoice(shipment_id=shp, invoice_date=date(2025, 3, 30),
                         recorded_at=utc(2025, 4, 2))  # fmt: skip
    b = one(db_session, sup.supplier_id)
    assert [(i.invoice_id, i.expected, i.invoiced) for i in b.invoices] == [
        (inside, Decimal("100.00"), Decimal("110.00"))
    ]
    assert (late, "cost", "invoice_recorded_after_cutoff") in _excluded(b)


# --- capacity / demand ----------------------------------------------------------------------


def test_demand_eligibility(graph, db_session):
    sup = graph.supplier_graph()

    def po_line(order_date, **kw):
        po = graph.purchase_order(supplier_id=sup.supplier_id, contract_id=sup.contract_id,
                                  order_date=order_date, **kw)  # fmt: skip
        return graph.po_line(po_id=po, ordered_quantity="7.5", unit_of_measure="units/month")

    ok = po_line(date(2025, 2, 14))
    po_line(date(2025, 2, 14), status=POStatus.CANCELLED)
    po_line(date(2025, 4, 1))
    po_line(date(2025, 3, 20), created_at=utc(2025, 3, 21))  # created after C below
    b = one(db_session, sup.supplier_id, q1(cutoff=utc(2025, 3, 20, 23)))
    (d,) = b.demand
    assert (d.po_line_id, d.month, d.quantity, d.unit_of_measure) == (
        ok, date(2025, 2, 1), Decimal("7.5"), "units/month",
    )  # fmt: skip


def test_capacity_site_declarations_override_supplier_wide(graph, db_session):
    sup = graph.supplier_graph()
    site2 = graph.site(supplier_id=sup.supplier_id)
    jan, feb = date(2025, 1, 1), date(2025, 2, 1)
    a = graph.capacity(supplier_id=sup.supplier_id, site_id=sup.site_id, month=jan,
                       available="300", committed="200")  # fmt: skip
    b_ = graph.capacity(supplier_id=sup.supplier_id, site_id=site2, month=jan,
                        available="80", committed="30", verified=False)  # fmt: skip
    wide_jan = graph.capacity(supplier_id=sup.supplier_id, month=jan, available="500")
    wide_feb = graph.capacity(supplier_id=sup.supplier_id, month=feb, available="400")
    b = one(db_session, sup.supplier_id)
    jan_rows = [c for c in b.capacities if c.month == jan]
    assert {c.capacity_declaration_id for c in jan_rows} == {a, b_}
    assert sum(c.available - c.committed for c in jan_rows) == Decimal("150")  # 100 + 50
    assert {c.capacity_declaration_id: c.verified for c in jan_rows} == {a: True, b_: False}
    assert [c.capacity_declaration_id for c in b.capacities if c.month == feb] == [wide_feb]
    assert (wide_jan, "capacity", "capacity_scope_overridden") in _excluded(b)
    assert all(isinstance(c, CapacityObs) for c in b.capacities)


def test_capacity_recorded_after_cutoff_and_multi_month(graph, db_session):
    sup = graph.supplier_graph()
    late = graph.capacity(supplier_id=sup.supplier_id, month=date(2025, 1, 1), available="9",
                          recorded_at=utc(2025, 4, 2))  # fmt: skip
    q = graph.capacity(supplier_id=sup.supplier_id, month=date(2024, 12, 1), available="50",
                       period_end=date(2025, 2, 28))  # fmt: skip
    b = one(db_session, sup.supplier_id)
    assert [(c.capacity_declaration_id, c.month) for c in b.capacities] == [
        (q, date(2025, 1, 1)), (q, date(2025, 2, 1)),
    ]  # fmt: skip
    assert (late, "capacity", "capacity_recorded_after_cutoff") in _excluded(b)


# --- compliance -----------------------------------------------------------------------------


def test_unknown_compliance_when_no_certification(graph, db_session):
    sup = graph.supplier_graph()
    req = graph.requirement(contract_id=sup.contract_id, certification_type=CertificationType.REACH)
    (obs,) = one(db_session, sup.supplier_id).compliance
    assert obs.requirement_id == req
    assert obs.certification_type == CertificationType.REACH
    assert obs.scope == RequirementScope.SUPPLIER and obs.site_id is None
    assert obs.mandatory is True
    assert obs.certification_id is None
    assert obs.state == ComplianceState.UNKNOWN


def test_certification_superseded_after_cutoff_still_counts_before(graph, db_session):
    sup = graph.supplier_graph()
    graph.requirement(contract_id=sup.contract_id)
    base = graph.certification(supplier_id=sup.supplier_id, recorded_at=utc(2024, 6, 1))
    revoked = graph.certification(supplier_id=sup.supplier_id,
                                  compliance=CertificationStatus.NONCOMPLIANT,
                                  recorded_at=utc(2025, 2, 10), supersedes=base)  # fmt: skip
    (at_c,) = one(db_session, sup.supplier_id, q1(cutoff=utc(2025, 2, 1))).compliance
    assert (at_c.certification_id, at_c.state) == (base, ComplianceState.COMPLIANT)
    b = one(db_session, sup.supplier_id)
    (after,) = b.compliance
    assert (after.certification_id, after.state) == (revoked, ComplianceState.NONCOMPLIANT)
    assert (base, "compliance", "certification_superseded") in _excluded(b)


@pytest.mark.parametrize(
    ("verification", "compliance", "valid_to", "expected"),
    [
        ("VERIFIED", "COMPLIANT", date(2025, 3, 31), ComplianceState.COMPLIANT),
        ("VERIFIED", "COMPLIANT", date(2025, 3, 30), ComplianceState.EXPIRED),
        ("VERIFIED", "NONCOMPLIANT", date(2026, 1, 1), ComplianceState.NONCOMPLIANT),
        ("REJECTED", "COMPLIANT", date(2026, 1, 1), ComplianceState.NONCOMPLIANT),
        ("PENDING_VERIFICATION", "COMPLIANT", date(2026, 1, 1),
         ComplianceState.PENDING_VERIFICATION),
    ],
)  # fmt: skip
def test_compliance_state_mapping(graph, db_session, verification, compliance, valid_to, expected):
    sup = graph.supplier_graph()
    graph.requirement(contract_id=sup.contract_id)
    graph.certification(supplier_id=sup.supplier_id, verification=VerificationStatus(verification),
                        compliance=CertificationStatus(compliance), valid_to=valid_to)  # fmt: skip
    assert one(db_session, sup.supplier_id).compliance[0].state == expected


def test_site_requirement_needs_site_certification(graph, db_session):
    sup = graph.supplier_graph()
    other_site = graph.site(supplier_id=sup.supplier_id)
    site_req = graph.requirement(contract_id=sup.contract_id, site_id=sup.site_id)
    wide_req = graph.requirement(contract_id=sup.contract_id)
    other_req = graph.requirement(contract_id=sup.contract_id, site_id=other_site)
    site_cert = graph.certification(supplier_id=sup.supplier_id, site_id=sup.site_id)
    obs = {o.requirement_id: o for o in one(db_session, sup.supplier_id).compliance}
    assert obs[site_req].certification_id == site_cert
    assert obs[site_req].state == ComplianceState.COMPLIANT
    assert obs[site_req].scope == RequirementScope.SITE and obs[site_req].site_id == sup.site_id
    # a site certificate never satisfies a supplier-wide or another site's requirement
    assert obs[wide_req].state == ComplianceState.UNKNOWN
    assert obs[other_req].state == ComplianceState.UNKNOWN
    wide_cert = graph.certification(supplier_id=sup.supplier_id)
    obs = {o.requirement_id: o for o in one(db_session, sup.supplier_id).compliance}
    assert obs[wide_req].certification_id == wide_cert
    assert obs[other_req].state == ComplianceState.UNKNOWN  # supplier-wide cert does not count


def test_requirements_effective_at_cutoff_only(graph, db_session):
    sup = graph.supplier_graph()
    ended = graph.contract(supplier_id=sup.supplier_id, effective_to=date(2025, 1, 31))
    graph.requirement(contract_id=ended, effective_to=date(2025, 1, 31))
    graph.requirement(contract_id=sup.contract_id, effective_from=date(2025, 4, 1))
    current = graph.requirement(contract_id=sup.contract_id)
    graph.requirement(contract_id=sup.contract_id, created_at=utc(2025, 4, 1))  # recorded > C
    assert [o.requirement_id for o in one(db_session, sup.supplier_id).compliance] == [current]


# --- disruption -----------------------------------------------------------------------------


def test_future_or_late_recorded_risk_signal_excluded(graph, db_session):
    sup = graph.supplier_graph()
    c = utc(2025, 3, 1)
    ok = graph.risk_signal(supplier_id=sup.supplier_id, observed=utc(2025, 2, 1))
    graph.risk_signal(supplier_id=sup.supplier_id, observed=c + timedelta(hours=1))
    late = graph.risk_signal(supplier_id=sup.supplier_id, observed=utc(2025, 2, 2),
                             recorded=c + timedelta(days=1))  # fmt: skip
    b = one(db_session, sup.supplier_id, q1(cutoff=c))
    assert [r.risk_signal_id for r in b.risks] == [ok]
    assert (late, "disruption", "signal_recorded_after_cutoff") in _excluded(b)


def test_risk_signal_older_than_lookback_excluded(graph, db_session):
    sup = graph.supplier_graph()
    c = utc(2025, 9, 30)  # restatement of Q1 long after; lookback reaches 2025-04-03
    old = graph.risk_signal(supplier_id=sup.supplier_id, observed=utc(2025, 3, 1))
    edge = graph.risk_signal(supplier_id=sup.supplier_id, observed=c - timedelta(days=LOOKBACK))
    b = one(db_session, sup.supplier_id, q1(cutoff=c))
    assert [r.risk_signal_id for r in b.risks] == [edge]
    assert (old, "disruption", "signal_outside_lookback") in _excluded(b)


def test_risk_obs_fields_resolution_and_site_activity(graph, db_session):
    sup = graph.supplier_graph()
    closed = graph.site(supplier_id=sup.supplier_id, status=SiteStatus.INACTIVE,
                        effective_to=date(2025, 1, 31))  # fmt: skip
    resolved = graph.risk_signal(supplier_id=sup.supplier_id, site_id=sup.site_id,
                                 observed=utc(2025, 2, 1), resolution=ResolutionStatus.RESOLVED,
                                 valid_to=utc(2025, 2, 20))  # fmt: skip
    later = graph.risk_signal(supplier_id=sup.supplier_id, site_id=closed,
                              observed=utc(2025, 2, 2), resolution=ResolutionStatus.RESOLVED,
                              valid_to=utc(2025, 3, 20))  # fmt: skip
    attest = graph.risk_signal(supplier_id=sup.supplier_id, observed=utc(2025, 1, 3),
                               signal_type=RiskSignalType.MONITORING_ATTESTATION,
                               severity=Severity.NONE)  # fmt: skip
    obs = {r.risk_signal_id: r for r in one(db_session, sup.supplier_id,
                                              q1(cutoff=utc(2025, 3, 1))).risks}  # fmt: skip
    assert obs[resolved].resolved_at_cutoff is True
    assert obs[later].resolved_at_cutoff is False  # RESOLVED only from 2025-03-20 (R14)
    assert obs[later].resolution == ResolutionStatus.RESOLVED
    assert obs[resolved].site_active is True
    assert obs[later].site_active is False
    assert obs[attest].site_id is None and obs[attest].site_active is True
    assert obs[attest].severity == Severity.NONE
    r = obs[resolved]
    assert (r.signal_type, r.severity, r.observed) == (
        RiskSignalType.FACTORY_INCIDENT, Severity.HIGH, utc(2025, 2, 1),
    )  # fmt: skip
    assert r.dedup_key == db_session.get(RiskSignal, resolved).dedup_key


# --- exceptions, bundle shape, errors ---------------------------------------------------------


def test_open_critical_exceptions(graph, db_session):
    sup = graph.supplier_graph()
    other = graph.supplier_graph()
    crit = graph.data_exception(supplier_id=sup.supplier_id, detected_at=utc(2026, 1, 1))
    graph.data_exception(supplier_id=sup.supplier_id, severity=Severity.HIGH)
    graph.data_exception(supplier_id=sup.supplier_id, detection_status=DetectionStatus.DISMISSED)
    graph.data_exception(supplier_id=other.supplier_id)
    assert one(db_session, sup.supplier_id).open_critical_exception_ids == [crit]


def test_excluded_records_listed_with_reason(graph, db_session):
    sup = graph.supplier_graph()
    c = utc(2025, 2, 15)
    not_due = _shipment(graph, sup, utc(2025, 3, 1))
    due = _shipment(graph, sup, utc(2025, 2, 1))
    ev = graph.event(shipment_id=due, event_type=ARRIVED, event_timestamp=utc(2025, 2, 14),
                     recorded_timestamp=utc(2025, 2, 16))  # fmt: skip
    b = one(db_session, sup.supplier_id, q1(cutoff=c))
    assert ExcludedRecord("shipment", not_due, ComponentName.delivery, "not_yet_due") in b.excluded
    assert (
        ExcludedRecord("delivery_event", ev, ComponentName.delivery, "recorded_after_cutoff")
        in b.excluded
    )
    assert b.excluded == sorted(
        b.excluded, key=lambda x: (x.component, x.source_entity_type, x.source_record_id)
    )
    assert due not in {x.source_record_id for x in b.excluded}


def test_supplier_with_no_activity_returns_empty_bundle(graph, db_session):
    busy = graph.supplier_graph()
    _shipment(graph, busy, utc(2025, 2, 1))
    idle = graph.supplier()
    result = collect(db_session, [idle, busy.supplier_id], q1())
    assert set(result) == {idle, busy.supplier_id}
    b = result[idle]
    assert b.supplier_id == idle and b.window == q1()
    for field in ("deliveries", "inspections", "invoices", "demand", "capacities",
                  "compliance", "risks", "excluded", "open_critical_exception_ids"):  # fmt: skip
        assert getattr(b, field) == [], field
    assert len(result[busy.supplier_id].deliveries) == 1


def test_evidence_is_partitioned_by_supplier(graph, db_session):
    a, b = graph.supplier_graph(), graph.supplier_graph()
    sa_ = _shipment(graph, a, utc(2025, 2, 1))
    sb = _shipment(graph, b, utc(2025, 2, 2))
    result = collect(db_session, None, q1())  # None = every supplier
    assert [d.shipment_id for d in result[a.supplier_id].deliveries] == [sa_]
    assert [d.shipment_id for d in result[b.supplier_id].deliveries] == [sb]


def test_unknown_supplier_ids_raise(graph, db_session):
    sup = graph.supplier_graph()
    with pytest.raises(ValueError, match="SUP-404404"):
        collect(db_session, [sup.supplier_id, "SUP-404404"], q1())


def test_bundles_are_frozen(graph, db_session):
    sup = graph.supplier_graph()
    b = one(db_session, sup.supplier_id)
    with pytest.raises(AttributeError):
        b.supplier_id = "x"  # type: ignore[misc]


# --- generated dataset: set-based queries and D5 sanity ---------------------------------------


@pytest.fixture(scope="module")
def starter_db(app_config, tmp_path_factory, migrated_template):
    import shutil

    path = tmp_path_factory.mktemp("starter-evidence") / "starter.db"
    shutil.copyfile(migrated_template, path)
    engine = make_engine(f"sqlite:///{path}")
    factory = make_session_factory(engine)
    with session_scope(factory) as s:
        load_dataset(s, generate_dataset(GeneratorConfig.from_app_config(app_config, "starter")))
    yield engine, factory
    engine.dispose()


def _count_statements(engine, factory, ids, window) -> int:
    statements: list[str] = []

    def listener(conn, cursor, statement, *args):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", listener)
    try:
        with factory() as s:
            collect(s, ids, window)
    finally:
        sa.event.remove(engine, "before_cursor_execute", listener)
    return len(statements)


def test_statement_count_independent_of_supplier_count(starter_db):
    engine, factory = starter_db
    with factory() as s:
        ids = s.scalars(sa.select(Supplier.supplier_id).order_by(Supplier.supplier_id)).all()
    assert len(ids) == 50
    window = quarter_windows(utc(2025, 1, 1), utc(2026, 6, 30, 23, 59, 59))[2]
    five = _count_statements(engine, factory, ids[:5], window)
    fifty = _count_statements(engine, factory, ids, window)
    assert five == fifty
    assert fifty <= 15


def test_starter_dataset_evidence_respects_d5(starter_db):
    _, factory = starter_db
    w = quarter_windows(utc(2025, 1, 1), utc(2026, 6, 30, 23, 59, 59))[2]  # 2025Q3
    s_, top, c = w.start, min(w.end, w.cutoff), w.cutoff
    with factory() as s:
        ids = s.scalars(sa.select(Supplier.supplier_id)).all()
        result = collect(s, ids, w)
        assert set(result) == set(ids)
        totals = {k: 0 for k in ("deliveries", "inspections", "invoices", "demand", "risks")}
        for sid, b in result.items():
            assert b.supplier_id == sid
            for k in totals:
                totals[k] += len(getattr(b, k))
            for d in b.deliveries:
                shp = s.get(Shipment, d.shipment_id)
                assert s.get(PurchaseOrder, shp.po_id).supplier_id == sid
                assert s_ <= shp.expected_delivery_timestamp <= top
                if d.arrived_known is not None:
                    assert d.arrived_known <= c
                    arrived = s.scalars(sa.select(DeliveryEvent).where(
                        DeliveryEvent.shipment_id == d.shipment_id,
                        DeliveryEvent.event_type == ARRIVED)).one()  # fmt: skip
                    assert arrived.recorded_timestamp <= c
            lines = [i.shipment_line_id for i in b.inspections]
            assert len(lines) == len(set(lines))
            for i in b.inspections:
                ins = s.get(Inspection, i.inspection_id)
                assert s_ <= ins.inspection_timestamp <= top and ins.recorded_at <= c
                alloc = s.get(ShipmentLineAllocation, ins.shipment_line_id)
                po_id = s.get(Shipment, alloc.shipment_id).po_id
                assert s.get(PurchaseOrder, po_id).supplier_id == sid
                assert (ins.inspected_quantity, ins.defect_quantity) == (i.inspected, i.defect)
            for i in b.invoices:
                inv = s.get(Invoice, i.invoice_id)
                assert inv.supplier_id == sid and inv.recorded_at <= c
                assert s_.date() <= inv.invoice_date <= top.date()
            for d in b.demand:
                line = s.get(POLine, d.po_line_id)
                po = s.get(PurchaseOrder, line.po_id)
                assert po.supplier_id == sid and po.po_status != POStatus.CANCELLED
                assert s_.date() <= po.order_date <= top.date() and line.created_at <= c
            for cap in b.capacities:
                row = s.get(CapacityDeclaration, cap.capacity_declaration_id)
                assert row.supplier_id == sid and row.recorded_at <= c
                assert row.period_start <= cap.month <= row.period_end
            for o in b.compliance:
                if o.certification_id is not None:
                    cert = s.get(SupplierCertification, o.certification_id)
                    assert cert.supplier_id == sid and cert.recorded_at <= c
                    assert cert.site_id == o.site_id
            for r in b.risks:
                sig = s.get(RiskSignal, r.risk_signal_id)
                assert sig.supplier_id == sid and sig.recorded_timestamp <= c
                assert c - timedelta(days=LOOKBACK) <= sig.observed_timestamp <= c
    assert all(v > 0 for v in totals.values()), totals

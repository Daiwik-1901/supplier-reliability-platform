"""Task 13: every validation rule fires on a minimal corrupted graph.

Each test builds the same minimal VALID graph (proved finding-free by
`test_clean_graph_has_no_findings`), corrupts exactly one thing in a way the DB constraints
allow (a direct UPDATE the CHECKs permit, or an extra row), and asserts the rule reports that
entity. Corruptions the schema blocks outright are tested through `validate_payload` (enum,
inspection reconciliation) or, for orphan FKs, inside a transaction with deferred FK checks that
is rolled back (the orphan can never be committed).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from scoring.factories import GraphBuilder, SupplierGraph, utc
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import (
    DeliveryEventType,
    POStatus,
    RiskSignalType,
    Severity,
    ShipmentStatus,
)
from supplier_reliability.models import (
    CapacityDeclaration,
    Contract,
    DataException,
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RawIngestionRecord,
    RiskSignal,
    ScoreEvidence,
    Shipment,
    ShipmentLineAllocation,
    Site,
    SupplierScore,
    ValidationFinding,
    ValidationRun,
)
from supplier_reliability.services.scoring.evidence import EvaluationWindow
from supplier_reliability.services.scoring.service import CalculationRequest, ScoringService
from supplier_reliability.services.validation import (
    REGISTRY,
    Finding,
    run_validation,
    validate_payload,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
Q1 = EvaluationWindow(utc(2025, 1, 1), utc(2025, 3, 31, 23, 59, 59), utc(2025, 3, 31, 23, 59, 59))
E = DeliveryEventType

ALL_RULE_IDS = {
    *(f"RI-00{i}" for i in range(1, 10)),
    *(f"DC-00{i}" for i in range(1, 10)),
    "DC-010",
    *(f"CH-00{i}" for i in range(1, 8)),
    *(f"AR-00{i}" for i in range(1, 8)),
    *(f"SE-00{i}" for i in range(1, 6)),
}


# --- the minimal valid graph ---------------------------------------------------------------------


@dataclass(frozen=True)
class Graph:
    g: GraphBuilder
    a: SupplierGraph
    b: SupplierGraph
    po: str
    line: str
    shipment: str
    allocation: str
    events: dict[DeliveryEventType, str]
    inspection: str
    invoice: str
    risk: str
    capacity: str


@pytest.fixture
def graph(db_session: Session) -> Graph:
    """Supplier A with one fully delivered, inspected and invoiced PO; supplier B idle."""
    g = GraphBuilder(db_session)
    a, b = g.supplier_graph(), g.supplier_graph()
    po = g.purchase_order(
        supplier_id=a.supplier_id, contract_id=a.contract_id, order_date=date(2025, 1, 5)
    )
    line = g.po_line(po_id=po)  # 100 EA x 10.00
    shp = g.shipment(
        po_id=po, site_id=a.site_id, expected=utc(2025, 2, 1), status=ShipmentStatus.DELIVERED
    )  # dispatch 2025-01-27 00:00, created 2025-01-26 00:00
    sl = g.allocation(shipment_id=shp, po_line_id=line)
    events = {
        t: g.event(shipment_id=shp, event_type=t, event_timestamp=at)
        for t, at in [
            (E.BOOKED, utc(2025, 1, 26, 10)),
            (E.DISPATCHED, utc(2025, 1, 27)),
            (E.IN_TRANSIT, utc(2025, 1, 27, 6)),
            (E.ARRIVED, utc(2025, 2, 1, 6)),
            (E.RECEIVED, utc(2025, 2, 1, 10)),
        ]
    }
    ins = g.inspection(shipment_line_id=sl, inspection_timestamp=utc(2025, 2, 2))
    inv = g.invoice(shipment_id=shp, invoice_date=date(2025, 2, 3))  # expected = 100 x 10.00
    rsk = g.risk_signal(supplier_id=a.supplier_id, observed=utc(2025, 2, 10), severity=Severity.LOW)
    cap = g.capacity(supplier_id=a.supplier_id, month=date(2025, 1, 1), available="1000")
    db_session.commit()
    return Graph(g, a, b, po, line, shp, sl, events, ins, inv, rsk, cap)


def _score(session: Session, app_config, supplier_id: str) -> str:
    session.commit()
    out = ScoringService(session, app_config.scoring, lambda: NOW).calculate(
        CalculationRequest(Q1, [supplier_id])
    )
    return out.created[0]


def corrupt(session: Session, model, key: str, **values) -> None:
    """UPDATE one row bypassing the ORM (the CHECK constraints still apply)."""
    pk = sa.inspect(model).primary_key[0]
    session.execute(
        sa.update(model).where(pk == key).values(**values),
        execution_options={"synchronize_session": False},
    )
    session.expire_all()


def check(session: Session, rule_id: str) -> list[Finding]:
    return run_validation(session, [rule_id], persist=False, clock=lambda: NOW).findings


def fired_on(session: Session, rule_id: str) -> set[str | None]:
    found = check(session, rule_id)
    assert all(f.rule_id == rule_id for f in found)
    return {f.entity_id for f in found}


# --- registry and the clean baseline -------------------------------------------------------------


def test_registry_holds_exactly_the_documented_rules():
    assert set(REGISTRY) == ALL_RULE_IDS
    for rule_id, r in REGISTRY.items():
        assert r.rule_id == rule_id
        assert r.category in {"integrity", "domain", "chronology", "arithmetic", "semantic"}
        assert r.description and isinstance(r.severity, Severity)


def test_rule_severities_follow_the_ruling():
    assert REGISTRY["CH-005"].severity == Severity.CRITICAL
    assert REGISTRY["AR-006"].severity == Severity.CRITICAL
    for informational in ("SE-003", "SE-005", "DC-009"):
        assert REGISTRY[informational].severity == Severity.LOW
    for rule_id in ("RI-003", "CH-003", "AR-005"):
        assert REGISTRY[rule_id].severity == Severity.HIGH


def test_clean_graph_has_no_findings(db_session, graph):
    result = run_validation(db_session, persist=False, clock=lambda: NOW)
    assert result.findings == []
    assert result.counts_by_rule == {rule_id: 0 for rule_id in ALL_RULE_IDS}


def test_clean_scored_graph_has_no_findings(db_session, graph, app_config):
    _score(db_session, app_config, graph.a.supplier_id)
    result = run_validation(db_session, persist=False, clock=lambda: NOW)
    assert result.findings == []
    for rule_id in ("CH-005", "AR-006", "AR-007", "SE-005"):
        assert result.checked_counts_by_rule[rule_id] > 0, rule_id


# --- integrity -----------------------------------------------------------------------------------


def test_ri001_reports_checked_keys_and_no_duplicates(db_session, graph):
    # every key RI-001 checks is also a PK/unique constraint, so it cannot be corrupted here:
    # the rule is defence in depth (exports, other backends); it must still examine rows
    result = run_validation(db_session, ["RI-001"], persist=False, clock=lambda: NOW)
    assert result.findings == [] and result.checked_counts_by_rule["RI-001"] > 0


def test_ri002_orphan_foreign_key(db_session, graph):
    db_session.execute(sa.text("PRAGMA defer_foreign_keys = ON"))
    db_session.add(
        DeliveryEvent(
            delivery_event_id="EVT-ORPHAN",
            shipment_id="SHP-MISSING",
            event_type=E.BOOKED,
            event_timestamp=utc(2025, 1, 1),
            recorded_timestamp=utc(2025, 1, 1),
            source_system="SYNTHETIC_TMS",
        )
    )
    db_session.flush()
    try:
        found = check(db_session, "RI-002")
        assert [(f.entity_type, f.entity_id, f.field) for f in found] == [
            ("delivery_events", "EVT-ORPHAN", "shipment_id")
        ]
    finally:
        db_session.rollback()


def test_ri003_dispatch_site_of_another_supplier(db_session, graph):
    corrupt(db_session, Shipment, graph.shipment, originating_site_id=graph.b.site_id)
    assert fired_on(db_session, "RI-003") == {graph.shipment}


def test_ri004_site_not_effective_at_dispatch(db_session, graph):
    corrupt(db_session, Site, graph.a.site_id, effective_to=date(2025, 1, 10))
    assert fired_on(db_session, "RI-004") == {graph.shipment}


def test_ri004_dispatch_on_the_last_effective_day_is_valid(db_session, graph):
    corrupt(db_session, Site, graph.a.site_id, effective_to=date(2025, 1, 27))
    assert fired_on(db_session, "RI-004") == set()


def test_ri005_po_contract_of_another_supplier(db_session, graph):
    corrupt(db_session, PurchaseOrder, graph.po, contract_id=graph.b.contract_id)
    assert fired_on(db_session, "RI-005") == {graph.po}


def test_ri006_invoice_supplier_differs_from_po(db_session, graph):
    corrupt(db_session, Invoice, graph.invoice, supplier_id=graph.b.supplier_id)
    assert fired_on(db_session, "RI-006") == {graph.invoice}


def test_ri007_open_po_without_shipment(db_session, graph):
    a = graph.a
    lonely = graph.g.purchase_order(
        supplier_id=a.supplier_id, contract_id=a.contract_id, order_date=date(2025, 3, 1)
    )
    graph.g.purchase_order(
        supplier_id=a.supplier_id,
        contract_id=a.contract_id,
        order_date=date(2025, 3, 1),
        status=POStatus.CANCELLED,
    )
    assert fired_on(db_session, "RI-007") == {lonely}


def test_ri008_site_scoped_records_reference_another_suppliers_site(db_session, graph):
    a, b = graph.a, graph.b
    req = graph.g.requirement(contract_id=a.contract_id, site_id=b.site_id)
    cert = graph.g.certification(supplier_id=a.supplier_id, site_id=b.site_id)
    corrupt(db_session, CapacityDeclaration, graph.capacity, site_id=b.site_id)
    assert fired_on(db_session, "RI-008") == {req, cert, graph.capacity}


def test_ri009_allocation_line_of_another_po(db_session, graph):
    a = graph.a
    other_po = graph.g.purchase_order(
        supplier_id=a.supplier_id, contract_id=a.contract_id, order_date=date(2025, 1, 6)
    )
    other_line = graph.g.po_line(po_id=other_po)
    bad = graph.g.allocation(shipment_id=graph.shipment, po_line_id=other_line, quantity="5")
    assert fired_on(db_session, "RI-009") == {bad}


# --- domain --------------------------------------------------------------------------------------


def test_dc001_blank_required_field(db_session, graph):
    corrupt(db_session, RiskSignal, graph.risk, signal_source="   ")
    found = check(db_session, "DC-001")
    assert [(f.entity_id, f.field) for f in found] == [(graph.risk, "signal_source")]


def test_dc002_enum_value_via_payload():
    found = validate_payload("SHIPMENT", {**_shipment_payload(), "shipment_status": "BOGUS"})
    assert [(f.rule_id, f.field) for f in found] == [("DC-002", "shipment_status")]
    assert found[0].entity_id == "SHP-RAW-1" and found[0].severity == Severity.HIGH


def test_dc002_canonical_enums_are_checked(db_session, graph):
    result = run_validation(db_session, ["DC-002"], persist=False, clock=lambda: NOW)
    assert result.findings == [] and result.checked_counts_by_rule["DC-002"] > 0


def test_dc003_negative_amount(db_session, graph):
    corrupt(db_session, POLine, graph.line, line_amount=Decimal("-1.00"))
    assert fired_on(db_session, "DC-003") == {graph.line}


def test_dc004_money_with_more_than_two_decimals(db_session, graph):
    db_session.execute(
        sa.text("UPDATE invoices SET invoiced_amount = 1000.125 WHERE invoice_id = :i"),
        {"i": graph.invoice},
    )
    assert fired_on(db_session, "DC-004") == {graph.invoice}


def test_dc005_po_ordered_outside_contract_period(db_session, graph):
    corrupt(db_session, Contract, graph.a.contract_id, effective_from=date(2025, 2, 1))
    assert fired_on(db_session, "DC-005") == {graph.po}


def test_dc006_po_currency_differs_from_contract(db_session, graph):
    corrupt(db_session, PurchaseOrder, graph.po, currency="USD")
    assert fired_on(db_session, "DC-006") == {graph.po}


def test_dc007_negative_invoiced_amount(db_session, graph):
    corrupt(db_session, Invoice, graph.invoice, invoiced_amount=Decimal("-10.00"))
    assert fired_on(db_session, "DC-007") == {graph.invoice}


def test_dc008_unit_of_measure_mismatches(db_session, graph):
    corrupt(db_session, ShipmentLineAllocation, graph.allocation, unit_of_measure="KG")
    corrupt(db_session, CapacityDeclaration, graph.capacity, unit="kg/month")
    assert fired_on(db_session, "DC-008") == {graph.allocation, graph.capacity}


def test_dc009_monthly_demand_above_contracted_capacity_is_low(db_session, graph):
    a = graph.a
    po = graph.g.purchase_order(
        supplier_id=a.supplier_id, contract_id=a.contract_id, order_date=date(2025, 1, 20)
    )
    graph.g.po_line(po_id=po, ordered_quantity="20000")
    found = check(db_session, "DC-009")
    assert [(f.entity_id, f.severity) for f in found] == [(a.contract_id, Severity.LOW)]
    assert "2025-01" in (f := found[0]).observed_value and f.field == "contracted_capacity"


def test_dc010_verified_capacity_committed_above_available(db_session, graph):
    corrupt(db_session, CapacityDeclaration, graph.capacity, committed_capacity=Decimal("1500"))
    assert fired_on(db_session, "DC-010") == {graph.capacity}


# --- chronology ----------------------------------------------------------------------------------


def test_ch001_required_delivery_before_order(db_session, graph):
    corrupt(db_session, PurchaseOrder, graph.po, required_delivery_date=date(2025, 1, 1))
    assert fired_on(db_session, "CH-001") == {graph.po}


def test_ch002_dispatch_before_order_date(db_session, graph):
    corrupt(db_session, Shipment, graph.shipment, dispatch_timestamp=utc(2025, 1, 4, 23))
    assert fired_on(db_session, "CH-002") == {graph.shipment}


def test_ch003_arrived_before_dispatched(db_session, graph):
    corrupt(db_session, DeliveryEvent, graph.events[E.ARRIVED], event_timestamp=utc(2025, 1, 26))
    assert fired_on(db_session, "CH-003") == {graph.events[E.ARRIVED]}


def test_ch004_inspection_before_arrival(db_session, graph):
    corrupt(db_session, Inspection, graph.inspection, inspection_timestamp=utc(2025, 1, 31))
    assert fired_on(db_session, "CH-004") == {graph.inspection}


def test_ch005_score_evidence_recorded_after_cutoff(db_session, graph, app_config):
    score_id = _score(db_session, app_config, graph.a.supplier_id)
    corrupt(db_session, Inspection, graph.inspection, recorded_at=utc(2025, 4, 15))
    found = check(db_session, "CH-005")
    assert [(f.entity_type, f.entity_id) for f in found] == [("supplier_scores", score_id)]
    assert graph.inspection in found[0].observed_value
    assert found[0].severity == Severity.CRITICAL


def test_ch006_risk_signal_recorded_before_observed(db_session, graph):
    corrupt(db_session, RiskSignal, graph.risk, recorded_timestamp=utc(2025, 2, 9))
    assert fired_on(db_session, "CH-006") == {graph.risk}


def test_ch007_invalid_event_transition(db_session, graph):
    db_session.execute(
        sa.delete(DeliveryEvent).where(
            DeliveryEvent.delivery_event_id == graph.events[E.IN_TRANSIT]
        )
    )
    assert fired_on(db_session, "CH-007") == {graph.shipment}


def test_ch007_correction_after_arrival_is_valid(db_session, graph):
    graph.g.correction(
        shipment_id=graph.shipment,
        corrects_event_id=graph.events[E.ARRIVED],
        corrected_value=utc(2025, 2, 1, 8),
        recorded_timestamp=utc(2025, 2, 5),
    )
    assert fired_on(db_session, "CH-007") == set()


# --- arithmetic ----------------------------------------------------------------------------------


def test_ar001_line_amount_not_quantity_times_price(db_session, graph):
    corrupt(db_session, POLine, graph.line, line_amount=Decimal("999.99"))
    assert fired_on(db_session, "AR-001") == {graph.line}


def test_ar002_shipment_total_differs_from_lines(db_session, graph):
    corrupt(db_session, Shipment, graph.shipment, shipped_quantity=Decimal("150"))
    assert fired_on(db_session, "AR-002") == {graph.shipment}


def test_ar003_over_allocated_po_line(db_session, graph):
    a = graph.a
    second = graph.g.shipment(po_id=graph.po, site_id=a.site_id, expected=utc(2025, 2, 10))
    graph.g.allocation(shipment_id=second, po_line_id=graph.line, quantity="50")
    assert fired_on(db_session, "AR-003") == {graph.line}


def test_ar004_inspected_more_than_received(db_session, graph):
    corrupt(
        db_session,
        Inspection,
        graph.inspection,
        inspected_quantity=Decimal("150"),
        accepted_quantity=Decimal("150"),
    )
    assert fired_on(db_session, "AR-004") == {graph.inspection}


def test_ar004_reconciliation_via_payload():
    payload = {**_inspection_payload(), "accepted_quantity": "8", "rejected_quantity": "1"}
    found = validate_payload("INSPECTION", payload)
    assert [(f.rule_id, f.entity_id) for f in found] == [("AR-004", "INS-RAW-1")]


def test_ar005_invoice_with_wrong_variance(db_session, graph):
    corrupt(db_session, Invoice, graph.invoice, variance_amount=Decimal("5.00"))
    found = check(db_session, "AR-005")
    assert [(f.entity_id, f.field) for f in found] == [(graph.invoice, "variance_amount")]


def test_ar006_overall_score_tampered(db_session, graph, app_config):
    score_id = _score(db_session, app_config, graph.a.supplier_id)
    overall = db_session.get(SupplierScore, score_id).overall_score
    assert overall is not None
    tampered = overall - 5 if overall >= 5 else overall + 5
    corrupt(db_session, SupplierScore, score_id, overall_score=tampered)
    found = check(db_session, "AR-006")
    assert [(f.entity_id, f.severity) for f in found] == [(score_id, Severity.CRITICAL)]


def test_ar007_lineage_count_differs_from_explanation(db_session, graph, app_config):
    score_id = _score(db_session, app_config, graph.a.supplier_id)
    db_session.execute(
        sa.delete(ScoreEvidence).where(
            ScoreEvidence.score_id == score_id,
            ScoreEvidence.source_record_id == graph.inspection,
        )
    )
    found = check(db_session, "AR-007")
    assert [(f.entity_id, f.field) for f in found] == [(score_id, "quality")]


# --- semantic ------------------------------------------------------------------------------------


def test_se001_duplicate_event(db_session, graph):
    dup = graph.g.event(
        shipment_id=graph.shipment, event_type=E.ARRIVED, event_timestamp=utc(2025, 2, 1, 6)
    )
    assert fired_on(db_session, "SE-001") == {dup}


def test_se002_duplicate_supplier_invoice_number(db_session, graph):
    a = graph.a
    second = graph.g.shipment(po_id=graph.po, site_id=a.site_id, expected=utc(2025, 2, 10))
    inv2 = graph.g.invoice(
        shipment_id=second, invoice_date=date(2025, 2, 12), expected="0.00", invoiced="0.00"
    )
    first_number = db_session.get(Invoice, graph.invoice).supplier_invoice_number
    corrupt(db_session, Invoice, inv2, supplier_invoice_number=f" {first_number.lower()} ")
    assert fired_on(db_session, "SE-002") == {inv2}


def test_se003_repeated_risk_signal_is_low(db_session, graph):
    rep = graph.g.risk_signal(
        supplier_id=graph.a.supplier_id,
        observed=utc(2025, 2, 10, 5),
        signal_type=RiskSignalType.FACTORY_INCIDENT,
        severity=Severity.LOW,
    )
    found = check(db_session, "SE-003")
    assert [(f.entity_id, f.severity) for f in found] == [(rep, Severity.LOW)]


def test_se004_invoice_join_multiplication(db_session, graph):
    graph.g.invoice(shipment_id=graph.shipment, invoice_date=date(2025, 2, 4))  # second invoice
    assert fired_on(db_session, "SE-004") == {graph.po}


def test_se005_supplier_without_performance_evidence(db_session, graph, app_config):
    _score(db_session, app_config, graph.b.supplier_id)
    found = check(db_session, "SE-005")
    assert [(f.entity_id, f.severity) for f in found] == [(graph.b.supplier_id, Severity.LOW)]


# --- run_validation: persistence, expected anomalies, run ids ------------------------------------


def test_run_validation_persists_run_and_findings(db_session, graph):
    corrupt(db_session, Shipment, graph.shipment, originating_site_id=graph.b.site_id)
    db_session.commit()
    first = run_validation(db_session, clock=lambda: NOW)
    second = run_validation(db_session, ["RI-003"], clock=lambda: NOW)
    assert (first.run_id, second.run_id) == ("VAL-000001", "VAL-000002")
    assert first.counts_by_rule["RI-003"] == 1 and first.counts_by_severity["HIGH"] >= 1
    assert set(first.checked_counts_by_rule) == ALL_RULE_IDS
    run = db_session.get(ValidationRun, "VAL-000001")
    assert run.started_at == NOW and run.finished_at == NOW and run.scope == "all"
    assert run.finding_counts["by_rule"]["RI-003"] == 1
    rows = db_session.scalars(
        sa.select(ValidationFinding).where(ValidationFinding.run_id == "VAL-000001")
    ).all()
    assert len(rows) == len(first.findings)
    row = next(r for r in rows if r.rule_id == "RI-003")
    assert (row.entity_id, row.severity, row.detection_timestamp) == (
        graph.shipment,
        Severity.HIGH,
        NOW,
    )
    assert db_session.get(ValidationRun, "VAL-000002").scope == "RI-003"


def test_expected_anomaly_marker_comes_from_quarantined_records(db_session, graph):
    corrupt(db_session, Shipment, graph.shipment, originating_site_id=graph.b.site_id)
    corrupt(db_session, POLine, graph.line, line_amount=Decimal("999.99"))  # unexpected
    db_session.add(
        RawIngestionRecord(
            raw_record_id="RAW-000001",
            batch_id="BATCH-T",
            source_entity_type="SHIPMENT",
            source_record_id=graph.shipment,
            payload={},
            received_at=NOW,
        )
    )
    db_session.flush()
    db_session.add(
        DataException(
            exception_id="EXC-000001",
            batch_id="BATCH-T",
            raw_record_id="RAW-000001",
            source_entity_type="SHIPMENT",
            source_record_id=graph.shipment,
            issue_type="SITE_ATTRIBUTION_CONFLICT",
            rule_id="E",
            severity=Severity.HIGH,
            detection_status="DETECTED",
            detected_at=NOW,
            disposition="QUARANTINED",
            related_entity_ids={},
            evidence={},
        )
    )
    db_session.commit()
    result = run_validation(db_session, ["RI-003", "AR-001"], clock=lambda: NOW)
    marks = {f.rule_id: f.is_expected_anomaly for f in result.findings}
    assert marks == {"RI-003": True, "AR-001": False}
    stored = {
        r.rule_id: r.is_expected_anomaly for r in db_session.scalars(sa.select(ValidationFinding))
    }
    assert stored == marks


def test_run_validation_refuses_pending_changes(db_session, graph):
    db_session.get(PurchaseOrder, graph.po).currency = "USD"
    with pytest.raises(RuntimeError, match="pending"):
        run_validation(db_session, clock=lambda: NOW)


def test_run_validation_rejects_unknown_rule(db_session, graph):
    with pytest.raises(ValueError, match="XX-999"):
        run_validation(db_session, ["XX-999"], persist=False, clock=lambda: NOW)


def test_findings_are_sorted_and_deterministic(db_session, graph):
    corrupt(db_session, PurchaseOrder, graph.po, currency="USD")
    corrupt(db_session, POLine, graph.line, line_amount=Decimal("999.99"))
    one = run_validation(db_session, persist=False, clock=lambda: NOW).findings
    two = run_validation(db_session, persist=False, clock=lambda: NOW).findings
    assert one == two
    assert [(f.rule_id, f.entity_id) for f in one] == sorted((f.rule_id, f.entity_id) for f in one)
    assert all(f.detection_timestamp == NOW for f in one)


# --- payload validator ---------------------------------------------------------------------------


def _shipment_payload() -> dict:
    return {
        "shipment_id": "SHP-RAW-1",
        "po_id": "PO-2026-000001",
        "originating_site_id": "SITE-SUP-000001-01",
        "destination_warehouse_id": "WH-SYN-01",
        "carrier_id": "CARRIER-SYN-01",
        "dispatch_timestamp": "2025-01-27T00:00:00Z",
        "expected_delivery_timestamp": "2025-02-01T00:00:00Z",
        "actual_delivery_timestamp": "2025-02-01T06:00:00Z",
        "shipment_status": "DELIVERED",
        "shipped_quantity": "100",
        "received_quantity": "100",
        "created_at": "2025-01-26T00:00:00Z",
    }


def _event_payload() -> dict:
    return {
        "delivery_event_id": "EVT-RAW-1",
        "shipment_id": "SHP-2026-000001",
        "event_type": "ARRIVED",
        "event_timestamp": "2025-02-01T06:00:00Z",
        "recorded_timestamp": "2025-02-01T07:00:00Z",
        "location_id": "WH-SYN-01",
        "source_system": "SYNTHETIC_WMS",
        "event_metadata": None,
    }


def _inspection_payload() -> dict:
    return {
        "inspection_id": "INS-RAW-1",
        "shipment_line_id": "SHL-2026-000001",
        "inspection_kind": "INITIAL",
        "inspection_type": "SAMPLE",
        "inspection_timestamp": "2025-02-02T00:00:00Z",
        "recorded_at": "2025-02-02T02:00:00Z",
        "inspected_quantity": "10",
        "accepted_quantity": "9",
        "rejected_quantity": "1",
        "defect_quantity": "1",
        "inspection_status": "CONDITIONAL",
        "defect_category": "COSMETIC",
        "inspection_source": "SYNTHETIC_QMS",
    }


def _risk_payload() -> dict:
    return {
        "risk_signal_id": "RSK-RAW-1",
        "supplier_id": "SUP-000001",
        "site_id": None,
        "related_shipment_id": None,
        "related_contract_id": None,
        "signal_type": "FACTORY_INCIDENT",
        "severity": "HIGH",
        "signal_source": "SYNTHETIC_RISK_FEED",
        "observed_timestamp": "2025-02-10T00:00:00Z",
        "recorded_timestamp": "2025-02-10T01:00:00Z",
        "valid_from": "2025-02-10T00:00:00Z",
        "valid_to": None,
        "resolution_status": "OPEN",
        "evidence_reference": "synthetic://risk/RSK-RAW-1",
        "dedup_key": "k",
    }


def _score_payload() -> dict:
    return {
        "score_id": "SCORE-RAW-1",
        "supplier_id": "SUP-000001",
        "site_id": None,
        "evaluation_start": "2025-01-01T00:00:00Z",
        "evaluation_end": "2025-03-31T23:59:59Z",
        "data_cutoff_timestamp": "2025-03-31T23:59:59Z",
        "calculated_at": "2025-04-01T23:59:59Z",
        "delivery_score": "80.00",
        "quality_score": "90.00",
        "cost_score": None,
        "capacity_score": None,
        "compliance_score": None,
        "disruption_score": None,
        "overall_score": "84.55",  # (0.30 x 80 + 0.25 x 90) / 0.55 = 84.5454...
        "evidence_coverage": "0.5500",
        "effective_weights": {"delivery": "0.5455", "quality": "0.4545"},
        "reliability_category": "INSUFFICIENT_EVIDENCE",
        "decision": "COLLECT_MORE_DATA",
        "scoring_version": "1.0.0",
        "evidence_refs": ["SHP-2026-000001"],
    }


PAYLOADS = {
    "SHIPMENT": _shipment_payload,
    "DELIVERY_EVENT": _event_payload,
    "INSPECTION": _inspection_payload,
    "RISK_SIGNAL": _risk_payload,
    "SUPPLIER_SCORE": _score_payload,
}


@pytest.mark.parametrize("entity_type", sorted(PAYLOADS))
def test_valid_payloads_have_no_findings(entity_type):
    assert validate_payload(entity_type, PAYLOADS[entity_type]()) == []


@pytest.mark.parametrize(
    ("change", "rule_id", "field"),
    [
        ({"signal_source": "  "}, "DC-001", "signal_source"),
        ({"signal_source": None}, "DC-001", "signal_source"),
        ({"observed_timestamp": None}, "DC-001", "observed_timestamp"),
        ({"observed_timestamp": "2025-02-10T00:00:00"}, "DC-001", "observed_timestamp"),  # naive
        ({"severity": "SEVERE"}, "DC-002", "severity"),
        ({"recorded_timestamp": "2025-02-09T00:00:00Z"}, "CH-006", "recorded_timestamp"),
    ],
)
def test_risk_payload_findings(change, rule_id, field):
    found = validate_payload("RISK_SIGNAL", {**_risk_payload(), **change})
    assert [(f.rule_id, f.field, f.entity_id) for f in found] == [(rule_id, field, "RSK-RAW-1")]


def test_payload_missing_field_and_negative_quantity():
    payload = _shipment_payload()
    del payload["po_id"]
    payload["shipped_quantity"] = "-1"
    payload["received_quantity"] = "0"
    found = validate_payload("SHIPMENT", payload)
    assert sorted((f.rule_id, f.field) for f in found) == [
        ("DC-001", "po_id"),
        ("DC-003", "shipped_quantity"),
    ]


def test_payload_arithmetic_and_chronology_checks():
    shp = {**_shipment_payload(), "received_quantity": "120"}
    assert [(f.rule_id, f.field) for f in validate_payload("SHIPMENT", shp)] == [
        ("AR-003", "received_quantity")
    ]
    early = {**_shipment_payload(), "actual_delivery_timestamp": "2025-01-26T00:00:00Z"}
    assert [(f.rule_id, f.field) for f in validate_payload("SHIPMENT", early)] == [
        ("CH-003", "actual_delivery_timestamp")
    ]
    score = {**_score_payload(), "overall_score": "70.00"}
    assert [(f.rule_id, f.field) for f in validate_payload("SUPPLIER_SCORE", score)] == [
        ("AR-006", "overall_score")
    ]
    event = {**_event_payload(), "event_type": "TELEPORTED"}
    assert [(f.rule_id, f.field) for f in validate_payload("DELIVERY_EVENT", event)] == [
        ("DC-002", "event_type")
    ]


def test_payload_findings_use_the_given_detection_time():
    found = validate_payload(
        "SHIPMENT", {**_shipment_payload(), "shipment_status": "BOGUS"}, detected_at=NOW
    )
    assert found[0].detection_timestamp == NOW
    assert found[0].entity_type == "shipments"


def test_payload_validator_rejects_unknown_entity_type():
    with pytest.raises(ValueError, match="UNKNOWN"):
        validate_payload("UNKNOWN", {})


def test_dc004_payload_money_precision_is_checked_too():
    # score payloads carry 2-dp scores; more decimals are a precision finding
    found = validate_payload("SUPPLIER_SCORE", {**_score_payload(), "delivery_score": "80.001"})
    assert ("DC-004", "delivery_score") in {(f.rule_id, f.field) for f in found}

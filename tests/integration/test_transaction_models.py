from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm import selectinload

from supplier_reliability.database import make_engine, make_session_factory, session_scope
from supplier_reliability.models import (
    Base,
    Contract,
    DataException,
    DatasetJob,
    DatasetManifest,
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RawIngestionRecord,
    RiskSignal,
    ScoreEvidence,
    ScoringConfigSnapshot,
    Shipment,
    ShipmentLineAllocation,
    Site,
    Supplier,
    ValidationFinding,
    ValidationRun,
)
from supplier_reliability.models.scoring import SupplierScore

T0 = datetime(2025, 1, 1, tzinfo=UTC)
D = Decimal


@pytest.fixture
def factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def add_in_order(session, *objs):
    for obj in objs:
        session.add(obj)
        session.flush()


def commit_fails(factory, *objs, match=None):
    with pytest.raises(IntegrityError, match=match), session_scope(factory) as s:
        add_in_order(s, *objs)


def mk_supplier():
    return Supplier(
        supplier_id="SUP-000001",
        supplier_name="Synthetic Supplier",
        supplier_category="ELECTRONICS",
        country="US",
        region="NA",
        supplier_status="ACTIVE",
        onboarding_date=date(2024, 1, 1),
        created_at=T0,
        updated_at=T0,
    )


def mk_site():
    return Site(
        site_id="SITE-SUP-000001-01",
        supplier_id="SUP-000001",
        site_name="s",
        site_type="FACTORY",
        country="US",
        region="NA",
        site_status="ACTIVE",
        effective_from=date(2025, 1, 1),
        created_at=T0,
    )


def mk_contract():
    return Contract(
        contract_id="CTR-000001",
        supplier_id="SUP-000001",
        contract_status="ACTIVE",
        effective_from=date(2025, 1, 1),
        currency="USD",
        payment_terms="NET30",
        delivery_terms="FOB",
        created_at=T0,
    )


def mk_po(**kw):
    base = dict(
        po_id="PO-2026-000001",
        supplier_id="SUP-000001",
        contract_id="CTR-000001",
        order_date=date(2025, 1, 5),
        required_delivery_date=date(2025, 2, 1),
        po_status="OPEN",
        currency="USD",
        created_at=T0,
    )
    base.update(kw)
    return PurchaseOrder(**base)


def mk_line(lid="POL-2026-000001", n=1, **kw):
    base = dict(
        po_line_id=lid,
        po_id="PO-2026-000001",
        line_number=n,
        product_category="ELECTRONICS",
        ordered_quantity=D("100"),
        unit_of_measure="EA",
        agreed_unit_price=D("2.50"),
        line_amount=D("250.00"),
        created_at=T0,
    )
    base.update(kw)
    return POLine(**base)


def mk_shipment(sid="SHP-2026-000001", **kw):
    base = dict(
        shipment_id=sid,
        po_id="PO-2026-000001",
        originating_site_id="SITE-SUP-000001-01",
        destination_warehouse_id="WH-1",
        carrier_id=None,
        dispatch_timestamp=T0 + timedelta(days=6),
        expected_delivery_timestamp=T0 + timedelta(days=12),
        shipment_status="DELIVERED",
        shipped_quantity=D("100"),
        received_quantity=D("100"),
        created_at=T0,
    )
    base.update(kw)
    return Shipment(**base)


def mk_alloc(lid="SHL-2026-000001", sid="SHP-2026-000001", pl="POL-2026-000001", **kw):
    base = dict(
        shipment_line_id=lid,
        shipment_id=sid,
        po_line_id=pl,
        unit_of_measure="EA",
        allocated_quantity=D("100"),
        shipped_quantity=D("100"),
        received_quantity=D("100"),
        allocated_at=T0,
        shipped_at=T0 + timedelta(days=6),
        received_at=T0 + timedelta(days=12),
        created_at=T0,
    )
    base.update(kw)
    return ShipmentLineAllocation(**base)


def mk_event(eid="EVT-2026-000001", **kw):
    base = dict(
        delivery_event_id=eid,
        shipment_id="SHP-2026-000001",
        event_type="DISPATCHED",
        event_timestamp=T0 + timedelta(days=6),
        recorded_timestamp=T0 + timedelta(days=6),
        source_system="SYNTHETIC_TMS",
    )
    base.update(kw)
    return DeliveryEvent(**base)


def mk_inspection(iid="INS-2026-000001", kind="INITIAL", **kw):
    base = dict(
        inspection_id=iid,
        shipment_line_id="SHL-2026-000001",
        inspection_kind=kind,
        inspection_type="SAMPLE",
        inspection_timestamp=T0 + timedelta(days=13),
        recorded_at=T0 + timedelta(days=13),
        inspected_quantity=D("50"),
        accepted_quantity=D("48"),
        rejected_quantity=D("2"),
        defect_quantity=D("3"),
        inspection_status="PASSED",
        defect_category="COSMETIC",
        inspection_source="SYNTHETIC_QA",
    )
    base.update(kw)
    return Inspection(**base)


def mk_invoice(iid="INV-2026-000001", **kw):
    base = dict(
        invoice_id=iid,
        supplier_id="SUP-000001",
        contract_id="CTR-000001",
        po_id="PO-2026-000001",
        shipment_id="SHP-2026-000001",
        supplier_invoice_number="S-1",
        invoice_date=date(2025, 1, 20),
        invoice_status="RECEIVED",
        currency="USD",
        invoiced_amount=D("260.00"),
        expected_amount=D("250.00"),
        variance_amount=D("10.00"),
        variance_percentage=D("0.040000"),
        created_at=T0,
        recorded_at=T0,
    )
    base.update(kw)
    return Invoice(**base)


def mk_signal(rid="RSK-2026-000001", **kw):
    base = dict(
        risk_signal_id=rid,
        supplier_id="SUP-000001",
        signal_type="CARRIER_DELAY",
        severity="LOW",
        signal_source="SYNTHETIC_FEED",
        observed_timestamp=T0,
        recorded_timestamp=T0,
        valid_from=T0,
        resolution_status="OPEN",
        evidence_reference="synthetic://risk/1",
        dedup_key="k1",
    )
    base.update(kw)
    return RiskSignal(**base)


def mk_score(sid="SCORE-2026-000001", version=1, **kw):
    base = dict(
        score_id=sid,
        supplier_id="SUP-000001",
        evaluation_start=T0,
        evaluation_end=T0 + timedelta(days=89),
        data_cutoff_timestamp=T0 + timedelta(days=89),
        calculated_at=T0 + timedelta(days=90),
        delivery_score=D("90.00"),
        overall_score=D("90.00"),
        evidence_coverage=D("0.3000"),
        reliability_category="INSUFFICIENT_EVIDENCE",
        decision="COLLECT_MORE_DATA",
        explanation={},
        scoring_version="1.0.0",
        calculation_status="INSUFFICIENT_EVIDENCE",
        evaluation_key="key-1",
        version_number=version,
        is_current=True,
        is_provisional=False,
        effective_weights={"delivery": 1.0},
        evidence_fingerprint="f" * 64,
        config_hash="c" * 64,
        code_version="0.1.0",
    )
    base.update(kw)
    return SupplierScore(**base)


def seed_chain(factory, with_line=True):
    with session_scope(factory) as s:
        add_in_order(s, mk_supplier(), mk_site(), mk_contract(), mk_po(), mk_line(), mk_shipment())
        if with_line:
            add_in_order(s, mk_alloc())


def seed_supplier(factory):
    with session_scope(factory) as s:
        s.add(mk_supplier())


# ---- brief tests ---------------------------------------------------------------------------


def test_po_with_unknown_contract_rejected(factory):
    seed_supplier(factory)
    commit_fails(factory, mk_po(contract_id="CTR-NOPE"), match="FOREIGN KEY")


def test_po_line_number_unique_per_po(factory):
    seed_chain(factory, with_line=False)
    commit_fails(factory, mk_line("POL-2026-000002", n=1), match="UNIQUE")
    with session_scope(factory) as s:
        s.add(mk_line("POL-2026-000002", n=2))


def test_po_line_quantity_must_be_positive(factory):
    seed_chain(factory, with_line=False)
    commit_fails(factory, mk_line("POL-2026-000009", n=9, ordered_quantity=D("0")), match="ordered")


def test_shipment_received_above_shipped_rejected(factory):
    seed_chain(factory, with_line=False)
    commit_fails(
        factory,
        mk_shipment("SHP-2026-000002", shipped_quantity=D("10"), received_quantity=D("11")),
        match="received_le_shipped",
    )


def test_shipment_line_unique_per_shipment_and_po_line(factory):
    seed_chain(factory)
    commit_fails(factory, mk_alloc("SHL-2026-000002"), match="UNIQUE")


def test_allocation_shipped_above_allocated_rejected(factory):
    seed_chain(factory, with_line=False)
    commit_fails(
        factory,
        mk_alloc(shipped_quantity=D("101"), received_quantity=D("50")),
        match="quantity_order",
    )


def test_allocation_received_above_shipped_rejected(factory):
    seed_chain(factory, with_line=False)
    commit_fails(
        factory,
        mk_alloc(shipped_quantity=D("90"), received_quantity=D("91")),
        match="quantity_order",
    )


def test_allocation_negative_quantity_rejected(factory):
    seed_chain(factory, with_line=False)
    commit_fails(
        factory,
        mk_alloc(allocated_quantity=D("10"), shipped_quantity=D("-1"), received_quantity=D("-2")),
        match="nonneg",
    )


def test_allocation_received_at_optional(factory):
    seed_chain(factory, with_line=False)
    with session_scope(factory) as s:
        s.add(mk_alloc(received_quantity=D("0"), received_at=None))


def test_event_recorded_before_occurrence_rejected(factory):
    seed_chain(factory)
    commit_fails(
        factory,
        mk_event(recorded_timestamp=T0 + timedelta(days=5)),
        match="recorded_after_event",
    )


def test_event_metadata_json_roundtrip(factory):
    seed_chain(factory)
    meta = {"corrects_event_id": "EVT-2026-000001", "field": "event_timestamp", "n": 1}
    with session_scope(factory) as s:
        s.add(mk_event(event_type="CORRECTION", event_metadata=meta))
        s.add(mk_event("EVT-2026-000002"))
    with session_scope(factory) as s:
        assert s.get(DeliveryEvent, "EVT-2026-000001").event_metadata == meta
        assert s.get(DeliveryEvent, "EVT-2026-000002").event_metadata is None


def test_inspection_reconciliation_check(factory):
    seed_chain(factory)
    commit_fails(factory, mk_inspection(accepted_quantity=D("40")), match="reconciliation")


def test_inspection_defect_bounds(factory):
    seed_chain(factory)
    commit_fails(factory, mk_inspection(defect_quantity=D("1")), match="defect_bounds")
    commit_fails(
        factory,
        mk_inspection(
            "INS-2026-000002",
            inspected_quantity=D("50"),
            accepted_quantity=D("50"),
            rejected_quantity=D("0"),
            defect_quantity=D("51"),
        ),
        match="defect_bounds",
    )


def test_inspection_negative_rejected(factory):
    seed_chain(factory)
    commit_fails(
        factory,
        mk_inspection(
            inspected_quantity=D("-1"),
            accepted_quantity=D("-1"),
            rejected_quantity=D("0"),
            defect_quantity=D("0"),
        ),
        match="nonneg",
    )


def test_inspection_fractional_reconciliation_is_exact(factory):
    seed_chain(factory)
    with session_scope(factory) as s:
        s.add(
            mk_inspection(
                inspected_quantity=D("0.30"),
                accepted_quantity=D("0.10"),
                rejected_quantity=D("0.20"),
                defect_quantity=D("0.20"),
            )
        )


def test_one_initial_inspection_per_shipment_line(factory):
    seed_chain(factory)
    with session_scope(factory) as s:
        s.add(mk_inspection("INS-2026-000001", "INITIAL"))
    commit_fails(factory, mk_inspection("INS-2026-000002", "INITIAL"), match="UNIQUE")


def test_initial_plus_multiple_reinspections_allowed(factory):
    seed_chain(factory)
    with session_scope(factory) as s:
        s.add(mk_inspection("INS-2026-000001", "INITIAL"))
        s.add(mk_inspection("INS-2026-000002", "REINSPECTION"))
        s.add(mk_inspection("INS-2026-000003", "REINSPECTION"))
    with session_scope(factory) as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(Inspection)) == 3


def test_inspection_has_no_redundant_fks():
    cols = set(Base.metadata.tables["inspections"].columns.keys())
    assert "shipment_line_id" in cols
    assert not {"shipment_id", "po_line_id"} & cols


def test_duplicate_supplier_invoice_number_rejected(factory):
    seed_chain(factory)
    with session_scope(factory) as s:
        s.add(mk_invoice())
    commit_fails(factory, mk_invoice("INV-2026-000002"), match="UNIQUE")


def test_invoice_null_variance_percentage_allowed_and_precision(factory):
    seed_chain(factory)
    with session_scope(factory) as s:
        s.add(mk_invoice(expected_amount=D("0"), variance_percentage=None))
    with session_scope(factory) as s:
        assert s.get(Invoice, "INV-2026-000001").variance_percentage is None


def test_invoice_requires_existing_shipment(factory):
    seed_chain(factory)
    commit_fails(factory, mk_invoice(shipment_id="SHP-NOPE"), match="FOREIGN KEY")


def test_risk_signal_requires_provenance_in_canonical(factory):
    seed_chain(factory)
    for field in ("signal_source", "observed_timestamp"):
        commit_fails(
            factory, mk_signal(**{field: None}), match="NOT NULL constraint failed|NOT NULL"
        )


def test_risk_signal_optional_links(factory):
    seed_chain(factory)
    with session_scope(factory) as s:
        s.add(
            mk_signal(
                site_id="SITE-SUP-000001-01",
                related_shipment_id="SHP-2026-000001",
                related_contract_id="CTR-000001",
            )
        )
    commit_fails(factory, mk_signal("RSK-2026-000002", related_shipment_id="SHP-NOPE"))


def test_score_unique_evaluation_key_and_version(factory):
    seed_supplier(factory)
    with session_scope(factory) as s:
        s.add(mk_score())
    commit_fails(
        factory, mk_score("SCORE-2026-000002", is_current=False), match="UNIQUE"
    )  # same key + version
    with session_scope(factory) as s:
        s.add(mk_score("SCORE-2026-000003", version=2, is_current=False))


def test_only_one_current_score_per_evaluation_key(factory):
    seed_supplier(factory)
    with session_scope(factory) as s:
        s.add(mk_score())
    commit_fails(factory, mk_score("SCORE-2026-000002", version=2), match="UNIQUE")


def test_score_range_and_window_checks(factory):
    seed_supplier(factory)
    commit_fails(factory, mk_score(overall_score=D("100.01")), match="score_range")
    commit_fails(factory, mk_score(evidence_coverage=D("1.5000")), match="coverage_range")
    commit_fails(factory, mk_score(version=0), match="version_positive")
    commit_fails(factory, mk_score(evaluation_end=T0 - timedelta(days=1)), match="window_order")


def test_superseded_score_cannot_stay_current(factory):
    seed_supplier(factory)
    with session_scope(factory) as s:
        add_in_order(s, mk_score("SCORE-2026-000002", version=2))
    commit_fails(
        factory,
        mk_score(superseded_by_score_id="SCORE-2026-000002"),
        match="superseded_not_current",
    )


def test_score_self_references_must_exist(factory):
    seed_supplier(factory)
    commit_fails(factory, mk_score(restates_score_id="SCORE-NOPE"), match="FOREIGN KEY")
    commit_fails(
        factory,
        mk_score(superseded_by_score_id="SCORE-NOPE", is_current=False),
        match="FOREIGN KEY",
    )


def mk_evidence(score_id="SCORE-2026-000001", record="SHP-2026-000001", **kw):
    base = dict(
        score_id=score_id,
        component="delivery",
        source_entity_type="SHIPMENT",
        source_record_id=record,
        included=True,
        contribution={"late_days": 0},
    )
    base.update(kw)
    return ScoreEvidence(**base)


def test_score_evidence_requires_existing_score(factory):
    seed_supplier(factory)
    commit_fails(factory, mk_evidence("SCORE-NOPE"), match="FOREIGN KEY")


def test_score_evidence_composite_pk(factory):
    seed_supplier(factory)
    with session_scope(factory) as s:
        add_in_order(s, mk_score(), mk_evidence())
        s.add(mk_evidence(component="quality"))  # same record, other component is fine
        s.add(mk_evidence(included=False, exclusion_reason="not due", record="SHP-2026-000002"))
    commit_fails(factory, mk_evidence(), match="UNIQUE")
    with session_scope(factory) as s:
        rows = s.scalars(
            sa.select(ScoreEvidence).where(ScoreEvidence.source_record_id == "SHP-2026-000002")
        ).all()
        assert rows[0].included is False and rows[0].exclusion_reason == "not due"


def test_config_snapshot_roundtrip(factory):
    with session_scope(factory) as s:
        s.add(
            ScoringConfigSnapshot(
                config_hash="a" * 64,
                scoring_version="1.0.0",
                config={"w": {"d": 0.3}},
                created_at=T0,
            )
        )
    with session_scope(factory) as s:
        assert s.get(ScoringConfigSnapshot, "a" * 64).config == {"w": {"d": 0.3}}


# ---- operational tables ------------------------------------------------------------------


def mk_raw(rid="RAW-000001", **kw):
    base = dict(
        raw_record_id=rid,
        batch_id="BATCH-1",
        source_entity_type="SHIPMENT",
        source_record_id=None,
        payload={"po_id": "PO-MISSING"},
        received_at=T0,
    )
    base.update(kw)
    return RawIngestionRecord(**base)


def mk_exc(eid="EXC-000001", **kw):
    base = dict(
        exception_id=eid,
        batch_id="BATCH-1",
        raw_record_id="RAW-000001",
        source_entity_type="SHIPMENT",
        source_record_id=None,
        issue_type="SHIPMENT_MISSING_PO",
        rule_id="DC-001",
        severity="HIGH",
        detection_status="DETECTED",
        detected_at=T0,
        disposition="QUARANTINED",
        related_entity_ids={"po_id": "PO-MISSING"},
        evidence={"why": "po absent"},
    )
    base.update(kw)
    return DataException(**base)


def test_raw_record_payload_may_be_malformed_and_json_roundtrips(factory):
    with session_scope(factory) as s:
        s.add(mk_raw(payload={"observed_timestamp": None, "signal_source": ""}))
    with session_scope(factory) as s:
        assert s.get(RawIngestionRecord, "RAW-000001").payload["observed_timestamp"] is None


def test_exception_unique_per_batch_raw_record_issue(factory):
    with session_scope(factory) as s:
        add_in_order(s, mk_raw(), mk_exc())
    commit_fails(factory, mk_exc("EXC-000002"), match="UNIQUE")
    with session_scope(factory) as s:
        s.add(mk_exc("EXC-000003", issue_type="SITE_ATTRIBUTION_CONFLICT"))


def test_exception_requires_raw_record_and_optional_links_checked(factory):
    commit_fails(factory, mk_exc(raw_record_id="RAW-NOPE"), match="FOREIGN KEY")
    with session_scope(factory) as s:
        add_in_order(s, mk_raw())
    commit_fails(factory, mk_exc(supplier_id="SUP-NOPE"), match="FOREIGN KEY")
    commit_fails(factory, mk_exc(corrected_score_id="SCORE-NOPE"), match="FOREIGN KEY")


def test_validation_run_and_findings(factory):
    with session_scope(factory) as s:
        add_in_order(
            s,
            ValidationRun(run_id="RUN-1", started_at=T0, scope="all", finding_counts={"ERROR": 1}),
            ValidationFinding(
                run_id="RUN-1",
                rule_id="DC-007",
                severity="ERROR",
                entity_type="INVOICE",
                entity_id="INV-1",
                field="invoiced_amount",
                observed_value="-1",
                expected_condition=">= 0",
                message="negative",
                detection_timestamp=T0,
                is_expected_anomaly=False,
            ),
        )
    commit_fails(
        factory,
        ValidationFinding(
            run_id="RUN-NOPE",
            rule_id="X",
            severity="ERROR",
            entity_type="INVOICE",
            message="m",
            detection_timestamp=T0,
            is_expected_anomaly=False,
        ),
        match="FOREIGN KEY",
    )


def test_dataset_job_and_manifest(factory):
    with session_scope(factory) as s:
        s.add(
            DatasetJob(
                job_id="JOB-1", status="QUEUED", preset="starter", seed=20261022, requested_at=T0
            )
        )
        s.add(DatasetManifest(dataset_id="DS-1", manifest={"files": []}, created_at=T0))
    with session_scope(factory) as s:
        job = s.get(DatasetJob, "JOB-1")
        assert job.started_at is None and job.dataset_id is None and job.seed == 20261022
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(DatasetJob(job_id="JOB-2", status="BOGUS", preset="x", seed=1, requested_at=T0))


# ---- indexes, names, relationships --------------------------------------------------------


def index_columns(table: str) -> list[list[str]]:
    return [[c.name for c in ix.columns] for ix in Base.metadata.tables[table].indexes]


def test_required_indexes_exist():
    assert ["po_id"] in index_columns("shipments")
    assert ["shipment_id", "event_timestamp"] in index_columns("delivery_events")
    assert ["observed_timestamp"] in index_columns("risk_signals")
    assert ["invoice_date"] in index_columns("invoices")
    assert ["severity"] in index_columns("data_exceptions")
    assert ["issue_type"] in index_columns("data_exceptions")
    assert ["supplier_id", "evaluation_end"] in index_columns("supplier_scores")
    assert ["reliability_category"] in index_columns("supplier_scores")
    assert ["calculation_status"] in index_columns("supplier_scores")
    assert ["source_entity_type", "source_record_id"] in index_columns("score_evidence")
    assert ["originating_site_id"] in index_columns("shipments")
    assert ["shipment_id"] in index_columns("invoices")


def test_all_constraint_and_index_names_fit_postgres_limit():
    too_long = []
    for table in Base.metadata.tables.values():
        names = [c.name for c in table.constraints] + [ix.name for ix in table.indexes]
        too_long += [n for n in names if n is not None and len(str(n)) > 63]
    assert too_long == []


def test_every_foreign_key_column_is_covered_by_an_index_or_key():
    # Child-side FK lookups (selectinload) must not scan: leading column of an index/PK/unique.
    gaps = []
    for table in Base.metadata.tables.values():
        covered = {ix.columns.values()[0].name for ix in table.indexes}
        covered |= {c.columns.values()[0].name for c in table.constraints if len(c.columns)}
        for fk in table.foreign_keys:
            if fk.parent.name not in covered:
                gaps.append(f"{table.name}.{fk.parent.name}")
    # Optional, rarely traversed links are allowed to be unindexed; the hot ones must not be.
    hot = {
        "shipments.po_id",
        "po_lines.po_id",
        "shipment_lines.po_line_id",
        "inspections.shipment_line_id",
        "delivery_events.shipment_id",
        "invoices.shipment_id",
        "invoices.po_id",
        "risk_signals.supplier_id",
        "supplier_scores.supplier_id",
        "score_evidence.score_id",
        "data_exceptions.raw_record_id",
        "validation_findings.run_id",
    }
    assert hot.isdisjoint(gaps), hot & set(gaps)


def seed_graph(factory):
    seed_chain(factory)
    with session_scope(factory) as s:
        add_in_order(s, mk_inspection(), mk_invoice(), mk_event())


def test_unloaded_relationship_access_raises(factory):
    seed_graph(factory)
    with session_scope(factory) as s:
        sup = s.get(Supplier, "SUP-000001")
        with pytest.raises(InvalidRequestError, match="raise"):
            _ = sup.sites
        shp = s.get(Shipment, "SHP-2026-000001")
        with pytest.raises(InvalidRequestError, match="raise"):
            _ = shp.lines
        ctr = s.get(Contract, "CTR-000001")
        for attr in ("prices", "requirements", "purchase_orders"):
            with pytest.raises(InvalidRequestError, match="raise"):
                getattr(ctr, attr)


def test_selectinload_loads_nested_graph(factory):
    seed_graph(factory)
    with session_scope(factory) as s:
        po = s.scalars(
            sa.select(PurchaseOrder)
            .where(PurchaseOrder.po_id == "PO-2026-000001")
            .options(
                selectinload(PurchaseOrder.lines),
                selectinload(PurchaseOrder.shipments).selectinload(Shipment.lines),
                selectinload(PurchaseOrder.shipments).selectinload(Shipment.events),
                selectinload(PurchaseOrder.invoices),
            )
        ).one()
        assert [ln.line_number for ln in po.lines] == [1]
        shp = po.shipments[0]
        assert [ln.shipment_line_id for ln in shp.lines] == ["SHL-2026-000001"]
        assert [e.delivery_event_id for e in shp.events] == ["EVT-2026-000001"]
        assert [i.invoice_id for i in po.invoices] == ["INV-2026-000001"]
        with pytest.raises(InvalidRequestError):  # not requested: still raises
            _ = shp.lines[0].inspections


def test_back_populates_pairs_are_consistent():
    from sqlalchemy.orm import configure_mappers

    configure_mappers()
    for mapper in Base.registry.mappers:
        for rel in mapper.relationships:
            assert rel.lazy == "raise", f"{mapper.class_.__name__}.{rel.key}"
            if rel.back_populates:
                other = rel.mapper.relationships[rel.back_populates]
                assert other.back_populates == rel.key


def test_relationships_do_not_cascade_deletes_into_history():
    from sqlalchemy.orm import configure_mappers

    configure_mappers()
    for mapper in Base.registry.mappers:
        for rel in mapper.relationships:
            assert not rel.cascade.delete_orphan, f"{mapper.class_.__name__}.{rel.key}"
            assert not rel.cascade.delete, f"{mapper.class_.__name__}.{rel.key}"


def test_relationship_assignment_orders_inserts_without_manual_flush(factory):
    with session_scope(factory) as s:
        sup = mk_supplier()
        site = mk_site()
        site.supplier = sup
        s.add(site)  # supplier cascades via save-update and is inserted first
    with session_scope(factory) as s:
        got = s.scalars(sa.select(Supplier).options(selectinload(Supplier.sites))).one()
        assert [x.site_id for x in got.sites] == ["SITE-SUP-000001-01"]

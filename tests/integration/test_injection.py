"""Task 14: anomaly injection into the raw-ingestion layer, with separate ground truth (D9).

The seeded starter dataset is generated, loaded and scored (six trailing-12-month scorecards)
once per session; every test injects into its own copy of that DB file.
"""

import csv
import hashlib
import shutil
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.database import (
    make_engine,
    make_session_factory,
    run_migrations,
    session_scope,
)
from supplier_reliability.domain.enums import DeliveryEventType, Disposition, IssueType, Severity
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import (
    Base,
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RawIngestionRecord,
    RiskSignal,
    Shipment,
    Site,
    Supplier,
)
from supplier_reliability.services.anomalies import (
    GroundTruthCase,
    InjectionResult,
    inject_anomalies,
    write_ground_truth,
)
from supplier_reliability.services.generator.config import (
    GeneratorCapacityError,
    GeneratorConfig,
)
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import load_dataset
from supplier_reliability.services.generator.rng import RandomSource
from supplier_reliability.services.isolation import find_ground_truth_references
from supplier_reliability.services.scoring.evidence import scorecard_windows
from supplier_reliability.services.scoring.service import ScoringService
from supplier_reliability.services.validation.payloads import validate_payload

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DETECTED_AT = datetime(2026, 7, 2, tzinfo=UTC)
ENTITY_OF_CLASS = {
    IssueType.SHIPMENT_MISSING_PO: "SHIPMENT",
    IssueType.DELIVERY_BEFORE_DISPATCH: "DELIVERY_EVENT",
    IssueType.SCORE_FUTURE_LEAKAGE: "SUPPLIER_SCORE",
    IssueType.RISK_SIGNAL_MISSING_PROVENANCE: "RISK_SIGNAL",
    IssueType.SITE_ATTRIBUTION_CONFLICT: "SHIPMENT",
}
EXPECTED = {  # issue type -> (severity, expected decision)
    IssueType.SHIPMENT_MISSING_PO: (Severity.HIGH, Disposition.QUARANTINED),
    IssueType.DELIVERY_BEFORE_DISPATCH: (Severity.HIGH, Disposition.QUARANTINED),
    IssueType.SCORE_FUTURE_LEAKAGE: (Severity.CRITICAL, Disposition.CORRECTED),
    IssueType.RISK_SIGNAL_MISSING_PROVENANCE: (
        Severity.MEDIUM,
        Disposition.QUARANTINED_PENDING_PROVENANCE,
    ),
    IssueType.SITE_ATTRIBUTION_CONFLICT: (Severity.HIGH, Disposition.QUARANTINED),
}


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def gen_cfg(app_config) -> GeneratorConfig:
    return GeneratorConfig.from_app_config(app_config, "starter")


@pytest.fixture(scope="session")
def profiles(gen_cfg) -> dict[str, str]:
    return dict(generate_dataset(gen_cfg).supplier_profiles)


@pytest.fixture(scope="session")
def scored_starter_db(app_config, gen_cfg, tmp_path_factory) -> Path:
    """Starter dataset loaded and scored for every scorecard window (built once)."""
    path = tmp_path_factory.mktemp("injection") / "scored.db"
    url = f"sqlite:///{path}"
    run_migrations(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    with session_scope(factory) as s:
        load_dataset(s, generate_dataset(gen_cfg))
    windows = scorecard_windows(
        gen_cfg.reference_start, gen_cfg.reference_end, app_config.scoring.window_months
    )
    with factory() as s:
        ScoringService(s, app_config.scoring, lambda: NOW).score_history(windows)
    engine.dispose()
    return path


def _open(src: Path, target: Path) -> Iterator[Session]:
    shutil.copyfile(src, target)
    engine = make_engine(f"sqlite:///{target}")
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def session(scored_starter_db, tmp_path) -> Iterator[Session]:
    yield from _open(scored_starter_db, tmp_path / "test.db")


def _inject(session: Session, gen_cfg: GeneratorConfig, app_config) -> InjectionResult:
    result = inject_anomalies(
        session, gen_cfg, RandomSource(gen_cfg.seed), scoring=app_config.scoring
    )
    session.commit()
    return result


@pytest.fixture
def injected(session, gen_cfg, app_config) -> tuple[Session, InjectionResult]:
    return session, _inject(session, gen_cfg, app_config)


def _raw(session: Session) -> dict[str, RawIngestionRecord]:
    rows = session.scalars(sa.select(RawIngestionRecord))
    return {r.raw_record_id: r for r in rows}


def _positives(
    session: Session, result: InjectionResult, issue: IssueType
) -> list[RawIngestionRecord]:
    raw = _raw(session)
    return [raw[c.entity_id] for c in result.cases if c.issue_type == issue]


def _ts(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value))


# --- counts / ground truth --------------------------------------------------------------------


def test_injection_counts_per_class(injected, gen_cfg):
    session, result = injected
    preset = gen_cfg.anomaly_preset
    per_class = preset.positives_per_class
    assert per_class == 5
    assert Counter(c.issue_type for c in result.cases) == {i: per_class for i in IssueType}
    assert [c.case_id for c in result.cases] == [
        f"CASE-{n:06d}" for n in range(1, len(result.cases) + 1)
    ]

    neg = preset.negatives
    expected_counts = {
        "SHIPMENT": neg["shipments"] + 2 * per_class,  # A and E positives are shipments
        "DELIVERY_EVENT": 2 * neg["delivery_event_pairs"] + per_class,  # DISPATCHED + ARRIVED
        "RISK_SIGNAL": neg["risk_signals"] + per_class,
        "SUPPLIER_SCORE": neg["scores"] + per_class,
    }
    assert result.raw_counts == expected_counts
    assert result.batch_id == f"BATCH-{gen_cfg.dataset_id}"

    raw = _raw(session)
    assert Counter(r.source_entity_type for r in raw.values()) == expected_counts
    assert {r.batch_id for r in raw.values()} == {result.batch_id}
    assert sorted(raw) == [f"RAW-{n:06d}" for n in range(1, len(raw) + 1)]
    first = gen_cfg.reference_end + timedelta(days=1)
    assert sorted(ensure_utc(r.received_at) for r in raw.values()) == [
        ensure_utc(first) + timedelta(seconds=i) for i in range(len(raw))
    ]
    for c in result.cases:
        record = raw[c.entity_id]
        assert c.entity_type == record.source_entity_type == ENTITY_OF_CLASS[c.issue_type]
        assert record.source_record_id is not None
        assert (c.severity, c.expected_decision) == EXPECTED[c.issue_type]
        assert c.expected_detection is True
        assert c.injection_version == "1.0.0"
        assert c.rationale.strip()


def test_canonical_tables_unchanged_by_injection(session, gen_cfg, app_config):
    canonical = [t for t in Base.metadata.sorted_tables if t.name != "raw_ingestion_records"]

    def snapshot() -> dict[str, tuple[int, str]]:
        out = {}
        for table in canonical:
            rows = sorted(repr(tuple(r)) for r in session.execute(sa.select(table)))
            digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
            out[table.name] = (len(rows), digest)
        return out

    before = snapshot()
    assert before["supplier_scores"][0] > 0  # the fixture really is scored
    _inject(session, gen_cfg, app_config)
    assert snapshot() == before
    assert before["data_exceptions"][0] == 0


def test_injection_is_deterministic(scored_starter_db, tmp_path, gen_cfg, app_config):
    def run(name: str) -> tuple[list[tuple], list[GroundTruthCase]]:
        for s in _open(scored_starter_db, tmp_path / name):
            result = _inject(s, gen_cfg, app_config)
            rows = s.execute(
                sa.select(
                    RawIngestionRecord.raw_record_id,
                    RawIngestionRecord.batch_id,
                    RawIngestionRecord.source_entity_type,
                    RawIngestionRecord.source_record_id,
                    RawIngestionRecord.payload,
                    RawIngestionRecord.received_at,
                ).order_by(RawIngestionRecord.raw_record_id)
            )
            return [tuple(r) for r in rows], result.cases
        raise AssertionError("unreachable")

    assert run("one.db") == run("two.db")


def test_injection_refuses_an_existing_batch(injected, gen_cfg, app_config):
    session, _ = injected
    with pytest.raises(ValueError, match="already"):
        inject_anomalies(session, gen_cfg, RandomSource(gen_cfg.seed), scoring=app_config.scoring)


def test_not_enough_candidates_raises_capacity_error(session, gen_cfg, app_config):
    preset = gen_cfg.anomaly_preset.model_copy(
        update={"negatives": gen_cfg.anomaly_preset.negatives | {"shipments": 10_000}}
    )
    cfg = gen_cfg.model_copy(update={"anomaly_preset": preset})
    with pytest.raises(GeneratorCapacityError):
        inject_anomalies(session, cfg, RandomSource(cfg.seed), scoring=app_config.scoring)
    session.rollback()
    assert session.scalar(sa.select(sa.func.count()).select_from(RawIngestionRecord)) == 0


# --- payload validity -------------------------------------------------------------------------


def test_negative_payloads_pass_payload_validation(injected):
    session, result = injected
    positive_ids = {c.entity_id for c in result.cases}
    negatives = [r for r in _raw(session).values() if r.raw_record_id not in positive_ids]
    assert negatives
    for r in negatives:
        findings = validate_payload(r.source_entity_type, r.payload, detected_at=DETECTED_AT)
        assert findings == [], (r.raw_record_id, findings)


def test_negatives_are_canonical_copies_and_positives_use_raw_ids(injected):
    session, result = injected
    positive_ids = {c.entity_id for c in result.cases}
    id_field = {
        "SHIPMENT": "shipment_id",
        "DELIVERY_EVENT": "delivery_event_id",
        "RISK_SIGNAL": "risk_signal_id",
        "SUPPLIER_SCORE": "score_id",
    }
    model = {
        "SHIPMENT": Shipment,
        "DELIVERY_EVENT": DeliveryEvent,
        "RISK_SIGNAL": RiskSignal,
    }
    for r in _raw(session).values():
        assert r.source_record_id == r.payload[id_field[r.source_entity_type]]
        if r.raw_record_id in positive_ids:
            assert "-RAW-" in r.source_record_id
        else:
            assert "-RAW-" not in r.source_record_id
            if r.source_entity_type in model:
                assert session.get(model[r.source_entity_type], r.source_record_id) is not None


def test_positives_are_well_formed_except_missing_provenance(injected):
    """A, B, C and E are relational defects (D9): each payload is valid on its own. Only D is
    malformed: blank/null signal_source (half) or null observed_timestamp (other half)."""
    session, result = injected
    for c in result.cases:
        r = _raw(session)[c.entity_id]
        findings = validate_payload(r.source_entity_type, r.payload, detected_at=DETECTED_AT)
        if c.issue_type == IssueType.RISK_SIGNAL_MISSING_PROVENANCE:
            assert [(f.rule_id, f.field) for f in findings] in (
                [("DC-001", "signal_source")],
                [("DC-001", "observed_timestamp")],
            )
        else:
            assert findings == [], (c.issue_type, findings)

    d = _positives(session, result, IssueType.RISK_SIGNAL_MISSING_PROVENANCE)
    no_source = [r for r in d if not (r.payload["signal_source"] or "").strip()]
    no_observed = [r for r in d if r.payload["observed_timestamp"] is None]
    assert (len(no_source), len(no_observed)) == (3, 2)
    assert {r.payload["signal_source"] for r in no_source} == {None, ""}


# --- class-specific properties ----------------------------------------------------------------


def test_missing_po_ids_absent_from_canonical(injected):
    session, result = injected
    a = _positives(session, result, IssueType.SHIPMENT_MISSING_PO)
    assert len(a) == 5
    for r in a:
        po_id = r.payload["po_id"]
        assert po_id.startswith("PO-2026-")
        assert session.get(PurchaseOrder, po_id) is None
        assert session.get(Shipment, r.source_record_id) is None
        assert session.get(Site, r.payload["originating_site_id"]) is not None


def test_site_conflation_fks_individually_valid(injected, gen_cfg):
    session, result = injected
    e = _positives(session, result, IssueType.SITE_ATTRIBUTION_CONFLICT)
    assert len(e) == 5
    cross = inactive = 0
    for r in e:
        po = session.get(PurchaseOrder, r.payload["po_id"])
        site = session.get(Site, r.payload["originating_site_id"])
        assert po is not None and site is not None  # every FK exists
        assert session.get(Shipment, r.source_record_id) is None
        dispatch = _ts(r.payload["dispatch_timestamp"]).date()
        effective = site.effective_from <= dispatch and (
            site.effective_to is None or dispatch <= site.effective_to
        )
        if site.supplier_id != po.supplier_id:
            cross += 1
        else:
            assert not effective
            assert site.effective_to is not None and site.effective_to < dispatch
            inactive += 1
    expected_cross = round(gen_cfg.e_cross_supplier_fraction * 5)
    assert (cross, inactive) == (expected_cross, 5 - expected_cross)


def test_delivery_before_dispatch_is_only_a_chronology_defect(injected):
    session, result = injected
    b = _positives(session, result, IssueType.DELIVERY_BEFORE_DISPATCH)
    assert len(b) == 5
    for r in b:
        p = r.payload
        assert p["event_type"] == DeliveryEventType.ARRIVED.value
        assert session.get(DeliveryEvent, r.source_record_id) is None
        dispatched = session.scalar(
            sa.select(DeliveryEvent.event_timestamp).where(
                DeliveryEvent.shipment_id == p["shipment_id"],
                DeliveryEvent.event_type == DeliveryEventType.DISPATCHED,
            )
        )
        assert dispatched is not None
        gap = ensure_utc(dispatched) - _ts(p["event_timestamp"])
        assert timedelta(hours=1) <= gap <= timedelta(hours=48)
        assert _ts(p["recorded_timestamp"]) >= _ts(p["event_timestamp"])


_TIMES = {  # entity -> (model, id column, occurrence column, recording column)
    "shipment": (
        Shipment,
        Shipment.shipment_id,
        Shipment.expected_delivery_timestamp,
        Shipment.created_at,
    ),
    "delivery_event": (
        DeliveryEvent,
        DeliveryEvent.delivery_event_id,
        DeliveryEvent.event_timestamp,
        DeliveryEvent.recorded_timestamp,
    ),
    "inspection": (
        Inspection,
        Inspection.inspection_id,
        Inspection.inspection_timestamp,
        Inspection.recorded_at,
    ),
    "invoice": (Invoice, Invoice.invoice_id, Invoice.invoice_date, Invoice.recorded_at),
    "risk_signal": (
        RiskSignal,
        RiskSignal.risk_signal_id,
        RiskSignal.observed_timestamp,
        RiskSignal.recorded_timestamp,
    ),
    "po_line": (POLine, POLine.po_line_id, POLine.created_at, POLine.created_at),
}


def _after(value: datetime | date, cutoff: datetime) -> bool:
    if isinstance(value, datetime):
        return ensure_utc(value) > cutoff
    return value > cutoff.date()


def _post_cutoff_refs(session: Session, refs: list[dict], cutoff: datetime) -> list[str]:
    late = []
    for ref in refs:
        entity, record_id = ref["source_entity_type"], ref["source_record_id"]
        if entity not in _TIMES:
            continue
        _, id_col, occurred, recorded = _TIMES[entity]
        row = session.execute(sa.select(occurred, recorded).where(id_col == record_id)).one()
        if _after(row[0], cutoff) or _after(row[1], cutoff):
            late.append(record_id)
    return late


def test_leaky_score_cites_post_cutoff_evidence(injected, gen_cfg):
    session, result = injected
    c = _positives(session, result, IssueType.SCORE_FUTURE_LEAKAGE)
    assert len(c) == 5
    latest_cutoff = ensure_utc(gen_cfg.reference_end) - timedelta(days=90)
    for r in c:
        p = r.payload
        cutoff = _ts(p["data_cutoff_timestamp"])
        assert _ts(p["evaluation_end"]) == cutoff <= latest_cutoff
        assert session.get(Supplier, p["supplier_id"]) is not None
        assert p["evidence_refs"]
        for ref in p["evidence_refs"]:  # every cited record is a real canonical row
            entity = ref["source_entity_type"]
            if entity in _TIMES:
                model = _TIMES[entity][0]
                assert session.get(model, ref["source_record_id"]) is not None, ref
        assert _post_cutoff_refs(session, p["evidence_refs"], cutoff), r.raw_record_id

    positive_ids = {x.entity_id for x in result.cases}
    negatives = [
        r
        for r in _raw(session).values()
        if r.source_entity_type == "SUPPLIER_SCORE" and r.raw_record_id not in positive_ids
    ]
    assert len(negatives) == gen_cfg.anomaly_preset.negatives["scores"]
    for r in negatives:
        cutoff = _ts(r.payload["data_cutoff_timestamp"])
        assert r.payload["evidence_refs"] is not None
        assert _post_cutoff_refs(session, r.payload["evidence_refs"], cutoff) == []


# --- isolation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "package",
    ["supplier_reliability.services.scoring", "supplier_reliability.services.validation"],
)
def test_ground_truth_not_importable_from_scoring(package):
    assert find_ground_truth_references(package) == []


def test_suppliers_table_has_no_profile_column(session):
    columns = {c["name"] for c in sa.inspect(session.get_bind()).get_columns("suppliers")}
    assert columns
    assert not any("profile" in name for name in columns)
    assert not any("profile" in c.name for c in Supplier.__table__.columns)


# --- ground-truth files -----------------------------------------------------------------------


def test_write_ground_truth_files(injected, profiles, tmp_path):
    _, result = injected
    entries = write_ground_truth(result.cases, profiles, tmp_path / "gt")
    assert set(entries) == {"anomaly_ground_truth.csv", "supplier_profiles.csv"}

    gt_path = tmp_path / "gt" / "anomaly_ground_truth.csv"
    data = gt_path.read_bytes()
    assert b"\r" not in data and data.endswith(b"\n")
    with gt_path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == [
        "case_id",
        "entity_type",
        "entity_id",
        "issue_type",
        "expected_detection",
        "expected_decision",
        "severity",
        "rationale",
        "injection_version",
    ]
    assert [r["case_id"] for r in rows] == sorted(c.case_id for c in result.cases)
    assert {r["expected_detection"] for r in rows} == {"true"}
    first = min(result.cases, key=lambda c: c.case_id)
    assert rows[0]["entity_id"] == first.entity_id
    assert rows[0]["issue_type"] == first.issue_type.value
    entry = entries["anomaly_ground_truth.csv"]
    assert entry.rows == len(result.cases)
    assert entry.sha256 == hashlib.sha256(data).hexdigest()

    prof_path = tmp_path / "gt" / "supplier_profiles.csv"
    lines = prof_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "supplier_id,profile"
    assert lines[1:] == [f"{sid},{profiles[sid]}" for sid in sorted(profiles)]
    assert entries["supplier_profiles.csv"].rows == len(profiles)

    again = write_ground_truth(list(reversed(result.cases)), profiles, tmp_path / "gt2")
    assert again == entries  # deterministic, independent of input order

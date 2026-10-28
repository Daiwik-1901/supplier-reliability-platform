"""Task 12: score persistence, idempotency, versioning, restatements, lineage and rebuild.

The seeded starter dataset is generated and loaded once per session (`starter_db`), and scored
once for all six quarters (`scored_db`); every test works on its own copy of the DB file.
"""

import hashlib
import re
import shutil
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import supplier_reliability
from supplier_reliability.config import ScoringConfig
from supplier_reliability.database import (
    make_engine,
    make_session_factory,
    run_migrations,
    session_scope,
)
from supplier_reliability.domain.enums import (
    CalculationStatus,
    ComponentName,
    Decision,
    DeliveryEventType,
    Region,
    ReliabilityCategory,
    ShipmentStatus,
    SupplierCategory,
    SupplierStatus,
)
from supplier_reliability.models import (
    DeliveryEvent,
    Invoice,
    PurchaseOrder,
    ScoreEvidence,
    ScoringConfigSnapshot,
    Shipment,
    Supplier,
    SupplierScore,
)
from supplier_reliability.repositories.scores import (
    ScoreFilters,
    current_scores_for_supplier,
    get_score,
    latest_current_score,
    list_scores,
)
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import load_dataset
from supplier_reliability.services.scoring.evidence import EvaluationWindow, quarter_windows
from supplier_reliability.services.scoring.service import (
    CalculationRequest,
    ScoringService,
    evaluation_key,
    rebuild_from_lineage,
    scoring_config_hash,
)

WINDOWS = quarter_windows(
    datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
)
Q3_2025 = WINDOWS[2]
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
COMPONENTS = [c.value for c in ComponentName]
SCORE_COLUMNS = {c: f"{c}_score" for c in COMPONENTS}


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def starter_db(app_config, tmp_path_factory) -> Path:
    """The seeded starter dataset loaded into a migrated SQLite file (built once)."""
    path = tmp_path_factory.mktemp("scoring-service") / "starter.db"
    url = f"sqlite:///{path}"
    run_migrations(url)
    engine = make_engine(url)
    with session_scope(make_session_factory(engine)) as s:
        load_dataset(s, generate_dataset(GeneratorConfig.from_app_config(app_config, "starter")))
    engine.dispose()
    return path


@pytest.fixture(scope="session")
def scored_db(app_config, starter_db, tmp_path_factory) -> Path:
    """starter_db with all six quarters scored by `score_history` (built once)."""
    path = tmp_path_factory.mktemp("scoring-service-scored") / "scored.db"
    shutil.copyfile(starter_db, path)
    engine = make_engine(f"sqlite:///{path}")
    with make_session_factory(engine)() as s:
        ScoringService(s, app_config.scoring, lambda: NOW).score_history(WINDOWS)
    engine.dispose()
    return path


def _open(src: Path, tmp_path: Path) -> Iterator[tuple[sa.Engine, Session]]:
    path = tmp_path / "test.db"
    shutil.copyfile(src, path)
    engine = make_engine(f"sqlite:///{path}")
    with make_session_factory(engine)() as session:
        yield engine, session
    engine.dispose()


@pytest.fixture
def starter(starter_db, tmp_path) -> Iterator[tuple[sa.Engine, Session]]:
    yield from _open(starter_db, tmp_path)


@pytest.fixture
def session(starter) -> Session:
    return starter[1]


@pytest.fixture
def scored(scored_db, tmp_path) -> Iterator[Session]:
    for _, s in _open(scored_db, tmp_path):
        yield s


@pytest.fixture
def cfg(app_config) -> ScoringConfig:
    return app_config.scoring


def service(session: Session, cfg: ScoringConfig, now: datetime = NOW) -> ScoringService:
    return ScoringService(session, cfg, lambda: now)


def count_scores(session: Session) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(SupplierScore))


def scores_of(session: Session, window: EvaluationWindow) -> list[SupplierScore]:
    stmt = sa.select(SupplierScore).where(
        SupplierScore.evaluation_start == window.start,
        SupplierScore.evaluation_end == window.end,
        SupplierScore.data_cutoff_timestamp == window.cutoff,
    )
    return list(session.scalars(stmt.order_by(SupplierScore.score_id)))


def overdue_shipment(session: Session, window: EvaluationWindow) -> tuple[str, str, datetime]:
    """(supplier_id, shipment_id, expected) of a due shipment with no ARRIVED known at C."""
    c = window.cutoff
    rows = session.execute(
        sa.select(
            Shipment.shipment_id, PurchaseOrder.supplier_id, Shipment.expected_delivery_timestamp
        )
        .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
        .where(
            Shipment.expected_delivery_timestamp.between(window.start, window.occurrence_end),
            Shipment.created_at <= c,
            Shipment.shipment_status != ShipmentStatus.CANCELLED,
        )
        .order_by(Shipment.shipment_id)
    ).all()
    known = set(
        session.scalars(
            sa.select(DeliveryEvent.shipment_id).where(
                DeliveryEvent.event_type == DeliveryEventType.ARRIVED,
                DeliveryEvent.event_timestamp <= c,
                DeliveryEvent.recorded_timestamp <= c,
            )
        )
    )
    corrected = set(
        session.scalars(
            sa.select(DeliveryEvent.shipment_id).where(
                DeliveryEvent.event_type == DeliveryEventType.CORRECTION
            )
        )
    )
    for shipment_id, supplier_id, expected in rows:
        if shipment_id not in known and shipment_id not in corrected:
            return supplier_id, shipment_id, expected
    raise AssertionError("starter data has no overdue shipment in the window")


def add_arrival(session: Session, shipment_id: str, at: datetime, recorded: datetime) -> str:
    digest = hashlib.sha256(f"{shipment_id}|{at}|{recorded}".encode()).hexdigest()[:8]
    event_id = f"EVT-9999-{digest}"
    session.add(
        DeliveryEvent(
            delivery_event_id=event_id,
            shipment_id=shipment_id,
            event_type=DeliveryEventType.ARRIVED,
            event_timestamp=at,
            recorded_timestamp=recorded,
            location_id=None,
            source_system="SYNTHETIC_TMS",
            event_metadata=None,
        )
    )
    session.commit()
    return event_id


# --- calculate --------------------------------------------------------------------------------


def test_calculate_persists_one_score_per_supplier(session, cfg):
    outcome = service(session, cfg).calculate(CalculationRequest(Q3_2025, None, None))
    assert len(outcome.created) == 50
    assert outcome.existing == outcome.unchanged == [] and outcome.superseded == {}
    rows = scores_of(session, Q3_2025)
    assert sorted(r.score_id for r in rows) == sorted(outcome.created)
    assert len({r.supplier_id for r in rows}) == 50
    config_hash = scoring_config_hash(cfg)
    for r in rows:
        assert r.version_number == 1 and r.is_current and r.superseded_by_score_id is None
        assert r.calculated_at == NOW
        assert r.code_version == supplier_reliability.__version__
        assert r.scoring_version == cfg.scoring_version and r.config_hash == config_hash
        assert r.evaluation_key == evaluation_key(r.supplier_id, None, Q3_2025, cfg.scoring_version,
                                                  config_hash)  # fmt: skip
        assert r.change_reason is None and r.restates_score_id is None


def test_evaluation_key_formula():
    w = EvaluationWindow(
        datetime(2025, 7, 1, tzinfo=UTC),
        datetime(2025, 9, 30, 23, 59, 59, tzinfo=UTC),
        datetime(2025, 9, 30, 23, 59, 59, tzinfo=UTC),
    )
    raw = "SUP-000007|*|2025-07-01T00:00:00Z|2025-09-30T23:59:59Z|2025-09-30T23:59:59Z|1.0.0|abc"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    assert evaluation_key("SUP-000007", None, w, "1.0.0", "abc") == expected
    site_raw = raw.replace("|*|", "|SITE-SUP-000007-01|")
    assert (
        evaluation_key("SUP-000007", "SITE-SUP-000007-01", w, "1.0.0", "abc")
        == (hashlib.sha256(site_raw.encode("utf-8")).hexdigest()[:32])
    )


def test_calculate_twice_is_idempotent(session, cfg):
    svc = service(session, cfg)
    first = svc.calculate(CalculationRequest(Q3_2025, None, None))
    second = service(session, cfg, NOW + timedelta(days=3)).calculate(
        CalculationRequest(Q3_2025, None, None)
    )
    assert second.created == []
    assert len(second.existing) == 50
    assert sorted(second.existing) == sorted(first.created)
    assert count_scores(session) == 50
    assert all(r.calculated_at == NOW for r in scores_of(session, Q3_2025))


def test_calculate_subset_of_suppliers(session, cfg):
    outcome = service(session, cfg).calculate(
        CalculationRequest(Q3_2025, ["SUP-000003", "SUP-000001"], None)
    )
    assert len(outcome.created) == 2
    assert {r.supplier_id for r in scores_of(session, Q3_2025)} == {"SUP-000001", "SUP-000003"}


def test_calculate_rejects_other_scoring_version(session, cfg):
    with pytest.raises(ValueError, match="scoring version"):
        service(session, cfg).calculate(CalculationRequest(Q3_2025, None, "9.9.9"))
    assert count_scores(session) == 0


def test_calculate_unknown_supplier_raises_and_writes_nothing(session, cfg):
    with pytest.raises(ValueError, match="unknown supplier"):
        service(session, cfg).calculate(CalculationRequest(Q3_2025, ["SUP-999999"], None))
    assert count_scores(session) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(ScoringConfigSnapshot)) == 0


def test_supplier_without_activity_scores_insufficient(session, cfg):
    session.add(
        Supplier(
            supplier_id="SUP-900001",
            supplier_name="Synthetic Supplier Without Activity",
            supplier_category=SupplierCategory.ELECTRONICS,
            country="Germany",
            region=Region.EMEA,
            supplier_status=SupplierStatus.ACTIVE,
            onboarding_date=date(2024, 1, 1),
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    session.commit()
    outcome = service(session, cfg).calculate(CalculationRequest(Q3_2025, ["SUP-900001"], None))
    assert len(outcome.created) == 1
    row = session.get(SupplierScore, outcome.created[0])
    assert row.reliability_category == ReliabilityCategory.INSUFFICIENT_EVIDENCE
    assert row.decision == Decision.COLLECT_MORE_DATA
    assert row.calculation_status == CalculationStatus.INSUFFICIENT_EVIDENCE
    assert row.overall_score is None and row.evidence_coverage == 0
    assert all(getattr(row, col) is None for col in SCORE_COLUMNS.values())
    assert row.explanation["missing_data_reasons"]["delivery"]


# --- recalculate ------------------------------------------------------------------------------


def test_recalculate_unchanged_evidence_no_new_version(session, cfg):
    created = service(session, cfg).calculate(CalculationRequest(Q3_2025, None, None)).created
    outcome = service(session, cfg).recalculate(CalculationRequest(Q3_2025, None, None), "check")
    assert sorted(outcome.unchanged) == sorted(created)
    assert outcome.created == [] and outcome.superseded == {}
    assert count_scores(session) == 50
    assert all(r.is_current and r.version_number == 1 for r in scores_of(session, Q3_2025))


def test_recalculate_after_new_evidence_creates_version_2(starter, cfg):
    engine, session = starter
    window = WINDOWS[5]
    service(session, cfg).calculate(CalculationRequest(window, None, None))
    supplier_id, shipment_id, expected = overdue_shipment(session, window)
    old = next(r for r in scores_of(session, window) if r.supplier_id == supplier_id)
    old_id = old.score_id
    old_state = {
        c.key: getattr(old, c.key)
        for c in sa.inspect(SupplierScore).columns
        if c.key not in ("is_current", "superseded_by_score_id")
    }
    # an operator repairs a load gap: an ARRIVED recorded before the cutoff
    add_arrival(session, shipment_id, expected - timedelta(hours=2), expected - timedelta(hours=1))

    statements: list[str] = []

    def listener(conn, cursor, statement, *args):
        if "supplier_scores" in statement and not statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lstrip().upper())

    sa.event.listen(engine, "before_cursor_execute", listener)
    try:
        outcome = service(session, cfg, NOW + timedelta(days=1)).recalculate(
            CalculationRequest(window, None, None), "late ARRIVED loaded"
        )
    finally:
        sa.event.remove(engine, "before_cursor_execute", listener)

    assert len(outcome.unchanged) == 49
    assert list(outcome.superseded) == [old_id]
    new_id = outcome.superseded[old_id]
    session.expire_all()
    old, new = session.get(SupplierScore, old_id), session.get(SupplierScore, new_id)
    assert old.is_current is False and old.superseded_by_score_id == new_id
    assert new.is_current is True and new.version_number == 2 and new.superseded_by_score_id is None
    assert new.evaluation_key == old.evaluation_key
    assert new.change_reason == "late ARRIVED loaded"
    assert new.evidence_fingerprint != old.evidence_fingerprint
    assert new.calculated_at == NOW + timedelta(days=1)
    for key, value in old_state.items():  # the superseded row is otherwise untouched
        assert getattr(old, key) == value, key
    # supersession write order: demote, insert, link
    kinds = [
        "demote" if s.startswith("UPDATE") and "IS_CURRENT" in s else
        "insert" if s.startswith("INSERT") else
        "link" if s.startswith("UPDATE") and "SUPERSEDED_BY_SCORE_ID" in s else s
        for s in statements
    ]  # fmt: skip
    assert kinds == ["demote", "insert", "link"]


def test_recalculate_twice_after_change_is_stable(starter, cfg):
    _, session = starter
    window = WINDOWS[5]
    service(session, cfg).calculate(CalculationRequest(window, None, None))
    _, shipment_id, expected = overdue_shipment(session, window)
    add_arrival(session, shipment_id, expected - timedelta(hours=2), expected - timedelta(hours=1))
    req = CalculationRequest(window, None, None)
    first = service(session, cfg).recalculate(req, "repair")
    second = service(session, cfg).recalculate(req, "repair again")
    assert len(first.superseded) == 1 and second.superseded == {}
    assert len(second.unchanged) == 50
    assert count_scores(session) == 51


def test_historical_score_ignores_later_evidence(session, cfg):
    window = EvaluationWindow(Q3_2025.start, Q3_2025.end, datetime(2025, 8, 15, tzinfo=UTC))
    service(session, cfg).calculate(CalculationRequest(window, None, None))
    template = session.scalars(sa.select(Shipment).order_by(Shipment.shipment_id)).first()
    supplier_id = session.get(PurchaseOrder, template.po_id).supplier_id
    before = next(r for r in scores_of(session, window) if r.supplier_id == supplier_id)
    fingerprint = before.evidence_fingerprint
    session.add(
        Shipment(
            shipment_id="SHP-9999-000001",
            po_id=template.po_id,
            originating_site_id=template.originating_site_id,
            destination_warehouse_id=template.destination_warehouse_id,
            carrier_id=template.carrier_id,
            dispatch_timestamp=window.cutoff - timedelta(days=2),
            expected_delivery_timestamp=window.cutoff + timedelta(days=5),  # after C, before E
            actual_delivery_timestamp=None,
            shipment_status=ShipmentStatus.IN_TRANSIT,
            shipped_quantity=Decimal("0"),
            received_quantity=Decimal("0"),
            created_at=window.cutoff - timedelta(days=3),
        )
    )
    session.commit()
    outcome = service(session, cfg).recalculate(
        CalculationRequest(window, [supplier_id], None), "new shipment"
    )
    assert outcome.unchanged == [before.score_id] and outcome.superseded == {}
    assert session.get(SupplierScore, before.score_id).evidence_fingerprint == fingerprint


def test_late_arriving_event_does_not_change_historical_score(session, cfg):
    window = WINDOWS[5]
    service(session, cfg).calculate(CalculationRequest(window, None, None))
    supplier_id, shipment_id, expected = overdue_shipment(session, window)
    before = next(r for r in scores_of(session, window) if r.supplier_id == supplier_id)
    occurred = min(expected + timedelta(hours=3), window.cutoff)  # happened by C ...
    add_arrival(
        session, shipment_id, occurred, window.cutoff + timedelta(days=2)
    )  # ... known later
    outcome = service(session, cfg).recalculate(
        CalculationRequest(window, [supplier_id], None), "late event"
    )
    assert outcome.unchanged == [before.score_id]
    assert outcome.superseded == {} and outcome.created == []
    assert count_scores(session) == 50


# --- restatement ------------------------------------------------------------------------------


def _included(session: Session, score_id: str) -> set[str]:
    return set(
        session.scalars(
            sa.select(ScoreEvidence.source_record_id).where(
                ScoreEvidence.score_id == score_id, ScoreEvidence.included.is_(True)
            )
        )
    )


def test_restatement_with_later_cutoff_links_and_reports_differences(session, cfg):
    original = service(session, cfg).calculate(CalculationRequest(Q3_2025, None, None))
    later = EvaluationWindow(Q3_2025.start, Q3_2025.end, Q3_2025.cutoff + timedelta(days=45))
    outcome = service(session, cfg).restate(later, None, "late knowledge")
    assert len(outcome.created) == 50
    by_supplier = {r.supplier_id: r for r in scores_of(session, Q3_2025)}
    assert sorted(r.score_id for r in by_supplier.values()) == sorted(original.created)
    any_difference = False
    for score_id in outcome.created:
        new = session.get(SupplierScore, score_id)
        old = by_supplier[new.supplier_id]
        assert new.restates_score_id == old.score_id
        assert new.data_cutoff_timestamp == later.cutoff
        assert new.evaluation_key != old.evaluation_key and new.version_number == 1
        assert new.change_reason == "late knowledge"
        assert old.is_current and new.is_current
        diff = new.explanation["differences_from_restated"]
        assert diff["restated_score_id"] == old.score_id
        for name in COMPONENTS:
            a, b = getattr(old, SCORE_COLUMNS[name]), getattr(new, SCORE_COLUMNS[name])
            entry = diff[name]
            assert entry["from"] == (None if a is None else str(a))
            assert entry["to"] == (None if b is None else str(b))
            assert entry["delta"] == (None if a is None or b is None else str(b - a))
            any_difference |= entry["from"] != entry["to"]
        a, b = old.overall_score, new.overall_score
        assert diff["overall"]["delta"] == (None if a is None or b is None else str(b - a))
        old_ids, new_ids = _included(session, old.score_id), _included(session, new.score_id)
        assert diff["evidence_added"] == sorted(new_ids - old_ids)
        assert diff["evidence_removed"] == sorted(old_ids - new_ids)
        any_difference |= bool(diff["evidence_added"] or diff["evidence_removed"])
    assert any_difference  # 45 more days of knowledge changes something in the starter data
    again = service(session, cfg).restate(later, None, "late knowledge")
    assert again.created == [] and sorted(again.existing) == sorted(outcome.created)


def test_restate_links_to_latest_earlier_cutoff(session, cfg):
    sup = ["SUP-000001"]
    service(session, cfg).calculate(CalculationRequest(Q3_2025, sup, None))
    mid = EvaluationWindow(Q3_2025.start, Q3_2025.end, Q3_2025.cutoff + timedelta(days=10))
    first = service(session, cfg).restate(mid, sup, "r1").created[0]
    late = EvaluationWindow(Q3_2025.start, Q3_2025.end, Q3_2025.cutoff + timedelta(days=20))
    second = service(session, cfg).restate(late, sup, "r2").created[0]
    assert session.get(SupplierScore, second).restates_score_id == first


def test_restate_without_earlier_score_raises(session, cfg):
    later = EvaluationWindow(Q3_2025.start, Q3_2025.end, Q3_2025.cutoff + timedelta(days=45))
    with pytest.raises(ValueError, match="no earlier score"):
        service(session, cfg).restate(later, ["SUP-000001"], "nothing to restate")
    service(session, cfg).calculate(CalculationRequest(Q3_2025, ["SUP-000001"], None))
    with pytest.raises(ValueError, match="no earlier score"):  # same cutoff is not later
        service(session, cfg).restate(Q3_2025, ["SUP-000001"], "same cutoff")
    with pytest.raises(ValueError, match="no earlier score"):  # other supplier has none
        service(session, cfg).restate(later, ["SUP-000001", "SUP-000002"], "partial")
    assert count_scores(session) == 1


# --- batch history ----------------------------------------------------------------------------


def test_score_history_clock_and_idempotency(scored, cfg):
    assert count_scores(scored) == 300
    for w in WINDOWS:
        rows = scores_of(scored, w)
        assert len(rows) == 50
        assert {r.calculated_at for r in rows} == {w.cutoff + timedelta(days=1)}
    again = ScoringService(scored, cfg, lambda: NOW).score_history(WINDOWS)
    assert again.created == [] and len(again.existing) == 300
    assert count_scores(scored) == 300


def test_score_ids_follow_format(scored):
    ids = sorted(scored.scalars(sa.select(SupplierScore.score_id)))
    assert all(re.fullmatch(r"SCORE-2026-\d{6}", i) for i in ids)
    assert ids == [f"SCORE-2026-{n:06d}" for n in range(1, 301)]


def test_calculation_status_follows_category_and_provisional(scored):
    statuses = Counter()
    for r in scored.scalars(sa.select(SupplierScore)):
        if r.reliability_category == ReliabilityCategory.INSUFFICIENT_EVIDENCE:
            expected = CalculationStatus.INSUFFICIENT_EVIDENCE
        elif r.is_provisional:
            expected = CalculationStatus.PROVISIONAL
        else:
            expected = CalculationStatus.CALCULATED
        assert r.calculation_status == expected, r.score_id
        statuses[expected] += 1
    assert statuses[CalculationStatus.CALCULATED] > 0
    assert statuses[CalculationStatus.INSUFFICIENT_EVIDENCE] > 0


# --- lineage ----------------------------------------------------------------------------------


def _evidence_by_score(session: Session) -> dict[str, list[ScoreEvidence]]:
    out: dict[str, list[ScoreEvidence]] = defaultdict(list)
    for e in session.scalars(sa.select(ScoreEvidence)):
        out[e.score_id].append(e)
    return out


def test_every_score_has_lineage_rows_and_config_snapshot(scored, cfg):
    evidence = _evidence_by_score(scored)
    snapshots = scored.scalars(sa.select(ScoringConfigSnapshot)).all()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.config_hash == scoring_config_hash(cfg)
    assert snap.scoring_version == cfg.scoring_version
    assert ScoringConfig.model_validate(snap.config) == cfg
    prefixes = {
        "shipment": "SHP-", "inspection": "INS-", "invoice": "INV-", "po_line": "POL-",
        "capacity_declaration": "CAP-", "compliance_requirement": "REQ-",
        "supplier_certification": "CERT-", "risk_signal": "RSK-", "delivery_event": "EVT-",
    }  # fmt: skip
    for score in scored.scalars(sa.select(SupplierScore)):
        rows = evidence[score.score_id]
        assert rows, score.score_id
        assert score.config_hash == snap.config_hash
        for e in rows:
            assert e.source_record_id.startswith(prefixes[e.source_entity_type])
            assert (e.exclusion_reason is None) == e.included
        # included rows are exactly the explanation's (untruncated) source ids
        for name in COMPONENTS:
            listed = score.explanation["components"][name]
            included = sorted(
                e.source_record_id for e in rows if e.included and e.component == name
            )
            if listed["truncated_count"] == 0:
                assert included == listed["source_record_ids"]
        # excluded rows cover every excluded record the explanation lists
        for name, items in score.explanation["excluded_records"].items():
            reasons = {
                (e.source_entity_type, e.source_record_id): e.exclusion_reason
                for e in rows
                if e.component == name and not e.included
            }
            for item in items:
                key = (item["source_entity_type"], item["source_record_id"])
                assert item["reason"] in reasons[key].split("; ")


def test_lineage_counts_match_explanation_observations(scored):
    """AR-style reconciliation: included score_evidence rows per component equal the number of
    records the explanation says the component used (capacity counts months, not records)."""
    evidence = _evidence_by_score(scored)
    checked = 0
    for score in scored.scalars(sa.select(SupplierScore)):
        counts = Counter(e.component for e in evidence[score.score_id] if e.included)
        for name in COMPONENTS:
            listed = score.explanation["components"][name]
            assert counts[name] == len(listed["source_record_ids"]) + listed["truncated_count"]
            if name != "capacity":
                assert counts[name] == listed["observations"], (score.score_id, name)
            checked += 1
    assert checked == 300 * 6


def test_included_contribution_holds_the_values_used(scored):
    row = scored.scalars(
        sa.select(ScoreEvidence).where(
            ScoreEvidence.component == ComponentName.cost, ScoreEvidence.included.is_(True)
        )
    ).first()
    invoice = scored.get(Invoice, row.source_record_id)
    assert row.contribution == {
        "expected": str(invoice.expected_amount),
        "invoiced": str(invoice.invoiced_amount),
    }


def test_rebuild_from_lineage_matches_stored_components(scored):
    scores = scored.scalars(sa.select(SupplierScore).order_by(SupplierScore.score_id)).all()
    assert len(scores) == 300
    for stored in scores:
        rebuilt = rebuild_from_lineage(scored, stored.score_id)
        for name in COMPONENTS:
            assert rebuilt.components[name].score == getattr(stored, SCORE_COLUMNS[name]), (
                stored.score_id,
                name,
            )
        assert rebuilt.overall.overall == stored.overall_score
        assert rebuilt.overall.category == stored.reliability_category
        assert rebuilt.overall.decision == stored.decision
        assert rebuilt.overall.evidence_coverage == stored.evidence_coverage
        assert rebuilt.evidence_fingerprint == stored.evidence_fingerprint
        assert rebuilt.explanation["components"] == stored.explanation["components"]


def test_rebuild_uses_only_lineage_records(scored):
    """A record added after scoring (eligible at C) is invisible to the rebuild: the rebuild
    loads source records by the ids in score_evidence, not by re-querying the window."""
    stored = next(
        s
        for s in scored.scalars(sa.select(SupplierScore).order_by(SupplierScore.score_id))
        if s.cost_score is not None
    )
    template = scored.scalars(
        sa.select(Invoice).where(Invoice.invoice_id.in_(_included(scored, stored.score_id)))
    ).first()
    scored.add(
        Invoice(
            **{
                c.key: getattr(template, c.key)
                for c in sa.inspect(Invoice).columns
                if c.key not in ("invoice_id", "supplier_invoice_number", "invoiced_amount")
            },
            invoice_id="INV-9999-000001",
            supplier_invoice_number="SYN-REBUILD-1",
            invoiced_amount=template.expected_amount * 2,
        )
    )
    scored.commit()
    rebuilt = rebuild_from_lineage(scored, stored.score_id)
    assert rebuilt.components["cost"].score == stored.cost_score
    assert rebuilt.evidence_fingerprint == stored.evidence_fingerprint


def test_rebuild_unknown_score_raises(scored):
    with pytest.raises(LookupError):
        rebuild_from_lineage(scored, "SCORE-2026-999999")


# --- repository -------------------------------------------------------------------------------


def test_get_score_and_current_scores(scored):
    assert get_score(scored, "SCORE-2026-000001").score_id == "SCORE-2026-000001"
    assert get_score(scored, "SCORE-2026-999999") is None
    current = current_scores_for_supplier(scored, "SUP-000001")
    assert len(current) == 6
    assert [s.evaluation_end for s in current] == sorted(s.evaluation_end for s in current)
    latest = latest_current_score(scored, "SUP-000001")
    assert latest.evaluation_end == WINDOWS[-1].end
    assert latest_current_score(scored, "SUP-999999") is None


def test_list_scores_filters_pagination_and_sort(scored):
    rows, total = list_scores(
        scored, ScoreFilters(supplier_id="SUP-000002"), 4, 0, "-evaluation_end"
    )
    assert total == 6 and len(rows) == 4
    assert [r.evaluation_end for r in rows] == sorted(
        (r.evaluation_end for r in rows), reverse=True
    )
    rest, total = list_scores(
        scored, ScoreFilters(supplier_id="SUP-000002"), 4, 4, "-evaluation_end"
    )
    assert total == 6 and len(rest) == 2
    assert {r.score_id for r in rows}.isdisjoint(r.score_id for r in rest)

    insufficient, n = list_scores(
        scored,
        ScoreFilters(reliability_category=ReliabilityCategory.INSUFFICIENT_EVIDENCE),
        500,
        0,
        "score_id",
    )
    assert n == len(insufficient) > 0
    assert all(r.reliability_category == ReliabilityCategory.INSUFFICIENT_EVIDENCE
               for r in insufficient)  # fmt: skip

    q3, n = list_scores(
        scored,
        ScoreFilters(evaluation_start=Q3_2025.start, evaluation_end=Q3_2025.end),
        500,
        0,
        "score_id",
    )
    assert n == 50 and {r.evaluation_end for r in q3} == {Q3_2025.end}

    with pytest.raises(ValueError, match="sort"):
        list_scores(scored, ScoreFilters(), 10, 0, "explanation")


def test_list_scores_current_only_default(starter, cfg):
    _, session = starter
    window = WINDOWS[5]
    service(session, cfg).calculate(CalculationRequest(window, None, None))
    supplier_id, shipment_id, expected = overdue_shipment(session, window)
    add_arrival(session, shipment_id, expected - timedelta(hours=2), expected - timedelta(hours=1))
    service(session, cfg).recalculate(CalculationRequest(window, [supplier_id], None), "repair")
    current, n = list_scores(session, ScoreFilters(supplier_id=supplier_id), 10, 0, "score_id")
    assert n == 1 and current[0].version_number == 2
    history, n = list_scores(
        session, ScoreFilters(supplier_id=supplier_id, current_only=False), 10, 0, "score_id"
    )
    assert n == 2 and [r.version_number for r in history] == [1, 2]

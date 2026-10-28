"""Task 15: anomaly detection, quarantine and leakage correction over the injected raw batch.

The scored starter DB (tests/integration/conftest.py) gets the anomaly batch injected once per
session; every test works on its own copy. Ground truth (the injector's GroundTruthCase list) is
used HERE ONLY, to check outcomes; the detectors never see it.
"""

import hashlib
import json
import random
import shutil
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.database import make_engine, make_session_factory
from supplier_reliability.domain.enums import (
    CalculationStatus,
    DeliveryEventType,
    DetectionStatus,
    Disposition,
    IssueType,
    ResolutionStatus,
    Severity,
)
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import (
    DataException,
    DeliveryEvent,
    PurchaseOrder,
    RawIngestionRecord,
    RiskSignal,
    ScoreEvidence,
    Shipment,
    Site,
    SupplierScore,
)
from supplier_reliability.repositories.exceptions import (
    ExceptionFilters,
    get_exception,
    list_exceptions,
)
from supplier_reliability.services.anomalies import GroundTruthCase, inject_anomalies
from supplier_reliability.services.anomaly_detection import DetectionResult, detect_raw_batch
from supplier_reliability.services.generator.rng import RandomSource
from supplier_reliability.services.isolation import find_ground_truth_references
from supplier_reliability.services.scoring.engine import score_supplier
from supplier_reliability.services.scoring.evidence import (
    EvaluationWindow,
    collect_evidence,
    scorecard_windows,
)
from supplier_reliability.services.scoring.service import (
    CalculationRequest,
    ScoringService,
    evaluation_key,
    rebuild_from_lineage,
    scoring_config_hash,
)
from supplier_reliability.services.validation.framework import run_validation

CLOCK = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)
PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "supplier_reliability" / "services"
) / "anomaly_detection"
EXPECTED = {  # issue type -> (rule id, severity, disposition)
    IssueType.SHIPMENT_MISSING_PO: ("DET-A", Severity.HIGH, Disposition.QUARANTINED),
    IssueType.DELIVERY_BEFORE_DISPATCH: ("DET-B", Severity.HIGH, Disposition.QUARANTINED),
    IssueType.SCORE_FUTURE_LEAKAGE: ("DET-C", Severity.CRITICAL, Disposition.CORRECTED),
    IssueType.RISK_SIGNAL_MISSING_PROVENANCE: (
        "DET-D",
        Severity.MEDIUM,
        Disposition.QUARANTINED_PENDING_PROVENANCE,
    ),
    IssueType.SITE_ATTRIBUTION_CONFLICT: ("DET-E", Severity.HIGH, Disposition.QUARANTINED),
}
ID_FIELD = {
    "SHIPMENT": "shipment_id",
    "DELIVERY_EVENT": "delivery_event_id",
    "RISK_SIGNAL": "risk_signal_id",
    "SUPPLIER_SCORE": "score_id",
}


# --- fixtures ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Injected:
    path: Path
    batch_id: str
    cases: list[GroundTruthCase]


@pytest.fixture(scope="session")
def injected_db(scored_starter_db, gen_cfg, app_config, tmp_path_factory) -> Injected:
    path = tmp_path_factory.mktemp("detection") / "injected.db"
    shutil.copyfile(scored_starter_db, path)
    engine = make_engine(f"sqlite:///{path}")
    with make_session_factory(engine)() as s:
        result = inject_anomalies(
            s, gen_cfg, RandomSource(gen_cfg.seed), scoring=app_config.scoring
        )
        s.commit()
    engine.dispose()
    return Injected(path, result.batch_id, result.cases)


def _open(src: Path, target: Path) -> Iterator[Session]:
    shutil.copyfile(src, target)
    engine = make_engine(f"sqlite:///{target}")
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def session(injected_db, tmp_path) -> Iterator[Session]:
    yield from _open(injected_db.path, tmp_path / "test.db")


def _detect(session: Session, batch_id: str, app_config) -> DetectionResult:
    scoring = ScoringService(session, app_config.scoring, lambda: CLOCK)
    return detect_raw_batch(session, batch_id, scoring, lambda: CLOCK)


def _exceptions(session: Session) -> list[DataException]:
    return list(session.scalars(sa.select(DataException).order_by(DataException.exception_id)))


def _raw(session: Session) -> dict[str, RawIngestionRecord]:
    return {r.raw_record_id: r for r in session.scalars(sa.select(RawIngestionRecord))}


def _positives(cases: list[GroundTruthCase], issue: IssueType) -> set[str]:
    return {c.entity_id for c in cases if c.issue_type == issue}


def _ts(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value))


def _window(payload: dict) -> EvaluationWindow:
    return EvaluationWindow(
        _ts(payload["evaluation_start"]),
        _ts(payload["evaluation_end"]),
        _ts(payload["data_cutoff_timestamp"]),
    )


def _fresh_fingerprint(session: Session, app_config, supplier_id: str, w: EvaluationWindow):
    cfg = app_config.scoring
    bundle = collect_evidence(session, [supplier_id], w, lookback_days=cfg.lookback_days)
    return score_supplier(bundle[supplier_id], cfg, scoring_config_hash(cfg)).evidence_fingerprint


# --- per class --------------------------------------------------------------------------------


def test_each_class_detected_on_starter_batch(session, injected_db, app_config):
    result = _detect(session, injected_db.batch_id, app_config)
    assert result.by_issue_type == {i.value: 5 for i in IssueType}
    assert len(result.exceptions) == 25
    rows = _exceptions(session)
    assert [r.exception_id for r in rows] == [f"EXC-{n:06d}" for n in range(1, 26)]
    assert sorted(result.exceptions) == [r.exception_id for r in rows]

    raw = _raw(session)
    for r in rows:
        rule_id, severity, disposition = EXPECTED[r.issue_type]
        record = raw[r.raw_record_id]
        assert (r.rule_id, r.severity, r.disposition) == (rule_id, severity, disposition)
        assert r.detection_status == DetectionStatus.DETECTED
        assert ensure_utc(r.detected_at) == CLOCK
        assert r.batch_id == injected_db.batch_id
        assert r.source_entity_type == record.source_entity_type
        assert r.source_record_id == record.source_record_id
        assert r.evidence
        p = record.payload
        if r.issue_type == IssueType.SHIPMENT_MISSING_PO:
            assert r.evidence["po_id"] == p["po_id"]
            site = session.get(Site, p["originating_site_id"])
            assert r.supplier_id == site.supplier_id  # resolved via the real site reference
        elif r.issue_type == IssueType.DELIVERY_BEFORE_DISPATCH:
            assert r.related_entity_ids == [p["shipment_id"]]
            assert r.supplier_id is not None
        elif r.issue_type == IssueType.SITE_ATTRIBUTION_CONFLICT:
            assert r.related_entity_ids == [p["po_id"], p["originating_site_id"]]
            assert r.supplier_id == session.get(PurchaseOrder, p["po_id"]).supplier_id
        else:  # C and D: the payload's supplier
            assert r.supplier_id == p["supplier_id"]
        if r.issue_type == IssueType.SCORE_FUTURE_LEAKAGE:
            assert r.corrected_score_id is not None
        else:
            assert r.corrected_score_id is None


def test_valid_raw_negatives_not_flagged(session, injected_db, app_config):
    _detect(session, injected_db.batch_id, app_config)
    flagged = {(r.raw_record_id, r.issue_type) for r in _exceptions(session)}
    expected = {(c.entity_id, c.issue_type) for c in injected_db.cases}
    assert flagged == expected
    positives = {c.entity_id for c in injected_db.cases}
    negatives = [rid for rid in _raw(session) if rid not in positives]
    assert len(negatives) == 40 + 80 + 30 + 30
    assert not {rid for rid, _ in flagged} & set(negatives)


def test_quarantined_signal_not_used_in_scoring(session, injected_db, gen_cfg, app_config):
    raw = _raw(session)
    d_ids = _positives(injected_db.cases, IssueType.RISK_SIGNAL_MISSING_PROVENANCE)
    suppliers = sorted({raw[rid].payload["supplier_id"] for rid in d_ids})
    windows = scorecard_windows(
        gen_cfg.reference_start, gen_cfg.reference_end, app_config.scoring.window_months
    )
    lookback = app_config.scoring.lookback_days

    def disruption() -> list:
        return [
            (sid, b.risks)
            for w in windows
            for sid, b in sorted(
                collect_evidence(session, suppliers, w, lookback_days=lookback).items()
            )
        ]

    signals_before = session.scalar(sa.select(sa.func.count()).select_from(RiskSignal))
    before = disruption()
    _detect(session, injected_db.batch_id, app_config)
    quarantined = [
        r for r in _exceptions(session) if r.issue_type == IssueType.RISK_SIGNAL_MISSING_PROVENANCE
    ]
    assert {r.disposition for r in quarantined} == {Disposition.QUARANTINED_PENDING_PROVENANCE}
    # Holds by construction: raw records are never promoted into canonical tables (no promotion
    # path exists), so a quarantined signal cannot reach scoring. This pins that invariant.
    assert disruption() == before
    assert session.scalar(sa.select(sa.func.count()).select_from(RiskSignal)) == signals_before
    service = ScoringService(session, app_config.scoring, lambda: CLOCK)
    for w in windows:
        out = service.recalculate(CalculationRequest(w, suppliers), "after quarantine")
        assert out.created == [] and out.superseded == {}
        assert len(out.unchanged) == len(suppliers)


def test_leakage_correction_uses_only_eligible_evidence(session, injected_db, app_config):
    result = _detect(session, injected_db.batch_id, app_config)
    raw = _raw(session)
    leaks = [r for r in _exceptions(session) if r.issue_type == IssueType.SCORE_FUTURE_LEAKAGE]
    assert len(leaks) == 5
    assert sorted(result.corrected_scores) == sorted({r.corrected_score_id for r in leaks})
    for r in leaks:
        p = raw[r.raw_record_id].payload
        w = _window(p)
        score = session.get(SupplierScore, r.corrected_score_id)
        fresh = _fresh_fingerprint(session, app_config, p["supplier_id"], w)
        assert score.evidence_fingerprint == fresh
        assert score.evidence_fingerprint != p["evidence_fingerprint"]
        assert (score.supplier_id, score.is_current) == (p["supplier_id"], True)
        assert (
            ensure_utc(score.evaluation_start),
            ensure_utc(score.evaluation_end),
            ensure_utc(score.data_cutoff_timestamp),
        ) == (w.start, w.end, w.cutoff)
        late = r.evidence["late_evidence"]
        assert late and r.evidence["late_evidence_count"] >= len(late)
        cited = {(x["source_entity_type"], x["source_record_id"]) for x in p["evidence_refs"]}
        assert {(x["source_entity_type"], x["source_record_id"]) for x in late} <= cited
        for x in late:  # DATE values have day granularity (D5)
            if len(x["value"]) == 10:
                assert x["value"] > w.cutoff.date().isoformat()
            else:
                assert _ts(x["value"]) > w.cutoff
        # the scorecard windows are already scored with exactly the eligible evidence
        assert r.evidence["correction"]["outcome"] == "eligible canonical score already exists"
    # no corrected score includes a record knowable only after its cutoff (CH-005)
    findings = run_validation(session, ["CH-005"], persist=False).findings
    corrected = {r.corrected_score_id for r in leaks}
    assert not [f for f in findings if f.entity_id in corrected]


def _shift_leaky_payload(session: Session, cases, days: int) -> RawIngestionRecord:
    """Relabel one C positive to an earlier, never scored cutoff (still leaky)."""
    raw_id = sorted(_positives(cases, IssueType.SCORE_FUTURE_LEAKAGE))[0]
    record = session.get(RawIngestionRecord, raw_id)
    p = dict(record.payload)
    cutoff = _ts(p["data_cutoff_timestamp"]) - timedelta(days=days)
    p["evaluation_end"] = p["data_cutoff_timestamp"] = cutoff.isoformat().replace("+00:00", "Z")
    record.payload = p
    session.commit()
    return record


def test_leakage_correction_inserts_corrected_score(session, injected_db, app_config):
    record = _shift_leaky_payload(session, injected_db.cases, days=30)
    p = record.payload
    w = _window(p)
    cfg = app_config.scoring
    key = evaluation_key(p["supplier_id"], None, w, cfg.scoring_version, scoring_config_hash(cfg))
    assert (
        session.scalars(sa.select(SupplierScore).where(SupplierScore.evaluation_key == key)).all()
        == []
    )
    scores_before = session.scalar(sa.select(sa.func.count()).select_from(SupplierScore))

    result = _detect(session, injected_db.batch_id, app_config)
    exc = session.scalars(
        sa.select(DataException).where(DataException.raw_record_id == record.raw_record_id)
    ).one()
    score = session.get(SupplierScore, exc.corrected_score_id)
    assert score.evaluation_key == key
    assert (score.calculation_status, score.version_number, score.is_current) == (
        CalculationStatus.CORRECTED,
        1,
        True,
    )
    fresh = _fresh_fingerprint(session, app_config, p["supplier_id"], w)
    assert score.evidence_fingerprint == fresh
    assert exc.evidence["correction"]["outcome"] == "corrected score inserted"
    assert exc.corrected_score_id in result.corrected_scores
    assert session.scalar(sa.select(sa.func.count()).select_from(SupplierScore)) == (
        scores_before + 1
    )
    lineage = session.scalar(
        sa.select(sa.func.count())
        .select_from(ScoreEvidence)
        .where(ScoreEvidence.score_id == score.score_id)
    )
    assert lineage > 0
    assert rebuild_from_lineage(session, score.score_id).evidence_fingerprint == (
        score.evidence_fingerprint
    )


def test_leakage_correction_supersedes_a_stale_current_score(session, injected_db, app_config):
    """A current canonical score for the key whose evidence no longer matches (an operator
    inserted a record recorded <= C after scoring) is superseded by a CORRECTED version."""
    raw = _raw(session)
    raw_id = sorted(_positives(injected_db.cases, IssueType.SCORE_FUTURE_LEAKAGE))[0]
    p = raw[raw_id].payload
    w = _window(p)
    template = session.scalars(sa.select(RiskSignal).order_by(RiskSignal.risk_signal_id)).first()
    observed = w.cutoff - timedelta(days=10)
    session.execute(
        sa.insert(RiskSignal),
        [
            {
                c.name: getattr(template, c.name)
                for c in RiskSignal.__table__.columns
                if c.name not in {"risk_signal_id", "supplier_id", "site_id"}
            }
            | {
                "risk_signal_id": "RSK-2026-990001",
                "supplier_id": p["supplier_id"],
                "site_id": None,
                "related_shipment_id": None,
                "related_contract_id": None,
                "severity": Severity.HIGH,
                "observed_timestamp": observed,
                "recorded_timestamp": observed + timedelta(days=1),
                "valid_from": observed,
                "valid_to": None,
                "resolution_status": ResolutionStatus.OPEN,
                "dedup_key": "late-repair",
            }
        ],
    )
    session.commit()
    cfg = app_config.scoring
    key = evaluation_key(p["supplier_id"], None, w, cfg.scoring_version, scoring_config_hash(cfg))
    old = session.scalars(
        sa.select(SupplierScore).where(
            SupplierScore.evaluation_key == key, SupplierScore.is_current.is_(True)
        )
    ).one()

    _detect(session, injected_db.batch_id, app_config)
    exc = session.scalars(
        sa.select(DataException).where(DataException.raw_record_id == raw_id)
    ).one()
    new = session.get(SupplierScore, exc.corrected_score_id)
    session.refresh(old)
    assert new.score_id != old.score_id
    assert (new.calculation_status, new.version_number) == (
        CalculationStatus.CORRECTED,
        old.version_number + 1,
    )
    assert (old.is_current, old.superseded_by_score_id) == (False, new.score_id)
    assert new.evidence_fingerprint == _fresh_fingerprint(session, app_config, p["supplier_id"], w)
    assert exc.evidence["correction"]["outcome"] == "stale current score superseded"


def test_missing_provenance_includes_null_recorded_timestamp(session, injected_db, app_config):
    raw = _raw(session)
    d_ids = _positives(injected_db.cases, IssueType.RISK_SIGNAL_MISSING_PROVENANCE)
    negative = next(
        r
        for r in raw.values()
        if r.source_entity_type == "RISK_SIGNAL" and r.raw_record_id not in d_ids
    )
    p = dict(negative.payload)
    p["recorded_timestamp"] = None
    negative.payload = p
    session.commit()
    _detect(session, injected_db.batch_id, app_config)
    exc = session.scalars(
        sa.select(DataException).where(DataException.raw_record_id == negative.raw_record_id)
    ).one()
    assert exc.issue_type == IssueType.RISK_SIGNAL_MISSING_PROVENANCE
    assert [f["field"] for f in exc.evidence["findings"]] == ["recorded_timestamp"]
    assert [f["rule_id"] for f in exc.evidence["findings"]] == ["DC-001"]


# --- idempotency, transactions ----------------------------------------------------------------


def test_detection_idempotent(session, injected_db, app_config):
    first = _detect(session, injected_db.batch_id, app_config)
    rows = [(r.exception_id, r.raw_record_id, r.issue_type) for r in _exceptions(session)]
    scores = session.scalar(sa.select(sa.func.count()).select_from(SupplierScore))
    second = _detect(session, injected_db.batch_id, app_config)
    assert len(first.exceptions) == 25
    assert second.exceptions == [] and second.corrected_scores == []
    assert second.by_issue_type == {i.value: 0 for i in IssueType}
    assert [(r.exception_id, r.raw_record_id, r.issue_type) for r in _exceptions(session)] == rows
    assert session.scalar(sa.select(sa.func.count()).select_from(SupplierScore)) == scores


def test_detection_refuses_pending_changes(session, injected_db, app_config):
    record = session.scalars(sa.select(RawIngestionRecord)).first()
    record.payload = dict(record.payload) | {"note": "pending"}
    with pytest.raises(RuntimeError, match="pending"):
        _detect(session, injected_db.batch_id, app_config)


def test_detection_is_deterministic(injected_db, tmp_path, app_config):
    def run(name: str) -> list[tuple]:
        for s in _open(injected_db.path, tmp_path / name):
            _detect(s, injected_db.batch_id, app_config)
            return [
                (
                    r.exception_id,
                    r.raw_record_id,
                    r.issue_type,
                    r.supplier_id,
                    r.related_entity_ids,
                    r.evidence,
                    r.corrected_score_id,
                )
                for r in _exceptions(s)
            ]
        raise AssertionError("unreachable")

    assert run("one.db") == run("two.db")


# --- content-based detection (R38) ------------------------------------------------------------


def _content_hash(record: RawIngestionRecord) -> str:
    payload = {k: v for k, v in record.payload.items() if k != ID_FIELD[record.source_entity_type]}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _findings(session: Session) -> set[tuple[str, str]]:
    raw = _raw(session)
    return {(r.issue_type.value, _content_hash(raw[r.raw_record_id])) for r in _exceptions(session)}


CANONICAL_PK = {
    "SHIPMENT": Shipment.shipment_id,
    "DELIVERY_EVENT": DeliveryEvent.delivery_event_id,
    "RISK_SIGNAL": RiskSignal.risk_signal_id,
    "SUPPLIER_SCORE": SupplierScore.score_id,
}


def _permuted_ids(session: Session, entity_type: str, ids: list[str]) -> list[str]:
    rng = random.Random(20261028)
    permuted = ids[:]
    while permuted == ids:
        rng.shuffle(permuted)
    return permuted


def _canonical_ids(session: Session, entity_type: str, ids: list[str]) -> list[str]:
    """Real canonical ids of the same entity, picked deterministically (repeats allowed)."""
    pk = CANONICAL_PK[entity_type]
    real = list(session.scalars(sa.select(pk).order_by(pk).limit(len(ids))))
    return [real[i % len(real)] for i in range(len(ids))]


@pytest.mark.parametrize("relabel", [_permuted_ids, _canonical_ids], ids=["permuted", "canonical"])
def test_detection_ignores_natural_ids(injected_db, tmp_path, app_config, relabel):
    """Metamorphic: replacing every raw natural id (payload id + source_record_id), either by a
    permutation of the batch's ids or by ids that DO exist in the canonical table of that entity,
    leaves the set of (issue type, payload content) findings unchanged: detectors key neither on
    the id's format nor on "own id absent from canonical"."""
    original: set[tuple[str, str]] = set()
    for s in _open(injected_db.path, tmp_path / "original.db"):
        _detect(s, injected_db.batch_id, app_config)
        original = _findings(s)
    relabelled: set[tuple[str, str]] = set()
    for s in _open(injected_db.path, tmp_path / "relabelled.db"):
        by_type: dict[str, list[RawIngestionRecord]] = {}
        for r in sorted(_raw(s).values(), key=lambda r: r.raw_record_id):
            by_type.setdefault(r.source_entity_type, []).append(r)
        for entity_type, rows in by_type.items():
            new_ids = relabel(s, entity_type, [r.source_record_id for r in rows])
            assert new_ids != [r.source_record_id for r in rows]
            for r, new_id in zip(rows, new_ids, strict=True):
                r.source_record_id = new_id
                r.payload = dict(r.payload) | {ID_FIELD[entity_type]: new_id}
        s.commit()
        if relabel is _canonical_ids:  # every raw natural id now exists in canonical
            for r in _raw(s).values():
                model = CANONICAL_PK[r.source_entity_type].class_
                assert s.get(model, r.source_record_id) is not None
        _detect(s, injected_db.batch_id, app_config)
        relabelled = _findings(s)
    assert len(original) == 25
    assert relabelled == original


# --- edge cases on hand-built batches ---------------------------------------------------------

EDGE_BATCH = "BATCH-EDGE"


def _add_raw(session: Session, entity_type: str, payloads: list[dict]) -> list[str]:
    """Insert payloads as a separate raw batch; returns their raw_record_ids."""
    ids = [f"EDGE-{n:06d}" for n in range(1, len(payloads) + 1)]
    session.execute(
        sa.insert(RawIngestionRecord),
        [
            {
                "raw_record_id": raw_id,
                "batch_id": EDGE_BATCH,
                "source_entity_type": entity_type,
                "source_record_id": p.get(ID_FIELD[entity_type]),
                "payload": p,
                "received_at": CLOCK,
            }
            for raw_id, p in zip(ids, payloads, strict=True)
        ],
    )
    session.commit()
    return ids


def _edge_exceptions(session: Session) -> list[tuple[str, IssueType]]:
    rows = session.scalars(sa.select(DataException).where(DataException.batch_id == EDGE_BATCH))
    return sorted((r.raw_record_id, r.issue_type) for r in rows)


def _iso(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def _arrived_payload(session: Session, shipment_id: str, at: datetime) -> dict:
    template = next(
        r.payload
        for r in _raw(session).values()
        if r.source_entity_type == "DELIVERY_EVENT" and r.payload["event_type"] == "ARRIVED"
    )
    return dict(template) | {
        "delivery_event_id": "EVT-EDGE",
        "shipment_id": shipment_id,
        "event_timestamp": _iso(at),
        "recorded_timestamp": _iso(at),
    }


def _shipment_with_one_dispatch(session: Session) -> Shipment:
    stmt = (
        sa.select(DeliveryEvent.shipment_id)
        .where(DeliveryEvent.event_type == DeliveryEventType.DISPATCHED)
        .group_by(DeliveryEvent.shipment_id)
        .having(sa.func.count() == 1)
        .order_by(DeliveryEvent.shipment_id)
        .limit(1)
    )
    return session.get(Shipment, session.scalar(stmt))


def test_chronology_uses_canonical_dispatched_event_when_present(session, app_config):
    shipment = _shipment_with_one_dispatch(session)
    event = session.scalars(
        sa.select(DeliveryEvent).where(
            DeliveryEvent.shipment_id == shipment.shipment_id,
            DeliveryEvent.event_type == DeliveryEventType.DISPATCHED,
        )
    ).one()
    # the event (not the shipment header) is the reference when it exists: move it 5 days later
    later = ensure_utc(shipment.dispatch_timestamp) + timedelta(days=5)
    event.event_timestamp, event.recorded_timestamp = later, later
    session.commit()
    between = ensure_utc(shipment.dispatch_timestamp) + timedelta(days=1)
    (raw_id,) = _add_raw(
        session, "DELIVERY_EVENT", [_arrived_payload(session, shipment.shipment_id, between)]
    )
    _detect(session, EDGE_BATCH, app_config)
    assert _edge_exceptions(session) == [(raw_id, IssueType.DELIVERY_BEFORE_DISPATCH)]
    exc = session.scalars(
        sa.select(DataException).where(DataException.batch_id == EDGE_BATCH)
    ).one()
    assert exc.evidence["dispatch_source"] == "delivery_events.DISPATCHED"
    assert exc.evidence["canonical_dispatched_timestamp"] == _iso(later)


def test_chronology_falls_back_to_shipment_dispatch_timestamp(session, app_config):
    shipment = _shipment_with_one_dispatch(session)
    session.execute(
        sa.delete(DeliveryEvent).where(
            DeliveryEvent.shipment_id == shipment.shipment_id,
            DeliveryEvent.event_type == DeliveryEventType.DISPATCHED,
        )
    )
    session.commit()
    dispatch = ensure_utc(shipment.dispatch_timestamp)
    before, after = dispatch - timedelta(hours=1), dispatch + timedelta(hours=1)
    early, late = _add_raw(
        session,
        "DELIVERY_EVENT",
        [
            _arrived_payload(session, shipment.shipment_id, before),
            _arrived_payload(session, shipment.shipment_id, after),
        ],
    )
    _detect(session, EDGE_BATCH, app_config)
    assert _edge_exceptions(session) == [(early, IssueType.DELIVERY_BEFORE_DISPATCH)]
    exc = session.scalars(
        sa.select(DataException).where(DataException.batch_id == EDGE_BATCH)
    ).one()
    assert exc.evidence["dispatch_source"] == "shipments.dispatch_timestamp"
    assert exc.evidence["canonical_dispatched_timestamp"] == _iso(dispatch)
    assert exc.related_entity_ids == [shipment.shipment_id]
    assert late not in {r for r, _ in _edge_exceptions(session)}


def test_leakage_reference_entity_type_is_case_insensitive(session, injected_db, app_config):
    raw_id = sorted(_positives(injected_db.cases, IssueType.SCORE_FUTURE_LEAKAGE))[0]
    p = dict(session.get(RawIngestionRecord, raw_id).payload)
    p["evidence_refs"] = [
        x | {"source_entity_type": x["source_entity_type"].upper()} for x in p["evidence_refs"]
    ]
    (edge_id,) = _add_raw(session, "SUPPLIER_SCORE", [p])
    _detect(session, EDGE_BATCH, app_config)
    assert _edge_exceptions(session) == [(edge_id, IssueType.SCORE_FUTURE_LEAKAGE)]


def test_leakage_payload_with_malformed_cutoff_is_reported_as_skipped(
    session, injected_db, app_config
):
    raw_id = sorted(_positives(injected_db.cases, IssueType.SCORE_FUTURE_LEAKAGE))[0]
    p = dict(session.get(RawIngestionRecord, raw_id).payload)
    naive = p | {"data_cutoff_timestamp": p["data_cutoff_timestamp"].removesuffix("Z")}
    garbage = p | {"data_cutoff_timestamp": "not-a-timestamp"}
    naive_id, garbage_id = _add_raw(session, "SUPPLIER_SCORE", [naive, garbage])
    result = _detect(session, EDGE_BATCH, app_config)
    assert _edge_exceptions(session) == []
    assert [raw for raw, _ in result.skipped] == [naive_id, garbage_id]
    assert all("data_cutoff_timestamp" in reason for _, reason in result.skipped)


def test_detection_rejects_unknown_or_empty_batch(session, app_config):
    for batch_id in ("BATCH-NO-SUCH", ""):
        with pytest.raises(ValueError, match="batch"):
            _detect(session, batch_id, app_config)


def test_record_can_carry_missing_po_and_site_conflict(session, app_config):
    """One raw record, two defects: its PO does not exist (A) and its site is not effective at
    dispatch (E). One exception per (raw record, issue type), so it carries both."""
    template = next(r.payload for r in _raw(session).values() if r.source_entity_type == "SHIPMENT")
    site = session.get(Site, template["originating_site_id"])
    dispatch = datetime.combine(site.effective_from, datetime.min.time(), UTC) - timedelta(days=30)
    payload = dict(template) | {
        "shipment_id": "SHP-EDGE",
        "po_id": "PO-2026-999998",
        "dispatch_timestamp": _iso(dispatch),
    }
    assert session.get(PurchaseOrder, payload["po_id"]) is None
    (raw_id,) = _add_raw(session, "SHIPMENT", [payload])
    _detect(session, EDGE_BATCH, app_config)
    assert _edge_exceptions(session) == [
        (raw_id, IssueType.SHIPMENT_MISSING_PO),
        (raw_id, IssueType.SITE_ATTRIBUTION_CONFLICT),
    ]
    e = session.scalars(
        sa.select(DataException).where(
            DataException.batch_id == EDGE_BATCH,
            DataException.issue_type == IssueType.SITE_ATTRIBUTION_CONFLICT,
        )
    ).one()
    assert e.evidence["conflicts"] == ["site_not_effective_at_dispatch"]


def test_detection_sources_have_no_raw_id_literal():
    files = sorted(PACKAGE_DIR.rglob("*.py"))
    assert files
    for path in files:
        assert "RAW" not in path.read_text(encoding="utf-8"), path.name


def test_detector_module_does_not_import_ground_truth():
    assert find_ground_truth_references("supplier_reliability.services.anomaly_detection") == []
    assert find_ground_truth_references("supplier_reliability.repositories.exceptions") == []


# --- repository -------------------------------------------------------------------------------


def test_exception_repository_filters_and_pages(session, injected_db, app_config):
    _detect(session, injected_db.batch_id, app_config)
    rows, total = list_exceptions(session, ExceptionFilters(), limit=10, offset=20)
    assert total == 25
    assert [r.exception_id for r in rows] == [f"EXC-{n:06d}" for n in range(21, 26)]

    rows, total = list_exceptions(
        session, ExceptionFilters(severity=Severity.CRITICAL), limit=100, offset=0
    )
    assert total == len(rows) == 5
    assert {r.issue_type for r in rows} == {IssueType.SCORE_FUTURE_LEAKAGE}

    _, total = list_exceptions(
        session, ExceptionFilters(disposition=Disposition.QUARANTINED), limit=100, offset=0
    )
    assert total == 15
    rows, total = list_exceptions(
        session,
        ExceptionFilters(issue_type=IssueType.SITE_ATTRIBUTION_CONFLICT),
        limit=100,
        offset=0,
    )
    assert total == 5
    supplier = rows[0].supplier_id
    rows, total = list_exceptions(
        session, ExceptionFilters(supplier_id=supplier), limit=100, offset=0
    )
    expected = Counter(r.supplier_id for r in _exceptions(session))[supplier]
    assert total == len(rows) == expected
    assert {r.supplier_id for r in rows} == {supplier}

    assert get_exception(session, "EXC-000001").exception_id == "EXC-000001"
    assert get_exception(session, "EXC-999999") is None
    with pytest.raises(ValueError):
        list_exceptions(session, ExceptionFilters(), limit=0, offset=0)

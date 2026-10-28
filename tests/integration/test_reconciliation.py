"""Task 16: persisted scores reconciled against the independent pandas recomputation (Add-D).

Expected values come from `independent_supplier_metrics`, a separate implementation over the
exported CSVs; nothing is read back from the engine to form an expectation.
"""

import shutil
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
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
from supplier_reliability.domain.enums import CalculationStatus
from supplier_reliability.models import SupplierScore
from supplier_reliability.services.evaluation import (
    INDEPENDENT_TABLES,
    independent_supplier_metrics,
    profile_alignment,
    reconcile_scores,
)
from supplier_reliability.services.export import export_tables
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import load_dataset
from supplier_reliability.services.scoring.evidence import quarter_windows, scorecard_windows
from supplier_reliability.services.scoring.service import CorrectionOutcome, ScoringService

SCORED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
COMPONENTS = ["delivery", "quality", "cost", "capacity", "compliance", "disruption"]


@pytest.fixture(scope="session")
def windows(gen_cfg, app_config):
    return scorecard_windows(
        gen_cfg.reference_start, gen_cfg.reference_end, app_config.scoring.window_months
    )


@pytest.fixture(scope="session")
def starter_export(detected_db, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("starter-export")
    engine = make_engine(f"sqlite:///{detected_db.path}")
    with make_session_factory(engine)() as s:
        export_tables(s, out, INDEPENDENT_TABLES)
    engine.dispose()
    return out


def _open(src: Path, target: Path) -> Iterator[Session]:
    shutil.copyfile(src, target)
    engine = make_engine(f"sqlite:///{target}")
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


@pytest.fixture
def session(detected_db, tmp_path) -> Iterator[Session]:
    yield from _open(detected_db.path, tmp_path / "recon.db")


def test_independent_metrics_reconcile_with_engine_on_starter(session, starter_export, windows):
    """Exact agreement: tolerance 0 (the 0.01 API default is never needed on this data)."""
    report = reconcile_scores(session, starter_export, windows, tolerance=Decimal(0))
    current = session.scalar(
        sa.select(sa.func.count())
        .select_from(SupplierScore)
        .where(SupplierScore.is_current.is_(True))
    )
    assert report.checked == current >= 50 * len(windows)
    assert report.mismatches == []
    assert report.missing_persisted == []
    assert report.matched == report.checked
    assert report.component_mismatch_counts == {}


def test_independent_frame_covers_every_supplier_and_window(starter_export, windows, app_config):
    params = app_config.scoring.model_dump(mode="json")
    frame = independent_supplier_metrics(starter_export, windows[-1], params)
    assert len(frame) == 50
    assert frame["supplier_id"].is_unique
    for c in COMPONENTS:
        assert f"{c}_score" in frame.columns
    # non-vacuous: every component is actually scored for some supplier in the latest window
    for c in COMPONENTS:
        assert frame[f"{c}_score"].notna().any(), c
    assert {"on_time_rate", "defect_rate", "wavp", "capacity_util", "compliance_known",
            "disruption_penalty"} <= set(frame.columns)  # fmt: skip


def _victim(session: Session, *conditions) -> SupplierScore:
    victim = session.scalar(
        sa.select(SupplierScore)
        .where(SupplierScore.is_current.is_(True), *conditions)
        .order_by(SupplierScore.score_id)
        .limit(1)
    )
    assert victim is not None
    return victim


def _tamper(session: Session, assignment: str, score_id: str) -> None:
    session.execute(
        sa.text(f"UPDATE supplier_scores SET {assignment} WHERE score_id = :i"), {"i": score_id}
    )
    session.commit()
    session.expire_all()


def test_reconciliation_detects_tampered_score(session, starter_export, windows):
    victim = _victim(
        session, SupplierScore.delivery_score.is_not(None), SupplierScore.delivery_score <= 95
    )
    original = victim.delivery_score
    _tamper(session, "delivery_score = delivery_score + 5", victim.score_id)

    report = reconcile_scores(session, starter_export, windows)
    assert len(report.mismatches) == 1
    (m,) = report.mismatches
    assert m["score_id"] == victim.score_id
    assert m["supplier_id"] == victim.supplier_id
    assert m["field"] == "delivery_score"
    assert Decimal(m["persisted"]) == original + 5
    assert Decimal(m["independent"]) == original
    assert report.component_mismatch_counts == {"delivery": 1}
    assert report.matched == report.checked - 1


def test_reconciliation_detects_a_component_set_to_null(session, starter_export, windows):
    victim = _victim(session, SupplierScore.quality_score.is_not(None))
    original = victim.quality_score
    _tamper(session, "quality_score = NULL", victim.score_id)

    report = reconcile_scores(session, starter_export, windows, tolerance=Decimal(0))
    assert [(m["score_id"], m["field"]) for m in report.mismatches] == [
        (victim.score_id, "quality_score")
    ]
    (m,) = report.mismatches
    assert m["persisted"] is None  # missing must match missing
    assert Decimal(m["independent"]) == original
    assert report.component_mismatch_counts == {"quality": 1}


def test_reconciliation_detects_a_changed_category(session, starter_export, windows):
    victim = _victim(session, SupplierScore.reliability_category != "CRITICAL")
    original = str(victim.reliability_category)
    _tamper(session, "reliability_category = 'CRITICAL'", victim.score_id)

    report = reconcile_scores(session, starter_export, windows, tolerance=Decimal(0))
    assert [(m["score_id"], m["field"]) for m in report.mismatches] == [
        (victim.score_id, "reliability_category")
    ]
    (m,) = report.mismatches
    assert (m["persisted"], m["independent"]) == ("CRITICAL", original)
    assert report.component_mismatch_counts == {"category": 1}


def test_reconciliation_detects_a_changed_evidence_coverage(session, starter_export, windows):
    victim = _victim(session)
    original = victim.evidence_coverage
    delta = Decimal("-0.05") if original >= Decimal("0.5") else Decimal("0.05")
    _tamper(session, f"evidence_coverage = evidence_coverage + ({delta})", victim.score_id)

    report = reconcile_scores(session, starter_export, windows)  # coverage is always exact
    assert [(m["score_id"], m["field"]) for m in report.mismatches] == [
        (victim.score_id, "evidence_coverage")
    ]
    (m,) = report.mismatches
    assert Decimal(m["persisted"]) == original + delta
    assert Decimal(m["independent"]) == original
    assert report.component_mismatch_counts == {"coverage": 1}


def test_reconciliation_reports_missing_persisted_scores(session, starter_export, windows):
    victim = _victim(session)
    _tamper(session, "is_current = 0", victim.score_id)  # no current score for the pair any more

    report = reconcile_scores(session, starter_export, windows, tolerance=Decimal(0))
    assert report.mismatches == []
    (missing,) = report.missing_persisted
    assert missing["supplier_id"] == victim.supplier_id
    assert missing["window"]["start"] == victim.evaluation_start.isoformat().replace("+00:00", "Z")
    assert missing["window"]["cutoff"] == victim.data_cutoff_timestamp.isoformat().replace(
        "+00:00", "Z"
    )


def test_profile_alignment_is_a_sanity_check(session, ground_truth_dir):
    result = profile_alignment(session, ground_truth_dir / "supplier_profiles.csv")
    assert result["label"] == "sanity check, not accuracy"
    assert result["window_end"].startswith("2026-06-30")
    distribution = result["distribution"]
    assert sum(sum(cats.values()) for cats in distribution.values()) == 50
    assert "stable_high" in distribution


def test_reconciliation_includes_corrected_scores(
    session, starter_export, windows, gen_cfg, app_config
):
    """Ruling: every CURRENT score of the windows is checked, CORRECTED ones included. A
    CORRECTED row is staged for a window nobody scored (the last calendar quarter), so it is
    version 1 with calculation_status CORRECTED."""
    quarter = quarter_windows(gen_cfg.reference_start, gen_cfg.reference_end)[-1]
    supplier = "SUP-000001"
    service = ScoringService(session, app_config.scoring, lambda: SCORED_AT)
    score_id, outcome = service.stage_corrected(quarter, supplier, "test: corrected coverage")
    session.commit()
    assert outcome == CorrectionOutcome.INSERTED
    assert session.get(SupplierScore, score_id).calculation_status == CalculationStatus.CORRECTED

    report = reconcile_scores(session, starter_export, [*windows, quarter], tolerance=Decimal(0))
    assert report.by_status["CORRECTED"] == 1
    assert report.checked == sum(report.by_status.values())
    assert report.mismatches == []
    assert report.matched == report.checked
    # the quarter has only that one persisted score: the other 49 suppliers are reported missing
    missing = {m["supplier_id"] for m in report.missing_persisted}
    assert len(missing) == 49 and supplier not in missing


# --- full scale (slow) ------------------------------------------------------------------------


@pytest.mark.slow
def test_full_scale_reconciliation(app_config, tmp_path):
    started = time.perf_counter()
    cfg = GeneratorConfig.from_app_config(app_config, "full")
    url = f"sqlite:///{tmp_path / 'full.db'}"
    run_migrations(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    with session_scope(factory) as s:
        load_dataset(s, generate_dataset(cfg))
    windows = scorecard_windows(
        cfg.reference_start, cfg.reference_end, app_config.scoring.window_months
    )
    with factory() as s:
        ScoringService(s, app_config.scoring, lambda: SCORED_AT).score_history(windows)
        export_tables(s, tmp_path / "export", INDEPENDENT_TABLES)
        report = reconcile_scores(s, tmp_path / "export", windows, tolerance=Decimal(0))
    engine.dispose()
    print(
        f"\nfull-scale reconciliation: matched {report.matched}/{report.checked} "
        f"in {time.perf_counter() - started:.1f}s; mismatches by component "
        f"{report.component_mismatch_counts}"
    )
    assert report.checked == 1000 * len(windows)
    assert report.mismatches == []
    assert report.missing_persisted == []
    assert report.matched == report.checked

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
from supplier_reliability.services.scoring.evidence import scorecard_windows
from supplier_reliability.services.scoring.service import ScoringService

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
    report = reconcile_scores(session, starter_export, windows)
    current = session.scalar(
        sa.select(sa.func.count())
        .select_from(SupplierScore)
        .where(SupplierScore.is_current.is_(True))
    )
    assert report.checked == current >= 50 * len(windows)
    assert report.mismatches == []
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


def test_reconciliation_detects_tampered_score(session, starter_export, windows):
    victim = session.scalar(
        sa.select(SupplierScore)
        .where(
            SupplierScore.is_current.is_(True),
            SupplierScore.delivery_score.is_not(None),
            SupplierScore.delivery_score <= 95,
        )
        .order_by(SupplierScore.score_id)
        .limit(1)
    )
    assert victim is not None
    original = victim.delivery_score
    session.execute(
        sa.text(
            "UPDATE supplier_scores SET delivery_score = delivery_score + 5 WHERE score_id = :i"
        ),
        {"i": victim.score_id},
    )
    session.commit()
    session.expire_all()

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


def test_profile_alignment_is_a_sanity_check(session, ground_truth_dir):
    result = profile_alignment(session, ground_truth_dir / "supplier_profiles.csv")
    assert result["label"] == "sanity check, not accuracy"
    assert result["window_end"].startswith("2026-06-30")
    distribution = result["distribution"]
    assert sum(sum(cats.values()) for cats in distribution.values()) == 50
    assert "stable_high" in distribution


def test_reconciliation_counts_corrected_scores_like_any_current_score(
    session, starter_export, windows
):
    """Ruling: every CURRENT score of the windows is checked, CORRECTED ones included."""
    statuses = set(
        session.scalars(
            sa.select(SupplierScore.calculation_status).where(SupplierScore.is_current.is_(True))
        )
    )
    report = reconcile_scores(session, starter_export, windows)
    assert report.checked == sum(report.by_status.values())
    assert set(report.by_status) == {CalculationStatus(s).value for s in statuses}


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
        report = reconcile_scores(s, tmp_path / "export", windows)
    engine.dispose()
    print(
        f"\nfull-scale reconciliation: matched {report.matched}/{report.checked} "
        f"in {time.perf_counter() - started:.1f}s; mismatches by component "
        f"{report.component_mismatch_counts}"
    )
    assert report.checked == 1000 * len(windows)
    assert report.mismatches == []
    assert report.matched == report.checked

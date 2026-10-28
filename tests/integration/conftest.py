"""Shared session fixtures: the seeded starter dataset loaded, scored, injected and detected,
each stage built once per session.

Tests copy the DB file before writing to it (see test_injection / test_detection)."""

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from supplier_reliability.database import (
    make_engine,
    make_session_factory,
    run_migrations,
    session_scope,
)
from supplier_reliability.services.anomalies import (
    GroundTruthCase,
    inject_anomalies,
    write_ground_truth,
)
from supplier_reliability.services.anomaly_detection import detect_raw_batch
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import load_dataset
from supplier_reliability.services.generator.rng import RandomSource
from supplier_reliability.services.scoring.evidence import scorecard_windows
from supplier_reliability.services.scoring.service import ScoringService

SCORED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DETECTED_AT = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def gen_cfg(app_config) -> GeneratorConfig:
    return GeneratorConfig.from_app_config(app_config, "starter")


@pytest.fixture(scope="session")
def loaded_starter_db(gen_cfg, tmp_path_factory) -> Path:
    """Starter dataset loaded but NOT scored (built once)."""
    path = tmp_path_factory.mktemp("starter-loaded") / "loaded.db"
    url = f"sqlite:///{path}"
    run_migrations(url)
    engine = make_engine(url)
    with session_scope(make_session_factory(engine)) as s:
        load_dataset(s, generate_dataset(gen_cfg))
    engine.dispose()
    return path


@pytest.fixture(scope="session")
def scored_starter_db(app_config, gen_cfg, loaded_starter_db, tmp_path_factory) -> Path:
    """loaded_starter_db scored for every scorecard window (built once)."""
    path = tmp_path_factory.mktemp("starter-scored") / "scored.db"
    shutil.copyfile(loaded_starter_db, path)
    engine = make_engine(f"sqlite:///{path}")
    windows = scorecard_windows(
        gen_cfg.reference_start, gen_cfg.reference_end, app_config.scoring.window_months
    )
    with make_session_factory(engine)() as s:
        ScoringService(s, app_config.scoring, lambda: SCORED_AT).score_history(windows)
    engine.dispose()
    return path


@dataclass(frozen=True)
class Injected:
    path: Path
    batch_id: str
    cases: list[GroundTruthCase]


@pytest.fixture(scope="session")
def injected_db(scored_starter_db, gen_cfg, app_config, tmp_path_factory) -> Injected:
    """scored_starter_db with the anomaly batch injected (built once)."""
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


@pytest.fixture(scope="session")
def detected_db(injected_db, app_config, tmp_path_factory) -> Injected:
    """injected_db after detect_raw_batch (built once)."""
    path = tmp_path_factory.mktemp("detected") / "detected.db"
    shutil.copyfile(injected_db.path, path)
    engine = make_engine(f"sqlite:///{path}")
    with make_session_factory(engine)() as s:
        scoring = ScoringService(s, app_config.scoring, lambda: DETECTED_AT)
        detect_raw_batch(s, injected_db.batch_id, scoring, lambda: DETECTED_AT)
    engine.dispose()
    return Injected(path, injected_db.batch_id, injected_db.cases)


@pytest.fixture(scope="session")
def ground_truth_dir(injected_db, gen_cfg, tmp_path_factory) -> Path:
    """anomaly_ground_truth.csv + supplier_profiles.csv of the starter batch."""
    out = tmp_path_factory.mktemp("ground-truth")
    write_ground_truth(injected_db.cases, dict(generate_dataset(gen_cfg).supplier_profiles), out)
    return out

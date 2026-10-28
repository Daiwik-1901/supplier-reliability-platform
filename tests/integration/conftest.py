"""Shared session fixtures: the seeded starter dataset loaded (and scored) once per session.

Tests copy the DB file before writing to it (see test_injection / test_detection)."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from supplier_reliability.database import (
    make_engine,
    make_session_factory,
    run_migrations,
    session_scope,
)
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import load_dataset
from supplier_reliability.services.scoring.evidence import scorecard_windows
from supplier_reliability.services.scoring.service import ScoringService

SCORED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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

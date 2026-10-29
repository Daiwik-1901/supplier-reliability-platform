"""API fixtures: the starter pipeline runs ONCE per session; every test gets a TestClient over its
own COPY of that database file, so no test can leak mutations into another."""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from supplier_reliability.config import AppConfig, Settings
from supplier_reliability.database import run_migrations
from supplier_reliability.main import create_app
from supplier_reliability.services.pipeline import run_pipeline

SEED = 20261022


def _settings(tmp_path: Path, db: Path, config_path: Path, **extra) -> Settings:
    return Settings(
        database_url=f"sqlite:///{db}",
        config_path=config_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        **extra,
    )


@pytest.fixture(scope="session")
def api_template_db(app_config: AppConfig, config_path: Path, tmp_path_factory) -> Path:
    """Migrated starter database with the whole pipeline already run (built once)."""
    root = tmp_path_factory.mktemp("api-template")
    db = root / "template.db"
    run_pipeline(_settings(root, db, config_path), app_config, "starter", SEED, False)
    return db


@pytest.fixture
def api_settings(api_template_db: Path, config_path: Path, tmp_path: Path) -> Settings:
    db = tmp_path / "api.db"
    shutil.copyfile(api_template_db, db)
    return _settings(tmp_path, db, config_path)


@pytest.fixture
def api_client(api_settings: Settings) -> Iterator[TestClient]:
    app = create_app(api_settings)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def empty_api_client(config_path: Path, tmp_path: Path) -> Iterator[TestClient]:
    """Migrated database with no rows at all."""
    db = tmp_path / "empty.db"
    run_migrations(f"sqlite:///{db}")
    with TestClient(create_app(_settings(tmp_path, db, config_path))) as client:
        yield client

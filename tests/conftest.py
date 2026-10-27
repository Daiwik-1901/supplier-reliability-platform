from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from supplier_reliability.config import AppConfig, Settings, load_app_config
from supplier_reliability.database import make_engine, run_migrations
from supplier_reliability.main import create_app

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "default.yaml"


@pytest.fixture(scope="session")
def config_path() -> Path:
    return CONFIG_PATH


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    return load_app_config(CONFIG_PATH)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        config_path=CONFIG_PATH,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )


@pytest.fixture
def client(tmp_settings: Settings) -> TestClient:
    return TestClient(create_app(tmp_settings))


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Engine on a temp SQLite DB whose schema was built by `alembic upgrade head`."""
    url = f"sqlite:///{tmp_path}/migrated.db"
    run_migrations(url)
    engine = make_engine(url)
    yield engine
    engine.dispose()

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from supplier_reliability.config import AppConfig, Settings, load_app_config
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

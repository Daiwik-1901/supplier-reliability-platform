"""Data-quality fixtures: a migrated SQLite template copied per test (migrating once is fast)."""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from supplier_reliability.database import make_engine, make_session_factory, run_migrations


@pytest.fixture(scope="session")
def dq_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("dq-template") / "template.db"
    run_migrations(f"sqlite:///{path}")
    return path


@pytest.fixture
def db_session(dq_template: Path, tmp_path: Path) -> Iterator[Session]:
    """Session on a fresh copy of the migrated schema (FKs enforced)."""
    path = tmp_path / "dq.db"
    shutil.copyfile(dq_template, path)
    engine = make_engine(f"sqlite:///{path}")
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()

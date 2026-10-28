"""Scoring test fixtures: a migrated SQLite template copied per test (migrating once is fast)."""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from supplier_reliability.database import make_engine, make_session_factory, run_migrations

from .factories import GraphBuilder


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("scoring-template") / "template.db"
    run_migrations(f"sqlite:///{path}")
    return path


@pytest.fixture
def db_session(migrated_template: Path, tmp_path: Path) -> Iterator[Session]:
    """Session on a fresh copy of the migrated schema (FKs enforced)."""
    path = tmp_path / "scoring.db"
    shutil.copyfile(migrated_template, path)
    engine = make_engine(f"sqlite:///{path}")
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def graph(db_session: Session) -> GraphBuilder:
    return GraphBuilder(db_session)

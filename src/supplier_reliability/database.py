"""Engine / session helpers. SQLite connections always run with foreign keys enabled."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    kwargs: dict = {}
    is_sqlite = make_url(url).get_backend_name() == "sqlite"
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on any exception, always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


_REPO_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str) -> Config:
    """Alembic config bound to `database_url` (env.py prefers it over env var / Settings)."""
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    # set_main_option goes through ConfigParser interpolation, so "%" must be doubled
    script_location = str(_REPO_ROOT / "alembic").replace("%", "%%")
    cfg.set_main_option("script_location", script_location)
    cfg.attributes["database_url"] = database_url
    return cfg


def run_migrations(database_url: str) -> None:
    """Upgrade the database at `database_url` to the latest revision."""
    command.upgrade(alembic_config(database_url), "head")

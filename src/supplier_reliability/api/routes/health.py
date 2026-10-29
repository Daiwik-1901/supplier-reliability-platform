"""Liveness, readiness and metadata. Nothing here returns exception text or filesystem paths."""

import logging
from functools import lru_cache
from pathlib import Path

import sqlalchemy as sa
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.engine import make_url

from supplier_reliability import GENERATOR_VERSION, SCHEMA_VERSION, __version__
from supplier_reliability.api.dependencies import AppConfigDep, DbDep
from supplier_reliability.api.errors import error_responses
from supplier_reliability.database import alembic_config
from supplier_reliability.models import DatasetManifest
from supplier_reliability.schemas.common import HealthOut, MetadataOut, ReadyOut
from supplier_reliability.services.scoring.service import scoring_config_hash

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@lru_cache
def _head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head() or ""


def _missing_sqlite_file(url: str) -> bool:
    """True for a file-based SQLite URL whose file does not exist: connecting would
    silently create an empty database, so readiness must not try."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return False
    database = parsed.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return False
    return not Path(database).exists()


def _not_ready(database: str) -> JSONResponse:
    return JSONResponse(status_code=503, content={"status": "not_ready", "database": database})


@router.get("/health", response_model=HealthOut, summary="Liveness")
def health() -> HealthOut:
    """Liveness only: the process answers. Does not touch the database."""
    return HealthOut(status="ok", version=__version__)


@router.get(
    "/ready",
    response_model=ReadyOut,
    summary="Readiness",
    description="Runs `SELECT 1` and compares the database's alembic revision with the code's "
    "head. 503 with `database: unavailable` (cannot connect) or `not_migrated` (schema is not "
    "at head). Error details are logged, never returned.",
    responses={
        503: {
            "model": ReadyOut,
            "description": "Database unavailable or not migrated",
            "content": {
                "application/json": {"example": {"status": "not_ready", "database": "unavailable"}}
            },
        }
    },
)
def ready(request: Request) -> ReadyOut | JSONResponse:
    engine = request.app.state.engine
    if engine is None or _missing_sqlite_file(request.app.state.settings.database_url):
        return _not_ready("unavailable")
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            current = MigrationContext.configure(conn).get_current_revision()
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"; never leak it
        logger.warning("readiness check failed: %s", type(exc).__name__, exc_info=exc)
        return _not_ready("unavailable")
    head = _head_revision()
    if current is None or current != head:
        return _not_ready("not_migrated")
    return ReadyOut(status="ready", database="ok", schema_version=head)


@router.get(
    "/api/v1/metadata",
    response_model=MetadataOut,
    summary="Platform and dataset metadata",
    description="Versions, the current dataset id (null when nothing is loaded), the SHA-256 of "
    "the scoring configuration (`scoring_config_hash`), weights, category thresholds and the "
    "synthetic reference period. No filesystem paths or connection strings.",
    responses=error_responses(),
)
def metadata(db: DbDep, config: AppConfigDep) -> MetadataOut:
    stmt = (
        sa.select(DatasetManifest.dataset_id)
        .order_by(DatasetManifest.created_at.desc(), DatasetManifest.dataset_id.desc())
        .limit(1)
    )
    scoring = config.scoring
    return MetadataOut(
        app_version=__version__,
        generator_version=GENERATOR_VERSION,
        scoring_version=scoring.scoring_version,
        schema_version=SCHEMA_VERSION,
        dataset_id=db.scalar(stmt),
        scoring_config_hash=scoring_config_hash(scoring),
        weights=dict(scoring.weights),
        category_thresholds={k: v.model_dump() for k, v in scoring.category_thresholds.items()},
        reference_period=config.generator.reference_period,
    )

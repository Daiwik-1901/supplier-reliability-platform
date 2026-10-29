import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from supplier_reliability import __version__
from supplier_reliability.api.dependencies import wall_clock
from supplier_reliability.api.errors import register_error_handlers
from supplier_reliability.api.middleware import RequestSizeLimitMiddleware
from supplier_reliability.api.routes import (
    datasets,
    exceptions,
    health,
    reports,
    scores,
    suppliers,
    transactions,
    validation,
)
from supplier_reliability.config import Settings, get_settings
from supplier_reliability.database import make_engine, make_session_factory
from supplier_reliability.logging_config import configure_logging
from supplier_reliability.services.jobs import recover_interrupted_jobs

logger = logging.getLogger(__name__)


def _expose_limit_maximum(app: FastAPI, maximum: int) -> None:
    """Show the configured page-size maximum on every `limit` parameter in the schema."""
    generate = app.openapi

    def openapi() -> dict:
        schema = generate()
        for item in schema.get("paths", {}).values():
            for operation in item.values():
                for param in operation.get("parameters", []):
                    if param.get("name") == "limit":
                        param["schema"]["maximum"] = maximum
        return schema

    app.openapi = openapi


def _recover_jobs(app: FastAPI) -> None:
    """Jobs left QUEUED/RUNNING by a previous process can never finish (process-local runner)."""
    factory = app.state.session_factory
    if factory is None:
        return
    try:
        recover_interrupted_jobs(factory, wall_clock)
    except Exception as exc:  # noqa: BLE001 - e.g. database not migrated yet; startup must go on
        logger.warning("job recovery skipped: %s", type(exc).__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    # An unusable URL must not stop the process: /health stays up and /ready reports 503.
    try:
        engine = make_engine(settings.database_url)
    except Exception as exc:  # noqa: BLE001 - never log the URL (it may hold credentials)
        logger.warning("database engine could not be created: %s", type(exc).__name__)
        engine = None

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        _recover_jobs(app_)
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(
        title="Supplier Reliability Scoring Platform",
        version=__version__,
        description="Read API over synthetic supplier, order, shipment and score data. "
        "Errors use the envelope `{error: {code, message, details}}`.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine) if engine is not None else None
    app.state.job_stages = {}  # job_id -> last pipeline stage (best effort, lost on restart)
    app.state.app_config = None  # loaded lazily by api.dependencies.get_app_config

    _expose_limit_maximum(app, settings.max_page_size)
    register_error_handlers(app)
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(health.router)
    app.include_router(suppliers.router)
    app.include_router(transactions.router)
    app.include_router(scores.router)
    app.include_router(validation.router)
    app.include_router(exceptions.router)
    app.include_router(datasets.router)
    app.include_router(reports.router)
    app.include_router(reports.evaluation_router)
    return app


app = create_app()

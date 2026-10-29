from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from supplier_reliability import __version__
from supplier_reliability.api.errors import register_error_handlers
from supplier_reliability.api.middleware import RequestSizeLimitMiddleware
from supplier_reliability.api.routes import health, suppliers, transactions
from supplier_reliability.config import Settings, get_settings
from supplier_reliability.database import make_engine, make_session_factory
from supplier_reliability.logging_config import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    # An unusable URL must not stop the process: /health stays up and /ready reports 503.
    try:
        engine = make_engine(settings.database_url)
    except Exception:  # noqa: BLE001 - the URL (and its credentials) are not logged
        engine = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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
    app.state.app_config = None  # loaded lazily by api.dependencies.get_app_config

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
    return app


app = create_app()

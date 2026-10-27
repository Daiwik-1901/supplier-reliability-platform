from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from supplier_reliability import __version__
from supplier_reliability.api.routes import health
from supplier_reliability.config import Settings, get_settings
from supplier_reliability.logging_config import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    app = FastAPI(title="Supplier Reliability Scoring Platform", version=__version__)
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(health.router)
    return app


app = create_app()

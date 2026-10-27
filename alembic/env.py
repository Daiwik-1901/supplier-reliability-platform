"""Alembic environment. URL precedence: programmatic config > SRP_DATABASE_URL > Settings."""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from supplier_reliability.config import Settings
from supplier_reliability.models import Base  # imports every model module

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    return (
        config.attributes.get("database_url")
        or os.environ.get("SRP_DATABASE_URL")
        or Settings().database_url
    )


def _configure_kwargs(url: str) -> dict:
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
        "render_as_batch": make_url(url).get_backend_name() == "sqlite",
    }


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(url=url, literal_binds=True, **_configure_kwargs(url))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _database_url()
    # Pass the URL unchanged: it never goes through ConfigParser interpolation here.
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, **_configure_kwargs(url))
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

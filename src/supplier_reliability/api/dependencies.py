"""FastAPI dependencies: settings, DB session, app config, pagination and whitelisted sorting."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from supplier_reliability.api.errors import InvalidRequestError
from supplier_reliability.config import AppConfig, Settings, load_app_config

DEFAULT_PAGE_SIZE = 50


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    """One session per request, closed afterwards. Read endpoints never commit."""
    factory = request.app.state.session_factory
    if factory is None:
        raise RuntimeError("database is not configured")
    session = factory()
    try:
        yield session
    finally:
        session.close()


def get_app_config(request: Request) -> AppConfig:
    """The YAML config, loaded on first use and cached on the app."""
    state = request.app.state
    if getattr(state, "app_config", None) is None:
        state.app_config = load_app_config(state.settings.config_path)
    return state.app_config


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]
AppConfigDep = Annotated[AppConfig, Depends(get_app_config)]


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def pagination(
    settings: SettingsDep,
    limit: Annotated[
        int,
        Query(ge=1, description="Page size, 1 to the server maximum (default 50).", examples=[10]),
    ] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, description="Rows to skip.", examples=[0])] = 0,
) -> Pagination:
    if limit > settings.max_page_size:
        raise InvalidRequestError(
            ["query", "limit"],
            f"Input should be less than or equal to {settings.max_page_size}",
            "less_than_equal",
        )
    return Pagination(limit=limit, offset=offset)


PageParams = Annotated[Pagination, Depends(pagination)]


@dataclass(frozen=True)
class Sort:
    field: str
    descending: bool


def sort_dependency(allowed: Sequence[str], default: str) -> Callable[..., Sort]:
    """Dependency for a `sort=field:asc|desc` parameter restricted to `allowed` fields."""
    names = list(allowed)
    first = names[0]

    def parse(
        sort: Annotated[
            str | None,
            Query(
                description=f"`field:asc|desc`, field one of {', '.join(names)}. "
                f"Default `{default}`.",
                examples=[f"{first}:desc"],
            ),
        ] = None,
    ) -> Sort:
        raw = sort or default
        field, _, direction = raw.partition(":")
        if field not in names or direction not in {"asc", "desc"}:
            raise InvalidRequestError(
                ["query", "sort"],
                f"Sort must be 'field:asc' or 'field:desc' with field one of {', '.join(names)}",
            )
        return Sort(field=field, descending=direction == "desc")

    return parse

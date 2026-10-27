"""Declarative base, constraint naming convention, UTC datetime type and enum column helper."""

from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


class UTCDateTime(sa.TypeDecorator):
    """Stores UTC, returns timezone-aware UTC on every backend, rejects naive datetimes."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime rejected: timezone-aware datetime required")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:  # SQLite drops tzinfo; what we stored was UTC
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def enum_type(enum_cls: type[StrEnum], name: str | None = None) -> sa.Enum:
    """Non-native enum with a named CHECK constraint (portable across SQLite and PostgreSQL)."""
    return sa.Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        name=name or enum_cls.__name__.lower(),
        length=max(len(m.value) for m in enum_cls),
        values_callable=lambda cls: [m.value for m in cls],
    )

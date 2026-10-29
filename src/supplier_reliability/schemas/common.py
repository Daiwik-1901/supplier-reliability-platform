"""Shared API schemas: error envelope, pagination page, serialisation types, health payloads."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _money(value: Decimal) -> str:
    """Fixed two-decimal string (half-up), never scientific notation, zero unsigned."""
    q = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(q.copy_abs() if q.is_zero() else q, "f")


def _utc_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# Decimal money/quantities travel as strings ("1234.50"); floats never carry money.
Money = Annotated[Decimal, PlainSerializer(_money, return_type=str, when_used="json")]
# Timestamps are ISO-8601 UTC with a trailing Z.
UtcDatetime = Annotated[datetime, PlainSerializer(_utc_z, return_type=str, when_used="json")]


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] | None = None


class ErrorEnvelope(BaseModel):
    """Every non-2xx response from /api/v1 has exactly this shape."""

    error: ErrorBody

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "not_found",
                        "message": "Supplier 'SUP-999999' not found",
                        "details": None,
                    }
                },
                {
                    "error": {
                        "code": "validation_error",
                        "message": "Request validation failed",
                        "details": [
                            {
                                "loc": ["query", "limit"],
                                "msg": "Input should be less than or equal to 500",
                                "type": "less_than_equal",
                            }
                        ],
                    }
                },
            ]
        }
    )


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class HealthOut(BaseModel):
    status: str
    version: str


class ReadyOut(BaseModel):
    status: str
    database: str
    schema_version: str | None = None


class Threshold(BaseModel):
    lower: float
    upper: float


class ReferencePeriod(BaseModel):
    start: str
    end: str


class MetadataOut(BaseModel):
    app_version: str
    generator_version: str
    scoring_version: str
    schema_version: str
    dataset_id: str | None
    scoring_config_hash: str
    weights: dict[str, float]
    category_thresholds: dict[str, Threshold]
    reference_period: ReferencePeriod

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "app_version": "0.1.0",
                    "generator_version": "1.0.0",
                    "scoring_version": "1.0.0",
                    "schema_version": "1",
                    "dataset_id": "starter-20261022",
                    "scoring_config_hash": "9f2c...e1",
                    "weights": {"delivery": 0.3, "quality": 0.25},
                    "category_thresholds": {"HIGH": {"lower": 85, "upper": 100}},
                    "reference_period": {
                        "start": "2025-01-01T00:00:00Z",
                        "end": "2026-06-30T23:59:59Z",
                    },
                }
            ]
        }
    )


def details_of(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """{loc, msg, type} per validation error. The offending input and ctx are dropped so request
    values are never reflected back."""
    return [{"loc": [p for p in e["loc"]], "msg": e["msg"], "type": e["type"]} for e in errors]

"""Numeric and time helpers. Money and scores are Decimal, quantized HALF_UP."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

_TWO_DP = Decimal("0.01")


def _to_decimal(x: Decimal | int | str | float) -> Decimal:
    # str() of a float gives the shortest repr, so 84.995 stays 84.995 rather than binary noise.
    return x if isinstance(x, Decimal) else Decimal(str(x))


def to_money(x: Decimal | int | str) -> Decimal:
    return _to_decimal(x).quantize(_TWO_DP, rounding=ROUND_HALF_UP)


def round_score(x: float | Decimal) -> Decimal:
    return _to_decimal(x).quantize(_TWO_DP, rounding=ROUND_HALF_UP)


def safe_ratio(n: Decimal | int | str, d: Decimal | int | str) -> Decimal | None:
    """n / d, or None when d == 0 (missing is not zero)."""
    denominator = _to_decimal(d)
    if denominator == 0:
        return None
    return _to_decimal(n) / denominator


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime rejected: timezone-aware datetime required")
    return dt.astimezone(UTC)

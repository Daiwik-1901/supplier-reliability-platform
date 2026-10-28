"""Export number formatting: what our converter does beyond SQLAlchemy's own result processing."""

from decimal import Decimal

import pytest

from supplier_reliability.services.export import format_decimal


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        (Decimal("2.675"), 2, "2.68"),  # ROUND_HALF_UP, more digits than the scale
        (Decimal("2.665"), 2, "2.67"),  # half-up, not banker's rounding (would give 2.66)
        (Decimal("-2.675"), 2, "-2.68"),
        (Decimal("1234.5"), 2, "1234.50"),  # padded to the scale
        (Decimal("7"), 6, "7.000000"),
        (Decimal("1E+3"), 2, "1000.00"),  # never scientific notation
        (Decimal("0.0000004"), 6, "0.000000"),
    ],
)
def test_format_decimal_quantises_half_up_to_column_scale(value, scale, expected):
    assert format_decimal(value, scale) == expected


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        (Decimal("-0.00"), 2, "0.00"),
        (Decimal("-0"), 2, "0.00"),
        (Decimal("-0.004"), 2, "0.00"),  # rounds to zero: still unsigned
        (Decimal("-0E-6"), 6, "0.000000"),
        (Decimal("0"), 2, "0.00"),
    ],
)
def test_format_decimal_never_emits_negative_zero(value, scale, expected):
    """SQLite and PostgreSQL may disagree on the sign of zero; the export must not."""
    assert format_decimal(value, scale) == expected

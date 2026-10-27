from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from supplier_reliability.domain.enums import (
    DELIVERY_EVENT_TRANSITIONS,
    DeliveryEventType,
    is_valid_transition,
)
from supplier_reliability.domain.numeric import ensure_utc, round_score, safe_ratio, to_money


def test_to_money_rounds_half_up():
    assert to_money("2.345") == Decimal("2.35")
    assert to_money("-2.345") == Decimal("-2.35")
    assert to_money(3) == Decimal("3.00")
    assert str(to_money(Decimal("1.004"))) == "1.00"


def test_round_score_half_up_from_float_and_decimal():
    assert round_score(84.995) == Decimal("85.00")
    assert round_score(Decimal("70.125")) == Decimal("70.13")


def test_safe_ratio_zero_denominator_is_none():
    assert safe_ratio(5, 0) is None
    assert safe_ratio(Decimal("1"), Decimal("0.00")) is None


def test_safe_ratio_value():
    assert safe_ratio(1, 4) == Decimal("0.25")


def test_ensure_utc_rejects_naive():
    with pytest.raises(ValueError):
        ensure_utc(datetime(2025, 1, 1))


def test_ensure_utc_converts_offset():
    tz = timezone(timedelta(hours=5))
    out = ensure_utc(datetime(2025, 1, 1, 5, 0, tzinfo=tz))
    assert out == datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    assert out.utcoffset() == timedelta(0)


E = DeliveryEventType


def test_transition_table_matches_d4():
    assert DELIVERY_EVENT_TRANSITIONS[None] == {E.BOOKED}
    assert DELIVERY_EVENT_TRANSITIONS[E.IN_TRANSIT] == {E.DELAYED, E.ARRIVED}
    assert DELIVERY_EVENT_TRANSITIONS[E.DELAYED] == {E.DELAYED, E.ARRIVED}
    assert DELIVERY_EVENT_TRANSITIONS[E.RECEIVED] == frozenset()


@pytest.mark.parametrize(
    ("prev", "nxt", "ok"),
    [
        (None, E.BOOKED, True),
        (None, E.DISPATCHED, False),
        (E.BOOKED, E.DISPATCHED, True),
        (E.DISPATCHED, E.ARRIVED, False),
        (E.DELAYED, E.DELAYED, True),
        (E.ARRIVED, E.RECEIVED, True),
        (E.RECEIVED, E.BOOKED, False),
        (E.BOOKED, E.CORRECTION, True),
        (E.RECEIVED, E.CORRECTION, True),
        (None, E.CORRECTION, False),
    ],
)
def test_is_valid_transition(prev, nxt, ok):
    assert is_valid_transition(prev, nxt) is ok

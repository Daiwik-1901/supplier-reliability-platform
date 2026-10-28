"""Shipment timelines, delivery events (D4) and CORRECTION events (D13) for Task 7.

Timeline of one shipment (all times whole minutes, UTC):
BOOKED (>= PO created_at) -> DISPATCHED -> IN_TRANSIT -> DELAYED (late only, before ARRIVED)
-> ARRIVED (= actual delivery) -> RECEIVED (>= ARRIVED).

Rules:
- Every ``event_timestamp`` is <= reference_end. A shipment whose arrival would fall after
  reference_end has no ARRIVED/RECEIVED event (it stays IN_TRANSIT).
- ``recorded_timestamp = event_timestamp + lag`` (0-2 h normally, 1-10 days for ~5% of events).
  If that would exceed reference_end it is set to reference_end (still >= event_timestamp), so
  no generated record is "from the future".
- Dispatch is spread over [booking, required - transit] (split j of k in order). When that
  would fall after reference_end - 1 day, it is re-drawn uniformly over the whole allowed window
  [booking + 1 h, reference_end - 1 day], so there is no artificial cluster at the period end.
- Lateness uses the profile trend at fraction (expected - reference_start) / (reference_end -
  reference_start), clamped to [0, 1]. The late/on-time boundary is the D5 grace
  (``GeneratorConfig.grace_hours``, from ``scoring.grace_hours``): a late shipment arrives more
  than the grace after expected; an on-time one at most grace/2 after expected.
- Corrections: ~1% of delivered shipments get a CORRECTION event whose metadata is
  ``{"corrects_event_id": <ARRIVED id>, "field": "event_timestamp", "corrected_value": <ISO-8601
  UTC>}``, recorded strictly after the ARRIVED event was recorded. The corrected value differs
  by 1-48 h and stays after IN_TRANSIT/DELAYED and at or before RECEIVED (hence >= DISPATCHED
  and <= reference_end). ``Shipment.actual_delivery_timestamp`` stores the CORRECTED (latest
  known) value because the shipment row is the current-state view; the original value remains
  on the ARRIVED event, and evidence collection (Task 9) applies a correction only when it was
  recorded <= the cutoff.
- Because a correction moves the arrival by up to 48 h, for some corrected shipments (a subset
  of the ~1%) the corrected arrival lies on the other side of the late boundary than the
  original. The DELAYED event (present iff the originally recorded ARRIVED was late) can then
  disagree with ``Shipment.actual_delivery_timestamp``. Both are internally consistent: DELAYED
  always agrees with the ARRIVED event. Cutoff-aware logic must therefore read events by
  recording time (ARRIVED, then any CORRECTION recorded <= cutoff) and never the shipment row.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from supplier_reliability.config import ProfileParams
from supplier_reliability.domain.enums import DeliveryEventType
from supplier_reliability.services.generator.dataset import GeneratedDataset
from supplier_reliability.services.generator.ids import IdRegistry

E = DeliveryEventType
_LATE_RECORDING_P = 0.05
_CORRECTION_FRACTION = 0.01
_SOURCE = {
    E.BOOKED: "SYNTHETIC_ERP",
    E.DISPATCHED: "SYNTHETIC_TMS",
    E.IN_TRANSIT: "SYNTHETIC_TMS",
    E.DELAYED: "SYNTHETIC_TMS",
    E.ARRIVED: "SYNTHETIC_WMS",
    E.RECEIVED: "SYNTHETIC_WMS",
    E.CORRECTION: "SYNTHETIC_TMS_CORRECTION",
}


def _minutes(rng: np.random.Generator, lo: int, hi: int) -> timedelta:
    """Uniform whole-minute duration in [lo, hi] minutes."""
    return timedelta(minutes=int(rng.integers(lo, hi + 1)))


@dataclass
class Timeline:
    booking: datetime
    dispatch: datetime
    expected: datetime
    in_transit: datetime
    delayed: datetime | None  # None when on time or the DELAYED moment is after the period
    arrival: datetime | None  # None when arrival falls after reference_end
    received: datetime | None


def draw_timeline(
    rng: np.random.Generator,
    po: dict[str, Any],
    index: int,
    n_shipments: int,
    profile: ProfileParams,
    ref_start: datetime,
    ref_end: datetime,
    grace: timedelta,
) -> Timeline:
    """Shipment ``index`` of ``n_shipments`` for ``po``: split shipments dispatch in order."""
    booking = po["created_at"] + _minutes(rng, 60, 48 * 60)
    required = datetime.combine(po["required_delivery_date"], datetime.min.time(), ref_start.tzinfo)
    required += timedelta(hours=12)
    transit = _minutes(rng, 2 * 1440, 10 * 1440)
    span_min = max(0, int((required - transit - booking).total_seconds() // 60))
    frac = (index + float(rng.random())) / n_shipments
    dispatch = booking + timedelta(minutes=max(60, int(span_min * frac)))
    cap = ref_end - timedelta(days=1)  # leaves room for IN_TRANSIT inside the period
    if dispatch > cap:  # re-draw uniformly over the whole allowed window [booking + 1 h, cap]
        window_min = int((cap - booking).total_seconds() // 60)
        dispatch = booking + _minutes(rng, 60, max(60, window_min))
    expected = dispatch + transit
    in_transit = dispatch + _minutes(rng, 60, 6 * 60)
    fraction = min(1.0, max(0.0, (expected - ref_start) / (ref_end - ref_start)))
    late = float(rng.random()) >= profile.at("on_time_p", fraction)
    delayed: datetime | None = None
    if late:
        mean_hours = profile.at("mean_late_days", fraction) * 24
        grace_hours = grace.total_seconds() / 3600
        extra_hours = float(rng.exponential(max(mean_hours - grace_hours, 12.0)))
        arrival = expected + grace + timedelta(minutes=1 + int(extra_hours * 60))
        delayed = expected - _minutes(rng, 0, 12 * 60)
        if delayed > ref_end:
            delayed = None
    else:  # at most grace/2 after expected (12 h with the default 24 h grace)
        arrival = expected + _minutes(rng, -36 * 60, int(grace.total_seconds() // 120))
    received: datetime | None = min(arrival + _minutes(rng, 60, 24 * 60), ref_end)
    if arrival > ref_end:
        arrival = received = None
    return Timeline(booking, dispatch, expected, in_transit, delayed, arrival, received)


def _record(rng: np.random.Generator, at: datetime, ref_end: datetime) -> datetime:
    if float(rng.random()) < _LATE_RECORDING_P:
        lag = _minutes(rng, 1440, 10 * 1440)
    else:
        lag = _minutes(rng, 0, 120)
    return min(at + lag, ref_end)


def emit_events(
    rng: np.random.Generator,
    ids: IdRegistry,
    ds: GeneratedDataset,
    shipment: dict[str, Any],
    tl: Timeline,
    ref_end: datetime,
) -> dict[DeliveryEventType, dict[str, Any]]:
    """Append the shipment's events in sequence order; returns them by type."""
    site, warehouse = shipment["originating_site_id"], shipment["destination_warehouse_id"]
    plan: list[tuple[DeliveryEventType, datetime | None, str | None]] = [
        (E.BOOKED, tl.booking, site),
        (E.DISPATCHED, tl.dispatch, site),
        (E.IN_TRANSIT, tl.in_transit, None),
        (E.DELAYED, tl.delayed, None),
        (E.ARRIVED, tl.arrival, warehouse),
        (E.RECEIVED, tl.received, warehouse),
    ]
    out: dict[DeliveryEventType, dict[str, Any]] = {}
    for etype, at, location in plan:
        if at is None:
            continue
        row = {
            "delivery_event_id": ids.next("event"),
            "shipment_id": shipment["shipment_id"],
            "event_type": etype,
            "event_timestamp": at,
            "recorded_timestamp": _record(rng, at, ref_end),
            "location_id": location,
            "source_system": _SOURCE[etype],
            "event_metadata": None,
        }
        ds.delivery_events.append(row)
        out[etype] = row
    return out


def apply_corrections(
    rng: np.random.Generator,
    ids: IdRegistry,
    ds: GeneratedDataset,
    delivered: list[tuple[dict[str, Any], dict[DeliveryEventType, dict[str, Any]]]],
    ref_end: datetime,
) -> None:
    """Emit CORRECTION events for ~1% of delivered shipments (exact, sorted subset)."""
    one_hour = timedelta(hours=1)
    candidates = []
    for shipment, events in delivered:
        arrived = events[E.ARRIVED]
        before = events.get(E.DELAYED, events[E.IN_TRANSIT])["event_timestamp"]
        down = arrived["event_timestamp"] - before
        up = events[E.RECEIVED]["event_timestamp"] - arrived["event_timestamp"]
        if arrived["recorded_timestamp"] <= ref_end - 2 * one_hour and max(down, up) >= one_hour:
            candidates.append((shipment, events, down, up))
    k = min(len(candidates), max(1, round(_CORRECTION_FRACTION * len(delivered))))
    chosen = sorted(int(i) for i in rng.permutation(len(candidates))[:k])
    for pos in chosen:
        shipment, events, down, up = candidates[pos]
        arrived = events[E.ARRIVED]
        options = [(sign, room) for sign, room in ((-1, down), (1, up)) if room >= one_hour]
        sign, room = options[int(rng.integers(len(options)))]
        max_min = min(48 * 60, int(room.total_seconds() // 60))
        corrected = arrived["event_timestamp"] + sign * _minutes(rng, 60, max_min)
        at = min(arrived["recorded_timestamp"] + _minutes(rng, 60, 14 * 1440), ref_end - one_hour)
        ds.delivery_events.append(
            {
                "delivery_event_id": ids.next("event"),
                "shipment_id": shipment["shipment_id"],
                "event_type": E.CORRECTION,
                "event_timestamp": at,
                "recorded_timestamp": min(at + _minutes(rng, 0, 120), ref_end),
                "location_id": None,
                "source_system": _SOURCE[E.CORRECTION],
                "event_metadata": {
                    "corrects_event_id": arrived["delivery_event_id"],
                    "field": "event_timestamp",
                    "corrected_value": corrected.isoformat(),
                },
            }
        )
        shipment["actual_delivery_timestamp"] = corrected

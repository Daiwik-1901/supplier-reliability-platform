"""Delivery, recomputed from shipments.csv + delivery_events.csv (D4, D5, D6, D13, R12, R15).

- Due: expected_delivery_timestamp in [S, min(E, C)], shipment created_at <= C, not CANCELLED.
- Arrival known at C comes from events only (never shipments.actual_delivery_timestamp, which
  holds today's corrected value): among the shipment's ARRIVED events with event_timestamp <= C
  and recorded_timestamp <= C, the first by (recorded_timestamp, id). Its timestamp is replaced
  by the corrected_value of the latest (by recorded_timestamp, id) CORRECTION recorded <= C whose
  metadata names that event and the field "event_timestamp"; a missing/unparseable/naive
  corrected_value is ignored. A corrected arrival after C is not known at C (R15).
- On time iff arrival <= expected + grace. Late shipments contribute arrival - expected; due
  shipments without a known arrival are overdue and contribute C - expected (fractional days).
- score = 100 * on_time / due - min(cap, per_day * mean days of the late ones); missing below
  min_observations.delivery.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from ._tables import HUNDRED, ZERO, Params, Tables, Window, clamp_round, days


def _correction(meta: Any) -> tuple[Any, Any, pd.Timestamp | None]:
    """(corrects_event_id, field, corrected_value as UTC Timestamp or None)."""
    if not isinstance(meta, str):
        return None, None, None
    m = json.loads(meta)
    try:
        value = datetime.fromisoformat(m.get("corrected_value"))
    except (TypeError, ValueError):
        value = None
    if value is None or value.tzinfo is None:
        return m.get("corrects_event_id"), m.get("field"), None
    return m.get("corrects_event_id"), m.get("field"), pd.Timestamp(value).tz_convert("UTC")


def known_arrivals(t: Tables, w: Window, shipment_ids: set[str]) -> dict[str, pd.Timestamp]:
    """shipment_id -> arrival known at C (absent: no arrival known)."""
    ev = t["delivery_events"]
    ev = ev[ev["shipment_id"].isin(shipment_ids) & (ev["recorded_timestamp"] <= w.cutoff)]
    order = ["recorded_timestamp", "delivery_event_id"]
    arrived = (
        ev[(ev["event_type"] == "ARRIVED") & (ev["event_timestamp"] <= w.cutoff)]
        .sort_values(order)
        .drop_duplicates("shipment_id", keep="first")
    )
    when = dict(zip(arrived["delivery_event_id"], arrived["event_timestamp"], strict=True))
    for meta in ev[ev["event_type"] == "CORRECTION"].sort_values(order)["event_metadata"]:
        target, field, value = _correction(meta)
        if target in when and field == "event_timestamp" and value is not None:
            when[target] = value  # later (recorded, id) corrections win
    return {
        shipment_id: when[event_id]
        for event_id, shipment_id in zip(
            arrived["delivery_event_id"], arrived["shipment_id"], strict=True
        )
        if when[event_id] <= w.cutoff
    }


def delivery(t: Tables, w: Window, p: Params) -> dict[str, dict[str, Any]]:
    s = t["shipments"].merge(t.po_supplier(), on="po_id")
    expected = s["expected_delivery_timestamp"]
    due = s[
        (expected >= w.start)
        & (expected <= w.top)
        & (s["created_at"] <= w.cutoff)
        & (s["shipment_status"] != "CANCELLED")
    ].sort_values("shipment_id")
    arrivals = known_arrivals(t, w, set(due["shipment_id"]))
    out: dict[str, dict[str, Any]] = {}
    for supplier, group in due.groupby("supplier_id"):
        n, on_time, late = len(group), 0, []
        for shipment_id, exp in zip(
            group["shipment_id"], group["expected_delivery_timestamp"], strict=True
        ):
            arrived = arrivals.get(shipment_id)
            if arrived is None:
                late.append(days(w.cutoff - exp))  # overdue at C
            elif arrived <= exp + p.grace:
                on_time += 1
            else:
                late.append(days(arrived - exp))
        rate = Decimal(on_time) / Decimal(n)
        mean_late = sum(late, ZERO) / len(late) if late else ZERO
        score = None
        if n >= p.min_obs["delivery"]:
            penalty = min(p.penalty_cap, p.penalty_per_day * mean_late)
            score = clamp_round(HUNDRED * rate - penalty)
        out[str(supplier)] = {
            "delivery_due": n,
            "delivery_on_time": on_time,
            "on_time_rate": rate,
            "mean_late_days": mean_late,
            "delivery_score": score,
        }
    return out

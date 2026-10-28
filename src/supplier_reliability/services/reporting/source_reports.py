"""Reports computed from canonical source records: entity counts, delivery and quality trends,
invoice variance. Money and quantities are summed in Python on Decimal."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

import supplier_reliability.models  # noqa: F401  (complete Base.metadata)
from supplier_reliability.domain.enums import ShipmentStatus
from supplier_reliability.models import Base, Inspection, Invoice, Shipment, SupplierScore
from supplier_reliability.services.reporting.common import report, score_windows
from supplier_reliability.services.reporting.metrics import count_metric, counted, metric, ratio
from supplier_reliability.services.scoring.evidence import quarter_windows

__all__ = ["delivery_quality_trends", "entity_counts", "invoice_variance_distribution"]

ALL_TIME = "all persisted records"
ZERO = Decimal("0")


def entity_counts(session: Session) -> dict[str, Any]:
    rows = {
        t.name: count_metric(
            session.scalar(sa.select(sa.func.count()).select_from(t)) or 0,
            "none (every row of the table)",
            ALL_TIME,
        )
        for t in Base.metadata.sorted_tables
    }
    current = session.scalar(sa.select(sa.func.count()).where(SupplierScore.is_current.is_(True)))
    metrics = {
        "rows": rows,
        "current_supplier_scores": count_metric(
            current or 0, "supplier_scores with is_current = true", ALL_TIME
        ),
    }
    return report("entity_counts", "Entity row counts", metrics)


def _reference_period(session: Session):
    windows = score_windows(session)
    if not windows:
        raise LookupError("no current supplier scores; run scoring before reporting")
    return windows[0].start, windows[-1].end


def _quarter_label(start) -> str:
    return f"{start.year}Q{(start.month - 1) // 3 + 1}"


def delivery_quality_trends(session: Session, grace_hours: int) -> dict[str, Any]:
    """Per calendar quarter of the reference period, from canonical source records.

    On-time rate = shipments whose arrival is on or before expected_delivery + grace, over
    non-cancelled shipments with expected_delivery in the quarter and a known arrival.
    Arrival is Shipment.actual_delivery_timestamp, the CORRECTED current ARRIVED time (so
    corrections recorded after a historical cutoff are applied; this is an as-of-now trend, not
    a point-in-time score input). Due shipments without an arrival are reported separately and
    excluded from the denominator.
    Defect rate = sum(defect_quantity) / sum(inspected_quantity) of the latest inspection (by
    inspection_timestamp, then inspection_id) per shipment allocation among inspections whose
    timestamp is in the quarter; zero inspected units gives a null value.
    Quarter bounds are half-open: start <= timestamp < end + 1 second (end is the quarter's
    last second), so a boundary timestamp belongs to exactly one quarter."""
    start, end = _reference_period(session)
    grace = timedelta(hours=grace_hours)
    shipments = session.execute(
        sa.select(
            Shipment.expected_delivery_timestamp,
            Shipment.actual_delivery_timestamp,
            Shipment.shipment_status,
        )
    ).all()
    inspections = session.execute(
        sa.select(
            Inspection.shipment_line_id,
            Inspection.inspection_timestamp,
            Inspection.inspection_id,
            Inspection.inspected_quantity,
            Inspection.defect_quantity,
        ).order_by(Inspection.inspection_timestamp, Inspection.inspection_id)
    ).all()
    by_quarter: dict[str, Any] = {}
    for q in quarter_windows(start, end):
        label = _quarter_label(q.start)
        window = f"{label}: {q.start:%Y-%m-%d} to {q.end:%Y-%m-%d}"
        upper = q.end + timedelta(seconds=1)
        due = [s for s in shipments if q.start <= s[0] < upper and s[2] != ShipmentStatus.CANCELLED]
        known = [s for s in due if s[1] is not None]
        on_time = sum(s[1] <= s[0] + grace for s in known)
        latest: dict[str, Any] = {}
        for row in inspections:  # ordered, so the last write per allocation is the latest
            if q.start <= row[1] < upper:
                latest[row[0]] = row
        defects = sum((r[4] for r in latest.values()), ZERO)
        inspected = sum((r[3] for r in latest.values()), ZERO)
        by_quarter[label] = {
            "on_time_rate": ratio(
                on_time,
                len(known),
                "shipments not CANCELLED with expected_delivery in quarter; on time = "
                f"arrival <= expected + {grace_hours}h grace",
                window,
                "shipments without a recorded arrival are excluded from the denominator "
                "(see arrival_unknown_shipments)",
            ),
            "arrival_unknown_shipments": count_metric(
                len(due) - len(known),
                "non-cancelled shipments due in the quarter with no recorded arrival",
                window,
                "excluded from the on-time denominator",
            ),
            "defect_rate": ratio(
                defects,
                inspected,
                "latest inspection per shipment allocation with inspection_timestamp in quarter",
                window,
                "allocations without an inspection in the quarter contribute nothing",
            ),
        }
    return report(
        "delivery_quality_trends",
        "Delivery and quality trends by quarter",
        {"by_quarter": by_quarter},
        notes=[
            "Computed from canonical source records (shipments, inspections), not from scores.",
            "Arrival uses the corrected current arrival timestamp (as-of-now view).",
        ],
    )


BUCKETS = (
    ("lt_minus_5pct", "variance < -5%"),
    ("minus_5_to_minus_1pct", "-5% <= variance < -1%"),
    ("minus_1_to_1pct", "-1% <= variance < 1%"),
    ("1_to_5pct", "1% <= variance <= 5%"),
    ("gt_5pct", "variance > 5%"),
)
_EDGES = (Decimal("-0.05"), Decimal("-0.01"), Decimal("0.01"), Decimal("0.05"))


def _bucket(pct: Decimal) -> str:
    if pct < _EDGES[0]:
        return BUCKETS[0][0]
    if pct < _EDGES[1]:
        return BUCKETS[1][0]
    if pct < _EDGES[2]:
        return BUCKETS[2][0]
    if pct <= _EDGES[3]:
        return BUCKETS[3][0]
    return BUCKETS[4][0]


def invoice_variance_distribution(session: Session) -> dict[str, Any]:
    """Buckets of variance_amount / expected_amount (Decimal, not the stored 6-dp percentage)
    over ALL invoices with a nonzero expected amount (R43: no invoice-date filter). Zero-expected
    invoices are excluded and counted; invoices dated outside the reference period are counted
    separately for context but stay in the buckets."""
    start, end = _reference_period(session)
    window = "all invoices (no invoice-date filter)"
    rows = session.execute(
        sa.select(
            Invoice.expected_amount,
            Invoice.invoiced_amount,
            Invoice.variance_amount,
            Invoice.invoice_date,
        )
    ).all()
    eligible = [r for r in rows if r[0] != 0]
    counts: dict[str, int] = defaultdict(int)
    for r in eligible:
        counts[_bucket(r[2] / r[0])] += 1
    outside = sum(not (start.date() <= r[3] <= end.date()) for r in rows)
    filters = "invoices with expected_amount != 0"
    buckets = {
        key: counted(
            counts[key],
            len(eligible),
            f"{filters}; bucket {desc} (variance_amount / expected_amount)",
            window,
            "zero-expected invoices are excluded from numerator and denominator",
        )
        for key, desc in BUCKETS
    }
    expected = sum((r[0] for r in eligible), ZERO)
    invoiced = sum((r[1] for r in eligible), ZERO)
    variance = sum((r[2] for r in eligible), ZERO)
    money = "sum over eligible invoices (Decimal); no denominator"
    metrics = {
        "invoices": count_metric(len(rows), "all invoices", window),
        "invoices_outside_reference_period": count_metric(
            outside,
            f"invoices dated outside {start:%Y-%m-%d} to {end:%Y-%m-%d} (the scored windows)",
            window,
            "context only; they remain in the buckets",
        ),
        "excluded_zero_expected": count_metric(
            len(rows) - len(eligible),
            "invoices with expected_amount = 0 (variance undefined)",
            window,
            "excluded from every bucket",
        ),
        "buckets": buckets,
        "total_expected_amount": metric(expected, None, None, filters, window, money),
        "total_invoiced_amount": metric(invoiced, None, None, filters, window, money),
        "net_variance_amount": metric(variance, None, None, filters, window, money),
        "net_variance_share_of_expected": ratio(
            variance, expected, filters, window, "sum of variance over sum of expected"
        ),
    }
    return report("invoice_variance_distribution", "Invoice variance distribution", metrics)

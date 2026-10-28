"""Capacity, recomputed from po_lines/purchase_orders + capacity_declarations (D5, D6, D12).

- Demand: lines of non-CANCELLED POs with order_date (DATE) in [date(S), date(min(E, C))], PO and
  line created_at <= C, quantity > 0; required_m = sum ordered quantity per order month m.
- Supply: declarations recorded <= C whose [period_start, period_end] overlaps month m (window
  months only). Scope rule: for a (supplier, month) with any site-level declaration only the
  site-level ones count; supplier-wide (site_id empty) ones count only for months without any.
  Of those, only VERIFIED rows give free_m = sum(available - committed).
- util = max over months with both required_m and free_m of required_m / free_m; a month with
  free_m <= 0 makes util infinite (score 0). Score through the configured breakpoints (linear in
  between, the first score at or below the first breakpoint, 0 beyond the last). Missing when no
  month has both.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ._tables import ZERO, Params, Tables, Window, clamp_round

INFINITE = Decimal("Infinity")


def _months(first: date, last: date) -> list[date]:
    out, m = [], first.replace(day=1)
    while m <= last:
        out.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def _month_end(m: date) -> date:
    return date(m.year + (m.month == 12), m.month % 12 + 1, 1) - timedelta(days=1)


def breakpoint_score(util: Decimal, points: list[tuple[Decimal, Decimal]]) -> Decimal:
    if util <= points[0][0]:
        return points[0][1]
    for (u0, s0), (u1, s1) in zip(points, points[1:], strict=False):
        if util <= u1:
            return s0 + (s1 - s0) * (util - u0) / (u1 - u0)
    return ZERO


def _required(t: Tables, w: Window) -> dict[tuple[str, date], Decimal]:
    po = t["purchase_orders"][["po_id", "supplier_id", "order_date", "po_status", "created_at"]]
    lines = t["po_lines"].merge(po.rename(columns={"created_at": "po_created_at"}), on="po_id")
    day = lines["order_date"]
    eligible = lines[
        (lines["po_status"] != "CANCELLED")
        & (day >= w.first_day)
        & (day <= w.last_day)
        & (lines["po_created_at"] <= w.cutoff)
        & (lines["created_at"] <= w.cutoff)
    ]
    required: dict[tuple[str, date], Decimal] = defaultdict(lambda: ZERO)
    for supplier, ordered, qty in zip(
        eligible["supplier_id"], eligible["order_date"], eligible["ordered_quantity"], strict=True
    ):
        if qty > 0:
            required[(supplier, ordered.replace(day=1))] += qty
    return required


def _free(t: Tables, w: Window) -> dict[tuple[str, date], Decimal]:
    decl = t["capacity_declarations"]
    decl = decl[decl["recorded_at"] <= w.cutoff]
    months = _months(w.first_day, w.last_day)
    cells: dict[tuple[str, date], dict[str, list[Any]]] = defaultdict(
        lambda: {"site": [], "wide": []}
    )
    for r in decl.itertuples(index=False):
        scope = "site" if isinstance(r.site_id, str) else "wide"
        for m in months:
            if r.period_start <= _month_end(m) and r.period_end >= m:
                cells[(r.supplier_id, m)][scope].append(r)
    free: dict[tuple[str, date], Decimal] = {}
    for key, cell in cells.items():
        chosen = cell["site"] or cell["wide"]
        verified = [r for r in chosen if r.verification_status == "VERIFIED"]
        if verified:
            free[key] = sum((r.available_capacity - r.committed_capacity for r in verified), ZERO)
    return free


def capacity(t: Tables, w: Window, p: Params) -> dict[str, dict[str, Any]]:
    required, free = _required(t, w), _free(t, w)
    per_supplier: dict[str, list[date]] = defaultdict(list)
    for supplier, month in sorted(required):
        if (supplier, month) in free:
            per_supplier[supplier].append(month)
    out: dict[str, dict[str, Any]] = {}
    for supplier, months in per_supplier.items():
        utils = [
            required[(supplier, m)] / free[(supplier, m)] if free[(supplier, m)] > 0 else INFINITE
            for m in months
        ]
        util = max(utils)
        score = ZERO if util == INFINITE else breakpoint_score(util, p.breakpoints)
        out[supplier] = {
            "capacity_months": len(months),
            "capacity_util": util,
            "capacity_score": clamp_round(score),
        }
    return out

"""Quality (inspections.csv) and cost (invoices.csv), recomputed independently (D2, D3, D5, D6).

Quality: inspections with inspection_timestamp in [S, min(E, C)] and recorded_at <= C, reduced
to the latest per shipment_line by (inspection_timestamp, inspection_id), so re-inspections never
double-count; supplier via shipment_line -> shipment -> PO. defect_rate = sum defect / sum
inspected; score = 100 * max(0, 1 - rate / defect_rate_zero_score). Missing below
min_observations.quality or with zero inspected units.

Cost (R21): invoices with invoice_date (a DATE: day granularity) in [date(S), date(min(E, C))]
and recorded_at <= C; invoices with expected_amount 0 are unusable. wavp = sum(f_i *
|variance_amount_i|) / sum(expected_i), f = 1 for overbilling (variance >= 0) and
underbilling_weight for underbilling; score = 100 * max(0, 1 - wavp / variance_zero_score).
Missing below min_observations.cost usable invoices.
"""

from decimal import Decimal
from typing import Any

from ._tables import HUNDRED, ONE, ZERO, Params, Tables, Window, clamp_round


def quality(t: Tables, w: Window, p: Params) -> dict[str, dict[str, Any]]:
    ins = (
        t["inspections"]
        .merge(t["shipment_lines"][["shipment_line_id", "shipment_id"]], on="shipment_line_id")
        .merge(t["shipments"][["shipment_id", "po_id"]], on="shipment_id")
        .merge(t.po_supplier(), on="po_id")
    )
    ts = ins["inspection_timestamp"]
    eligible = ins[(ts >= w.start) & (ts <= w.top) & (ins["recorded_at"] <= w.cutoff)]
    latest = eligible.sort_values(["inspection_timestamp", "inspection_id"]).drop_duplicates(
        "shipment_line_id", keep="last"
    )
    out: dict[str, dict[str, Any]] = {}
    for supplier, g in latest.groupby("supplier_id"):
        n = len(g)
        inspected = sum(g["inspected_quantity"], ZERO)
        defects = sum(g["defect_quantity"], ZERO)
        rate = defects / inspected if inspected else None
        score = None
        if n >= p.min_obs["quality"] and rate is not None:
            score = clamp_round(HUNDRED * max(ZERO, ONE - rate / p.defect_rate_zero))
        out[str(supplier)] = {
            "quality_inspections": n,
            "inspected_units": inspected,
            "defect_rate": rate,
            "quality_score": score,
        }
    return out


def cost(t: Tables, w: Window, p: Params) -> dict[str, dict[str, Any]]:
    inv = t["invoices"]
    day = inv["invoice_date"]
    eligible = inv[(day >= w.first_day) & (day <= w.last_day) & (inv["recorded_at"] <= w.cutoff)]
    usable = eligible[eligible["expected_amount"].map(lambda x: x != 0)]
    out: dict[str, dict[str, Any]] = {}
    for supplier, g in usable.groupby("supplier_id"):
        n = len(g)
        numerator = sum(
            (
                abs(v) * (p.underbilling_weight if v < 0 else ONE)
                for v in g.sort_values("invoice_id")["variance_amount"]
            ),
            ZERO,
        )
        denominator: Decimal = sum(g["expected_amount"], ZERO)
        wavp = numerator / denominator
        score = None
        if n >= p.min_obs["cost"]:
            score = clamp_round(HUNDRED * max(ZERO, ONE - wavp / p.variance_zero))
        out[str(supplier)] = {"cost_invoices": n, "wavp": wavp, "cost_score": score}
    return out

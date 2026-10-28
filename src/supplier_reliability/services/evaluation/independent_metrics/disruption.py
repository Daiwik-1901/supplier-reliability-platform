"""Disruption, recomputed from risk_signals.csv + sites.csv (D5, D6, R14).

- Eligible: observed in [C - lookback_days, C] (not bounded by S) and recorded <= C.
- Dedup (computed here, not read from the stored dedup_key): key (supplier, site or "*",
  signal_type, UTC observed date ordinal // dedup_window_days); keep the highest severity weight,
  ties to the lowest risk_signal_id.
- MONITORING_ATTESTATION signals prove monitoring and add no penalty.
- penalty = sum weight x exp(-age_days / recency_days) x (resolved_multiplier if RESOLVED with
  valid_to <= C, else 1) x (1 if no site or the site is active at date(C), else
  inactive_site_multiplier). Site active at d: effective_from <= d and (effective_to empty ->
  site_status ACTIVE, else d <= effective_to).
- exp() is evaluated in float and enters Decimal via str (D6 precision): Decimal(str(math.exp(
  -float(age_days / recency_days)))), age_days exact in Decimal.
- score = 100 - penalty; missing with zero eligible signals (no monitoring evidence).
"""

import math
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from ._tables import HUNDRED, ONE, ZERO, Params, Tables, Window, clamp_round, days, is_null

ATTESTATION = "MONITORING_ATTESTATION"


def _site_active(site: Any, day: date) -> bool:
    if site is None:
        return True
    if site["effective_from"] > day:
        return False
    if is_null(site["effective_to"]):
        return site["site_status"] == "ACTIVE"
    return day <= site["effective_to"]


def disruption(t: Tables, w: Window, p: Params) -> dict[str, dict[str, Any]]:
    horizon = w.cutoff - p.lookback
    sig = t["risk_signals"]
    observed = sig["observed_timestamp"]
    sig = sig[
        (observed >= horizon) & (observed <= w.cutoff) & (sig["recorded_timestamp"] <= w.cutoff)
    ]
    sites = {r["site_id"]: r for r in t["sites"].to_dict("records")}
    groups: dict[tuple, list[Any]] = defaultdict(list)
    for r in sig.itertuples(index=False):
        site = r.site_id if isinstance(r.site_id, str) else "*"
        week = r.observed_timestamp.date().toordinal() // p.dedup_window_days
        groups[(r.supplier_id, site, r.signal_type, week)].append(r)
    kept: dict[str, list[Any]] = defaultdict(list)
    for members in groups.values():
        best = min(members, key=lambda r: (-p.severity_weights[r.severity], r.risk_signal_id))
        kept[best.supplier_id].append(best)

    out: dict[str, dict[str, Any]] = {}
    signals = sig.groupby("supplier_id").size().to_dict()
    for supplier, rows in kept.items():
        penalty = ZERO
        for r in sorted(rows, key=lambda r: r.risk_signal_id):
            if r.signal_type == ATTESTATION:
                continue
            age = max(ZERO, days(w.cutoff - r.observed_timestamp))
            decay = Decimal(str(math.exp(-float(age / p.recency_days))))
            resolved = (
                r.resolution_status == "RESOLVED"
                and not pd.isna(r.valid_to)
                and r.valid_to <= w.cutoff
            )
            factor = p.resolved_multiplier if resolved else ONE
            site = sites.get(r.site_id) if isinstance(r.site_id, str) else None
            scope = ONE if _site_active(site, w.cutoff_day) else p.inactive_site_multiplier
            penalty += p.severity_weights[r.severity] * decay * factor * scope
        out[supplier] = {
            "disruption_signals": int(signals[supplier]),
            "disruption_kept": len(rows),
            "disruption_penalty": penalty,
            "disruption_score": clamp_round(HUNDRED - penalty),
        }
    return out

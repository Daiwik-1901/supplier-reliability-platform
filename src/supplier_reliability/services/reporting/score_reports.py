"""Reports computed from persisted CURRENT supplier scores (is_current, supplier level).

"Latest window" is the greatest cutoff among current scores. Only current rows feed a report:
a superseded (older version) score never appears, a CORRECTED row appears where it superseded.
Scores are 2-dp Decimals; averages are taken on Decimal."""

import math
import statistics
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import ReliabilityCategory, Severity
from supplier_reliability.models import Supplier, SupplierScore
from supplier_reliability.services.reporting.common import (
    ScoreWindow,
    current_rows,
    latest_window,
    report,
    score_windows,
)
from supplier_reliability.services.reporting.metrics import (
    count_metric,
    counted,
    iso,
    metric,
    ratio,
)

__all__ = [
    "capacity_risk",
    "compliance_exceptions",
    "deteriorating_suppliers",
    "disruption_exposure",
    "missing_data_rates",
    "score_distribution",
    "supplier_rankings",
]

S = SupplierScore
INSUFFICIENT = ReliabilityCategory.INSUFFICIENT_EVIDENCE
COMPONENTS = ("delivery", "quality", "cost", "capacity", "compliance", "disruption")
ZERO = Decimal("0")
_SCOPE = "current (is_current) supplier-level scores"


def _mean(values: list[Decimal]) -> float | None:
    return round(float(sum(values, ZERO) / len(values)), 4) if values else None


def _pick_window(session: Session, cutoff: datetime | None) -> ScoreWindow:
    if cutoff is None:
        return latest_window(session)
    for w in score_windows(session):
        if w.cutoff == cutoff:
            return w
    raise LookupError(f"no current scores with cutoff {iso(cutoff)}")


def supplier_rankings(
    session: Session, window: datetime | None = None, limit: int | None = None
) -> dict[str, Any]:
    """Suppliers ranked by overall score (desc, then supplier_id) in one window (default: the
    latest; `window` is its cutoff). INSUFFICIENT_EVIDENCE suppliers are not ranked; they are
    listed separately with the missing-data reason of each absent component."""
    w = _pick_window(session, window)
    rows = current_rows(
        session,
        S.supplier_id,
        Supplier.supplier_name,
        Supplier.supplier_category,
        S.overall_score,
        S.reliability_category,
        S.decision,
        S.calculation_status,
        S.evidence_coverage,
        S.is_provisional,
        S.explanation["missing_data_decisions", "missing_components"],
        window=w,
        join_supplier=True,
    )
    ranked = sorted(
        (r for r in rows if r[4] != INSUFFICIENT and r[3] is not None),
        key=lambda r: (-r[3], r[0]),
    )
    insufficient = [r for r in rows if r[4] == INSUFFICIENT]
    shown = ranked if limit is None else ranked[:limit]

    def base(r: Any) -> dict[str, Any]:
        return {
            "supplier_id": r[0],
            "supplier_name": r[1],
            "supplier_category": str(r[2]),
            "overall_score": None if r[3] is None else float(r[3]),
            "category": str(r[4]),
            "decision": str(r[5]),
            "calculation_status": str(r[6]),
            "evidence_coverage": float(r[7]),
            "is_provisional": r[8],
        }

    tables = {
        "ranked": [{"rank": i, **base(r)} for i, r in enumerate(shown, 1)],
        "insufficient_evidence": [
            {**base(r), "missing_reasons": r[9] or {}}
            for r in sorted(insufficient, key=lambda r: r[0])
        ],
    }
    filters = f"{_SCOPE} in this window"
    metrics = {
        "window_suppliers": count_metric(len(rows), filters, w.label),
        "ranked_suppliers": counted(
            len(ranked),
            len(rows),
            filters + "; category != INSUFFICIENT_EVIDENCE",
            w.label,
            "INSUFFICIENT_EVIDENCE suppliers are not ranked",
        ),
        "insufficient_evidence_suppliers": counted(
            len(insufficient),
            len(rows),
            filters + "; category = INSUFFICIENT_EVIDENCE",
            w.label,
            "listed separately with their missing reasons",
        ),
        "mean_overall_ranked": metric(
            _mean([r[3] for r in ranked]),
            sum((r[3] for r in ranked), ZERO),
            len(ranked),
            filters + "; ranked suppliers",
            w.label,
            "mean of overall_score; null when nobody is ranked",
        ),
    }
    notes = [f"Top {limit} of {len(ranked)} ranked suppliers shown."] if limit is not None else []
    return report(
        "supplier_rankings",
        "Supplier reliability rankings",
        metrics,
        tables,
        notes,
        window=w.as_dict(),
    )


def score_distribution(session: Session) -> dict[str, Any]:
    """Per window: current scores by reliability category, and mean overall (non-insufficient)
    by supplier category."""
    rows = current_rows(
        session,
        S.data_cutoff_timestamp,
        S.reliability_category,
        S.overall_score,
        Supplier.supplier_category,
        join_supplier=True,
    )
    by_window: dict[datetime, list[Any]] = defaultdict(list)
    for r in rows:
        by_window[r[0]].append(r)
    windows = {w.cutoff: w for w in score_windows(session)}
    by_category: dict[str, Any] = {}
    by_supplier_category: dict[str, Any] = {}
    for cutoff, group in sorted(by_window.items()):
        label = windows[cutoff].label
        by_category[iso(cutoff)] = {
            c.value: counted(
                sum(r[1] == c for r in group),
                len(group),
                f"{_SCOPE}; reliability_category = {c.value}",
                label,
                "every current score has a category; none is dropped",
            )
            for c in ReliabilityCategory
        }
        scored = [r for r in group if r[1] != INSUFFICIENT and r[2] is not None]
        cats = sorted({str(r[3]) for r in scored})
        by_supplier_category[iso(cutoff)] = {
            cat: metric(
                _mean([r[2] for r in scored if str(r[3]) == cat]),
                sum((r[2] for r in scored if str(r[3]) == cat), ZERO),
                sum(str(r[3]) == cat for r in scored),
                f"{_SCOPE}; supplier_category = {cat}; category != INSUFFICIENT_EVIDENCE",
                label,
                "mean overall_score; INSUFFICIENT_EVIDENCE scores are excluded",
            )
            for cat in cats
        }
    return report(
        "score_distribution",
        "Score distribution per window",
        {
            "by_reliability_category": by_category,
            "mean_overall_by_supplier_category": by_supplier_category,
        },
    )


def missing_data_rates(session: Session) -> dict[str, Any]:
    """Share of supplier-windows whose component score is missing (null), per window and in
    total."""
    columns = [getattr(S, f"{c}_score") for c in COMPONENTS]
    rows = current_rows(session, S.data_cutoff_timestamp, *columns)
    windows = {w.cutoff: w for w in score_windows(session)}
    by_window: dict[datetime, list[Any]] = defaultdict(list)
    for r in rows:
        by_window[r[0]].append(r)

    def rates(group: list[Any], window: str) -> dict[str, Any]:
        return {
            c: ratio(
                sum(r[i + 1] is None for r in group),
                len(group),
                f"{_SCOPE}; {c}_score is null (missing_reason recorded in the explanation)",
                window,
                "a missing component is null, never 0; the denominator counts every "
                "supplier-window",
            )
            for i, c in enumerate(COMPONENTS)
        }

    metrics = {
        "overall": rates(rows, "all six scorecard windows"),
        "by_window": {iso(c): rates(g, windows[c].label) for c, g in sorted(by_window.items())},
    }
    return report("missing_data_rates", "Missing-data rates per component", metrics)


def _nearest_rank(sorted_values: list[Decimal], p: float) -> Decimal:
    return sorted_values[max(math.ceil(p * len(sorted_values)) - 1, 0)]


def deteriorating_suppliers(session: Session, *, threshold: Any) -> dict[str, Any]:
    """D7: latest current overall is at least `threshold` points below the previous window's
    current overall, both non-INSUFFICIENT_EVIDENCE. Also reports the distribution of the
    window-over-window change (latest minus previous, negative = drop) over comparable
    suppliers."""
    limit = Decimal(str(threshold))
    windows = score_windows(session)
    if not windows:
        raise LookupError("no current supplier scores; run scoring before reporting")
    latest = windows[-1]
    previous = windows[-2] if len(windows) > 1 else None
    cols = (S.supplier_id, S.reliability_category, S.overall_score)

    def usable(w: ScoreWindow) -> dict[str, Decimal]:
        return {
            r[0]: r[2]
            for r in current_rows(session, *cols, window=w)
            if r[1] != INSUFFICIENT and r[2] is not None
        }

    now = usable(latest)
    before = usable(previous) if previous else {}
    pairs = sorted((sid, before[sid], now[sid]) for sid in now if sid in before)
    changes = sorted(n - p for _, p, n in pairs)
    flagged = [(s, p, n) for s, p, n in pairs if p - n >= limit]
    window = f"{previous.label if previous else 'n/a'} -> {latest.label}"
    filters = (
        "suppliers with a current non-INSUFFICIENT_EVIDENCE overall score in both the latest "
        f"and the previous window; deteriorating = previous - latest >= {limit}"
    )
    stats = {
        "delta_min": changes[0] if changes else None,
        "delta_p5": _nearest_rank(changes, 0.05) if changes else None,
        "delta_median": statistics.median(changes) if changes else None,
    }
    spread = (
        f"latest minus previous overall over {len(changes)} comparable suppliers; "
        "no denominator; null when no supplier is comparable"
    )
    metrics = {
        "deteriorating": counted(
            len(flagged),
            len(pairs),
            filters,
            window,
            "suppliers without a usable score in both windows are not comparable and are excluded",
        ),
        "not_comparable": count_metric(
            len(now) - len(pairs),
            "latest-window non-insufficient suppliers without a usable previous-window score",
            window,
        ),
        "quarter_over_quarter_delta": {
            k: metric(None if v is None else float(v), None, None, filters, window, spread)
            for k, v in stats.items()
        },
    }
    tables = {
        "deteriorating": [
            {
                "supplier_id": s,
                "previous_overall": float(p),
                "latest_overall": float(n),
                "change": float(n - p),
            }
            for s, p, n in sorted(flagged, key=lambda t: (t[2] - t[1], t[0]))
        ]
    }
    return report(
        "deteriorating_suppliers",
        "Deteriorating suppliers",
        metrics,
        tables,
        [f"Threshold {limit} points (scoring.deteriorating_threshold), inclusive."],
        window=latest.as_dict(),
    )


def capacity_risk(session: Session) -> dict[str, Any]:
    """Latest window: suppliers whose peak monthly utilisation (required / verified free
    capacity, stored in the capacity component metrics) exceeds 1.0."""
    w = latest_window(session)
    rows = current_rows(
        session,
        S.supplier_id,
        S.capacity_score,
        S.explanation["components", "capacity", "metrics", "util"].as_string(),
        S.explanation["components", "capacity", "metrics", "util_month"].as_string(),
        window=w,
    )
    with_util = [r for r in rows if r[2] is not None]
    over = [r for r in with_util if r[2] == "inf" or Decimal(r[2]) > 1]
    exhausted = [r for r in over if r[2] == "inf"]
    over.sort(
        key=lambda r: (0 if r[2] == "inf" else 1, -(Decimal(r[2]) if r[2] != "inf" else 0), r[0])
    )
    filters = f"{_SCOPE}; capacity component not missing"
    metrics = {
        "suppliers_over_capacity": counted(
            len(over),
            len(with_util),
            filters + "; peak utilisation > 1.0",
            w.label,
            "suppliers with missing capacity evidence (unknown or unverified) are excluded",
        ),
        "suppliers_without_capacity_evidence": counted(
            len(rows) - len(with_util),
            len(rows),
            f"{_SCOPE}; capacity component missing",
            w.label,
            "reported, not counted as over or under capacity",
        ),
        "suppliers_with_exhausted_capacity": counted(
            len(exhausted),
            len(with_util),
            filters + "; verified free capacity <= 0 while demand > 0",
            w.label,
            "utilisation is infinite and shown as inf",
        ),
    }
    tables = {
        "over_capacity": [
            {
                "supplier_id": r[0],
                "peak_utilisation": r[2],
                "peak_month": r[3],
                "capacity_score": None if r[1] is None else float(r[1]),
            }
            for r in over
        ]
    }
    return report("capacity_risk", "Capacity risk", metrics, tables, window=w.as_dict())


def compliance_exceptions(session: Session) -> dict[str, Any]:
    """Latest window: suppliers with a mandatory compliance failure (an EXPIRED or
    NONCOMPLIANT mandatory requirement) and requirement states summed over suppliers."""
    w = latest_window(session)
    rows = current_rows(
        session,
        S.supplier_id,
        S.compliance_score,
        S.decision,
        S.explanation["applied_overrides"],
        S.explanation["components", "compliance", "metrics"],
        window=w,
    )
    failing = [r for r in rows if "mandatory_compliance_failure" in (r[3] or [])]
    states = ("COMPLIANT", "PENDING_VERIFICATION", "EXPIRED", "NONCOMPLIANT", "UNKNOWN")
    required = sum((r[4] or {}).get("required_count", 0) for r in rows)
    filters = f"{_SCOPE} in the latest window"
    metrics = {
        "mandatory_failures": counted(
            len(failing),
            len(rows),
            filters + "; applied_overrides contains mandatory_compliance_failure",
            w.label,
            "suppliers without compliance requirements cannot fail and stay in the denominator",
        ),
        "requirement_states": {
            st: counted(
                sum((r[4] or {}).get(f"state_{st}", 0) for r in rows),
                required,
                f"{filters}; requirements in state {st} at the cutoff",
                w.label,
                "requirement-level counts; suppliers without requirements add nothing",
            )
            for st in states
        },
    }
    tables = {
        "mandatory_failures": [
            {
                "supplier_id": r[0],
                "compliance_score": None if r[1] is None else float(r[1]),
                "decision": str(r[2]),
                "requirement_states": {
                    k[6:]: v for k, v in sorted((r[4] or {}).items()) if k.startswith("state_")
                },
            }
            for r in failing
        ]
    }
    return report(
        "compliance_exceptions", "Compliance exceptions", metrics, tables, window=w.as_dict()
    )


def disruption_exposure(session: Session, top: int = 20) -> dict[str, Any]:
    """Latest window: the deduplicated risk signals that fed each supplier's disruption score
    (stored in the score explanation), by severity, and the most exposed suppliers."""
    w = latest_window(session)
    rows = current_rows(
        session,
        S.supplier_id,
        S.disruption_score,
        S.explanation["risk_signals"],
        S.explanation["components", "disruption", "metrics", "penalty"].as_string(),
        window=w,
    )
    signals = [(r[0], sig) for r in rows for sig in (r[2] or [])]
    with_evidence = [r for r in rows if r[1] is not None]
    serious = {"HIGH", "CRITICAL"}
    exposed = sorted({sid for sid, sig in signals if sig["severity"] in serious})
    unresolved = sorted(
        {
            sid
            for sid, sig in signals
            if sig["severity"] in serious and not sig.get("resolved_at_cutoff")
        }
    )
    filters = f"{_SCOPE}; deduplicated signals counted by the disruption component"
    metrics = {
        "signals_by_severity": {
            sev.value: counted(
                sum(sig["severity"] == sev.value for _, sig in signals),
                len(signals),
                f"{filters}; severity = {sev.value}",
                w.label,
                "signals quarantined or outside the 180-day lookback never reach the score",
            )
            for sev in Severity
        },
        "suppliers_with_high_or_critical_signal": counted(
            len(exposed),
            len(with_evidence),
            f"{_SCOPE}; at least one HIGH or CRITICAL signal; disruption component not missing",
            w.label,
            "suppliers with no risk-monitoring evidence are excluded",
        ),
        "suppliers_with_unresolved_high_or_critical_signal": counted(
            len(unresolved),
            len(with_evidence),
            f"{_SCOPE}; HIGH or CRITICAL signal not resolved at the cutoff",
            w.label,
            "suppliers with no risk-monitoring evidence are excluded",
        ),
        "mean_disruption_score": metric(
            _mean([r[1] for r in with_evidence]),
            sum((r[1] for r in with_evidence), ZERO),
            len(with_evidence),
            f"{_SCOPE}; disruption component not missing",
            w.label,
            "suppliers with missing disruption evidence are excluded, not scored 0 or 100",
        ),
    }
    worst = sorted(
        (r for r in with_evidence if r[3] is not None),
        key=lambda r: (-Decimal(r[3]), r[0]),
    )[:top]
    tables = {
        "most_exposed": [
            {
                "supplier_id": r[0],
                "disruption_score": float(r[1]),
                "penalty": float(Decimal(r[3])),
                "signals": len(r[2] or []),
            }
            for r in worst
        ]
    }
    return report("disruption_exposure", "Disruption exposure", metrics, tables, window=w.as_dict())

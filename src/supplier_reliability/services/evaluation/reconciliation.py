"""Persisted scores reconciled against the independent recomputation (Add-D, Add-E gate).

`reconcile_scores(session, export_dir, windows, tolerance)` checks every CURRENT supplier-level
score (site_id NULL; CORRECTED and any other calculation status alike) whose (start, end,
cutoff) equals one of `windows`. Each is compared with `independent_supplier_metrics` computed
from the CSVs in `export_dir` with the score's own stored ScoringConfigSnapshot (so a score is
judged by the parameters it claims, and the config itself is never re-read from the engine):
the six component scores (missing must match missing), overall_score and reliability_category.

Tolerance: component scores are compared EXACTLY except the fields in TOLERANT_FIELDS, where the
two implementations take a different route to the same formula and may differ in the last kept
digit of a 28-digit Decimal before rounding (delivery: fractional-day sums; disruption: float
exp() with summation order), which can flip a 2-dp half-up rounding at an exact ...5 boundary;
overall inherits that. There `|persisted - independent| <= tolerance` (default 0.01) matches.
Category is compared exactly. The export must reflect the data the scores were computed from.

`profile_alignment(session, profiles_path)` tabulates the latest window's categories per
generator profile: a SANITY CHECK, NOT ACCURACY (profiles are generator intent, not truth).
"""

import csv
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.models import ScoringConfigSnapshot, SupplierScore
from supplier_reliability.services.evaluation.independent_metrics import (
    COMPONENTS,
    WindowLike,
    compute_metrics,
    is_null,
    load_tables,
)

__all__ = [
    "SANITY_LABEL",
    "TOLERANT_FIELDS",
    "ReconciliationReport",
    "profile_alignment",
    "reconcile_scores",
]

SANITY_LABEL = "sanity check, not accuracy"
SCORE_FIELDS = (*(f"{c}_score" for c in COMPONENTS), "overall_score")
TOLERANT_FIELDS = frozenset({"delivery_score", "disruption_score", "overall_score"})


@dataclass
class ReconciliationReport:
    checked: int = 0
    matched: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    # component name / "overall" / "category" -> number of mismatched scores
    component_mismatch_counts: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)  # checked scores per status
    tolerance: Decimal = Decimal("0.01")


def _iso(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str | None:
    return None if is_null(value) else str(value)


def _same(name: str, persisted: Any, independent: Any, tolerance: Decimal) -> bool:
    if is_null(persisted) or is_null(independent):
        return is_null(persisted) and is_null(independent)
    a, b = Decimal(str(persisted)), Decimal(str(independent))
    return abs(a - b) <= tolerance if name in TOLERANT_FIELDS else a == b


def _current_scores(session: Session, w: WindowLike) -> list[SupplierScore]:
    s = SupplierScore
    stmt = (
        sa.select(s)
        .where(
            s.is_current.is_(True),
            s.site_id.is_(None),
            s.evaluation_start == w.start,
            s.evaluation_end == w.end,
            s.data_cutoff_timestamp == w.cutoff,
        )
        .order_by(s.score_id)
    )
    return list(session.scalars(stmt))


def reconcile_scores(
    session: Session,
    export_dir: Path,
    windows: Sequence[WindowLike],
    tolerance: Decimal = Decimal("0.01"),
) -> ReconciliationReport:
    """Compare current persisted scores of `windows` with the independent recomputation."""
    tables = load_tables(Path(export_dir))
    report = ReconciliationReport(tolerance=tolerance)
    counts: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    configs: dict[str, dict] = {}
    for w in windows:
        by_config: dict[str, list[SupplierScore]] = defaultdict(list)
        for score in _current_scores(session, w):
            by_config[score.config_hash].append(score)
        for config_hash, scores in sorted(by_config.items()):
            if config_hash not in configs:
                snapshot = session.get(ScoringConfigSnapshot, config_hash)
                if snapshot is None:
                    raise LookupError(f"config snapshot {config_hash} not found")
                configs[config_hash] = snapshot.config
            frame = compute_metrics(tables, w, configs[config_hash])
            independent = {r["supplier_id"]: r for r in frame.to_dict("records")}
            for score in scores:
                report.checked += 1
                statuses[str(score.calculation_status)] += 1
                found = _compare(score, independent.get(score.supplier_id), w, tolerance)
                report.mismatches.extend(found)
                if not found:
                    report.matched += 1
                for kind in {m["component"] for m in found}:
                    counts[kind] += 1
    report.component_mismatch_counts = dict(sorted(counts.items()))
    report.by_status = dict(sorted(statuses.items()))
    return report


def _compare(
    score: SupplierScore, row: dict[str, Any] | None, w: WindowLike, tolerance: Decimal
) -> list[dict[str, Any]]:
    base = {
        "score_id": score.score_id,
        "supplier_id": score.supplier_id,
        "window": {"start": _iso(w.start), "end": _iso(w.end), "cutoff": _iso(w.cutoff)},
    }
    if row is None:
        return [base | {"field": "supplier_id", "component": "supplier",
                        "persisted": score.supplier_id, "independent": None}]  # fmt: skip
    out = []
    for name in SCORE_FIELDS:
        persisted, independent = getattr(score, name), row[name]
        if not _same(name, persisted, independent, tolerance):
            out.append(
                base
                | {
                    "field": name,
                    "component": name.removesuffix("_score"),
                    "persisted": _text(persisted),
                    "independent": _text(independent),
                }
            )
    persisted_category = str(score.reliability_category)
    if persisted_category != row["reliability_category"]:
        out.append(
            base
            | {
                "field": "reliability_category",
                "component": "category",
                "persisted": persisted_category,
                "independent": row["reliability_category"],
            }
        )
    return out


def profile_alignment(session: Session, profiles_path: Path) -> dict[str, Any]:
    """Category distribution per generator profile for the latest window (sanity check only).

    The latest window = the greatest evaluation_end among current supplier-level scores; per
    supplier the score with the latest cutoff, then the earliest start (the scorecard window)."""
    with Path(profiles_path).open(encoding="utf-8", newline="") as fh:
        profiles = {row["supplier_id"]: row["profile"] for row in csv.DictReader(fh)}
    s = SupplierScore
    current = sa.and_(s.is_current.is_(True), s.site_id.is_(None))
    latest_end = session.scalar(sa.select(sa.func.max(s.evaluation_end)).where(current))
    if latest_end is None:
        return {"label": SANITY_LABEL, "window_end": None, "distribution": {}}
    rows = session.execute(
        sa.select(
            s.supplier_id, s.reliability_category, s.data_cutoff_timestamp, s.evaluation_start
        )
        .where(current, s.evaluation_end == latest_end)
        .order_by(s.supplier_id, s.data_cutoff_timestamp.desc(), s.evaluation_start, s.score_id)
    ).all()
    chosen: dict[str, str] = {}
    for supplier_id, category, *_ in rows:
        chosen.setdefault(supplier_id, str(category))
    distribution: dict[str, Counter[str]] = defaultdict(Counter)
    for supplier_id, category in chosen.items():
        distribution[profiles.get(supplier_id, "unknown")][category] += 1
    return {
        "label": SANITY_LABEL,
        "window_end": _iso(latest_end),
        "distribution": {p: dict(sorted(c.items())) for p, c in sorted(distribution.items())},
    }

"""Data-quality, anomaly-detection and score-reconciliation reports (persisted validation
findings, data exceptions vs ground truth, scores vs the independent recomputation)."""

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import IssueType, Severity
from supplier_reliability.models import ValidationFinding, ValidationRun
from supplier_reliability.services.evaluation import (
    SANITY_LABEL,
    ConfusionCounts,
    evaluate_anomalies,
    reconcile_scores,
)
from supplier_reliability.services.evaluation.independent_metrics import WindowLike
from supplier_reliability.services.reporting.common import report
from supplier_reliability.services.reporting.metrics import count_metric, counted, iso, ratio

__all__ = ["anomaly_evaluation_report", "data_quality", "reconciliation_report"]

DEFECT_SEVERITIES = (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)


def data_quality(session: Session, validation_run_id: str) -> dict[str, Any]:
    """Findings of one validation run. Defects are MEDIUM and worse; LOW (and NONE) findings are
    informational and reported separately (R32). Findings flagged is_expected_anomaly (records
    that are known quarantined anomalies) are also counted on their own."""
    run = session.get(ValidationRun, validation_run_id)
    if run is None:
        raise LookupError(f"validation run {validation_run_id} not found")
    f = ValidationFinding
    rows = session.execute(
        sa.select(
            f.rule_id,
            f.severity,
            sa.func.count(),
            sa.func.sum(sa.cast(f.is_expected_anomaly, sa.Integer)),
        )
        .where(f.run_id == validation_run_id)
        .group_by(f.rule_id, f.severity)
        .order_by(f.rule_id, f.severity)
    ).all()
    window = f"validation run {validation_run_id} (scope: {run.scope})"
    defect_rule: dict[str, int] = {}
    defect_sev: dict[str, int] = {}
    info_rule: dict[str, int] = {}
    expected = 0
    table = []
    for rule_id, severity, n, exp in rows:
        defect = severity in DEFECT_SEVERITIES
        target = defect_rule if defect else info_rule
        target[rule_id] = target.get(rule_id, 0) + n
        if defect:
            defect_sev[severity.value] = defect_sev.get(severity.value, 0) + n
        expected += exp or 0
        table.append(
            {
                "rule_id": rule_id,
                "severity": severity.value,
                "class": "defect" if defect else "informational",
                "findings": n,
                "expected_anomalies": exp or 0,
            }
        )
    d_filter = "findings of the run with severity MEDIUM, HIGH or CRITICAL"
    i_filter = "findings of the run with severity LOW (informational, not a defect)"
    note = "findings are persisted rows; nothing is missing by construction"

    def counts(values: dict[str, int], filters: str, label: str) -> dict[str, Any]:
        return {
            k: count_metric(n, f"{filters}; {label} {k}", window, note) for k, n in values.items()
        }

    metrics = {
        "defects": {
            "by_rule": counts(defect_rule, d_filter, "rule"),
            "by_severity": counts(defect_sev, d_filter, "severity"),
            "total": count_metric(sum(defect_rule.values()), d_filter, window, note),
        },
        "informational": {
            "by_rule": counts(info_rule, i_filter, "rule"),
            "total": count_metric(sum(info_rule.values()), i_filter, window, note),
        },
        "expected_anomalies": count_metric(
            expected,
            "findings with is_expected_anomaly = true (defects and informational)",
            window,
            note,
        ),
    }
    run_row = {
        "run_id": run.run_id,
        "scope": run.scope,
        "started_at": iso(run.started_at),
        "finished_at": None if run.finished_at is None else iso(run.finished_at),
    }
    return report(
        "data_quality",
        "Data-quality findings",
        metrics,
        {"run": [run_row], "findings_by_rule_and_severity": table},
        ["Defects (MEDIUM+) and informational (LOW) findings are reported separately."],
    )


def _confusion(cc: ConfusionCounts, filters: str, window: str) -> dict[str, Any]:
    note = "record-level counts; rates are null when undefined"
    return {
        "tp": count_metric(cc.tp, filters + "; predicted and positive", window, note),
        "fp": count_metric(cc.fp, filters + "; predicted, not positive", window, note),
        "fn": count_metric(cc.fn, filters + "; positive, not predicted", window, note),
        "tn": count_metric(cc.tn, filters + "; negative population, not predicted", window, note),
        "precision": ratio(cc.tp, cc.tp + cc.fp, filters, window, "TP / (TP + FP)"),
        "recall": ratio(cc.tp, cc.tp + cc.fn, filters, window, "TP / (TP + FN)"),
        "f1": ratio(
            2 * cc.tp,
            2 * cc.tp + cc.fp + cc.fn,
            filters,
            window,
            "2TP / (2TP + FP + FN), the harmonic mean of precision and recall",
        ),
    }


def anomaly_evaluation_report(
    session: Session, batch_id: str, ground_truth_path: Path
) -> dict[str, Any]:
    """Detected data exceptions of a raw batch against the ground-truth positives, per issue
    class and micro-averaged (evaluation unit: one raw record per issue class)."""
    ev = evaluate_anomalies(session, batch_id, ground_truth_path)
    window = f"raw batch {batch_id}"
    by_class: dict[str, Any] = {}
    for issue in IssueType:
        filters = f"raw records that could exhibit {issue.value} (unit {ev.unit})"
        cell = _confusion(ev.per_class[issue], filters, window)
        cell["negative_population"] = count_metric(
            ev.negative_population[issue],
            filters + "; not positive for the class",
            window,
            "records outside the class's entity type are not negatives",
        )
        agree = ev.decision_agreement.get(issue, {"agree": 0, "disagree": 0})
        cell["decision_agreement"] = counted(
            agree["agree"],
            agree["agree"] + agree["disagree"],
            f"true positives of {issue.value}; disposition equals expected_decision",
            window,
            "only detected positives are compared; undetected ones have no disposition",
        )
        by_class[issue.value] = cell
    micro = _confusion(ev.micro, "all five issue classes, counts summed", window)
    return report(
        "anomaly_evaluation",
        "Anomaly detection performance",
        {"by_class": by_class, "micro": micro},
        notes=[f"Evaluation unit: {ev.unit}. Ground truth contains only the injected positives."],
        batch_id=batch_id,
    )


def reconciliation_report(
    session: Session,
    export_dir: Path,
    windows: Sequence[WindowLike],
    tolerance: Decimal = Decimal("0.01"),
) -> dict[str, Any]:
    """Persisted current scores against the independent pandas recomputation of the exported
    source tables. A sanity check on the engine, not an accuracy claim."""
    rec = reconcile_scores(session, export_dir, windows, tolerance)
    label = (
        f"{len(windows)} scorecard windows, cutoffs {iso(windows[0].cutoff)} to "
        f"{iso(windows[-1].cutoff)}"
        if windows
        else "no windows"
    )
    filters = "current supplier-level scores in the windows"
    checked = rec.checked
    metrics = {
        "checked": count_metric(checked, filters, label),
        "matched": count_metric(rec.matched, filters + "; every field within tolerance", label),
        "match_rate": ratio(rec.matched, checked, filters, label, "scores checked"),
        "mismatched_scores": count_metric(checked - rec.matched, filters, label),
        "missing_persisted": count_metric(
            len(rec.missing_persisted),
            "supplier-windows recomputed independently without a current persisted score",
            label,
            "listed in the tables",
        ),
        "mismatches_by_component": {
            k: counted(n, checked, f"{filters}; {k} differs", label, "scores checked")
            for k, n in rec.component_mismatch_counts.items()
        },
        "checked_by_status": {
            k: counted(n, checked, f"{filters}; calculation_status {k}", label, "scores checked")
            for k, n in rec.by_status.items()
        },
    }
    return report(
        "reconciliation",
        "Score reconciliation",
        metrics,
        {
            "mismatches": [dict(m) for m in rec.mismatches],
            "missing_persisted": [dict(m) for m in rec.missing_persisted],
        },
        [f"{SANITY_LABEL}; tolerance {rec.tolerance} on delivery, disruption and overall scores."],
    )

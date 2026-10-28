"""Report builders and the headline summary. Builders live by area (source_reports,
score_reports, quality_reports); the summary composes their metrics without recomputing."""

import copy
from typing import Any

from sqlalchemy.orm import Session

from supplier_reliability.services.reporting.common import report
from supplier_reliability.services.reporting.quality_reports import (
    anomaly_evaluation_report,
    data_quality,
    reconciliation_report,
)
from supplier_reliability.services.reporting.score_reports import (
    capacity_risk,
    compliance_exceptions,
    deteriorating_suppliers,
    disruption_exposure,
    missing_data_rates,
    score_distribution,
    supplier_rankings,
)
from supplier_reliability.services.reporting.source_reports import (
    delivery_quality_trends,
    entity_counts,
    invoice_variance_distribution,
)

__all__ = [
    "anomaly_evaluation_report",
    "capacity_risk",
    "compliance_exceptions",
    "data_quality",
    "delivery_quality_trends",
    "deteriorating_suppliers",
    "disruption_exposure",
    "entity_counts",
    "invoice_variance_distribution",
    "missing_data_rates",
    "reconciliation_report",
    "score_distribution",
    "summary",
    "supplier_rankings",
]


def summary(
    session: Session,
    validation_run_id: str | None = None,
    *,
    reports: dict[str, dict[str, Any]] | None = None,
    threshold: Any,
) -> dict[str, Any]:
    """Headline numbers lifted (copied, not recomputed) from the other reports. `reports` holds
    already-built payloads keyed by report name (entity_counts, supplier_rankings,
    deteriorating_suppliers, data_quality, anomaly_evaluation, reconciliation); missing ones of
    the first four are built here. Anomaly and reconciliation figures appear only when their
    reports are supplied, because they need ground truth and an export."""
    have = dict(reports or {})
    if "entity_counts" not in have:
        have["entity_counts"] = entity_counts(session)
    if "supplier_rankings" not in have:
        have["supplier_rankings"] = supplier_rankings(session)
    if "deteriorating_suppliers" not in have:
        have["deteriorating_suppliers"] = deteriorating_suppliers(session, threshold=threshold)
    if "data_quality" not in have and validation_run_id is not None:
        have["data_quality"] = data_quality(session, validation_run_id)

    rank = have["supplier_rankings"]["metrics"]
    metrics: dict[str, Any] = {
        "suppliers": have["entity_counts"]["metrics"]["rows"]["suppliers"],
        "suppliers_scored_latest_window": rank["window_suppliers"],
        "ranked_suppliers": rank["ranked_suppliers"],
        "insufficient_evidence_suppliers": rank["insufficient_evidence_suppliers"],
        "mean_overall_ranked": rank["mean_overall_ranked"],
        "deteriorating_suppliers": have["deteriorating_suppliers"]["metrics"]["deteriorating"],
    }
    if "data_quality" in have:
        dq = have["data_quality"]["metrics"]
        metrics["defects"] = dq["defects"]["total"]
        metrics["informational_findings"] = dq["informational"]["total"]
        metrics["expected_anomalies"] = dq["expected_anomalies"]
    if "anomaly_evaluation" in have:
        micro = have["anomaly_evaluation"]["metrics"]["micro"]
        metrics["anomaly_micro_precision"] = micro["precision"]
        metrics["anomaly_micro_recall"] = micro["recall"]
    if "reconciliation" in have:
        metrics["reconciliation_match_rate"] = have["reconciliation"]["metrics"]["match_rate"]
    return report(
        "summary",
        "Headline summary",
        copy.deepcopy(metrics),
        notes=["Every figure is copied from the report it names; nothing is recomputed here."],
        window=have["supplier_rankings"]["window"],
    )

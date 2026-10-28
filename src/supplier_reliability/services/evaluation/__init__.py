"""Evaluation layer (Task 16): anomaly metrics against ground truth, an independent pandas
recomputation of supplier metrics, and reconciliation of persisted scores against it.

This package may read ground truth; scoring, validation and detection must never import it
(services/isolation.py)."""

from supplier_reliability.services.evaluation.anomaly_eval import (
    UNIT,
    AnomalyEvaluation,
    ConfusionCounts,
    evaluate_anomalies,
)
from supplier_reliability.services.evaluation.independent_metrics import (
    INDEPENDENT_TABLES,
    independent_supplier_metrics,
)
from supplier_reliability.services.evaluation.reconciliation import (
    SANITY_LABEL,
    TOLERANT_FIELDS,
    ReconciliationReport,
    profile_alignment,
    reconcile_scores,
)

__all__ = [
    "INDEPENDENT_TABLES",
    "SANITY_LABEL",
    "TOLERANT_FIELDS",
    "UNIT",
    "AnomalyEvaluation",
    "ConfusionCounts",
    "ReconciliationReport",
    "evaluate_anomalies",
    "independent_supplier_metrics",
    "profile_alignment",
    "reconcile_scores",
]

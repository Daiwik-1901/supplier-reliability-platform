"""Reporting (Task 17): JSON and Markdown reports computed from persisted outputs. Every metric is
`{value, numerator, denominator, filters, window, missing_data_handling}` (see metrics.py)."""

from supplier_reliability.services.reporting.metrics import (
    METRIC_KEYS,
    count_metric,
    counted,
    metric,
    metric_problems,
    ratio,
)
from supplier_reliability.services.reporting.reports import (
    anomaly_evaluation_report,
    capacity_risk,
    compliance_exceptions,
    data_quality,
    delivery_quality_trends,
    deteriorating_suppliers,
    disruption_exposure,
    entity_counts,
    invoice_variance_distribution,
    missing_data_rates,
    reconciliation_report,
    score_distribution,
    summary,
    supplier_rankings,
)
from supplier_reliability.services.reporting.writers import render_markdown, write_report

__all__ = [
    "METRIC_KEYS",
    "anomaly_evaluation_report",
    "capacity_risk",
    "compliance_exceptions",
    "count_metric",
    "counted",
    "data_quality",
    "delivery_quality_trends",
    "deteriorating_suppliers",
    "disruption_exposure",
    "entity_counts",
    "invoice_variance_distribution",
    "metric",
    "metric_problems",
    "missing_data_rates",
    "ratio",
    "reconciliation_report",
    "render_markdown",
    "score_distribution",
    "summary",
    "supplier_rankings",
    "write_report",
]

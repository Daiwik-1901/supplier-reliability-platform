"""Anomaly injection into the raw-ingestion layer and its separate ground truth (D9).

Detection, scoring and validation code must never import this package
(`services.isolation.find_ground_truth_references`)."""

from supplier_reliability.services.anomalies.injector import (
    INJECTION_VERSION,
    GroundTruthCase,
    InjectionResult,
    batch_id_for,
    inject_anomalies,
    write_ground_truth,
)

__all__ = [
    "INJECTION_VERSION",
    "GroundTruthCase",
    "InjectionResult",
    "batch_id_for",
    "inject_anomalies",
    "write_ground_truth",
]

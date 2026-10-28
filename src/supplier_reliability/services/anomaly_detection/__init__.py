"""Anomaly detection, quarantine and leakage correction over raw batches (Task 15, D9, spec 8).

Reads raw-ingestion records and canonical tables only; never ground truth (checked by
services/isolation.py).
"""

from supplier_reliability.services.anomaly_detection.batch import (
    DETECTORS,
    DetectionResult,
    detect_raw_batch,
)
from supplier_reliability.services.anomaly_detection.detectors import (
    DetectionHit,
    detect_chronology,
    detect_missing_parent,
    detect_missing_provenance,
    detect_site_conflation,
)
from supplier_reliability.services.anomaly_detection.detectors_leakage import (
    correct_leakage,
    detect_score_leakage,
)

__all__ = [
    "DETECTORS",
    "DetectionHit",
    "DetectionResult",
    "correct_leakage",
    "detect_chronology",
    "detect_missing_parent",
    "detect_missing_provenance",
    "detect_raw_batch",
    "detect_score_leakage",
    "detect_site_conflation",
]

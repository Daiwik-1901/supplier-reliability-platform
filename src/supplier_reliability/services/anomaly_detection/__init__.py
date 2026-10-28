"""Anomaly detection, quarantine and leakage correction over raw batches (Task 15, D9, spec 8).

Reads raw-ingestion records and canonical tables only; never ground truth (checked by
services/isolation.py).

Quarantine is enforced by construction today: raw records are NEVER promoted into canonical
tables (no promotion path exists), so a quarantined record cannot reach scoring. Any future
promotion path MUST skip raw records that have an open data exception whose disposition is
QUARANTINED or QUARANTINED_PENDING_PROVENANCE (until that exception is resolved).
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
    skipped_score_payloads,
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
    "skipped_score_payloads",
]

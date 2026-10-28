"""`detect_raw_batch`: run detectors A-E over one raw batch and record data exceptions.

Transactions (the run_validation / ScoringService pattern): ONE transaction that this function
commits itself. It refuses to start while the session holds pending ORM changes (RuntimeError)
and rolls back on any error, so the exceptions, the leakage-corrected scores
(`ScoringService.stage_corrected` flushes into this same transaction) and their lineage commit
together or not at all. As with run_validation, the guard sees ORM state only: callers must not
hold uncommitted Core writes. `scoring` must be bound to the same session.

Idempotency: one exception per (batch_id, raw_record_id, issue_type) (also a unique
constraint). Pairs that already have an exception are skipped before any correction, so a
re-run writes nothing. Exception ids EXC-NNNNNN continue from the maximum, assigned in
(raw_record_id, issue_type) order; detected_at comes from the injected `clock` (called once).

An unknown or empty batch_id (no raw records) raises ValueError. SUPPLIER_SCORE records whose
cutoff is unusable are listed in DetectionResult.skipped as (raw_record_id, reason) and get no
exception (malformed payloads belong to validate_payload / run_validation).
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import DetectionStatus, IssueType
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import RawIngestionRecord
from supplier_reliability.repositories import exceptions as repo
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
from supplier_reliability.services.scoring.service import ScoringService

DETECTORS = (
    detect_missing_parent,
    detect_chronology,
    detect_score_leakage,
    detect_missing_provenance,
    detect_site_conflation,
)


@dataclass(frozen=True)
class DetectionResult:
    exceptions: list[str]  # ids of the exceptions created by this run
    by_issue_type: dict[str, int]  # new exceptions per issue type (every type, zeros included)
    corrected_scores: list[str]  # distinct corrected_score_ids of this run's exceptions
    skipped: list[tuple[str, str]]  # (raw_record_id, reason): records no detector could judge


def detect_raw_batch(
    session: Session,
    batch_id: str,
    scoring: ScoringService,
    clock: Callable[[], datetime],
) -> DetectionResult:
    """Detect anomalies in raw batch `batch_id` (see the module docstring)."""
    if session.new or session.dirty or session.deleted:
        raise RuntimeError(
            "session has pending changes; commit or roll them back before detection "
            "(it commits its own transaction)"
        )
    if scoring.session is not session:
        raise ValueError("the scoring service must use the detection session")
    if not batch_id or not session.scalar(
        sa.select(sa.func.count())
        .select_from(RawIngestionRecord)
        .where(RawIngestionRecord.batch_id == batch_id)
    ):
        raise ValueError(f"unknown or empty raw batch {batch_id!r}")
    detected_at = ensure_utc(clock())
    try:
        records = list(
            session.scalars(
                sa.select(RawIngestionRecord)
                .where(RawIngestionRecord.batch_id == batch_id)
                .order_by(RawIngestionRecord.raw_record_id)
            )
        )
        done = repo.detected_keys(session, batch_id)
        found: dict[tuple[str, IssueType], DetectionHit] = {}
        for detect in DETECTORS:
            for h in detect(session, records):
                key = (h.raw_record_id, h.issue_type)
                if key not in done:
                    found.setdefault(key, h)
        new = correct_leakage([found[k] for k in sorted(found)], scoring)
        ids = repo.next_exception_ids(session, len(new)) if new else []
        repo.insert_exceptions(
            session,
            [
                _row(exception_id, batch_id, h, detected_at)
                for exception_id, h in zip(ids, new, strict=True)
            ],
        )
        skipped = skipped_score_payloads(records)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    counts = Counter(h.issue_type.value for h in new)
    corrected = [h.corrected_score_id for h in new if h.corrected_score_id is not None]
    return DetectionResult(
        exceptions=ids,
        by_issue_type={i.value: counts.get(i.value, 0) for i in IssueType},
        corrected_scores=list(dict.fromkeys(corrected)),
        skipped=skipped,
    )


def _row(exception_id: str, batch_id: str, h: DetectionHit, detected_at: datetime) -> dict:
    return {
        "exception_id": exception_id,
        "batch_id": batch_id,
        "raw_record_id": h.raw_record_id,
        "source_entity_type": h.source_entity_type,
        "source_record_id": h.source_record_id,
        "issue_type": h.issue_type,
        "rule_id": h.rule_id,
        "severity": h.severity,
        "detection_status": DetectionStatus.DETECTED,
        "detected_at": detected_at,
        "disposition": h.disposition,
        "related_entity_ids": h.related_entity_ids,
        "evidence": h.evidence,
        "supplier_id": h.supplier_id,
        "corrected_score_id": h.corrected_score_id,
    }

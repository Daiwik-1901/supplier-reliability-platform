"""Anomaly C, SCORE_FUTURE_LEAKAGE (DET-C, CRITICAL, CORRECTED): detection and correction.

Detection (`detect_score_leakage`, content-based like every detector): a SUPPLIER_SCORE payload
whose `evidence_refs` cite a canonical record whose occurrence or recording time is after the
payload's data_cutoff_timestamp. The per-entity times are SOURCE_TIMES, the very mapping CH-005
uses (services/validation/source_times.py), loaded in one bulk statement per entity type and id
chunk for all payloads of the batch, then compared per payload in Python (DATE values at day
granularity). A reference's source_entity_type is matched case-insensitively (stripped and
lowercased). Cited records that are absent from canonical or of an unknown entity type cannot
prove leakage and are ignored here. A payload whose data_cutoff_timestamp is missing, naive or
unparseable cannot be judged: it gets no exception (it is a malformed payload for
validate_payload / run_validation) and is listed by `skipped_score_payloads`.

Correction (`correct_leakage`, Ruling R4): the eligible-evidence score for the payload's
supplier and window [evaluation_start, evaluation_end] at its cutoff, via
`ScoringService.stage_corrected` (collect_evidence + score_supplier; an equal current canonical
score is reused, otherwise a CORRECTED score is written with full lineage) inside the caller's
transaction. Its id becomes corrected_score_id; evidence["correction"]["outcome"] says which
case applied. A payload whose supplier is not in canonical or whose window is unusable cannot be
corrected: its exception is QUARANTINED instead, with the reason in the evidence.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import Disposition, IssueType
from supplier_reliability.models import RawIngestionRecord
from supplier_reliability.services.anomaly_detection.detectors import (
    SUPPLIER_SCORE,
    DetectionHit,
    existing_suppliers,
    hit,
    iso,
    of_type,
    parse_ts,
    payload,
    text,
)
from supplier_reliability.services.scoring.evidence import EvaluationWindow
from supplier_reliability.services.scoring.service import ScoringService
from supplier_reliability.services.validation.source_times import (
    SOURCE_TIMES,
    after_cutoff,
    load_source_times,
)

MAX_LISTED = 50  # late evidence entries listed per exception (the count is always complete)


def _cited(p: dict[str, Any]) -> list[tuple[str, str]]:
    refs = p.get("evidence_refs")
    if not isinstance(refs, list):
        return []
    out = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        entity, record_id = ref.get("source_entity_type"), ref.get("source_record_id")
        entity = entity.strip().lower() if isinstance(entity, str) else None
        if entity in SOURCE_TIMES and isinstance(record_id, str):
            out.append((entity, record_id))
    return sorted(set(out))


def _window(p: dict[str, Any]) -> EvaluationWindow | None:
    start, end = parse_ts(p.get("evaluation_start")), parse_ts(p.get("evaluation_end"))
    cutoff = parse_ts(p.get("data_cutoff_timestamp"))
    if start is None or end is None or cutoff is None:
        return None
    try:
        return EvaluationWindow(start, end, cutoff)
    except ValueError:
        return None


SKIP_REASON = "data_cutoff_timestamp missing, unparseable or naive (malformed payload: DC-001)"


def skipped_score_payloads(raw_records: Sequence[RawIngestionRecord]) -> list[tuple[str, str]]:
    """(raw_record_id, reason) of the SUPPLIER_SCORE records leakage detection cannot judge
    because their cutoff is unusable. They get no exception here: they are malformed payloads,
    reported by validate_payload / run_validation (DC-001), and detect_raw_batch lists them in
    DetectionResult.skipped so they never vanish silently."""
    return [
        (r.raw_record_id, SKIP_REASON)
        for r in of_type(raw_records, SUPPLIER_SCORE)
        if parse_ts(payload(r).get("data_cutoff_timestamp")) is None
    ]


def detect_score_leakage(
    session: Session, raw_records: Sequence[RawIngestionRecord]
) -> list[DetectionHit]:
    scores = [
        (r, payload(r), parse_ts(payload(r).get("data_cutoff_timestamp")))
        for r in of_type(raw_records, SUPPLIER_SCORE)
    ]
    scores = [(r, p, cutoff) for r, p, cutoff in scores if cutoff is not None]
    wanted: dict[str, set[str]] = defaultdict(set)
    for _, p, _ in scores:
        for entity, record_id in _cited(p):
            wanted[entity].add(record_id)
    times = load_source_times(session, wanted)
    suppliers = existing_suppliers(session, (text(p.get("supplier_id")) for _, p, _ in scores))

    hits = []
    for r, p, cutoff in scores:
        cited = _cited(p)
        late = [
            {
                "source_entity_type": entity,
                "source_record_id": record_id,
                "field": label,
                "value": iso(value),
            }
            for entity, record_id in cited
            for label, value in times.get((entity, record_id), [])
            if value is not None and after_cutoff(value, cutoff)
        ]
        if not late:
            continue
        supplier = text(p.get("supplier_id"))
        supplier = supplier if supplier in suppliers else None
        evidence = {
            "supplier_id": p.get("supplier_id"),
            "evaluation_start": p.get("evaluation_start"),
            "evaluation_end": p.get("evaluation_end"),
            "data_cutoff_timestamp": iso(cutoff),
            "payload_evidence_fingerprint": p.get("evidence_fingerprint"),
            "cited_evidence_count": len(cited),
            "late_record_count": len(
                {(x["source_entity_type"], x["source_record_id"]) for x in late}
            ),
            "late_evidence_count": len(late),
            "late_evidence": late[:MAX_LISTED],
            "expected_condition": (
                "every cited evidence record occurred and was recorded at or before "
                "data_cutoff_timestamp (D5; per-entity times as CH-005)"
            ),
        }
        hits.append(
            hit(r, IssueType.SCORE_FUTURE_LEAKAGE, supplier, [supplier], evidence, _window(p))
        )
    return hits


def correct_leakage(hits: Sequence[DetectionHit], scoring: ScoringService) -> list[DetectionHit]:
    """`hits` with every SCORE_FUTURE_LEAKAGE hit corrected (see the module docstring)."""
    out = []
    for h in hits:
        if h.issue_type != IssueType.SCORE_FUTURE_LEAKAGE:
            out.append(h)
            continue
        if h.supplier_id is None or h.window is None:
            reason = "supplier not in canonical" if h.supplier_id is None else "unusable window"
            evidence = h.evidence | {"correction": {"outcome": f"not corrected: {reason}"}}
            out.append(replace(h, disposition=Disposition.QUARANTINED, evidence=evidence))
            continue
        score_id, outcome = scoring.stage_corrected(
            h.window,
            h.supplier_id,
            f"leakage correction of {h.source_entity_type} record {h.raw_record_id}",
        )
        correction = {"outcome": outcome.value, "score_id": score_id}
        out.append(
            replace(
                h, corrected_score_id=score_id, evidence=h.evidence | {"correction": correction}
            )
        )
    return out

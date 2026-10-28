"""Score payloads for anomaly class C (SCORE_FUTURE_LEAKAGE) and its negatives (Task 14, D9).

For a scorecard window (start S, cutoff C <= reference_end - 90 d) a LEAKY score is computed
with end = cutoff = C + 90 d (start unchanged) but labelled evaluation_end =
data_cutoff_timestamp = C. A negative is the correctly computed score (cutoff C, evidence
through C), built the same way. Both carry `evidence_refs`: {source_entity_type,
source_record_id} of every included record, entity type by the scoring service's id-prefix
mapping (`entity_type_for`, lowercase lineage vocabulary), citing real canonical ids.

Only (supplier, window) pairs whose leaky evidence set differs from the eligible one AND cites
a shipment / delivery_event / inspection / invoice / risk_signal / po_line occurring or recorded
after C become positives (verified against canonical timestamps; a po_line occurs on its PO's
order_date, as in evidence.py).

The candidates must already be scored (current canonical scores for every (supplier, window)
pair): run score_history before injecting, otherwise ValueError. The payload's score_id is left
None here; the injector assigns the neutral SCORE-RAW id after shuffling (R38).
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability import __version__
from supplier_reliability.config import ScoringConfig
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import (
    SupplierScore,
)
from supplier_reliability.services.anomalies.sampling import Picker, format_ts
from supplier_reliability.services.export import column_converter
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.scoring.engine import ScoreResult, score_supplier
from supplier_reliability.services.scoring.evidence import (
    EvaluationWindow,
    EvidenceBundle,
    collect_evidence,
    scorecard_windows,
)
from supplier_reliability.services.scoring.service import (
    calculation_status,
    entity_type_for,
    evaluation_key,
    scoring_config_hash,
)
from supplier_reliability.services.validation.source_times import after_cutoff, load_source_times

LEAKAGE_HORIZON = timedelta(days=90)
SELF_ID = "<self>"  # placeholder in rationales for the payload's (later assigned) natural id

# Score payload fields: the supplier_scores columns minus explanation and version bookkeeping.
SCORE_FIELDS = (
    "score_id",
    "supplier_id",
    "site_id",
    "evaluation_start",
    "evaluation_end",
    "data_cutoff_timestamp",
    "calculated_at",
    "delivery_score",
    "quality_score",
    "cost_score",
    "capacity_score",
    "compliance_score",
    "disruption_score",
    "overall_score",
    "evidence_coverage",
    "is_provisional",
    "reliability_category",
    "decision",
    "calculation_status",
    "effective_weights",
    "scoring_version",
    "config_hash",
    "code_version",
    "evidence_fingerprint",
)


def evidence_refs(result: ScoreResult) -> list[dict[str, str]]:
    record_ids = {rid for comp in result.components.values() for rid in comp.source_record_ids}
    return [
        {"source_entity_type": entity_type_for(rid), "source_record_id": rid}
        for rid in sorted(record_ids)
    ]


def records_after(session: Session, refs: Iterable[dict[str, str]], cutoff: datetime) -> list[str]:
    """Cited ids whose canonical occurrence or recording time is after `cutoff`, by the per-entity
    times CH-005 and the leakage detector use (SOURCE_TIMES)."""
    wanted: dict[str, set[str]] = defaultdict(set)
    for ref in refs:
        wanted[ref["source_entity_type"]].add(ref["source_record_id"])
    times = load_source_times(session, wanted)
    return sorted(
        record_id
        for (_, record_id), values in times.items()
        if any(v is not None and after_cutoff(v, cutoff) for _, v in values)
    )


def score_payload(
    label: EvaluationWindow, result: ScoreResult, scoring: ScoringConfig
) -> dict[str, Any]:
    """`result` as a supplier_scores-shaped payload labelled with `label` (score_id None)."""
    o = result.overall
    values: dict[str, Any] = {
        "supplier_id": result.supplier_id,
        "evaluation_start": label.start,
        "evaluation_end": label.end,
        "data_cutoff_timestamp": label.cutoff,
        "calculated_at": label.cutoff + timedelta(days=1),
        "overall_score": o.overall,
        "evidence_coverage": o.evidence_coverage,
        "is_provisional": o.is_provisional,
        "reliability_category": o.category,
        "decision": o.decision,
        "calculation_status": calculation_status(result),
        "effective_weights": {k: str(v) for k, v in o.effective_weights.items()},
        "scoring_version": scoring.scoring_version,
        "config_hash": scoring_config_hash(scoring),
        "code_version": __version__,
        "evidence_fingerprint": result.evidence_fingerprint,
    }
    for name, comp in result.components.items():
        values[f"{name}_score"] = comp.score
    columns = SupplierScore.__table__.c
    payload = {
        f: None if values.get(f) is None else column_converter(columns[f])(values[f])
        for f in SCORE_FIELDS
    }
    payload["evidence_refs"] = evidence_refs(result)
    return payload


def build_score_payloads(
    session: Session, cfg: GeneratorConfig, rng: np.random.Generator, scoring: ScoringConfig
) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, Any]]]:
    """(positives as (payload, rationale), negatives) for class C; see the module docstring."""
    config_hash = scoring_config_hash(scoring)
    latest = ensure_utc(cfg.reference_end) - LEAKAGE_HORIZON
    windows = [
        w
        for w in scorecard_windows(cfg.reference_start, cfg.reference_end, scoring.window_months)
        if w.cutoff <= latest
    ]
    eligible: list[dict[str, EvidenceBundle]] = []
    leaky: list[dict[str, EvidenceBundle]] = []
    for w in windows:
        eligible.append(collect_evidence(session, None, w, lookback_days=scoring.lookback_days))
        wide = EvaluationWindow(w.start, w.cutoff + LEAKAGE_HORIZON, w.cutoff + LEAKAGE_HORIZON)
        leaky.append(collect_evidence(session, None, wide, lookback_days=scoring.lookback_days))
    pairs = [(i, sid) for i in range(len(windows)) for sid in sorted(eligible[i])]
    _require_scored(session, pairs, windows, scoring.scoring_version, config_hash)

    candidates = [f"{i}|{sid}" for i, sid in pairs]
    picker = Picker(rng, candidates, "(supplier, scorecard window) pairs")

    def split(key: str) -> tuple[EvaluationWindow, int, str]:
        i, sid = key.split("|", 1)
        return windows[int(i)], int(i), sid

    leaks: dict[str, tuple[ScoreResult, list[str]]] = {}  # key -> (leaky result, late ids)

    def leaks_after_cutoff(key: str) -> bool:
        w, i, sid = split(key)
        good = score_supplier(eligible[i][sid], scoring, config_hash)
        bad = score_supplier(leaky[i][sid], scoring, config_hash)
        good_refs, bad_refs = evidence_refs(good), evidence_refs(bad)
        late = records_after(session, bad_refs, w.cutoff) if bad_refs != good_refs else []
        leaks[key] = (bad, late)
        return bool(late)

    positives: list[tuple[dict[str, Any], str]] = []
    for key in picker.take(cfg.anomaly_preset.positives_per_class, leaks_after_cutoff):
        w, _, sid = split(key)
        bad, late = leaks[key]
        rationale = (
            f"Score {SELF_ID} for {sid} is labelled cutoff {format_ts(w.cutoff)} but was "
            f"computed with evidence through {format_ts(w.cutoff + LEAKAGE_HORIZON)}; it cites "
            f"{len(late)} record(s) occurring or recorded after the cutoff (first: {late[0]})."
        )
        positives.append((score_payload(w, bad, scoring), rationale))

    negatives = []
    for key in picker.take(cfg.anomaly_preset.negatives["scores"]):
        w, i, sid = split(key)
        negatives.append(
            score_payload(w, score_supplier(eligible[i][sid], scoring, config_hash), scoring)
        )
    return positives, negatives


def _require_scored(
    session: Session,
    pairs: list[tuple[int, str]],
    windows: list[EvaluationWindow],
    scoring_version: str,
    config_hash: str,
) -> None:
    keys = {evaluation_key(sid, None, windows[i], scoring_version, config_hash) for i, sid in pairs}
    found = set(
        session.scalars(
            sa.select(SupplierScore.evaluation_key).where(
                SupplierScore.evaluation_key.in_(sorted(keys)),
                SupplierScore.is_current.is_(True),
            )
        )
    )
    missing = len(keys - found)
    if missing:
        raise ValueError(
            f"{missing} of {len(keys)} (supplier, scorecard window) pairs have no current "
            "canonical score: score the scorecard windows (score_history) before injecting "
            "anomalies"
        )

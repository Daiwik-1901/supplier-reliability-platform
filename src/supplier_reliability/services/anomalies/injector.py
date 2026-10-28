"""Anomaly injection into the raw-ingestion layer, with separate ground truth (Task 14, D9).

`inject_anomalies` READS canonical tables and writes ONLY `raw_ingestion_records`: one batch
`BATCH-<dataset_id>`, inserted in the caller's transaction (it flushes, never commits, like
`load_dataset`; `session_scope` commits or rolls back the whole batch). It never writes canonical
tables, data_exceptions or ground truth to the database; ground truth is returned as
`GroundTruthCase`s and written to CSV by `write_ground_truth` only.

Payloads are the canonical row's columns as JSON, formatted exactly like the export
(`column_converter`: decimals as fixed-scale strings, timestamps `...Z`), so
`validate_payload` reads them. `source_record_id` is the payload's natural id.
- Negatives (valid records, D9 sample sizes from `anomaly_preset.negatives`) are copies of
  canonical rows and KEEP their canonical ids: a re-delivered valid record is normal in a feed
  and not an anomaly class. "delivery_event_pairs" = the DISPATCHED and ARRIVED events of one
  shipment (two raw records per pair). Score negatives are correctly computed scores (cutoff C,
  evidence through C) built with the same builder as C; their id is the canonical current score
  with the same evaluation key when one exists, else a fresh SCORE-RAW id.
- Positives get NEW ids that cannot exist in canonical (`SHP-RAW-000001`, `EVT-RAW-...`,
  `RSK-RAW-...`, `SCORE-RAW-...`):
  - A SHIPMENT_MISSING_PO: a canonical shipment copy whose po_id is a fresh `PO-2026-9NNNNN`
    verified absent from purchase_orders.
  - B DELIVERY_BEFORE_DISPATCH: a copy of a shipment's canonical ARRIVED event with
    event_timestamp 1-48 h (whole minutes) BEFORE its canonical DISPATCHED event and
    recorded_timestamp >= event_timestamp (0-12 h later): chronology is the only defect.
  - C SCORE_FUTURE_LEAKAGE: for a scorecard window (start S, cutoff C <= reference_end - 90 d)
    the score is computed with end = cutoff = C + 90 d (start unchanged) but labelled
    evaluation_end = data_cutoff_timestamp = C. `evidence_refs` lists
    {source_entity_type, source_record_id} of every included record (entity type by the
    scoring service's id-prefix mapping). Only (supplier, window) pairs whose leaky evidence
    set differs from the eligible one AND cites a record occurring or recorded after C are used.
  - D RISK_SIGNAL_MISSING_PROVENANCE: a canonical risk-signal copy with signal_source null or
    "" (even case indexes, alternating) or observed_timestamp null (odd indexes).
  - E SITE_ATTRIBUTION_CONFLICT: a canonical shipment copy whose originating_site_id is
    round(e_cross_supplier_fraction * k) times a site of ANOTHER supplier (one effective at
    dispatch, so attribution is the only defect) and otherwise a site of the PO's supplier whose
    effective_to is before the dispatch date. Every FK exists.
  The canonical records behind positives and negatives of one entity type are disjoint.

Determinism: every choice comes from `rs.stream("anomalies")` over id-sorted candidates. Raw
records are shuffled with the same stream, numbered RAW-000001..., and received at
reference_end + 1 day + index seconds. Ground-truth cases (positives only) are numbered
CASE-000001... in raw_record_id order. Too few candidates -> GeneratorCapacityError.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability import __version__
from supplier_reliability.config import ScoringConfig
from supplier_reliability.domain.enums import (
    DeliveryEventType,
    Disposition,
    IssueType,
    Severity,
)
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import (
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RawIngestionRecord,
    RiskSignal,
    Shipment,
    Site,
    SupplierScore,
)
from supplier_reliability.services.export import FileEntry, column_converter, write_csv
from supplier_reliability.services.generator.config import GeneratorCapacityError, GeneratorConfig
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.rng import RandomSource
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

__all__ = [
    "INJECTION_VERSION",
    "GroundTruthCase",
    "InjectionResult",
    "inject_anomalies",
    "write_ground_truth",
]

INJECTION_VERSION = "1.0.0"
STREAM = "anomalies"
LEAKAGE_HORIZON = timedelta(days=90)
GROUND_TRUTH_FILE = "anomaly_ground_truth.csv"
PROFILES_FILE = "supplier_profiles.csv"

SHIPMENT, DELIVERY_EVENT, RISK_SIGNAL, SUPPLIER_SCORE = (
    "SHIPMENT",
    "DELIVERY_EVENT",
    "RISK_SIGNAL",
    "SUPPLIER_SCORE",
)
_ID_FIELD = {
    SHIPMENT: "shipment_id",
    DELIVERY_EVENT: "delivery_event_id",
    RISK_SIGNAL: "risk_signal_id",
    SUPPLIER_SCORE: "score_id",
}
_OUTCOME: dict[IssueType, tuple[Severity, Disposition]] = {
    IssueType.SHIPMENT_MISSING_PO: (Severity.HIGH, Disposition.QUARANTINED),
    IssueType.DELIVERY_BEFORE_DISPATCH: (Severity.HIGH, Disposition.QUARANTINED),
    IssueType.SCORE_FUTURE_LEAKAGE: (Severity.CRITICAL, Disposition.CORRECTED),
    IssueType.RISK_SIGNAL_MISSING_PROVENANCE: (
        Severity.MEDIUM,
        Disposition.QUARANTINED_PENDING_PROVENANCE,
    ),
    IssueType.SITE_ATTRIBUTION_CONFLICT: (Severity.HIGH, Disposition.QUARANTINED),
}
# Score payload fields: the supplier_scores columns minus explanation and version bookkeeping.
_SCORE_FIELDS = (
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


@dataclass(frozen=True)
class GroundTruthCase:
    case_id: str
    entity_type: str  # the raw record's source_entity_type
    entity_id: str  # the raw_record_id
    issue_type: IssueType
    expected_detection: bool
    expected_decision: Disposition
    severity: Severity
    rationale: str
    injection_version: str


@dataclass(frozen=True)
class InjectionResult:
    batch_id: str
    raw_counts: dict[str, int]  # raw records per source_entity_type
    cases: list[GroundTruthCase]


@dataclass(frozen=True)
class _Draft:
    entity_type: str
    payload: dict[str, Any]
    issue: IssueType | None = None
    rationale: str | None = None


# --- helpers ----------------------------------------------------------------------------------


def _payload(table: sa.Table, row: Any) -> dict[str, Any]:
    """A canonical row as a JSON payload (column order, export formatting)."""
    out: dict[str, Any] = {}
    for column in table.columns:
        value = row[column.name]
        out[column.name] = None if value is None else column_converter(column)(value)
    return out


def _rows(session: Session, table: sa.Table, *where: Any) -> list[Any]:
    pk = list(table.primary_key.columns)
    return list(session.execute(sa.select(table).where(*where).order_by(*pk)).mappings())


_TS = column_converter(Shipment.__table__.c.created_at)  # datetime -> "...Z"


class _Picker:
    """Deterministic sampling without replacement across all calls for one entity type."""

    def __init__(self, rng: np.random.Generator, ids: Sequence[str], what: str) -> None:
        self.order = [ids[i] for i in rng.permutation(len(ids))]
        self.used: set[str] = set()
        self.what = what

    def take(self, k: int, accept: Callable[[str], bool] = lambda _: True) -> list[str]:
        out: list[str] = []
        for record_id in self.order:
            if len(out) == k:
                break
            if record_id not in self.used and accept(record_id):
                out.append(record_id)
        if len(out) < k:
            raise GeneratorCapacityError(
                f"anomaly injection needs {k} {self.what}, only {len(out)} available"
            )
        self.used.update(out)
        return out


class _RawIds:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()

    def next(self, prefix: str) -> str:
        self.counters[prefix] += 1
        return f"{prefix}-RAW-{self.counters[prefix]:06d}"


def _effective(site: Any, day: date) -> bool:
    return site["effective_from"] <= day and (
        site["effective_to"] is None or day <= site["effective_to"]
    )


# --- shipments: A, E and shipment negatives ---------------------------------------------------


def _shipments(
    session: Session, cfg: GeneratorConfig, rng: np.random.Generator, ids: _RawIds
) -> list[_Draft]:
    k = cfg.anomaly_preset.positives_per_class
    table = Shipment.__table__
    shipments = {r["shipment_id"]: r for r in _rows(session, table)}
    po_supplier = dict(
        session.execute(sa.select(PurchaseOrder.po_id, PurchaseOrder.supplier_id)).all()
    )
    sites = _rows(session, Site.__table__)
    by_supplier: dict[str, list[Any]] = defaultdict(list)
    for s in sites:
        by_supplier[s["supplier_id"]].append(s)
    picker = _Picker(rng, sorted(shipments), "shipments")

    def dispatch_day(shipment_id: str) -> date:
        return ensure_utc(shipments[shipment_id]["dispatch_timestamp"]).date()

    drafts: list[_Draft] = []
    # A: fresh PO ids PO-2026-9NNNNN verified absent from canonical
    fresh = (f"PO-2026-{n:06d}" for n in range(900_001, 1_000_000))
    fresh = (po for po in fresh if po not in po_supplier)
    for base_id in picker.take(k):
        payload = _payload(table, shipments[base_id])
        payload["shipment_id"] = ids.next("SHP")
        payload["po_id"] = next(fresh)
        rationale = (
            f"Shipment {payload['shipment_id']} references purchase order {payload['po_id']}, "
            f"which does not exist in canonical purchase_orders (copy of shipment {base_id})."
        )
        drafts.append(_Draft(SHIPMENT, payload, IssueType.SHIPMENT_MISSING_PO, rationale))

    # E: cross-supplier sites, then the supplier's own sites ended before dispatch
    n_cross = round(cfg.e_cross_supplier_fraction * k)

    def ended_sites(shipment_id: str) -> list[Any]:
        supplier = po_supplier[shipments[shipment_id]["po_id"]]
        day = dispatch_day(shipment_id)
        return [
            s
            for s in by_supplier[supplier]
            if s["effective_to"] is not None and s["effective_to"] < day
        ]

    for i, base_id in enumerate(picker.take(n_cross) + picker.take(k - n_cross, ended_sites)):
        base = shipments[base_id]
        supplier, day = po_supplier[base["po_id"]], dispatch_day(base_id)
        if i < n_cross:
            options = [s for s in sites if s["supplier_id"] != supplier and _effective(s, day)]
            why = "a site of supplier {owner}, not the PO's supplier {supplier}"
        else:
            options = ended_sites(base_id)
            why = "a site of the PO's supplier {supplier} whose effective period ended {end}"
        site = options[int(rng.integers(len(options)))]
        payload = _payload(table, base)
        payload["shipment_id"] = ids.next("SHP")
        payload["originating_site_id"] = site["site_id"]
        detail = why.format(owner=site["supplier_id"], supplier=supplier, end=site["effective_to"])
        rationale = (
            f"Shipment {payload['shipment_id']} on PO {base['po_id']} dispatched {day} is "
            f"attributed to site {site['site_id']}, {detail}; every FK exists "
            f"(copy of shipment {base_id})."
        )
        drafts.append(_Draft(SHIPMENT, payload, IssueType.SITE_ATTRIBUTION_CONFLICT, rationale))

    for base_id in picker.take(cfg.anomaly_preset.negatives["shipments"]):
        drafts.append(_Draft(SHIPMENT, _payload(table, shipments[base_id])))
    return drafts


# --- delivery events: B and event-pair negatives ----------------------------------------------


def _events(
    session: Session, cfg: GeneratorConfig, rng: np.random.Generator, ids: _RawIds
) -> list[_Draft]:
    table = DeliveryEvent.__table__
    kinds = (DeliveryEventType.DISPATCHED, DeliveryEventType.ARRIVED)
    by_shipment: dict[str, dict[DeliveryEventType, list[Any]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for e in _rows(session, table, DeliveryEvent.event_type.in_(kinds)):
        by_shipment[e["shipment_id"]][e["event_type"]].append(e)
    pairs = sorted(
        sid for sid, ev in by_shipment.items() if all(len(ev[kind]) == 1 for kind in kinds)
    )
    picker = _Picker(rng, pairs, "shipments with one DISPATCHED and one ARRIVED event")

    drafts: list[_Draft] = []
    for shipment_id in picker.take(cfg.anomaly_preset.positives_per_class):
        dispatched = by_shipment[shipment_id][DeliveryEventType.DISPATCHED][0]
        arrived = by_shipment[shipment_id][DeliveryEventType.ARRIVED][0]
        gap = timedelta(minutes=int(rng.integers(60, 48 * 60 + 1)))
        event_ts = ensure_utc(dispatched["event_timestamp"]) - gap
        recorded = event_ts + timedelta(minutes=int(rng.integers(0, 12 * 60 + 1)))
        payload = _payload(table, arrived)
        payload["delivery_event_id"] = ids.next("EVT")
        payload["event_timestamp"] = _TS(event_ts)
        payload["recorded_timestamp"] = _TS(recorded)
        rationale = (
            f"ARRIVED event {payload['delivery_event_id']} for shipment {shipment_id} at "
            f"{payload['event_timestamp']} precedes its canonical DISPATCHED event "
            f"{dispatched['delivery_event_id']} at {_TS(dispatched['event_timestamp'])} by "
            f"{gap}; recorded_timestamp >= event_timestamp "
            f"(copy of {arrived['delivery_event_id']})."
        )
        drafts.append(
            _Draft(DELIVERY_EVENT, payload, IssueType.DELIVERY_BEFORE_DISPATCH, rationale)
        )
    for shipment_id in picker.take(cfg.anomaly_preset.negatives["delivery_event_pairs"]):
        for kind in kinds:
            drafts.append(
                _Draft(DELIVERY_EVENT, _payload(table, by_shipment[shipment_id][kind][0]))
            )
    return drafts


# --- risk signals: D and negatives ------------------------------------------------------------


def _risks(
    session: Session, cfg: GeneratorConfig, rng: np.random.Generator, ids: _RawIds
) -> list[_Draft]:
    table = RiskSignal.__table__
    signals = {r["risk_signal_id"]: r for r in _rows(session, table)}
    picker = _Picker(rng, sorted(signals), "risk signals")
    drafts: list[_Draft] = []
    for i, base_id in enumerate(picker.take(cfg.anomaly_preset.positives_per_class)):
        payload = _payload(table, signals[base_id])
        payload["risk_signal_id"] = ids.next("RSK")
        if i % 2 == 0:
            payload["signal_source"] = None if (i // 2) % 2 == 0 else ""
            what = f"signal_source {payload['signal_source']!r}"
        else:
            payload["observed_timestamp"] = None
            what = "observed_timestamp null"
        rationale = (
            f"Risk signal {payload['risk_signal_id']} lacks provenance ({what}); other fields "
            f"valid (copy of {base_id})."
        )
        drafts.append(
            _Draft(RISK_SIGNAL, payload, IssueType.RISK_SIGNAL_MISSING_PROVENANCE, rationale)
        )
    for base_id in picker.take(cfg.anomaly_preset.negatives["risk_signals"]):
        drafts.append(_Draft(RISK_SIGNAL, _payload(table, signals[base_id])))
    return drafts


# --- scores: C and negatives ------------------------------------------------------------------


def _evidence_refs(result: ScoreResult) -> list[dict[str, str]]:
    record_ids = {rid for comp in result.components.values() for rid in comp.source_record_ids}
    return [
        {"source_entity_type": entity_type_for(rid), "source_record_id": rid}
        for rid in sorted(record_ids)
    ]


def _records_after(session: Session, refs: Iterable[dict[str, str]], cutoff: datetime) -> list:
    """Cited ids whose canonical occurrence or recording time is after `cutoff`."""
    wanted: dict[str, list[str]] = defaultdict(list)
    for ref in refs:
        wanted[ref["source_entity_type"]].append(ref["source_record_id"])
    queries = {  # entity -> (id, occurrence, recording)
        "shipment": (
            Shipment.shipment_id,
            Shipment.expected_delivery_timestamp,
            Shipment.created_at,
        ),
        "delivery_event": (
            DeliveryEvent.delivery_event_id,
            DeliveryEvent.event_timestamp,
            DeliveryEvent.recorded_timestamp,
        ),
        "inspection": (
            Inspection.inspection_id,
            Inspection.inspection_timestamp,
            Inspection.recorded_at,
        ),
        "invoice": (Invoice.invoice_id, Invoice.invoice_date, Invoice.recorded_at),
        "risk_signal": (
            RiskSignal.risk_signal_id,
            RiskSignal.observed_timestamp,
            RiskSignal.recorded_timestamp,
        ),
        "po_line": (POLine.po_line_id, PurchaseOrder.order_date, POLine.created_at),
    }

    def after(value: datetime | date) -> bool:
        if isinstance(value, datetime):
            return ensure_utc(value) > cutoff
        return value > cutoff.date()

    late: list[str] = []
    for entity, (id_col, occurred, recorded) in queries.items():
        if not wanted.get(entity):
            continue
        stmt = sa.select(id_col, occurred, recorded).where(id_col.in_(wanted[entity]))
        if entity == "po_line":
            stmt = stmt.join(PurchaseOrder, PurchaseOrder.po_id == POLine.po_id)
        late.extend(r[0] for r in session.execute(stmt) if after(r[1]) or after(r[2]))
    return sorted(late)


def _score_payload(
    score_id: str, label: EvaluationWindow, result: ScoreResult, scoring: ScoringConfig
) -> dict[str, Any]:
    o = result.overall
    values: dict[str, Any] = {
        "score_id": score_id,
        "supplier_id": result.supplier_id,
        "site_id": None,
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
        for f in _SCORE_FIELDS
    }
    payload["evidence_refs"] = _evidence_refs(result)
    return payload


def _scores(
    session: Session,
    cfg: GeneratorConfig,
    rng: np.random.Generator,
    ids: _RawIds,
    scoring: ScoringConfig,
) -> list[_Draft]:
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
    candidates = [f"{i}|{sid}" for i in range(len(windows)) for sid in sorted(eligible[i])]
    picker = _Picker(rng, candidates, "(supplier, scorecard window) pairs")

    def split(key: str) -> tuple[EvaluationWindow, int, str]:
        i, sid = key.split("|", 1)
        return windows[int(i)], int(i), sid

    leaks: dict[str, tuple[ScoreResult, list[str]]] = {}  # key -> (leaky result, late ids)

    def leaks_after_cutoff(key: str) -> bool:
        w, i, sid = split(key)
        good = score_supplier(eligible[i][sid], scoring, config_hash)
        bad = score_supplier(leaky[i][sid], scoring, config_hash)
        good_refs, bad_refs = _evidence_refs(good), _evidence_refs(bad)
        late = _records_after(session, bad_refs, w.cutoff) if bad_refs != good_refs else []
        leaks[key] = (bad, late)
        return bool(late)

    drafts: list[_Draft] = []
    for key in picker.take(cfg.anomaly_preset.positives_per_class, leaks_after_cutoff):
        w, _, sid = split(key)
        bad, late = leaks[key]
        payload = _score_payload(ids.next("SCORE"), w, bad, scoring)
        rationale = (
            f"Score {payload['score_id']} for {sid} is labelled cutoff {_TS(w.cutoff)} but was "
            f"computed with evidence through {_TS(w.cutoff + LEAKAGE_HORIZON)}; it cites "
            f"{len(late)} record(s) occurring or recorded after the cutoff (first: {late[0]})."
        )
        drafts.append(_Draft(SUPPLIER_SCORE, payload, IssueType.SCORE_FUTURE_LEAKAGE, rationale))

    negatives = [split(key) for key in picker.take(cfg.anomaly_preset.negatives["scores"])]
    keys = [
        evaluation_key(sid, None, w, scoring.scoring_version, config_hash)
        for w, _, sid in negatives
    ]
    stored = dict(
        session.execute(
            sa.select(SupplierScore.evaluation_key, SupplierScore.score_id).where(
                SupplierScore.evaluation_key.in_(sorted(keys)),
                SupplierScore.is_current.is_(True),
            )
        ).all()
    )
    for (w, i, sid), key in zip(negatives, keys, strict=True):
        result = score_supplier(eligible[i][sid], scoring, config_hash)
        score_id = stored.get(key) or ids.next("SCORE")
        drafts.append(_Draft(SUPPLIER_SCORE, _score_payload(score_id, w, result, scoring)))
    return drafts


# --- public API -------------------------------------------------------------------------------


def inject_anomalies(
    session: Session, cfg: GeneratorConfig, rs: RandomSource, *, scoring: ScoringConfig
) -> InjectionResult:
    """Write the raw batch (negatives + positives A-E, D9) in the caller's transaction and
    return its ground truth. `scoring` is the ScoringConfig used to build score payloads."""
    batch_id = f"BATCH-{cfg.dataset_id}"
    existing = session.scalar(
        sa.select(sa.func.count())
        .select_from(RawIngestionRecord)
        .where(RawIngestionRecord.batch_id == batch_id)
    )
    if existing:
        raise ValueError(f"raw batch {batch_id} was already injected ({existing} records)")

    rng, ids = rs.stream(STREAM), _RawIds()
    drafts = (
        _shipments(session, cfg, rng, ids)
        + _events(session, cfg, rng, ids)
        + _risks(session, cfg, rng, ids)
        + _scores(session, cfg, rng, ids, scoring)
    )
    order = rng.permutation(len(drafts))
    registry = IdRegistry()
    first = ensure_utc(cfg.reference_end) + timedelta(days=1)
    rows: list[dict[str, Any]] = []
    cases: list[GroundTruthCase] = []
    for index, draft_index in enumerate(order):
        d = drafts[int(draft_index)]
        raw_id = registry.next("raw")
        rows.append(
            {
                "raw_record_id": raw_id,
                "batch_id": batch_id,
                "source_entity_type": d.entity_type,
                "source_record_id": d.payload[_ID_FIELD[d.entity_type]],
                "payload": d.payload,
                "received_at": first + timedelta(seconds=index),
            }
        )
        if d.issue is not None:
            severity, decision = _OUTCOME[d.issue]
            cases.append(
                GroundTruthCase(
                    case_id=registry.next("case"),
                    entity_type=d.entity_type,
                    entity_id=raw_id,
                    issue_type=d.issue,
                    expected_detection=True,
                    expected_decision=decision,
                    severity=severity,
                    rationale=d.rationale or "",
                    injection_version=INJECTION_VERSION,
                )
            )
    session.execute(sa.insert(RawIngestionRecord), rows)
    session.flush()
    counts = Counter(r["source_entity_type"] for r in rows)
    return InjectionResult(batch_id, dict(sorted(counts.items())), cases)


def write_ground_truth(
    cases: Iterable[GroundTruthCase], profiles: dict[str, str], out_dir: Path
) -> dict[str, FileEntry]:
    """Write anomaly_ground_truth.csv (sorted by case_id) and supplier_profiles.csv (sorted by
    supplier_id) with the export's CSV conventions; returns FileEntry by relative path."""
    header = [
        "case_id",
        "entity_type",
        "entity_id",
        "issue_type",
        "expected_detection",
        "expected_decision",
        "severity",
        "rationale",
        "injection_version",
    ]
    rows = [
        [
            c.case_id,
            c.entity_type,
            c.entity_id,
            c.issue_type.value,
            "true" if c.expected_detection else "false",
            c.expected_decision.value,
            c.severity.value,
            c.rationale,
            c.injection_version,
        ]
        for c in sorted(cases, key=lambda c: c.case_id)
    ]
    entries = [
        write_csv(out_dir, GROUND_TRUTH_FILE, header, rows),
        write_csv(
            out_dir,
            PROFILES_FILE,
            ["supplier_id", "profile"],
            [[sid, profiles[sid]] for sid in sorted(profiles)],
        ),
    ]
    return {e.path: e for e in entries}

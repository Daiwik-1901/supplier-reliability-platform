"""Anomaly injection into the raw-ingestion layer, with separate ground truth (Task 14, D9).

`inject_anomalies` READS canonical tables and writes ONLY `raw_ingestion_records`: one batch
`BATCH-<dataset_id>`, inserted in the caller's transaction (it flushes, never commits, like
`load_dataset`; `session_scope` commits or rolls back the whole batch). It never writes canonical
tables, data_exceptions or ground truth to the database; ground truth is returned as
`GroundTruthCase`s and written to CSV by `write_ground_truth` only.

Payloads are the canonical row's columns as JSON, formatted exactly like the export
(`column_converter`: decimals as fixed-scale strings, timestamps `...Z`), so
`validate_payload` reads them.

Natural ids (Ruling R38, replaces R35): EVERY raw record, positive or negative, gets a NEW
neutral natural id (`SHP-RAW-NNNNNN`, `EVT-RAW-...`, `RSK-RAW-...`, `SCORE-RAW-...`) from one
counter per prefix, assigned AFTER the deterministic shuffle, so the number follows the
shuffled raw order and encodes neither the class nor a sub-variant (E cross-supplier vs
not-effective, D null source vs null observed). `source_record_id` = that id. No raw natural id
exists in canonical. References to other entities (po_id, shipment_id, supplier_id, site ids,
evidence_refs) stay real.

- Negatives (valid records, D9 sample sizes from `anomaly_preset.negatives`) are copies of
  canonical rows, identical except the id field. "delivery_event_pairs" = the DISPATCHED and
  ARRIVED events of one shipment: two raw records per pair, each with its own EVT-RAW id and
  the real shipment_id. Score negatives are correctly computed scores (cutoff C, evidence
  through C; see score_payloads.py), equal to the canonical current score except the id.
- Positives:
  - A SHIPMENT_MISSING_PO: a canonical shipment copy whose po_id is a fresh `PO-2026-9NNNNN`
    verified absent from purchase_orders.
  - B DELIVERY_BEFORE_DISPATCH: a copy of a shipment's canonical ARRIVED event with
    event_timestamp 1-48 h (whole minutes) BEFORE its canonical DISPATCHED event and
    recorded_timestamp >= event_timestamp (0-12 h later): chronology is the only defect.
  - C SCORE_FUTURE_LEAKAGE: score_payloads.py (requires a scored DB: score before inject).
  - D RISK_SIGNAL_MISSING_PROVENANCE: a canonical risk-signal copy with signal_source null or
    "" (even case indexes, alternating) or observed_timestamp null (odd indexes).
  - E SITE_ATTRIBUTION_CONFLICT: a canonical shipment copy whose originating_site_id is
    round(e_cross_supplier_fraction * k) times a site of ANOTHER supplier (one effective at
    dispatch, so attribution is the only defect) and otherwise a site of the PO's supplier whose
    effective_to is before the dispatch date. Every FK exists.
  The canonical records behind positives and negatives of one entity type are disjoint.

Determinism: every choice comes from `rs.stream("anomalies")` over id-sorted candidates. Raw
records are shuffled with the same stream, numbered RAW-000001... (and given their natural ids)
in that order, and received at reference_end + 1 day + index seconds. Ground-truth cases
(positives only) are numbered CASE-000001... in raw_record_id order. Too few candidates ->
GeneratorCapacityError.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy.orm import Session

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
    PurchaseOrder,
    RawIngestionRecord,
    RiskSignal,
    Shipment,
    Site,
)
from supplier_reliability.services.anomalies.sampling import (
    Picker,
    format_ts,
    row_payload,
    table_rows,
)
from supplier_reliability.services.anomalies.score_payloads import SELF_ID, build_score_payloads
from supplier_reliability.services.export import FileEntry, write_csv
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.rng import RandomSource

__all__ = [
    "INJECTION_VERSION",
    "GroundTruthCase",
    "InjectionResult",
    "inject_anomalies",
    "write_ground_truth",
]

INJECTION_VERSION = "1.0.0"
STREAM = "anomalies"
GROUND_TRUTH_FILE = "anomaly_ground_truth.csv"
PROFILES_FILE = "supplier_profiles.csv"

SHIPMENT, DELIVERY_EVENT, RISK_SIGNAL, SUPPLIER_SCORE = (
    "SHIPMENT",
    "DELIVERY_EVENT",
    "RISK_SIGNAL",
    "SUPPLIER_SCORE",
)
# entity type -> (payload id field, natural id prefix)
_NATURAL_ID = {
    SHIPMENT: ("shipment_id", "SHP"),
    DELIVERY_EVENT: ("delivery_event_id", "EVT"),
    RISK_SIGNAL: ("risk_signal_id", "RSK"),
    SUPPLIER_SCORE: ("score_id", "SCORE"),
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
    """A raw payload before its natural id is assigned (rationale uses SELF_ID for it)."""

    entity_type: str
    payload: dict[str, Any]
    issue: IssueType | None = None
    rationale: str | None = None


def _copy(table: sa.Table, row: Any) -> dict[str, Any]:
    """A canonical row as a payload with its id field blanked (assigned after the shuffle)."""
    payload = row_payload(table, row)
    payload[table.primary_key.columns.values()[0].name] = None
    return payload


def _effective(site: Any, day: date) -> bool:
    return site["effective_from"] <= day and (
        site["effective_to"] is None or day <= site["effective_to"]
    )


# --- shipments: A, E and shipment negatives ---------------------------------------------------


def _shipments(session: Session, cfg: GeneratorConfig, rng: np.random.Generator) -> list[_Draft]:
    k = cfg.anomaly_preset.positives_per_class
    table = Shipment.__table__
    shipments = {r["shipment_id"]: r for r in table_rows(session, table)}
    po_supplier = dict(
        session.execute(sa.select(PurchaseOrder.po_id, PurchaseOrder.supplier_id)).all()
    )
    sites = table_rows(session, Site.__table__)
    by_supplier: dict[str, list[Any]] = defaultdict(list)
    for s in sites:
        by_supplier[s["supplier_id"]].append(s)
    picker = Picker(rng, sorted(shipments), "shipments")

    def dispatch_day(shipment_id: str) -> date:
        return ensure_utc(shipments[shipment_id]["dispatch_timestamp"]).date()

    drafts: list[_Draft] = []
    # A: fresh PO ids PO-2026-9NNNNN verified absent from canonical
    fresh = (f"PO-2026-{n:06d}" for n in range(900_001, 1_000_000))
    fresh = (po for po in fresh if po not in po_supplier)
    for base_id in picker.take(k):
        payload = _copy(table, shipments[base_id])
        payload["po_id"] = next(fresh)
        rationale = (
            f"Shipment {SELF_ID} references purchase order {payload['po_id']}, which does not "
            f"exist in canonical purchase_orders (copy of shipment {base_id})."
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
        payload = _copy(table, base)
        payload["originating_site_id"] = site["site_id"]
        detail = why.format(owner=site["supplier_id"], supplier=supplier, end=site["effective_to"])
        rationale = (
            f"Shipment {SELF_ID} on PO {base['po_id']} dispatched {day} is attributed to site "
            f"{site['site_id']}, {detail}; every FK exists (copy of shipment {base_id})."
        )
        drafts.append(_Draft(SHIPMENT, payload, IssueType.SITE_ATTRIBUTION_CONFLICT, rationale))

    for base_id in picker.take(cfg.anomaly_preset.negatives["shipments"]):
        drafts.append(_Draft(SHIPMENT, _copy(table, shipments[base_id])))
    return drafts


# --- delivery events: B and event-pair negatives ----------------------------------------------


def _events(session: Session, cfg: GeneratorConfig, rng: np.random.Generator) -> list[_Draft]:
    table = DeliveryEvent.__table__
    kinds = (DeliveryEventType.DISPATCHED, DeliveryEventType.ARRIVED)
    by_shipment: dict[str, dict[DeliveryEventType, list[Any]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for e in table_rows(session, table, DeliveryEvent.event_type.in_(kinds)):
        by_shipment[e["shipment_id"]][e["event_type"]].append(e)
    pairs = sorted(
        sid for sid, ev in by_shipment.items() if all(len(ev[kind]) == 1 for kind in kinds)
    )
    picker = Picker(rng, pairs, "shipments with one DISPATCHED and one ARRIVED event")

    drafts: list[_Draft] = []
    for shipment_id in picker.take(cfg.anomaly_preset.positives_per_class):
        dispatched = by_shipment[shipment_id][DeliveryEventType.DISPATCHED][0]
        arrived = by_shipment[shipment_id][DeliveryEventType.ARRIVED][0]
        gap = timedelta(minutes=int(rng.integers(60, 48 * 60 + 1)))
        event_ts = ensure_utc(dispatched["event_timestamp"]) - gap
        recorded = event_ts + timedelta(minutes=int(rng.integers(0, 12 * 60 + 1)))
        payload = _copy(table, arrived)
        payload["event_timestamp"] = format_ts(event_ts)
        payload["recorded_timestamp"] = format_ts(recorded)
        rationale = (
            f"ARRIVED event {SELF_ID} for shipment {shipment_id} at "
            f"{payload['event_timestamp']} precedes its canonical DISPATCHED event "
            f"{dispatched['delivery_event_id']} at {format_ts(dispatched['event_timestamp'])} "
            f"by {gap}; recorded_timestamp >= event_timestamp "
            f"(copy of {arrived['delivery_event_id']})."
        )
        drafts.append(
            _Draft(DELIVERY_EVENT, payload, IssueType.DELIVERY_BEFORE_DISPATCH, rationale)
        )
    for shipment_id in picker.take(cfg.anomaly_preset.negatives["delivery_event_pairs"]):
        for kind in kinds:
            drafts.append(_Draft(DELIVERY_EVENT, _copy(table, by_shipment[shipment_id][kind][0])))
    return drafts


# --- risk signals: D and negatives ------------------------------------------------------------


def _risks(session: Session, cfg: GeneratorConfig, rng: np.random.Generator) -> list[_Draft]:
    table = RiskSignal.__table__
    signals = {r["risk_signal_id"]: r for r in table_rows(session, table)}
    picker = Picker(rng, sorted(signals), "risk signals")
    drafts: list[_Draft] = []
    for i, base_id in enumerate(picker.take(cfg.anomaly_preset.positives_per_class)):
        payload = _copy(table, signals[base_id])
        if i % 2 == 0:
            payload["signal_source"] = None if (i // 2) % 2 == 0 else ""
            what = f"signal_source {payload['signal_source']!r}"
        else:
            payload["observed_timestamp"] = None
            what = "observed_timestamp null"
        rationale = (
            f"Risk signal {SELF_ID} lacks provenance ({what}); other fields valid "
            f"(copy of {base_id})."
        )
        drafts.append(
            _Draft(RISK_SIGNAL, payload, IssueType.RISK_SIGNAL_MISSING_PROVENANCE, rationale)
        )
    for base_id in picker.take(cfg.anomaly_preset.negatives["risk_signals"]):
        drafts.append(_Draft(RISK_SIGNAL, _copy(table, signals[base_id])))
    return drafts


# --- scores: C and negatives (built in score_payloads.py) -------------------------------------


def _scores(
    session: Session, cfg: GeneratorConfig, rng: np.random.Generator, scoring: ScoringConfig
) -> list[_Draft]:
    positives, negatives = build_score_payloads(session, cfg, rng, scoring)
    return [
        _Draft(SUPPLIER_SCORE, payload, IssueType.SCORE_FUTURE_LEAKAGE, rationale)
        for payload, rationale in positives
    ] + [_Draft(SUPPLIER_SCORE, payload) for payload in negatives]


# --- public API -------------------------------------------------------------------------------


def batch_id_for(dataset_id: str) -> str:
    """The raw batch id the injector writes for dataset `dataset_id` (D9)."""
    return f"BATCH-{dataset_id}"


def inject_anomalies(
    session: Session, cfg: GeneratorConfig, rs: RandomSource, *, scoring: ScoringConfig
) -> InjectionResult:
    """Write the raw batch (negatives + positives A-E, D9) in the caller's transaction and
    return its ground truth. `scoring` is the ScoringConfig used to build score payloads; the
    scorecard windows must already be scored with it (ValueError otherwise)."""
    batch_id = batch_id_for(cfg.dataset_id)
    existing = session.scalar(
        sa.select(sa.func.count())
        .select_from(RawIngestionRecord)
        .where(RawIngestionRecord.batch_id == batch_id)
    )
    if existing:
        raise ValueError(f"raw batch {batch_id} was already injected ({existing} records)")

    rng = rs.stream(STREAM)
    drafts = (
        _shipments(session, cfg, rng)
        + _events(session, cfg, rng)
        + _risks(session, cfg, rng)
        + _scores(session, cfg, rng, scoring)
    )
    order = rng.permutation(len(drafts))
    registry, natural = IdRegistry(), Counter[str]()
    first = ensure_utc(cfg.reference_end) + timedelta(days=1)
    rows: list[dict[str, Any]] = []
    cases: list[GroundTruthCase] = []
    for index, draft_index in enumerate(order):
        d = drafts[int(draft_index)]
        raw_id = registry.next("raw")
        id_field, prefix = _NATURAL_ID[d.entity_type]
        natural[prefix] += 1
        natural_id = f"{prefix}-RAW-{natural[prefix]:06d}"
        payload = d.payload | {id_field: natural_id}
        rows.append(
            {
                "raw_record_id": raw_id,
                "batch_id": batch_id,
                "source_entity_type": d.entity_type,
                "source_record_id": natural_id,
                "payload": payload,
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
                    rationale=(d.rationale or "").replace(SELF_ID, natural_id),
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

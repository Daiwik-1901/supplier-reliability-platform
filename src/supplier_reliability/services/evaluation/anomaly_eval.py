"""Anomaly detection evaluated against ground truth (spec section 10, D9, Ruling R42).

Evaluation unit: one raw record per issue class ("raw_record_per_issue_class"). For class k:
- positives: ground-truth cases of class k whose entity_id (a raw_record_id) belongs to the batch
  (expected_detection true);
- predictions: the batch's data_exceptions of issue type k, by raw_record_id;
- negative population (R42): raw records of the batch that COULD exhibit k and are not positive
  for k. A and E: SHIPMENT records; B: DELIVERY_EVENT records whose payload event_type is
  ARRIVED (DISPATCHED copies cannot be B); C: SUPPLIER_SCORE; D: RISK_SIGNAL. Other entity types
  are not meaningful negatives for k and are not counted.
- TP = positive and predicted; FN = positive, not predicted; FP = predicted, not positive (any
  record); TN = negative-population records without a prediction of k.
`micro` sums the per-class counts. `decision_agreement[k]` compares, for each TP, the ground
truth's expected_decision with the exception's disposition.

This is the evaluation layer: it may read ground truth. Scoring, validation and detection must
never import it (services/isolation.py).
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import IssueType
from supplier_reliability.models import DataException, RawIngestionRecord

__all__ = ["UNIT", "AnomalyEvaluation", "ConfusionCounts", "evaluate_anomalies"]

UNIT = "raw_record_per_issue_class"
ENTITY_OF_CLASS: dict[IssueType, str] = {
    IssueType.SHIPMENT_MISSING_PO: "SHIPMENT",
    IssueType.DELIVERY_BEFORE_DISPATCH: "DELIVERY_EVENT",
    IssueType.SCORE_FUTURE_LEAKAGE: "SUPPLIER_SCORE",
    IssueType.RISK_SIGNAL_MISSING_PROVENANCE: "RISK_SIGNAL",
    IssueType.SITE_ATTRIBUTION_CONFLICT: "SHIPMENT",
}


def _ratio(n: int, d: int) -> float | None:
    return None if d == 0 else n / d


@dataclass(frozen=True)
class ConfusionCounts:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float | None:
        """2PR / (P + R) = 2TP / (2TP + FP + FN); None when there is nothing to judge."""
        return _ratio(2 * self.tp, 2 * self.tp + self.fp + self.fn)

    def __add__(self, other: "ConfusionCounts") -> "ConfusionCounts":
        return ConfusionCounts(
            self.tp + other.tp, self.fp + other.fp, self.fn + other.fn, self.tn + other.tn
        )


@dataclass(frozen=True)
class AnomalyEvaluation:
    per_class: dict[IssueType, ConfusionCounts]
    micro: ConfusionCounts
    negative_population: dict[IssueType, int]
    decision_agreement: dict[IssueType, dict[str, int]] = field(default_factory=dict)
    unit: str = UNIT


def _population(records: list[RawIngestionRecord], issue: IssueType) -> set[str]:
    entity = ENTITY_OF_CLASS[issue]
    return {
        r.raw_record_id
        for r in records
        if r.source_entity_type == entity
        and (
            issue != IssueType.DELIVERY_BEFORE_DISPATCH
            or (r.payload or {}).get("event_type") == "ARRIVED"
        )
    }


def _ground_truth(path: Path, raw_ids: set[str]) -> dict[IssueType, dict[str, str]]:
    """issue type -> {raw_record_id: expected_decision} for this batch's positives."""
    out: dict[IssueType, dict[str, str]] = {i: {} for i in IssueType}
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["entity_id"] in raw_ids and row["expected_detection"] == "true":
                out[IssueType(row["issue_type"])][row["entity_id"]] = row["expected_decision"]
    return out


def evaluate_anomalies(
    session: Session, batch_id: str, ground_truth_path: Path
) -> AnomalyEvaluation:
    """Per-class confusion counts of the batch's data exceptions against ground truth."""
    records = list(
        session.scalars(
            sa.select(RawIngestionRecord).where(RawIngestionRecord.batch_id == batch_id)
        )
    )
    if not records:
        raise ValueError(f"unknown or empty raw batch {batch_id!r}")
    truth = _ground_truth(ground_truth_path, {r.raw_record_id for r in records})
    predicted: dict[IssueType, dict[str, str | None]] = {i: {} for i in IssueType}
    for raw_id, issue, disposition in session.execute(
        sa.select(
            DataException.raw_record_id, DataException.issue_type, DataException.disposition
        ).where(DataException.batch_id == batch_id)
    ):
        predicted[IssueType(issue)][raw_id] = None if disposition is None else str(disposition)

    per_class: dict[IssueType, ConfusionCounts] = {}
    negatives: dict[IssueType, int] = {}
    agreement: dict[IssueType, dict[str, int]] = {}
    for issue in IssueType:
        positives, hits = set(truth[issue]), set(predicted[issue])
        negative = _population(records, issue) - positives
        tp = positives & hits
        per_class[issue] = ConfusionCounts(
            tp=len(tp),
            fp=len(hits - positives),
            fn=len(positives - hits),
            tn=len(negative - hits),
        )
        negatives[issue] = len(negative)
        agree = sum(truth[issue][r] == predicted[issue][r] for r in tp)
        agreement[issue] = {"agree": agree, "disagree": len(tp) - agree}
    micro = sum(per_class.values(), ConfusionCounts(0, 0, 0, 0))
    return AnomalyEvaluation(per_class, micro, negatives, agreement)

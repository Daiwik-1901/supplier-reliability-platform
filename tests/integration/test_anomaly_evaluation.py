"""Task 16: anomaly detection evaluated against the starter batch's ground truth (D9, R42)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from supplier_reliability.database import make_engine, make_session_factory
from supplier_reliability.domain.enums import IssueType
from supplier_reliability.services.evaluation import ConfusionCounts, evaluate_anomalies


@pytest.fixture
def session(detected_db) -> Iterator[Session]:
    engine = make_engine(f"sqlite:///{detected_db.path}")
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def test_evaluation_on_starter_batch(session, detected_db, ground_truth_dir: Path, gen_cfg):
    ev = evaluate_anomalies(
        session, detected_db.batch_id, ground_truth_dir / "anomaly_ground_truth.csv"
    )
    k = gen_cfg.anomaly_preset.positives_per_class
    negatives = gen_cfg.anomaly_preset.negatives
    assert set(ev.per_class) == set(IssueType)  # all five classes evaluated
    for issue, counts in ev.per_class.items():
        assert counts.tp == k == 5, issue
        assert counts.fp == 0, issue
        assert counts.fn == 0, issue
        assert (counts.precision, counts.recall, counts.f1) == (1.0, 1.0, 1.0), issue
        assert ev.decision_agreement[issue] == {"agree": k, "disagree": 0}, issue
    # R42 negative populations: A/E = every other SHIPMENT record (negatives + the other class),
    # B = ARRIVED copies only (one per negative pair), C/D = the negatives of their entity type
    shipments = negatives["shipments"] + k
    assert ev.negative_population == {
        IssueType.SHIPMENT_MISSING_PO: shipments,
        IssueType.SITE_ATTRIBUTION_CONFLICT: shipments,
        IssueType.DELIVERY_BEFORE_DISPATCH: negatives["delivery_event_pairs"],
        IssueType.SCORE_FUTURE_LEAKAGE: negatives["scores"],
        IssueType.RISK_SIGNAL_MISSING_PROVENANCE: negatives["risk_signals"],
    }
    for issue, counts in ev.per_class.items():
        assert counts.tn == ev.negative_population[issue], issue
    assert ev.micro == ConfusionCounts(
        tp=5 * k, fp=0, fn=0, tn=sum(ev.negative_population.values())
    )

"""Task 16: anomaly-evaluation metrics (confusion counts, R42 negative populations) and the
import rule that keeps the independent recomputation independent of the engine."""

import ast
import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import (
    DetectionStatus,
    Disposition,
    IssueType,
    Severity,
)
from supplier_reliability.models import DataException, RawIngestionRecord
from supplier_reliability.services.evaluation import (
    ConfusionCounts,
    evaluate_anomalies,
)

BATCH = "BATCH-UNIT"
AT = datetime(2026, 7, 1, tzinfo=UTC)
A, B, C, D, E = (
    IssueType.SHIPMENT_MISSING_PO,
    IssueType.DELIVERY_BEFORE_DISPATCH,
    IssueType.SCORE_FUTURE_LEAKAGE,
    IssueType.RISK_SIGNAL_MISSING_PROVENANCE,
    IssueType.SITE_ATTRIBUTION_CONFLICT,
)
PACKAGE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "supplier_reliability"
    / "services"
    / "evaluation"
    / "independent_metrics"
)


# --- confusion math ---------------------------------------------------------------------------


def test_confusion_metrics_math():
    c = ConfusionCounts(tp=8, fp=2, fn=2, tn=100)
    assert c.precision == pytest.approx(0.8)
    assert c.recall == pytest.approx(0.8)
    assert c.f1 == pytest.approx(0.8)
    # asymmetric case: precision 6/8, recall 6/9, f1 = 2pr/(p+r) = 12/17
    d = ConfusionCounts(tp=6, fp=2, fn=3, tn=0)
    assert (d.precision, d.recall) == (pytest.approx(0.75), pytest.approx(6 / 9))
    assert d.f1 == pytest.approx(12 / 17)


def test_zero_denominators_return_none():
    empty = ConfusionCounts(tp=0, fp=0, fn=0, tn=7)
    assert (empty.precision, empty.recall, empty.f1) == (None, None, None)
    no_predictions = ConfusionCounts(tp=0, fp=0, fn=3, tn=7)
    assert no_predictions.precision is None  # nothing predicted: precision undefined, not 0 or 1
    assert no_predictions.recall == 0.0
    no_positives = ConfusionCounts(tp=0, fp=2, fn=0, tn=7)
    assert no_positives.recall is None
    assert no_positives.precision == 0.0


def test_confusion_counts_add_up():
    total = ConfusionCounts(1, 2, 3, 4) + ConfusionCounts(10, 20, 30, 40)
    assert total == ConfusionCounts(11, 22, 33, 44)


# --- evaluate_anomalies on a hand-built batch ------------------------------------------------


def _raw(session: Session, raw_id: str, entity: str, payload: dict) -> None:
    session.add(
        RawIngestionRecord(
            raw_record_id=raw_id,
            batch_id=BATCH,
            source_entity_type=entity,
            source_record_id=f"X-{raw_id}",
            payload=payload,
            received_at=AT,
        )
    )


def _exception(session: Session, n: int, raw_id: str, entity: str, issue: IssueType,
               disposition: Disposition) -> None:  # fmt: skip
    session.add(
        DataException(
            exception_id=f"EXC-{n:06d}",
            batch_id=BATCH,
            raw_record_id=raw_id,
            source_entity_type=entity,
            source_record_id=f"X-{raw_id}",
            issue_type=issue,
            rule_id="DET-X",
            severity=Severity.HIGH,
            detection_status=DetectionStatus.DETECTED,
            detected_at=AT,
            disposition=disposition,
            related_entity_ids={},
            evidence={},
        )
    )


def _ground_truth(path: Path, cases: list[tuple[str, str, IssueType, Disposition]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(
            ["case_id", "entity_type", "entity_id", "issue_type", "expected_detection",
             "expected_decision", "severity", "rationale", "injection_version"]
        )  # fmt: skip
        for i, (entity, raw_id, issue, decision) in enumerate(cases, 1):
            w.writerow([f"CASE-{i:06d}", entity, raw_id, issue.value, "true", decision.value,
                        "HIGH", "unit", "1.0.0"])  # fmt: skip
    return path


@pytest.fixture
def unit_batch(migrated_engine, tmp_path) -> tuple[Session, Path]:
    """Shipments RAW-1 (A positive, detected), RAW-2 (clean), RAW-3 (E positive, detected as E
    with the wrong disposition); risk signals RAW-4/RAW-5 (clean; RAW-5 falsely flagged D);
    delivery events RAW-6 (ARRIVED, B positive, missed) and RAW-7 (DISPATCHED)."""
    session = Session(migrated_engine)
    _raw(session, "RAW-000001", "SHIPMENT", {"shipment_id": "a"})
    _raw(session, "RAW-000002", "SHIPMENT", {"shipment_id": "b"})
    _raw(session, "RAW-000003", "SHIPMENT", {"shipment_id": "c"})
    _raw(session, "RAW-000004", "RISK_SIGNAL", {"risk_signal_id": "d"})
    _raw(session, "RAW-000005", "RISK_SIGNAL", {"risk_signal_id": "e"})
    _raw(session, "RAW-000006", "DELIVERY_EVENT", {"event_type": "ARRIVED"})
    _raw(session, "RAW-000007", "DELIVERY_EVENT", {"event_type": "DISPATCHED"})
    session.flush()
    _exception(session, 1, "RAW-000001", "SHIPMENT", A, Disposition.QUARANTINED)
    _exception(session, 2, "RAW-000003", "SHIPMENT", E, Disposition.REJECTED)
    _exception(session, 3, "RAW-000005", "RISK_SIGNAL", D,
               Disposition.QUARANTINED_PENDING_PROVENANCE)  # fmt: skip
    session.commit()
    gt = _ground_truth(
        tmp_path / "gt.csv",
        [
            ("SHIPMENT", "RAW-000001", A, Disposition.QUARANTINED),
            ("SHIPMENT", "RAW-000003", E, Disposition.QUARANTINED),
            ("DELIVERY_EVENT", "RAW-000006", B, Disposition.QUARANTINED),
        ],
    )
    yield session, gt
    session.close()


def test_negatives_scoped_to_entity_type(unit_batch):
    session, gt = unit_batch
    ev = evaluate_anomalies(session, BATCH, gt)
    assert ev.unit == "raw_record_per_issue_class"
    # A: shipments only; the risk-signal and delivery-event rows are not A negatives
    assert ev.negative_population[A] == 2  # RAW-2, RAW-3 (E positive is still an A negative)
    assert ev.per_class[A] == ConfusionCounts(tp=1, fp=0, fn=0, tn=2)
    assert ev.per_class[E] == ConfusionCounts(tp=1, fp=0, fn=0, tn=2)
    # B: ARRIVED-type delivery events only (R42); the DISPATCHED copy is no negative
    assert ev.negative_population[B] == 0
    assert ev.per_class[B] == ConfusionCounts(tp=0, fp=0, fn=1, tn=0)
    # C: no SUPPLIER_SCORE records at all
    assert ev.negative_population[C] == 0
    assert ev.per_class[C] == ConfusionCounts(0, 0, 0, 0)
    assert ev.per_class[C].precision is None
    # D: both risk signals are negatives; RAW-5 is a false positive
    assert ev.negative_population[D] == 2
    assert ev.per_class[D] == ConfusionCounts(tp=0, fp=1, fn=0, tn=1)
    assert ev.micro == ConfusionCounts(tp=2, fp=1, fn=1, tn=5)


def test_decision_agreement_compares_disposition_with_expected_decision(unit_batch):
    session, gt = unit_batch
    ev = evaluate_anomalies(session, BATCH, gt)
    assert ev.decision_agreement[A] == {"agree": 1, "disagree": 0}
    assert ev.decision_agreement[E] == {"agree": 0, "disagree": 1}  # REJECTED != QUARANTINED
    assert ev.decision_agreement[B] == {"agree": 0, "disagree": 0}  # missed: nothing to compare


def test_unknown_batch_raises(unit_batch):
    session, gt = unit_batch
    with pytest.raises(ValueError, match="batch"):
        evaluate_anomalies(session, "BATCH-NOPE", gt)


def test_ground_truth_of_another_batch_is_ignored(unit_batch, tmp_path):
    session, _ = unit_batch
    gt = _ground_truth(tmp_path / "other.csv", [("SHIPMENT", "RAW-999999", A,
                                                 Disposition.QUARANTINED)])  # fmt: skip
    ev = evaluate_anomalies(session, BATCH, gt)
    assert ev.per_class[A] == ConfusionCounts(tp=0, fp=1, fn=0, tn=2)
    assert session.scalar(sa.select(sa.func.count()).select_from(DataException)) == 3


# --- independence of the pandas recomputation (Add-D) -----------------------------------------

FORBIDDEN = (
    "supplier_reliability.services.scoring",
    "supplier_reliability.services.validation",
    "supplier_reliability.services.anomaly_detection",
)
ALLOWED_PROJECT_MODULES = ("supplier_reliability.services.evaluation.independent_metrics",)


def _imports(path: Path, module: str, is_init: bool) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = module.split(".") if is_init else module.split(".")[:-1]
                base = ".".join(package[: len(package) - node.level + 1] + [node.module or ""])
            else:
                base = node.module or ""
            base = base.rstrip(".")
            out.append((node.lineno, base))
            out.extend((node.lineno, f"{base}.{a.name}") for a in node.names)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and any(f in node.value for f in FORBIDDEN)
        ):
            out.append((node.lineno, node.value))  # e.g. a dynamic importlib target
    return out


def _offending(name: str) -> bool:
    if any(f in name for f in FORBIDDEN):
        return True
    return name.startswith("supplier_reliability") and not name.startswith(ALLOWED_PROJECT_MODULES)


def test_independent_metrics_imports_nothing_from_the_engine():
    """The recomputation may use stdlib, pandas and numpy plus its own modules only: nothing from
    scoring, validation or detection (or any other project module), so it cannot share a bug
    with the code it checks."""
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, "independent_metrics package not found"
    offending = []
    for path in files:
        parts = path.relative_to(PACKAGE).with_suffix("").parts
        is_init = parts[-1] == "__init__"
        module = ".".join([ALLOWED_PROJECT_MODULES[0], *(parts[:-1] if is_init else parts)])
        offending.extend(
            f"{path.name}:{line}: {name}"
            for line, name in _imports(path, module, is_init)
            if _offending(name)
        )
    assert offending == []


def test_import_rule_catches_a_forbidden_import(tmp_path):
    """The checker above is not vacuous."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import pandas as pd\n"
        "from . import _tables\n"
        "from supplier_reliability.services.scoring.components import score_cost\n"
        "from ...validation import rules_domain\n"
        "from supplier_reliability.services import anomaly_detection\n"
        "from supplier_reliability.models import Shipment\n"
        "MOD = 'supplier_reliability.services.scoring.evidence'\n",
        encoding="utf-8",
    )
    module = f"{ALLOWED_PROJECT_MODULES[0]}.bad"
    flagged = sorted({line for line, n in _imports(bad, module, False) if _offending(n)})
    assert flagged == [3, 4, 5, 6, 7]  # pandas and the package's own module are allowed

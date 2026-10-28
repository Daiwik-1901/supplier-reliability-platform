"""Task 16: the independent pandas recomputation agrees with the hand-calculated benchmarks.

The Task 12b benchmark histories are built into a temp DB, exported to CSV and recomputed by
`independent_supplier_metrics` alone (the scoring engine never runs here). Expectations are the
literals of expected.yaml (component scores, overall, category) and the raw metrics written in
its hand arithmetic.
"""

import math
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from supplier_reliability.services.evaluation import (
    INDEPENDENT_TABLES,
    independent_supplier_metrics,
)
from supplier_reliability.services.export import export_tables

from ..factories import GraphBuilder
from .builders import build_supplier_history
from .scenarios import ONE_FACTOR, QUARTERS, SCENARIOS, STABLE_Q, SUPPLIER, WINDOWS
from .test_benchmarks import _add_outside_window_records

EXPECTED = yaml.safe_load((Path(__file__).parent / "expected.yaml").read_text(encoding="utf-8"))
COMPONENTS = ["delivery", "quality", "cost", "capacity", "compliance", "disruption"]

# Raw metrics of the deteriorating scenario, from the hand arithmetic in expected.yaml:
# (on_time_rate, mean_late_days, defect_rate, wavp, capacity_util, compliance known, penalty)
DETERIORATING_RAW = [
    ("0.95", "3", "0.005", "0", "0.7", 2, 0.0),
    ("0.8", "5", "0.02", "0.02", "0.9", 2, 0.0),
    ("0.65", "8", "0.04", "0.04", "1.1", 2, 30 * math.exp(-45 / 90)),  # 18.1959198
    ("0.5", "10", "0.06", "0.06", "1.3", 2, 30 * math.exp(-137 / 90)),  # 6.5467920
]


def dec(value) -> Decimal:
    return Decimal(str(value))


@pytest.fixture
def params(app_config) -> dict:
    return app_config.scoring.model_dump(mode="json")


def _independent(session: Session, tmp_path: Path, params: dict, n_windows: int) -> list[dict]:
    """Export the DB and recompute SUPPLIER for the first `n_windows` quarter windows."""
    out = tmp_path / "export"
    export_tables(session, out, INDEPENDENT_TABLES)
    rows = []
    for window in WINDOWS[:n_windows]:
        frame = independent_supplier_metrics(out, window, params)
        (row,) = frame[frame["supplier_id"] == SUPPLIER].to_dict("records")
        rows.append(row)
    return rows


@pytest.mark.parametrize("scenario", ["stable", "deteriorating", "recovering"])
def test_independent_metrics_agree_on_benchmarks(db_session, tmp_path, params, scenario):
    build_supplier_history(db_session, SUPPLIER, SCENARIOS[scenario])
    db_session.commit()
    rows = _independent(db_session, tmp_path, params, 4)
    expected = EXPECTED["scenarios"][scenario]
    assert [q["quarter"] for q in expected] == QUARTERS
    for row, want in zip(rows, expected, strict=True):
        label = f"{scenario} {want['quarter']}"
        assert {c: row[f"{c}_score"] for c in COMPONENTS} == {
            c: dec(want[c]) for c in COMPONENTS
        }, label
        assert row["overall_score"] == dec(want["overall"]), label
        assert row["reliability_category"] == want["category"], label


def test_independent_raw_metrics_agree_on_deteriorating_benchmark(db_session, tmp_path, params):
    build_supplier_history(db_session, SUPPLIER, SCENARIOS["deteriorating"])
    db_session.commit()
    rows = _independent(db_session, tmp_path, params, 4)
    for k, (row, raw) in enumerate(zip(rows, DETERIORATING_RAW, strict=True)):
        otr, late, defect, wavp, util, known, penalty = raw
        assert row["delivery_due"] == 20, k
        assert row["on_time_rate"] == dec(otr), k
        assert row["mean_late_days"] == dec(late), k
        assert row["quality_inspections"] == 20, k
        assert row["defect_rate"] == dec(defect), k
        assert row["cost_invoices"] == 20, k
        assert row["wavp"] == dec(wavp), k
        assert row["capacity_util"] == dec(util), k
        assert (row["compliance_required"], row["compliance_known"]) == (2, known), k
        assert float(row["disruption_penalty"]) == pytest.approx(penalty, abs=1e-6), k
    # Q4: mandatory ISO9001 EXPIRED
    assert [r["mandatory_compliance_failure"] for r in rows] == [False, False, False, True]


@pytest.mark.parametrize("factor", COMPONENTS)
def test_independent_one_factor_changes_only_its_component(db_session, tmp_path, params, factor):
    build_supplier_history(db_session, SUPPLIER, [ONE_FACTOR[factor]])
    db_session.commit()
    (row,) = _independent(db_session, tmp_path, params, 1)
    baseline = EXPECTED["scenarios"]["stable"][0]
    want = EXPECTED["one_factor"][factor]
    for c in COMPONENTS:
        expected = want["score"] if c == factor else baseline[c]
        assert row[f"{c}_score"] == dec(expected), c
    assert row["overall_score"] == dec(want["overall"])


def test_independent_metrics_ignore_evidence_outside_window(db_session, tmp_path, params):
    """The Task 12b adversarial records (before S, after E, recorded after C, a correction and a
    superseding certification recorded after C) must not move the independent result either."""
    graph = GraphBuilder(db_session)
    build_supplier_history(db_session, SUPPLIER, [STABLE_Q], graph=graph)
    _add_outside_window_records(db_session, graph)
    db_session.commit()
    (row,) = _independent(db_session, tmp_path, params, 1)
    want = EXPECTED["scenarios"]["stable"][0]
    assert {c: row[f"{c}_score"] for c in COMPONENTS} == {c: dec(want[c]) for c in COMPONENTS}
    assert row["overall_score"] == dec(want["overall"])
    assert row["delivery_due"] == 20  # the after-C shipment did not exist at C

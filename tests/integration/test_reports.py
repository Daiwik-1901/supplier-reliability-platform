"""Task 17: JSON + Markdown reports computed from persisted outputs of the starter chain
(scored, injected, detected, validated).

Expected values are recomputed in plain Python / SQL over the persisted rows, never read back from
a builder under test."""

import json
import re
import shutil
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.database import make_engine, make_session_factory
from supplier_reliability.domain.enums import ReliabilityCategory, Severity
from supplier_reliability.models import (
    Base,
    Inspection,
    Invoice,
    Shipment,
    ShipmentLineAllocation,
    SupplierScore,
    ValidationFinding,
)
from supplier_reliability.services.evaluation import INDEPENDENT_TABLES
from supplier_reliability.services.export import export_tables
from supplier_reliability.services.reporting import (
    METRIC_KEYS,
    anomaly_evaluation_report,
    capacity_risk,
    compliance_exceptions,
    data_quality,
    delivery_quality_trends,
    deteriorating_suppliers,
    disruption_exposure,
    entity_counts,
    invoice_variance_distribution,
    metric,
    metric_problems,
    missing_data_rates,
    reconciliation_report,
    score_distribution,
    summary,
    supplier_rankings,
    write_report,
)
from supplier_reliability.services.scoring.evidence import scorecard_windows
from supplier_reliability.services.validation import run_validation

NOW = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)
COMPONENTS = ["delivery", "quality", "cost", "capacity", "compliance", "disruption"]


@pytest.fixture(scope="module")
def session(detected_db, tmp_path_factory) -> Iterator[Session]:
    path = tmp_path_factory.mktemp("reports") / "reports.db"
    shutil.copyfile(detected_db.path, path)
    engine = make_engine(f"sqlite:///{path}")
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


@pytest.fixture(scope="module")
def run_id(session) -> str:
    result = run_validation(session, clock=lambda: NOW)
    assert result.run_id is not None
    return result.run_id


@pytest.fixture(scope="module")
def windows(gen_cfg, app_config):
    return scorecard_windows(
        gen_cfg.reference_start, gen_cfg.reference_end, app_config.scoring.window_months
    )


@pytest.fixture(scope="module")
def reports(session, run_id, detected_db, ground_truth_dir, windows, app_config, tmp_path_factory):
    export = tmp_path_factory.mktemp("reports-export")
    export_tables(session, export, INDEPENDENT_TABLES)
    threshold = app_config.scoring.deteriorating_threshold
    out = {
        "entity_counts": entity_counts(session),
        "supplier_rankings": supplier_rankings(session, limit=10),
        "score_distribution": score_distribution(session),
        "delivery_quality_trends": delivery_quality_trends(
            session, grace_hours=app_config.scoring.grace_hours
        ),
        "invoice_variance_distribution": invoice_variance_distribution(session),
        "capacity_risk": capacity_risk(session),
        "compliance_exceptions": compliance_exceptions(session),
        "disruption_exposure": disruption_exposure(session),
        "missing_data_rates": missing_data_rates(session),
        "deteriorating_suppliers": deteriorating_suppliers(session, threshold=threshold),
        "data_quality": data_quality(session, run_id),
        "anomaly_evaluation": anomaly_evaluation_report(
            session, detected_db.batch_id, ground_truth_dir / "anomaly_ground_truth.csv"
        ),
        "reconciliation": reconciliation_report(session, export, windows),
    }
    out["summary"] = summary(session, run_id, reports=out, threshold=threshold)
    return out


def _current(session) -> list[SupplierScore]:
    return list(
        session.scalars(
            sa.select(SupplierScore).where(
                SupplierScore.is_current.is_(True), SupplierScore.site_id.is_(None)
            )
        )
    )


def _latest(scores):
    cutoff = max(s.data_cutoff_timestamp for s in scores)
    return cutoff, [s for s in scores if s.data_cutoff_timestamp == cutoff]


def test_entity_counts_match_db(session, reports):
    expected = {
        t.name: session.execute(sa.text(f"SELECT COUNT(*) FROM {t.name}")).scalar_one()
        for t in Base.metadata.sorted_tables
    }
    got = reports["entity_counts"]["metrics"]["rows"]
    assert {k: m["value"] for k, m in got.items()} == expected
    assert expected["suppliers"] == 50 and expected["shipments"] == 450
    cur = session.execute(sa.text("SELECT COUNT(*) FROM supplier_scores WHERE is_current")).scalar()
    assert reports["entity_counts"]["metrics"]["current_supplier_scores"]["value"] == cur


def test_rankings_sorted_and_exclude_insufficient(session, reports):
    rep = reports["supplier_rankings"]
    _, latest = _latest(_current(session))
    ranked = sorted(
        (s for s in latest if s.reliability_category != ReliabilityCategory.INSUFFICIENT_EVIDENCE),
        key=lambda s: (-s.overall_score, s.supplier_id),
    )
    insufficient = {
        s.supplier_id
        for s in latest
        if s.reliability_category == ReliabilityCategory.INSUFFICIENT_EVIDENCE
    }
    rows = rep["tables"]["ranked"]
    assert [r["supplier_id"] for r in rows] == [s.supplier_id for s in ranked][:10]
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(r["category"] != "INSUFFICIENT_EVIDENCE" for r in rows)
    assert all(r["decision"] for r in rows)
    ins_rows = rep["tables"]["insufficient_evidence"]
    assert {r["supplier_id"] for r in ins_rows} == insufficient
    assert all(r["missing_reasons"] is not None for r in ins_rows)
    assert rep["metrics"]["ranked_suppliers"]["numerator"] == len(ranked)
    assert rep["metrics"]["insufficient_evidence_suppliers"]["value"] == len(insufficient)
    assert not set(r["supplier_id"] for r in rows) & insufficient


def test_rankings_for_an_explicit_earlier_window(session):
    scores = _current(session)
    cutoffs = sorted({s.data_cutoff_timestamp for s in scores})
    rep = supplier_rankings(session, window=cutoffs[0])
    assert rep["window"]["cutoff"].startswith(cutoffs[0].strftime("%Y-%m-%dT%H:%M:%S"))
    n = sum(s.data_cutoff_timestamp == cutoffs[0] for s in scores)
    assert rep["metrics"]["window_suppliers"]["value"] == n


def test_every_metric_has_numerator_and_denominator(reports):
    assert set(reports) >= {"summary", "reconciliation", "anomaly_evaluation", "data_quality"}
    for name, payload in reports.items():
        assert metric_problems(payload) == [], name
        leaves = list(_walk(payload["metrics"]))
        assert leaves, name
        for leaf in leaves:
            assert set(leaf) == set(METRIC_KEYS)
            assert leaf["filters"] and leaf["window"] and leaf["missing_data_handling"]
            if leaf["numerator"] is None or leaf["denominator"] is None:
                assert "no denominator" in leaf["missing_data_handling"]


def _walk(node):
    if isinstance(node, dict) and "value" in node and "missing_data_handling" in node:
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)


def test_metric_problems_flags_malformed_metrics():
    good = metric(3, None, None, "f", "w", "count; no denominator")
    assert metric_problems({"metrics": {"a": {"b": good}}}) == []
    bad_missing_key = {k: v for k, v in good.items() if k != "window"}
    assert metric_problems({"metrics": {"a": bad_missing_key}})
    assert metric_problems({"metrics": {"a": 5}})
    assert metric_problems({"metrics": {"a": [good]}})
    assert metric_problems({"metrics": {"a": metric(3, None, None, "f", "w", "x")}})
    assert metric_problems({"metrics": {"a": metric(3, 1, 2, "", "w", "x")}})


def test_variance_buckets_sum_to_invoice_count_minus_excluded(session, reports):
    rep = reports["invoice_variance_distribution"]
    invoices = list(session.scalars(sa.select(Invoice)))
    excluded = [i for i in invoices if i.expected_amount == 0]
    buckets = rep["metrics"]["buckets"]
    assert set(buckets) == {"lt_minus_5pct", "minus_5_to_minus_1pct", "minus_1_to_1pct",
                            "1_to_5pct", "gt_5pct"}  # fmt: skip
    assert sum(m["value"] for m in buckets.values()) == len(invoices) - len(excluded)
    assert rep["metrics"]["excluded_zero_expected"]["value"] == len(excluded)
    assert rep["metrics"]["invoices"]["value"] == len(invoices)

    def bucket(v: Decimal) -> str:
        if v < Decimal("-0.05"):
            return "lt_minus_5pct"
        if v < Decimal("-0.01"):
            return "minus_5_to_minus_1pct"
        if v < Decimal("0.01"):
            return "minus_1_to_1pct"
        if v <= Decimal("0.05"):
            return "1_to_5pct"
        return "gt_5pct"

    expected = Counter(bucket(i.variance_amount / i.expected_amount) for i in invoices
                       if i.expected_amount != 0)  # fmt: skip
    got = {k: m["value"] for k, m in buckets.items() if m["value"]}
    # buckets use the stored 6-dp variance_percentage; allow only boundary-rounding drift
    assert sum(abs(got.get(k, 0) - expected.get(k, 0)) for k in set(got) | set(expected)) <= 2
    total_exp = sum((i.expected_amount for i in invoices), Decimal("0"))
    assert Decimal(str(rep["metrics"]["total_expected_amount"]["value"])) == total_exp


def _quarter_bounds(label: str):
    year, q = int(label[:4]), int(label[-1])
    start = datetime(year, 3 * q - 2, 1, tzinfo=UTC)
    end = datetime(year + (q == 4), 1 if q == 4 else 3 * q + 1, 1, tzinfo=UTC)
    return start, end


def test_trends_match_hand_computation(session, reports, app_config):
    by_q = reports["delivery_quality_trends"]["metrics"]["by_quarter"]
    assert sorted(by_q) == ["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2"]
    grace = timedelta(hours=app_config.scoring.grace_hours)
    shipments = list(session.scalars(sa.select(Shipment)))
    inspections = list(session.scalars(sa.select(Inspection)))
    for label, cell in by_q.items():
        start, end = _quarter_bounds(label)
        due = [s for s in shipments if start <= s.expected_delivery_timestamp < end]
        known = [s for s in due if s.actual_delivery_timestamp is not None]
        on_time = [
            s for s in known if s.actual_delivery_timestamp <= s.expected_delivery_timestamp + grace
        ]
        otr = cell["on_time_rate"]
        assert (otr["numerator"], otr["denominator"]) == (len(on_time), len(known))
        assert otr["value"] == pytest.approx(len(on_time) / len(known))
        latest: dict[str, Inspection] = {}
        for i in sorted(inspections, key=lambda i: (i.inspection_timestamp, i.inspection_id)):
            if start <= i.inspection_timestamp < end:
                latest[i.shipment_line_id] = i
        defects = sum((i.defect_quantity for i in latest.values()), Decimal("0"))
        inspected = sum((i.inspected_quantity for i in latest.values()), Decimal("0"))
        dr = cell["defect_rate"]
        assert Decimal(str(dr["numerator"])) == defects
        assert Decimal(str(dr["denominator"])) == inspected
        assert dr["value"] == pytest.approx(float(defects / inspected), abs=1e-6)
    assert sum(1 for _ in session.scalars(sa.select(ShipmentLineAllocation))) > 0


def test_score_distribution_and_missing_rates_match_rows(session, reports):
    scores = _current(session)
    dist = reports["score_distribution"]["metrics"]["by_reliability_category"]
    assert len(dist) == len({s.data_cutoff_timestamp for s in scores})
    for _, cats in dist.items():
        assert sum(m["value"] for m in cats.values()) == cats["HIGH"]["denominator"]
    rates = reports["missing_data_rates"]["metrics"]["overall"]
    for comp in COMPONENTS:
        missing = sum(getattr(s, f"{comp}_score") is None for s in scores)
        assert rates[comp]["numerator"] == missing
        assert rates[comp]["denominator"] == len(scores)
        assert rates[comp]["value"] == pytest.approx(missing / len(scores), abs=1e-6)


def test_deteriorating_matches_definition_d7(session, reports, app_config):
    scores = _current(session)
    cutoffs = sorted({s.data_cutoff_timestamp for s in scores})
    latest = {s.supplier_id: s for s in scores if s.data_cutoff_timestamp == cutoffs[-1]}
    prev = {s.supplier_id: s for s in scores if s.data_cutoff_timestamp == cutoffs[-2]}
    threshold = Decimal(str(app_config.scoring.deteriorating_threshold))
    ok = ReliabilityCategory.INSUFFICIENT_EVIDENCE
    expected = []
    comparable = 0
    for sid, now in latest.items():
        before = prev.get(sid)
        if before is None or ok in (now.reliability_category, before.reliability_category):
            continue
        if now.overall_score is None or before.overall_score is None:
            continue
        comparable += 1
        if before.overall_score - now.overall_score >= threshold:
            expected.append(sid)
    rep = reports["deteriorating_suppliers"]
    assert sorted(r["supplier_id"] for r in rep["tables"]["deteriorating"]) == sorted(expected)
    assert rep["metrics"]["deteriorating"]["numerator"] == len(expected)
    assert rep["metrics"]["deteriorating"]["denominator"] == comparable
    assert {"delta_min", "delta_p5", "delta_median"} <= set(
        rep["metrics"]["quarter_over_quarter_delta"]
    )


def test_deteriorating_boundary_is_inclusive(session):
    """A drop of exactly the threshold counts; a hair less does not."""
    scores = _current(session)
    cutoffs = sorted({s.data_cutoff_timestamp for s in scores})
    pairs = [
        (a, b)
        for a in scores
        for b in scores
        if a.supplier_id == b.supplier_id
        and a.data_cutoff_timestamp == cutoffs[-2]
        and b.data_cutoff_timestamp == cutoffs[-1]
        and a.overall_score is not None
        and b.overall_score is not None
        and a.reliability_category != ReliabilityCategory.INSUFFICIENT_EVIDENCE
        and b.reliability_category != ReliabilityCategory.INSUFFICIENT_EVIDENCE
    ]
    deltas = [a.overall_score - b.overall_score for a, b in pairs]
    exact = next(d for d in deltas if d > 0)
    at = deteriorating_suppliers(session, threshold=exact)
    above = deteriorating_suppliers(session, threshold=exact + Decimal("0.01"))
    n_at = sum(d >= exact for d in deltas)
    assert at["metrics"]["deteriorating"]["numerator"] == n_at
    assert above["metrics"]["deteriorating"]["numerator"] == sum(
        d >= exact + Decimal("0.01") for d in deltas
    )


def test_capacity_and_compliance_and_disruption_from_persisted_scores(session, reports):
    _, latest = _latest(_current(session))
    cap = reports["capacity_risk"]
    over = []
    for s in latest:
        util = s.explanation["components"]["capacity"]["metrics"].get("util")
        if util is not None and (util == "inf" or Decimal(util) > 1):
            over.append(s.supplier_id)
    assert sorted(r["supplier_id"] for r in cap["tables"]["over_capacity"]) == sorted(over)
    assert cap["metrics"]["suppliers_over_capacity"]["numerator"] == len(over)

    failing = [s.supplier_id for s in latest
               if "mandatory_compliance_failure" in s.explanation["applied_overrides"]]  # fmt: skip
    comp = reports["compliance_exceptions"]
    assert sorted(r["supplier_id"] for r in comp["tables"]["mandatory_failures"]) == sorted(failing)
    assert comp["metrics"]["mandatory_failures"]["numerator"] == len(failing)

    disr = reports["disruption_exposure"]
    sev = Counter(r["severity"] for s in latest for r in s.explanation["risk_signals"])
    for name, m in disr["metrics"]["signals_by_severity"].items():
        assert m["value"] == sev.get(name, 0)


def test_data_quality_separates_informational_from_defects(session, run_id, reports):
    rep = reports["data_quality"]
    findings = list(session.scalars(sa.select(ValidationFinding).where(
        ValidationFinding.run_id == run_id)))  # fmt: skip
    low = [f for f in findings if f.severity in (Severity.LOW, Severity.NONE)]
    defects = [f for f in findings if f not in low]
    m = rep["metrics"]
    assert m["informational"]["total"]["value"] == len(low)
    assert {k: v["value"] for k, v in m["informational"]["by_rule"].items()} == dict(
        Counter(f.rule_id for f in low)
    )
    assert m["defects"]["total"]["value"] == len(defects)
    assert {k: v["value"] for k, v in m["defects"]["by_rule"].items()} == dict(
        Counter(f.rule_id for f in defects)
    )
    assert {k: v["value"] for k, v in m["defects"]["by_severity"].items()} == dict(
        Counter(f.severity.value for f in defects)
    )
    assert not set(m["defects"]["by_rule"]) & set(m["informational"]["by_rule"])
    assert m["expected_anomalies"]["value"] == sum(f.is_expected_anomaly for f in findings)
    with pytest.raises(LookupError):
        data_quality(session, "RUN-NOPE")


def test_anomaly_and_reconciliation_reports(reports):
    an = reports["anomaly_evaluation"]["metrics"]
    micro = an["micro"]
    tp, fp, fn = (micro[k]["value"] for k in ("tp", "fp", "fn"))
    assert micro["recall"]["numerator"] == tp and micro["recall"]["denominator"] == tp + fn
    assert micro["precision"]["denominator"] == tp + fp
    assert set(an["by_class"]) == {
        "SHIPMENT_MISSING_PO", "DELIVERY_BEFORE_DISPATCH", "SCORE_FUTURE_LEAKAGE",
        "RISK_SIGNAL_MISSING_PROVENANCE", "SITE_ATTRIBUTION_CONFLICT",
    }  # fmt: skip
    rec = reports["reconciliation"]["metrics"]
    assert rec["match_rate"]["numerator"] == rec["matched"]["value"]
    assert rec["match_rate"]["denominator"] == rec["checked"]["value"]
    assert rec["checked"]["value"] > 0
    assert "missing_persisted" in rec


def test_summary_reuses_builder_values(reports):
    s = reports["summary"]["metrics"]
    assert (
        s["suppliers_scored_latest_window"]["value"]
        == (reports["supplier_rankings"]["metrics"]["window_suppliers"]["value"])
    )
    assert (
        s["deteriorating_suppliers"]
        == reports["deteriorating_suppliers"]["metrics"]["deteriorating"]
    )
    assert (
        s["suppliers"]["value"] == reports["entity_counts"]["metrics"]["rows"]["suppliers"]["value"]
    )
    assert s["defects"] == reports["data_quality"]["metrics"]["defects"]["total"]
    assert s["anomaly_micro_recall"] == reports["anomaly_evaluation"]["metrics"]["micro"]["recall"]


def test_writer_produces_json_and_markdown(reports, tmp_path):
    payload = reports["supplier_rankings"]
    json_path, md_path = write_report("supplier_rankings", payload, tmp_path / "out")
    assert json_path.name == "supplier_rankings.json" and md_path.name == "supplier_rankings.md"
    raw = json_path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"}\n")
    text = raw.decode("utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False,
                              default=str) + "\n"  # fmt: skip
    assert "generated_at" not in json.loads(text)
    md = md_path.read_bytes().decode("utf-8")
    assert b"\r" not in md_path.read_bytes() and not re.search(r"<[A-Za-z/!]", md)
    assert md.startswith("# ") and "| rank | supplier_id |" in md
    assert "| metric | value | numerator | denominator |" in md
    # deterministic: same payload, same bytes; generated_at only when asked
    write_report("again", payload, tmp_path / "out")
    assert (tmp_path / "out" / "again.json").read_bytes() == raw
    stamp = datetime(2026, 7, 3, 12, tzinfo=UTC)
    jp, _ = write_report("stamped", payload, tmp_path / "out", generated_at=stamp)
    assert json.loads(jp.read_text(encoding="utf-8"))["generated_at"] == "2026-07-03T12:00:00Z"
    assert "generated_at" not in payload
    with pytest.raises(ValueError):
        write_report("../evil", payload, tmp_path)


def test_every_report_renders_markdown(reports, tmp_path):
    for name, payload in reports.items():
        _, md = write_report(name, payload, tmp_path)
        assert md.read_text(encoding="utf-8").startswith("# ")

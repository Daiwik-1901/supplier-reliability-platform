"""Overall scoring (Task 11, D7/D8): aggregation, categories, decisions, explanation, fingerprint.

Expected values are hand-calculated literals (weights 0.30/0.25/0.15/0.10/0.10/0.10).
"""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from supplier_reliability.domain.enums import (
    ComplianceState,
    ComponentName,
    Decision,
    ReliabilityCategory,
    RiskSignalType,
    Severity,
)
from supplier_reliability.services.scoring.aggregate import aggregate, categorize
from supplier_reliability.services.scoring.components import ComponentResult
from supplier_reliability.services.scoring.engine import ScoreResult, score_supplier
from supplier_reliability.services.scoring.evidence import DeliveryObs, ExcludedRecord

from .builders import (
    JAN,
    bundle,
    cap,
    comp,
    deliveries,
    demand,
    inspections,
    invoices,
    risk,
)

D = Decimal
C = ComponentName
ORDER = [C.delivery, C.quality, C.cost, C.capacity, C.compliance, C.disruption]
FULL = ("80", "90", "70", "100", "100", "60")


@pytest.fixture
def cfg(app_config):
    return app_config.scoring


def result(name: C, score: str | None, **flags: bool) -> ComponentResult:
    return ComponentResult(
        name=name,
        score=None if score is None else D(score),
        numerator=None,
        denominator=None,
        observations=0 if score is None else 5,
        coverage=D("0") if score is None else D("1"),
        metrics={},
        missing_reason="no data" if score is None else None,
        source_record_ids=[],
        flags=flags,
    )


def comps(*scores: str | None, **flags: bool) -> list[ComponentResult]:
    """Six components in canonical order; flags go to compliance."""
    return [
        result(n, s, **(flags if n == C.compliance else {}))
        for n, s in zip(ORDER, scores, strict=True)
    ]


def test_full_weighted_sum(cfg):
    r = aggregate(comps(*FULL), cfg, [])
    assert r.overall == D("83.00")  # 24 + 22.5 + 10.5 + 10 + 10 + 6
    assert r.category == ReliabilityCategory.ACCEPTABLE
    assert r.decision == Decision.APPROVED
    assert r.evidence_coverage == D("1") and r.is_provisional is False
    assert r.effective_weights["delivery"] == D("0.3000")
    assert r.decision_rationale and r.applied_overrides == []


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("85.00", ReliabilityCategory.HIGH),
        ("84.99", ReliabilityCategory.ACCEPTABLE),
        ("70.00", ReliabilityCategory.ACCEPTABLE),
        ("69.99", ReliabilityCategory.AT_RISK),
        ("50.00", ReliabilityCategory.AT_RISK),
        ("49.99", ReliabilityCategory.CRITICAL),
        ("100", ReliabilityCategory.HIGH),
        ("0", ReliabilityCategory.CRITICAL),
    ],
)
def test_category_boundaries(cfg, score, expected):
    assert categorize(D(score), cfg.category_thresholds) == expected


def test_category_uses_configured_thresholds(cfg):
    moved = {k: v.model_copy() for k, v in cfg.category_thresholds.items()}
    moved["HIGH"].lower, moved["ACCEPTABLE"].upper = 90, 90
    assert categorize(D("87"), moved) == ReliabilityCategory.ACCEPTABLE


def test_renormalized_when_capacity_missing(cfg):
    r = aggregate(comps("80", "90", "70", None, "100", "60"), cfg, [])
    assert r.evidence_coverage == D("0.90") and r.is_provisional is True
    assert r.effective_weights["delivery"] == D("0.3333")
    assert "capacity" not in r.effective_weights
    assert r.overall == D("81.11")  # (24 + 22.5 + 10.5 + 10 + 6) / 0.9
    assert r.category == ReliabilityCategory.ACCEPTABLE and r.decision == Decision.APPROVED


def test_insufficient_when_delivery_missing(cfg):
    r = aggregate(comps(None, "90", "70", "100", "100", "60"), cfg, [])
    assert r.evidence_coverage == D("0.70")  # at the threshold, but delivery is mandatory
    assert r.is_provisional is True
    assert r.overall == D("84.29")  # 59 / 0.7 still computed
    assert r.category == ReliabilityCategory.INSUFFICIENT_EVIDENCE
    assert r.decision == Decision.COLLECT_MORE_DATA


def test_insufficient_when_coverage_below_threshold(cfg):
    r = aggregate(comps("80", "90", None, None, None, None), cfg, [])
    assert r.evidence_coverage == D("0.55") and r.is_provisional is False
    assert r.overall == D("84.55")  # 46.5 / 0.55
    assert r.category == ReliabilityCategory.INSUFFICIENT_EVIDENCE
    assert r.decision == Decision.COLLECT_MORE_DATA


def test_no_components_available(cfg):
    r = aggregate(comps(None, None, None, None, None, None), cfg, [])
    assert r.overall is None and r.effective_weights == {}
    assert r.evidence_coverage == D("0")
    assert r.category == ReliabilityCategory.INSUFFICIENT_EVIDENCE
    assert r.decision == Decision.COLLECT_MORE_DATA


def test_compliance_failure_overrides_high_score(cfg):
    r = aggregate(comps(*("100",) * 6, mandatory_compliance_failure=True), cfg, [])
    assert r.overall == D("100.00") and r.category == ReliabilityCategory.HIGH
    assert r.decision == Decision.SUSPEND_PENDING_REVIEW
    assert r.applied_overrides == ["mandatory_compliance_failure"]
    assert any("mandatory" in line for line in r.decision_rationale)


def test_compliance_failure_flag_counts_even_when_compliance_score_missing(cfg):
    r = aggregate(
        comps("80", "90", "70", "100", None, "60", mandatory_compliance_failure=True), cfg, []
    )
    assert r.decision == Decision.SUSPEND_PENDING_REVIEW


def test_open_critical_exception_holds(cfg):
    r = aggregate(comps(*FULL), cfg, ["EXC-000007", "EXC-000002"])
    assert r.decision == Decision.HOLD_FOR_DATA_REVIEW
    assert r.category == ReliabilityCategory.ACCEPTABLE  # category itself is unchanged
    assert r.applied_overrides == ["open_critical_exception"]
    assert any("EXC-000002" in line and "EXC-000007" in line for line in r.decision_rationale)


def test_precedence_compliance_beats_exception_beats_insufficient(cfg):
    both = aggregate(comps(*FULL, mandatory_compliance_failure=True), cfg, ["EXC-000001"])
    assert both.decision == Decision.SUSPEND_PENDING_REVIEW
    assert both.applied_overrides == ["mandatory_compliance_failure"]
    held = aggregate(comps(None, None, None, None, None, "50"), cfg, ["EXC-000001"])
    assert held.category == ReliabilityCategory.INSUFFICIENT_EVIDENCE
    assert held.decision == Decision.HOLD_FOR_DATA_REVIEW


@pytest.mark.parametrize(
    ("score", "category", "decision"),
    [
        ("100", ReliabilityCategory.HIGH, Decision.PREFERRED),
        ("60", ReliabilityCategory.AT_RISK, Decision.MONITOR),
        ("30", ReliabilityCategory.CRITICAL, Decision.ESCALATE),
    ],
)
def test_category_decision_map(cfg, score, category, decision):
    r = aggregate(comps(*(score,) * 6), cfg, [])
    assert (r.category, r.decision) == (category, decision)


def test_overall_is_weighted_mean_of_given_rounded_scores(cfg):
    r = aggregate(comps("33.33", "33.33", "33.33", "33.33", "33.33", "33.33"), cfg, [])
    assert r.overall == D("33.33")


# --- score_supplier --------------------------------------------------------------------------


def full_bundle(**override):
    kw = {
        "deliveries": deliveries(on_time=10),
        "inspections": inspections((100, 0), (100, 0), (100, 0)),
        "invoices": invoices(("100", "100"), ("100", "100"), ("100", "100")),
        "demand": [demand(JAN, 50)],
        "capacities": [cap(JAN, 100)],
        "compliance": [comp(ComplianceState.COMPLIANT)],
        "risks": [risk(severity=Severity.NONE, signal_type=RiskSignalType.MONITORING_ATTESTATION)],
    }
    return bundle(**(kw | override))


def fingerprint(b, cfg, config_hash="h"):
    return score_supplier(b, cfg, config_hash).evidence_fingerprint


def test_score_supplier_full_evidence(cfg):
    s = score_supplier(full_bundle(), cfg, "hash-1")
    assert isinstance(s, ScoreResult) and s.supplier_id == "SUP-000001"
    assert s.window == full_bundle().window
    assert set(s.components) == {n.value for n in ORDER}
    assert all(v.score == D("100.00") for v in s.components.values())
    assert s.overall.overall == D("100.00") and s.overall.decision == Decision.PREFERRED
    assert len(s.evidence_fingerprint) == 64


EXPECTED_KEYS = {
    "overall_score", "components", "evidence_coverage", "missing_data_reasons", "risk_signals",
    "detected_exceptions", "scoring_version", "evaluation_period", "data_cutoff", "decision",
    "decision_rationale", "is_provisional", "formula", "applied_overrides",
    "missing_data_decisions", "exceptions", "excluded_records",
    "excluded_records_truncated_count",
}  # fmt: skip
COMPONENT_KEYS = {
    "score", "weight", "effective_weight", "numerator", "denominator", "observations",
    "coverage", "metrics", "missing_reason", "source_record_ids", "truncated_count",
}  # fmt: skip


def test_explanation_full_evidence_has_every_key_and_is_json_serialisable(cfg):
    e = score_supplier(full_bundle(), cfg, "hash-1").explanation
    assert set(e) >= EXPECTED_KEYS
    json.dumps(e)  # must not raise
    assert e["overall_score"] == "100.00" and e["decision"] == "PREFERRED"
    assert e["scoring_version"] == cfg.scoring_version
    assert e["evaluation_period"] == {
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-03-31T23:59:59Z",
    }
    assert e["data_cutoff"] == "2026-03-31T23:59:59Z"
    assert e["evidence_coverage"] == "1.0000" and e["is_provisional"] is False
    assert e["missing_data_reasons"] == {} and e["applied_overrides"] == []
    assert e["exceptions"] == [] and e["detected_exceptions"] == []
    assert set(e["components"]) == {n.value for n in ORDER}
    for body in e["components"].values():
        assert set(body) >= COMPONENT_KEYS
    d = e["components"]["delivery"]
    assert d["score"] == "100.00" and d["weight"] == "0.3" and d["effective_weight"] == "0.3000"
    assert d["numerator"] == "10" and d["denominator"] == "10" and d["observations"] == 10
    assert d["truncated_count"] == 0 and len(d["source_record_ids"]) == 10
    assert e["risk_signals"][0]["risk_signal_id"] == "RSK-1"
    assert "Σ" in e["formula"]


def test_missing_component_never_zero_or_hundred(cfg):
    s = score_supplier(bundle(deliveries=deliveries(on_time=10)), cfg, "h")
    e = s.explanation
    for name in ("quality", "cost", "capacity", "compliance", "disruption"):
        body = e["components"][name]
        assert body["score"] is None and body["missing_reason"]
        assert body["effective_weight"] is None
        assert e["missing_data_reasons"][name] == body["missing_reason"]
    assert e["components"]["delivery"]["score"] == "100.00"
    assert e["missing_data_reasons"]["quality"] == "insufficient inspections: 0 < 3"
    note = e["missing_data_decisions"]["renormalisation"]
    assert "delivery" in note and "0.30" in note
    assert e["missing_data_decisions"]["missing_components"] == e["missing_data_reasons"]
    assert e["decision"] == "COLLECT_MORE_DATA"
    assert s.overall.category == ReliabilityCategory.INSUFFICIENT_EVIDENCE
    assert s.overall.overall == D("100.00")  # only delivery, renormalised


def test_empty_bundle_scores_insufficient_without_crashing(cfg):
    s = score_supplier(bundle(), cfg, "h")
    assert s.overall.overall is None and s.explanation["overall_score"] is None
    assert s.explanation["decision"] == "COLLECT_MORE_DATA"
    assert s.explanation["missing_data_decisions"]["renormalisation"] is None
    json.dumps(s.explanation)


def test_explanation_overrides_and_exceptions(cfg):
    b = full_bundle(
        compliance=[comp(ComplianceState.EXPIRED, mandatory=True)],
        open_critical_exception_ids=["EXC-000003"],
    )
    e = score_supplier(b, cfg, "h").explanation
    assert e["decision"] == "SUSPEND_PENDING_REVIEW"
    assert e["applied_overrides"] == ["mandatory_compliance_failure"]
    assert e["exceptions"] == ["EXC-000003"] == e["detected_exceptions"]
    held = score_supplier(full_bundle(open_critical_exception_ids=["EXC-000003"]), cfg, "h")
    assert held.explanation["applied_overrides"] == ["open_critical_exception"]
    assert held.explanation["decision"] == "HOLD_FOR_DATA_REVIEW"


def test_source_record_ids_capped_at_200_with_truncated_count(cfg):
    b = bundle(deliveries=deliveries(on_time=250))
    body = score_supplier(b, cfg, "h").explanation["components"]["delivery"]
    assert len(body["source_record_ids"]) == 200 and body["truncated_count"] == 50
    assert body["observations"] == 250
    assert body["source_record_ids"] == sorted(body["source_record_ids"])


def test_excluded_records_listed_and_capped(cfg):
    excluded = [
        ExcludedRecord("shipment", "SHP-900", C.delivery, "not_yet_due"),
        ExcludedRecord(
            "capacity_declaration", "CAP-9", C.capacity, "capacity_scope_overridden", JAN
        ),
    ]
    excluded += [ExcludedRecord("invoice", f"INV-{i:04d}", C.cost, "x") for i in range(205)]
    b = full_bundle(
        excluded=excluded,
        invoices=invoices(("0", "5"), ("100", "100"), ("100", "100"), ("100", "100")),
    )
    e = score_supplier(b, cfg, "h").explanation
    ex = e["excluded_records"]
    assert ex["delivery"] == [
        {"source_entity_type": "shipment", "source_record_id": "SHP-900", "reason": "not_yet_due"}
    ]
    assert ex["capacity"][0] == {
        "source_entity_type": "capacity_declaration",
        "source_record_id": "CAP-9",
        "reason": "capacity_scope_overridden",
        "month": "2026-01-01",
    }
    assert len(ex["cost"]) == 200
    assert e["excluded_records_truncated_count"]["cost"] == 6  # 205 + zero_expected - 200
    assert e["excluded_records_truncated_count"]["delivery"] == 0
    assert {
        "source_entity_type": "invoice",
        "source_record_id": "INV-001",
        "reason": "zero_expected",
    } in ex["cost"]
    json.dumps(e)


# --- fingerprint -----------------------------------------------------------------------------


def test_fingerprint_is_sha256_of_canonical_json_of_included_observations(cfg):
    b = bundle(deliveries=deliveries(on_time=1))  # delivery component missing; item still counts
    payload = {
        "scoring_version": cfg.scoring_version,
        "config_hash": "cfg-hash",
        "observations": [
            {
                "component": "delivery",
                "record_id": "SHP-001",
                "expected": "2026-01-02T00:00:00Z",
                "arrived": "2026-01-02T00:00:00Z",
            }
        ],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert fingerprint(b, cfg, "cfg-hash") == hashlib.sha256(text.encode()).hexdigest()


def test_score_supplier_deterministic_fingerprint(cfg):
    a = score_supplier(full_bundle(), cfg, "h1")
    b = score_supplier(full_bundle(), cfg, "h1")
    assert a.evidence_fingerprint == b.evidence_fingerprint
    assert a.explanation == b.explanation
    assert fingerprint(full_bundle(), cfg, "h2") != a.evidence_fingerprint
    bumped = cfg.model_copy(update={"scoring_version": "1.0.1"})
    assert fingerprint(full_bundle(), bumped, "h1") != a.evidence_fingerprint


def test_fingerprint_changes_with_included_values_and_ids(cfg):
    base = fingerprint(full_bundle(), cfg)
    shifted = deliveries(on_time=10)
    first = shifted[0]
    shifted[0] = DeliveryObs(first.shipment_id, first.expected, first.expected + timedelta(hours=1))
    assert fingerprint(full_bundle(deliveries=shifted), cfg) != base
    one_defect = inspections((100, 0), (100, 0), (100, 1))
    assert fingerprint(full_bundle(inspections=one_defect), cfg) != base
    assert fingerprint(full_bundle(deliveries=deliveries(on_time=11)), cfg) != base


def test_fingerprint_ignores_input_order_and_excluded_records(cfg):
    base = fingerprint(full_bundle(), cfg)
    assert fingerprint(full_bundle(deliveries=deliveries(on_time=10)[::-1]), cfg) == base
    extra = [ExcludedRecord("shipment", "SHP-900", C.delivery, "not_yet_due")]
    assert fingerprint(full_bundle(excluded=extra), cfg) == base
    # a zero-expected invoice is excluded from the cost component, hence from the fingerprint
    zero = invoices(("100", "100"), ("100", "100"), ("100", "100"), ("0", "5"))
    assert fingerprint(full_bundle(invoices=zero), cfg) == base


def test_fingerprint_covers_all_ids_even_when_explanation_is_truncated(cfg):
    d = deliveries(on_time=250)
    last = d[-1]
    renamed = [*d[:-1], DeliveryObs("SHP-999", last.expected, last.arrived_known)]
    a = score_supplier(bundle(deliveries=d), cfg, "h")
    b = score_supplier(bundle(deliveries=renamed), cfg, "h")
    ids_a = a.explanation["components"]["delivery"]["source_record_ids"]
    ids_b = b.explanation["components"]["delivery"]["source_record_ids"]
    assert ids_a == ids_b  # the changed id is beyond the 200 shown ids
    assert a.evidence_fingerprint != b.evidence_fingerprint

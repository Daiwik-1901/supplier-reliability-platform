"""Component scoring (Task 10). Pure functions over hand-built EvidenceBundles (no DB).

Every expected value is a hand-calculated literal from the task brief / D6.
"""

import json
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from supplier_reliability.config import AppConfig, DeliveryConfig, ScoringConfig
from supplier_reliability.domain.enums import (
    ComplianceState,
    ComponentName,
    ResolutionStatus,
    RiskSignalType,
    Severity,
)
from supplier_reliability.services.scoring.components import (
    InvalidEvidenceError,
    score_capacity,
    score_compliance,
    score_cost,
    score_delivery,
    score_disruption,
    score_quality,
)
from supplier_reliability.services.scoring.evidence import (
    CapacityObs,
    DeliveryObs,
    InspectionObs,
    InvoiceObs,
)

from .builders import (  # noqa: F401
    CUTOFF,
    FEB,
    JAN,
    START,
    WINDOW,
    D,
    assert_missing,
    bundle,
    cap,
    comp,
    deliveries,
    demand,
    inspections,
    invoices,
    risk,
)


@pytest.fixture
def cfg(app_config) -> ScoringConfig:
    return app_config.scoring


# --- delivery ---------------------------------------------------------------------------------


def test_delivery_otr_and_delay_penalty(cfg):
    r = score_delivery(bundle(deliveries=deliveries(on_time=8, late_days=(3, 5))), cfg)
    assert r.name == ComponentName.delivery
    assert r.score == D("72.00")  # 100*0.8 - min(20, 2*4)
    assert r.observations == 10 and r.missing_reason is None
    assert D(r.metrics["on_time_rate"]) == D("0.8")
    assert D(r.metrics["avg_delay_days"]) == D("0.8")  # (3+5)/10 over all due
    assert D(r.metrics["mean_late_days"]) == D("4")
    assert D(r.metrics["late_frequency"]) == D("0.2")
    assert r.metrics["eligible_count"] == 10 and r.metrics["known_outcomes"] == 10
    assert r.coverage == D("1") and D(r.metrics["coverage"]) == D("1")
    assert r.numerator == D("8") and r.denominator == D("10")
    assert r.source_record_ids == sorted(r.source_record_ids) and len(r.source_record_ids) == 10


def test_delivery_penalty_off_reproduces_addendum_formula(cfg):
    off = cfg.model_copy(update={"delivery": DeliveryConfig(delay_penalty_per_day=0)})
    r = score_delivery(bundle(deliveries=deliveries(on_time=8, late_days=(3, 5))), off)
    assert r.score == D("80.00")  # Add-B(1): 100 * otr


def test_delivery_below_minimum_is_missing(cfg):
    r = score_delivery(bundle(deliveries=deliveries(on_time=4)), cfg)
    assert_missing(r, "insufficient deliveries: 4 < 5")
    assert r.observations == 4


def test_delivery_no_records_is_missing(cfg):
    assert_missing(score_delivery(bundle(), cfg), "insufficient deliveries: 0 < 5")


def test_delivery_grace_boundary(cfg):
    on_time = score_delivery(
        bundle(deliveries=deliveries(on_time=4, late_days=(timedelta(hours=24),))), cfg
    )
    assert on_time.score == D("100.00")
    assert D(on_time.metrics["on_time_rate"]) == D("1")
    late = score_delivery(
        bundle(deliveries=deliveries(on_time=4, late_days=(timedelta(hours=24, seconds=1),))), cfg
    )
    assert D(late.metrics["on_time_rate"]) == D("0.8")
    assert abs(D(late.metrics["mean_late_days"]) - D("1.0000115740")) < D("1e-9")
    assert late.score == D("78.00")  # 80 - 2*1.0000116 = 77.99998


def test_delivery_grace_comes_from_config(cfg):
    wide = cfg.model_copy(update={"grace_hours": 48})
    r = score_delivery(
        bundle(deliveries=deliveries(on_time=4, late_days=(timedelta(hours=40),))), wide
    )
    assert r.score == D("100.00")


def test_delivery_all_very_late_clamps_to_zero(cfg):
    r = score_delivery(bundle(deliveries=deliveries(late_days=(30,) * 10)), cfg)
    assert D(r.metrics["on_time_rate"]) == D("0")
    assert r.score == D("0.00")  # 0 - min(20, 60) clamped


def test_delivery_penalty_cap_applies(cfg):
    r = score_delivery(bundle(deliveries=deliveries(on_time=5, late_days=(30,) * 5)), cfg)
    assert r.score == D("30.00")  # 50 - cap 20


def test_delivery_overdue_counts_late_with_delay_to_cutoff(cfg):
    r = score_delivery(bundle(deliveries=deliveries(on_time=4, overdue=1)), cfg)
    assert r.score == D("76.00")  # 80 - 2 * 2 days (C - expected)
    assert r.metrics["known_outcomes"] == 4 and r.metrics["eligible_count"] == 5
    assert r.coverage == D("0.8000")
    assert D(r.metrics["mean_late_days"]) == D("2")


def test_delivery_invalid_arrival_far_before_expected_raises(cfg):
    obs = deliveries(on_time=5)
    bad = DeliveryObs("SHP-BAD", START + timedelta(days=400), START + timedelta(days=34))
    with pytest.raises(InvalidEvidenceError, match="SHP-BAD"):
        score_delivery(bundle(deliveries=[*obs, bad]), cfg)


def test_delivery_arrival_365_days_early_is_not_invalid(cfg):
    edge = DeliveryObs("SHP-EDGE", START + timedelta(days=400), START + timedelta(days=35))
    r = score_delivery(bundle(deliveries=[*deliveries(on_time=4), edge]), cfg)
    assert r.score == D("100.00")


# --- quality ----------------------------------------------------------------------------------


def test_quality_two_percent_defects(cfg):
    r = score_quality(bundle(inspections=inspections((400, 8), (300, 6), (300, 6))), cfg)
    assert r.name == ComponentName.quality
    assert r.score == D("80.00")  # 100 * (1 - 0.02/0.10)
    assert r.numerator == D("20") and r.denominator == D("1000")
    assert D(r.metrics["defect_rate"]) == D("0.02")
    assert r.observations == 3
    assert r.source_record_ids == ["INS-001", "INS-002", "INS-003"]


def test_quality_high_defects_clamp_to_zero(cfg):
    r = score_quality(bundle(inspections=inspections((400, 60), (300, 45), (300, 45))), cfg)
    assert r.score == D("0.00")  # rate 0.15


def test_quality_zero_defects_is_100(cfg):
    r = score_quality(bundle(inspections=inspections((10, 0), (20, 0), (30, 0))), cfg)
    assert r.score == D("100.00")


def test_quality_zero_inspected_units_is_missing_not_100(cfg):
    r = score_quality(bundle(inspections=inspections((0, 0), (0, 0), (0, 0))), cfg)
    assert_missing(r, "no inspected units")


def test_quality_below_minimum_is_missing(cfg):
    r = score_quality(bundle(inspections=inspections((100, 1), (100, 1))), cfg)
    assert_missing(r, "insufficient inspections: 2 < 3")


def test_quality_zero_score_boundary_is_config_driven(cfg):
    tight = cfg.model_copy(update={"defect_rate_zero_score": 0.05})
    r = score_quality(bundle(inspections=inspections((400, 8), (300, 6), (300, 6))), tight)
    assert r.score == D("60.00")


def test_quality_invalid_defect_above_inspected_raises(cfg):
    obs = [*inspections((10, 0), (10, 0)), InspectionObs("INS-BAD", "SHL-9", D(5), D(6))]
    with pytest.raises(InvalidEvidenceError, match="INS-BAD"):
        score_quality(bundle(inspections=obs), cfg)


# --- cost -------------------------------------------------------------------------------------


def test_cost_weighted_absolute_variance(cfg):
    r = score_cost(bundle(invoices=invoices(("100", "105"), ("100", "95"), ("100", "100"))), cfg)
    assert r.name == ComponentName.cost
    assert r.score == D("75.00")  # wavp = (5 + 0.5*5)/300 = 0.025
    assert r.numerator == D("7.5") and r.denominator == D("300")
    assert D(r.metrics["wavp"]) == D("0.025")
    assert r.metrics["excluded_zero_expected"] == 0 and r.excluded_record_ids == []


def test_cost_zero_expected_is_excluded_and_reported(cfg):
    inv = invoices(("100", "105"), ("100", "95"), ("100", "100"), ("0", "50"))
    r = score_cost(bundle(invoices=inv), cfg)
    assert r.score == D("75.00")
    assert r.metrics["excluded_zero_expected"] == 1
    assert r.excluded_record_ids == [("INV-004", "zero_expected")]
    assert "INV-004" not in r.source_record_ids and r.observations == 3
    assert r.coverage == D("0.7500")


def test_cost_below_minimum_usable_is_missing(cfg):
    inv = invoices(("100", "105"), ("100", "95"), ("0", "50"))
    r = score_cost(bundle(invoices=inv), cfg)
    assert_missing(r, "insufficient invoices: 2 < 3")
    assert r.excluded_record_ids == [("INV-003", "zero_expected")]


def test_cost_underbilling_weight_comes_from_config(cfg):
    full = cfg.model_copy(update={"underbilling_weight": 1.0})
    r = score_cost(bundle(invoices=invoices(("100", "105"), ("100", "95"), ("100", "100"))), full)
    assert r.score == D("66.67")  # wavp = 10/300


def test_cost_large_variance_clamps_to_zero(cfg):
    r = score_cost(bundle(invoices=invoices(("100", "150"), ("100", "150"), ("100", "150"))), cfg)
    assert r.score == D("0.00")


def test_cost_invalid_negative_expected_raises(cfg):
    inv = [*invoices(("100", "100"), ("100", "100")), InvoiceObs("INV-BAD", D(-5), D(10))]
    with pytest.raises(InvalidEvidenceError, match="INV-BAD"):
        score_cost(bundle(invoices=inv), cfg)


# --- capacity ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("demand_qty", "expected_score"),
    [
        (80, "100.00"),
        (90, "85.00"),
        (100, "70.00"),
        (125, "35.00"),
        (150, "0.00"),
        (160, "0.00"),
        (79, "100.00"),
        (81, "98.50"),
        (101, "68.60"),
    ],
)
def test_capacity_breakpoints(cfg, demand_qty, expected_score):
    r = score_capacity(bundle(demand=[demand(JAN, demand_qty)], capacities=[cap(JAN, 100)]), cfg)
    assert r.name == ComponentName.capacity
    assert r.score == D(expected_score)


def test_capacity_uses_free_capacity_and_max_month(cfg):
    r = score_capacity(
        bundle(
            demand=[demand(JAN, 50, "POL-1"), demand(FEB, 125, "POL-2")],
            capacities=[cap(JAN, 150, 50, cid="CAP-1"), cap(FEB, 100, cid="CAP-2")],
        ),
        cfg,
    )
    assert r.score == D("35.00")
    assert D(r.metrics["util"]) == D("1.25")
    assert r.metrics["util_month"] == "2026-02-01"
    monthly = r.metrics["monthly_utilisation"]
    assert D(monthly["2026-01-01"]) == D("0.5") and D(monthly["2026-02-01"]) == D("1.25")
    assert r.observations == 2 and r.coverage == D("1.0000")
    assert set(r.source_record_ids) == {"CAP-1", "CAP-2", "POL-1", "POL-2"}


def test_capacity_sums_demand_and_free_capacity_within_a_month(cfg):
    r = score_capacity(
        bundle(
            demand=[demand(JAN, 40, "POL-1"), demand(JAN, 50, "POL-2")],
            capacities=[cap(JAN, 60, cid="CAP-1"), cap(JAN, 40, cid="CAP-2")],
        ),
        cfg,
    )
    assert D(r.metrics["util"]) == D("0.9") and r.score == D("85.00")


def test_capacity_zero_free_with_demand_scores_zero(cfg):
    r = score_capacity(bundle(demand=[demand(JAN, 10)], capacities=[cap(JAN, 100, 100)]), cfg)
    assert r.score == D("0.00")
    assert r.flags["capacity_exhausted"] is True
    assert r.metrics["util"] == "inf"


def test_capacity_unverified_rows_do_not_add_free_capacity(cfg):
    r = score_capacity(
        bundle(
            demand=[demand(JAN, 90)],
            capacities=[cap(JAN, 100, cid="CAP-V"), cap(JAN, 1000, verified=False, cid="CAP-U")],
        ),
        cfg,
    )
    assert r.score == D("85.00")
    assert ("CAP-U", "capacity_unverified") in r.excluded_record_ids
    assert "CAP-U" not in r.source_record_ids


def test_capacity_only_unverified_is_missing(cfg):
    r = score_capacity(
        bundle(demand=[demand(JAN, 90)], capacities=[cap(JAN, 100, verified=False)]), cfg
    )
    assert_missing(r, "capacity unverified")


def test_capacity_no_declarations_is_unknown(cfg):
    assert_missing(score_capacity(bundle(demand=[demand(JAN, 90)]), cfg), "capacity unknown")


def test_capacity_no_demand_is_missing(cfg):
    assert_missing(score_capacity(bundle(capacities=[cap(JAN, 100)]), cfg), "no demand in window")


def test_capacity_month_without_verified_capacity_is_skipped(cfg):
    r = score_capacity(
        bundle(
            demand=[demand(JAN, 90, "POL-1"), demand(FEB, 500, "POL-2")],
            capacities=[cap(JAN, 100)],
        ),
        cfg,
    )
    assert r.score == D("85.00")  # Feb has no verified capacity: not a month with both
    assert r.coverage == D("0.5000")
    assert ("POL-2", "no_verified_capacity_for_month") in r.excluded_record_ids


def test_capacity_no_month_with_both_is_missing(cfg):
    r = score_capacity(bundle(demand=[demand(FEB, 90)], capacities=[cap(JAN, 100)]), cfg)
    assert_missing(r, "no verified capacity covering demand months")


def test_capacity_breakpoints_come_from_config(cfg):
    from supplier_reliability.config import CapacityBreakpoint

    flat = cfg.model_copy(
        update={
            "capacity_breakpoints": [
                CapacityBreakpoint(utilization=0.5, score=100),
                CapacityBreakpoint(utilization=1.0, score=0),
            ]
        }
    )
    r = score_capacity(bundle(demand=[demand(JAN, 75)], capacities=[cap(JAN, 100)]), flat)
    assert r.score == D("50.00")


def test_capacity_invalid_committed_above_available_raises(cfg):
    bad = CapacityObs("CAP-BAD", None, JAN, D(100), D(150), True)
    with pytest.raises(InvalidEvidenceError, match="CAP-BAD"):
        score_capacity(bundle(demand=[demand(JAN, 10)], capacities=[bad]), cfg)
    # an UNVERIFIED declaration is not trusted anyway: it is never scored, so not rejected
    lax = CapacityObs("CAP-U", None, JAN, D(100), D(150), False)
    assert_missing(
        score_capacity(bundle(demand=[demand(JAN, 10)], capacities=[lax]), cfg),
        "capacity unverified",
    )


# --- compliance -------------------------------------------------------------------------------

S = ComplianceState


def test_compliance_points_and_coverage(cfg):
    r = score_compliance(
        bundle(
            compliance=[
                comp(S.COMPLIANT, rid="REQ-1"),
                comp(S.PENDING_VERIFICATION, rid="REQ-2"),
                comp(S.UNKNOWN, rid="REQ-3"),
            ]
        ),
        cfg,
    )
    assert r.name == ComponentName.compliance
    assert r.score == D("75.00")
    assert r.coverage == D("0.6667")
    assert r.numerator == D("150") and r.denominator == D("2")
    assert r.observations == 2
    assert r.flags["mandatory_compliance_failure"] is False


def test_compliance_expired_scores_zero_points(cfg):
    r = score_compliance(
        bundle(compliance=[comp(S.COMPLIANT, rid="REQ-1"), comp(S.EXPIRED, rid="REQ-2")]), cfg
    )
    assert r.score == D("50.00")


def test_compliance_mandatory_expired_sets_flag(cfg):
    r = score_compliance(bundle(compliance=[comp(S.EXPIRED, mandatory=True)]), cfg)
    assert r.flags["mandatory_compliance_failure"] is True
    assert r.score == D("0.00")


def test_compliance_mandatory_noncompliant_sets_flag(cfg):
    # evidence maps a REJECTED certification to NONCOMPLIANT (D11)
    r = score_compliance(
        bundle(compliance=[comp(S.COMPLIANT, rid="REQ-1"), comp(S.NONCOMPLIANT, True, "REQ-2")]),
        cfg,
    )
    assert r.flags["mandatory_compliance_failure"] is True and r.score == D("50.00")


def test_compliance_non_mandatory_noncompliant_does_not_set_flag(cfg):
    r = score_compliance(bundle(compliance=[comp(S.NONCOMPLIANT, mandatory=False)]), cfg)
    assert r.flags["mandatory_compliance_failure"] is False


def test_compliance_mandatory_pending_or_unknown_does_not_set_flag(cfg):
    r = score_compliance(
        bundle(
            compliance=[
                comp(S.PENDING_VERIFICATION, True, "REQ-1"),
                comp(S.UNKNOWN, True, "REQ-2"),
            ]
        ),
        cfg,
    )
    assert r.flags["mandatory_compliance_failure"] is False and r.score == D("50.00")


def test_compliance_all_unknown_is_missing(cfg):
    r = score_compliance(
        bundle(compliance=[comp(S.UNKNOWN, rid="A"), comp(S.UNKNOWN, rid="B")]), cfg
    )
    assert_missing(r, "unknown")
    assert r.coverage == D("0.0000")


def test_compliance_no_requirements_is_missing(cfg):
    assert_missing(score_compliance(bundle(), cfg), "no compliance requirements")


def test_compliance_points_come_from_config(cfg):
    pts = dict(cfg.compliance_points) | {"PENDING_VERIFICATION": 20}
    r = score_compliance(
        bundle(compliance=[comp(S.PENDING_VERIFICATION)]),
        cfg.model_copy(update={"compliance_points": pts}),
    )
    assert r.score == D("20.00")


def test_compliance_invalid_validity_range_raises(cfg):
    bad = comp(S.COMPLIANT, rid="REQ-BAD", valid_from=date(2026, 6, 1), valid_to=date(2026, 1, 1))
    with pytest.raises(InvalidEvidenceError, match="REQ-BAD"):
        score_compliance(bundle(compliance=[bad]), cfg)


# --- disruption -------------------------------------------------------------------------------


def test_disruption_open_high_signal_age_zero(cfg):
    r = score_disruption(bundle(risks=[risk()]), cfg)
    assert r.name == ComponentName.disruption
    assert r.score == D("70.00")
    assert D(r.metrics["penalty"]) == D("30")
    assert r.metrics["deduplicated_signal_ids"] == []
    assert r.observations == 1 and r.source_record_ids == ["RSK-1"]


def test_disruption_resolved_halves_penalty(cfg):
    assert score_disruption(bundle(risks=[risk(resolved_at_cutoff=True)]), cfg).score == D("85.00")


def test_disruption_resolved_flag_not_stored_status_decides(cfg):
    # stored status RESOLVED but only after the cutoff (R14): full penalty
    late = risk(resolved_at_cutoff=False, resolution=ResolutionStatus.RESOLVED)
    assert score_disruption(bundle(risks=[late]), cfg).score == D("70.00")


def test_disruption_inactive_site_halves_penalty(cfg):
    assert score_disruption(bundle(risks=[risk(site_active=False)]), cfg).score == D("85.00")
    both = risk(site_active=False, resolved_at_cutoff=True)
    assert score_disruption(bundle(risks=[both]), cfg).score == D("92.50")


def test_disruption_recency_decay(cfg):
    r = score_disruption(bundle(risks=[risk(age_days=90)]), cfg)
    assert r.score == D("88.96")  # 100 - 30*exp(-1) = 88.9636


def test_disruption_severity_weights(cfg):
    for sev, expected in [
        (Severity.LOW, "95.00"),
        (Severity.MEDIUM, "85.00"),
        (Severity.CRITICAL, "50.00"),
    ]:
        assert score_disruption(bundle(risks=[risk(severity=sev)]), cfg).score == D(expected)


def test_disruption_penalties_sum_and_clamp(cfg):
    two = [risk("RSK-1", Severity.HIGH), risk("RSK-2", Severity.MEDIUM)]
    assert score_disruption(bundle(risks=two), cfg).score == D("55.00")
    many = [risk(f"RSK-{i}", Severity.CRITICAL) for i in range(3)]
    assert score_disruption(bundle(risks=many), cfg).score == D("0.00")


def test_disruption_duplicates_count_once_keeping_highest_severity(cfg):
    dup = [
        risk("RSK-1", Severity.MEDIUM, key="K"),
        risk("RSK-2", Severity.HIGH, key="K"),
        risk("RSK-3", Severity.LOW, key="K"),
    ]
    r = score_disruption(bundle(risks=dup), cfg)
    assert r.score == D("70.00")
    assert r.metrics["deduplicated_signal_ids"] == ["RSK-1", "RSK-3"]
    assert r.source_record_ids == ["RSK-2"]
    assert r.excluded_record_ids == [("RSK-1", "deduplicated"), ("RSK-3", "deduplicated")]


def test_disruption_duplicate_tie_keeps_lowest_id(cfg):
    dup = [risk("RSK-9", key="K"), risk("RSK-2", key="K")]
    r = score_disruption(bundle(risks=dup), cfg)
    assert r.source_record_ids == ["RSK-2"] and r.metrics["deduplicated_signal_ids"] == ["RSK-9"]


def test_disruption_attestation_only_is_100(cfg):
    att = risk("RSK-A", Severity.NONE, signal_type=RiskSignalType.MONITORING_ATTESTATION)
    r = score_disruption(bundle(risks=[att]), cfg)
    assert r.score == D("100.00") and r.observations == 1


def test_disruption_attestation_adds_no_penalty_to_real_signal(cfg):
    att = risk("RSK-A", Severity.NONE, signal_type=RiskSignalType.MONITORING_ATTESTATION)
    assert score_disruption(bundle(risks=[att, risk()]), cfg).score == D("70.00")


def test_disruption_no_signals_is_missing(cfg):
    assert_missing(score_disruption(bundle(), cfg), "no risk monitoring evidence")


def test_disruption_parameters_come_from_config(cfg):
    heavier = cfg.model_copy(
        update={
            "resolved_multiplier": 0.25,
            "severity_weights": {**cfg.severity_weights, "HIGH": 40},
        }
    )
    assert score_disruption(bundle(risks=[risk(resolved_at_cutoff=True)]), heavier).score == D(
        "90.00"
    )
    fast = cfg.model_copy(update={"recency_days": 45})
    assert score_disruption(bundle(risks=[risk(age_days=45)]), fast).score == D("88.96")


def test_disruption_invalid_none_severity_on_real_signal_raises(cfg):
    bad = risk("RSK-BAD", Severity.NONE)
    with pytest.raises(InvalidEvidenceError, match="RSK-BAD"):
        score_disruption(bundle(risks=[bad]), cfg)


# --- cross-cutting ----------------------------------------------------------------------------


def test_results_are_deterministic_and_json_friendly(cfg):
    ev = bundle(
        deliveries=deliveries(on_time=8, late_days=(3, 5)),
        inspections=inspections((400, 8), (300, 6), (300, 6)),
        invoices=invoices(("100", "105"), ("100", "95"), ("100", "100")),
        demand=[demand(JAN, 90)],
        capacities=[cap(JAN, 100)],
        compliance=[comp(S.COMPLIANT)],
        risks=[risk(age_days=12.5), risk("RSK-2", Severity.LOW, age_days=3, key="X")],
    )
    for fn in (
        score_delivery,
        score_quality,
        score_cost,
        score_capacity,
        score_compliance,
        score_disruption,
    ):
        first, second = fn(ev, cfg), fn(ev, cfg)
        assert first == second
        assert first.score is not None and first.score == first.score.quantize(D("0.01"))
        assert D("0") <= first.score <= D("100")
        assert first.missing_reason is None
        json.dumps(first.metrics)  # metrics are JSON-serialisable


# --- fix round 1 ------------------------------------------------------------------------------


def test_disruption_fractional_day_recency_literal(cfg):
    r = score_disruption(bundle(risks=[risk(age_days=12.5)]), cfg)
    assert r.score == D("73.89")  # 100 - 30*exp(-12.5/90) = 73.8938


def test_attestation_contributes_zero_even_if_weights_are_patched(cfg):
    patched = cfg.model_copy(
        update={"severity_weights": {**cfg.severity_weights, "NONE": 10}}  # bypasses validation
    )
    att = risk("RSK-A", Severity.NONE, signal_type=RiskSignalType.MONITORING_ATTESTATION)
    r = score_disruption(bundle(risks=[att]), patched)
    assert r.score == D("100.00") and r.source_record_ids == ["RSK-A"]


def test_config_rejects_nonzero_none_severity_weight(app_config):
    data = app_config.model_dump(mode="json")
    data["scoring"]["severity_weights"]["NONE"] = 10
    with pytest.raises(ValidationError, match="NONE"):
        AppConfig.model_validate(data)


def test_capacity_negative_demand_is_invalid_evidence(cfg):
    bad = demand(JAN, -5, "POL-BAD")
    with pytest.raises(InvalidEvidenceError, match="POL-BAD"):
        score_capacity(bundle(demand=[demand(JAN, 90), bad], capacities=[cap(JAN, 100)]), cfg)


def test_capacity_zero_quantity_demand_is_reported_not_dropped(cfg):
    r = score_capacity(
        bundle(demand=[demand(JAN, 90), demand(JAN, 0, "POL-0")], capacities=[cap(JAN, 100)]), cfg
    )
    assert r.score == D("85.00")
    assert ("POL-0", "zero_quantity_demand") in r.excluded_record_ids
    assert "POL-0" not in r.source_record_ids
    only_zero = score_capacity(
        bundle(demand=[demand(JAN, 0, "POL-0")], capacities=[cap(JAN, 100)]), cfg
    )
    assert_missing(only_zero, "no demand in window")
    assert only_zero.excluded_record_ids == [("POL-0", "zero_quantity_demand")]

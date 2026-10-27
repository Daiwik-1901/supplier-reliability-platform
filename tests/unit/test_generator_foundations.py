# ruff: noqa: E501  (the D10 literal table is kept one row per profile)
import numpy as np
import pytest

from supplier_reliability.services.generator.config import (
    GeneratorCapacityError,
    GeneratorConfig,
)
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.profiles import allocate_exact, assign_profiles
from supplier_reliability.services.generator.rng import RandomSource


def test_same_seed_same_stream_values():
    a = RandomSource(7).stream("x").random(5)
    b = RandomSource(7).stream("x").random(5)
    c = RandomSource(8).stream("x").random(5)
    assert (a == b).all() and not (a == c).all()


def test_stream_independent_of_creation_order():
    r1 = RandomSource(7)
    r1.stream("other").random(3)
    v1 = r1.stream("x").random(5)
    v2 = RandomSource(7).stream("x").random(5)
    assert (v1 == v2).all()


def test_stream_is_cached_and_continues():
    r = RandomSource(7)
    assert r.stream("x") is r.stream("x")
    first, second = r.stream("x").random(3), r.stream("x").random(3)
    assert (RandomSource(7).stream("x").random(6) == np.concatenate([first, second])).all()


def test_faker_deterministic_and_seed_dependent():
    a = RandomSource(7).faker("f").name()
    b = RandomSource(7).faker("f").name()
    c = RandomSource(9).faker("f").name()
    assert a == b and a != c


def test_id_format_and_sequence():
    ids = IdRegistry()
    assert ids.next("supplier") == "SUP-000001"
    assert ids.next("supplier") == "SUP-000002"
    assert ids.next("site", parent="SUP-000001") == "SITE-SUP-000001-01"
    assert ids.next("site", parent="SUP-000001") == "SITE-SUP-000001-02"
    assert ids.next("site", parent="SUP-000002") == "SITE-SUP-000002-01"
    assert ids.all("supplier") == ["SUP-000001", "SUP-000002"]
    assert ids.exists("supplier", "SUP-000002") and not ids.exists("supplier", "SUP-000003")


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("contract", "CTR-000001"),
        ("po", "PO-2026-000001"),
        ("po_line", "POL-2026-000001"),
        ("shipment", "SHP-2026-000001"),
        ("shipment_line", "SHL-2026-000001"),
        ("event", "EVT-2026-000001"),
        ("inspection", "INS-2026-000001"),
        ("invoice", "INV-2026-000001"),
        ("risk", "RSK-2026-000001"),
        ("score", "SCORE-2026-000001"),
        ("raw", "RAW-000001"),
        ("exception", "EXC-000001"),
        ("case", "CASE-000001"),
        ("contract_price", "CPR-000001"),
        ("compliance_requirement", "REQ-000001"),
        ("certification", "CERT-000001"),
        ("capacity_declaration", "CAP-000001"),
    ],
)
def test_all_id_kinds(kind, expected):
    assert IdRegistry().next(kind) == expected


def test_id_registry_errors():
    ids = IdRegistry()
    with pytest.raises(ValueError, match="unknown"):
        ids.next("bogus")
    with pytest.raises(ValueError, match="parent"):
        ids.next("site")
    ids.register("supplier", "SUP-X")
    with pytest.raises(ValueError, match="duplicate"):
        ids.register("supplier", "SUP-X")


def _shares(app_config):
    return {k: p.share for k, p in app_config.generator.profiles.items()}


def test_assign_profiles_exact_counts(app_config):
    shares = _shares(app_config)
    out = assign_profiles(1000, shares, RandomSource(1).stream("p"))
    counts = {k: out.count(k) for k in shares}
    assert len(out) == 1000
    assert counts["stable_high"] == 200
    assert counts["average"] == 250
    assert counts["recovering"] == 50
    assert counts["sparse_history"] == 50
    assert sum(counts.values()) == 1000


def test_assign_profiles_small_n_follows_rule(app_config):
    shares = _shares(app_config)
    out = assign_profiles(50, shares, RandomSource(1).stream("p"))
    # Python round() is banker's rounding: 3.5 -> 4, 2.5 -> 2.
    expected = {
        "stable_high": 10,
        "average": 13,
        "deteriorating_lead_time": 5,
        "quality_issues": 4,
        "invoice_discrepancy": 4,
        "capacity_constrained": 4,
        "compliance_risk": 3,
        "disruption_prone": 3,
        "sparse_history": 2,
        "recovering": 2,
    }
    assert {k: out.count(k) for k in shares} == expected
    assert len(out) == 50


def test_assign_profiles_independent_of_dict_order(app_config):
    shares = _shares(app_config)
    reordered = dict(reversed(list(shares.items())))
    a = assign_profiles(100, shares, RandomSource(1).stream("p"))
    b = assign_profiles(100, reordered, RandomSource(1).stream("p"))
    assert a == b


D10 = {
    # share, po_weight, on_time_p, mean_late_days, defect_mean, inv_p, inv_mag, dcr, cert, risk
    "stable_high": (0.20, 1.2, (0.97, 0.97), (1, 1), (0.005, 0.005), 0.02, 0.01, 0.55, 0.02, 0.5),
    "average": (0.25, 1.0, (0.88, 0.88), (3, 3), (0.02, 0.02), 0.08, 0.02, 0.70, 0.05, 1),
    "deteriorating_lead_time": (
        0.10,
        1.0,
        (0.92, 0.55),
        (6, 6),
        (0.02, 0.02),
        0.08,
        0.02,
        0.75,
        0.05,
        1.5,
    ),
    "quality_issues": (0.08, 1.0, (0.88, 0.85), (3, 3), (0.07, 0.07), 0.08, 0.02, 0.70, 0.05, 1),
    "invoice_discrepancy": (
        0.08,
        1.0,
        (0.88, 0.88),
        (3, 3),
        (0.02, 0.02),
        0.45,
        0.09,
        0.70,
        0.05,
        1,
    ),
    "capacity_constrained": (
        0.07,
        1.3,
        (0.80, 0.75),
        (5, 5),
        (0.03, 0.03),
        0.08,
        0.02,
        1.35,
        0.05,
        1.5,
    ),
    "compliance_risk": (0.06, 0.9, (0.86, 0.86), (3, 3), (0.025, 0.025), 0.08, 0.02, 0.70, 0.60, 1),
    "disruption_prone": (0.06, 0.9, (0.80, 0.70), (7, 7), (0.03, 0.03), 0.08, 0.02, 0.80, 0.05, 6),
    "sparse_history": (0.05, 0.1, (0.85, 0.85), (4, 4), (0.02, 0.02), 0.08, 0.02, 0.60, 0.10, 0.2),
    "recovering": (0.05, 1.0, (0.60, 0.93), (6, 2), (0.06, 0.015), 0.08, 0.02, 0.85, 0.05, 1),
}
D10_FIELDS = (
    "share",
    "po_weight",
    "on_time_p",
    "mean_late_days",
    "defect_mean",
    "invoice_variance_p",
    "invoice_variance_magnitude",
    "demand_capacity_ratio",
    "cert_expiry_p",
    "risk_signals_per_year",
)


def test_yaml_profiles_match_d10_table(app_config):
    profiles = app_config.generator.profiles
    assert set(profiles) == set(D10)
    for name, row in D10.items():
        for field, want in zip(D10_FIELDS, row, strict=True):
            got = getattr(profiles[name], field)
            if isinstance(want, tuple):
                assert tuple(got) == pytest.approx(want), (name, field)
            else:
                assert got == pytest.approx(want), (name, field)
    assert sum(p.share for p in profiles.values()) == pytest.approx(1.0, abs=1e-9)


def test_anomaly_preset_resolved_by_construction(app_config):
    starter = GeneratorConfig.from_app_config(app_config, "starter")
    full = GeneratorConfig.from_app_config(app_config, "full")
    assert starter.anomaly_preset.positives_per_class == 5
    assert starter.anomaly_preset.negatives == {
        "shipments": 40,
        "delivery_event_pairs": 40,
        "risk_signals": 30,
        "scores": 30,
    }
    assert full.anomaly_preset.positives_per_class == 25
    assert full.anomaly_preset.negatives["shipments"] == 500
    assert starter.e_cross_supplier_fraction == 0.6


def test_missing_anomaly_entry_raises(app_config):
    data = app_config.model_dump()
    data["generator"]["presets"]["tiny"] = {
        "suppliers": 5,
        "purchase_orders": 10,
        "shipments": 15,
        "inspections": 20,
    }
    cfg = type(app_config).model_validate(data)
    with pytest.raises(ValueError, match="anomaly"):
        GeneratorConfig.from_app_config(cfg, "tiny")


def test_dataset_id_ignores_other_presets_anomalies(app_config):
    base = GeneratorConfig.from_app_config(app_config, "starter")
    data = app_config.model_dump()
    data["anomalies"]["full"]["positives_per_class"] = 99
    other = type(app_config).model_validate(data)
    assert GeneratorConfig.from_app_config(other, "starter").dataset_id == base.dataset_id
    data["anomalies"]["starter"]["positives_per_class"] = 6
    changed = type(app_config).model_validate(data)
    assert GeneratorConfig.from_app_config(changed, "starter").dataset_id != base.dataset_id


def test_allocate_exact_rejects_bad_inputs():
    rng = RandomSource(3).stream("a")
    with pytest.raises(ValueError):
        allocate_exact(10, np.array([1.0, np.nan]), 0, rng)
    with pytest.raises(ValueError):
        allocate_exact(10, np.array([1.0, np.inf]), 0, rng)
    with pytest.raises(ValueError):
        allocate_exact(10, np.ones(2), -1, rng)


def test_allocate_exact_boundaries():
    rng = RandomSource(3).stream("a")
    assert allocate_exact(6, np.array([1.0, 5.0, 2.0]), 2, rng).tolist() == [2, 2, 2]
    even = allocate_exact(9, np.array([1.0, 1.0, 1.0]), 0, rng, maximum=3)
    assert even.tolist() == [3, 3, 3]
    # zero-weight slot with binding maximum: weight-1 slots cap at 4, remainder must go to slot 0
    out = allocate_exact(10, np.array([0.0, 1.0, 1.0]), 0, rng, maximum=4)
    assert out.sum() == 10 and out.max() <= 4 and out[0] == 2
    # all open weights zero: even-split branch
    out = allocate_exact(3, np.array([1.0, 0.0, 0.0]), 0, rng, maximum=1)
    assert out.tolist() == [1, 1, 1]


def test_assign_profiles_deterministic_and_shuffled(app_config):
    shares = _shares(app_config)
    a = assign_profiles(100, shares, RandomSource(1).stream("p"))
    b = assign_profiles(100, shares, RandomSource(1).stream("p"))
    assert a == b
    assert a != sorted(a)


def test_allocate_exact_sums_and_minimum():
    w = np.array([5.0, 1.0, 0.0, 2.0])
    out = allocate_exact(100, w, 2, RandomSource(3).stream("a"))
    assert out.sum() == 100 and (out >= 2).all()
    again = allocate_exact(100, w, 2, RandomSource(3).stream("a"))
    assert (out == again).all()


def test_allocate_exact_maximum():
    rng = RandomSource(3).stream("a")
    out = allocate_exact(30, np.array([10.0, 1.0, 1.0]), 1, rng, maximum=12)
    assert out.sum() == 30 and out.max() <= 12 and out.min() >= 1


def test_allocate_exact_impossible_raises():
    rng = RandomSource(3).stream("a")
    with pytest.raises(GeneratorCapacityError):
        allocate_exact(5, np.ones(4), 2, rng)
    with pytest.raises(GeneratorCapacityError):
        allocate_exact(50, np.ones(4), 1, rng, maximum=10)


def test_profile_at_interpolates(app_config):
    p = app_config.generator.profiles["recovering"]
    assert p.at("on_time_p", 0) == pytest.approx(0.60)
    assert p.at("on_time_p", 1) == pytest.approx(0.93)
    assert p.at("on_time_p", 0.5) == pytest.approx(0.765)
    assert p.at("mean_late_days", 0.25) == pytest.approx(5.0)
    assert p.at("defect_mean", 1) == pytest.approx(0.015)
    assert p.at("po_weight", 0.7) == 1.0  # scalar field is constant
    with pytest.raises(KeyError):
        p.at("nope", 0.5)


def test_generator_config_from_app_config(app_config):
    full = GeneratorConfig.from_app_config(app_config, "full")
    assert (full.n_suppliers, full.n_purchase_orders) == (1000, 10000)
    assert (full.n_shipments, full.n_inspections) == (15000, 20000)
    assert full.seed == 20261022
    assert full.reference_start.isoformat() == "2025-01-01T00:00:00+00:00"
    assert full.reference_end.isoformat() == "2026-06-30T23:59:59+00:00"
    starter = GeneratorConfig.from_app_config(app_config, "starter")
    assert (starter.n_suppliers, starter.n_inspections) == (50, 600)
    assert full.profiles["average"].share == 0.25
    with pytest.raises(ValueError, match="unknown preset"):
        GeneratorConfig.from_app_config(app_config, "huge")


def test_dataset_id_deterministic_and_seed_sensitive(app_config):
    a = GeneratorConfig.from_app_config(app_config, "starter")
    b = GeneratorConfig.from_app_config(app_config, "starter")
    c = GeneratorConfig.from_app_config(app_config, "starter", seed=1)
    assert a.dataset_id == b.dataset_id
    assert a.dataset_id != c.dataset_id
    assert a.dataset_id.startswith("DS-starter-20261022-")
    assert len(a.dataset_id.rsplit("-", 1)[1]) == 8

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
    assert len(out) == 50
    others = 0
    for k, s in shares.items():
        if k != "average":
            assert out.count(k) == round(s * 50)
            others += round(s * 50)
    assert out.count("average") == 50 - others


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

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
import sqlalchemy as sa

from supplier_reliability.models.master import (
    CapacityDeclaration,
    ComplianceRequirement,
    Contract,
    ContractPrice,
    Site,
    Supplier,
    SupplierCertification,
)
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.constants import MEAN_LINES_PER_PO, MEAN_QTY_PER_LINE
from supplier_reliability.services.generator.dataset import GeneratedDataset
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.master_data import (
    expected_monthly_demand,
    generate_master_data,
)
from supplier_reliability.services.generator.rng import RandomSource


def _run(app_config, preset="starter", seed=None):
    cfg = GeneratorConfig.from_app_config(app_config, preset, seed)
    ds = GeneratedDataset(config=cfg)
    generate_master_data(cfg, RandomSource(cfg.seed), IdRegistry(), ds)
    return cfg, ds


@pytest.fixture(scope="module")
def starter(app_config):
    return _run(app_config)


REF_START = date(2025, 1, 1)
REF_END = date(2026, 6, 30)


def test_supplier_count_exact(starter):
    cfg, ds = starter
    assert len(ds.suppliers) == 50 == cfg.n_suppliers
    assert len(ds.supplier_profiles) == 50
    assert set(ds.supplier_profiles) == {s["supplier_id"] for s in ds.suppliers}


def test_every_site_belongs_to_existing_supplier(starter):
    _, ds = starter
    sup = {s["supplier_id"] for s in ds.suppliers}
    per = defaultdict(list)
    for s in ds.sites:
        assert s["supplier_id"] in sup
        assert s["site_id"].startswith("SITE-" + s["supplier_id"])
        per[s["supplier_id"]].append(s)
    assert set(per) == sup
    for sid, sites in per.items():
        profile = ds.supplier_profiles[sid]
        assert len(sites) == 1 if profile == "sparse_history" else 1 <= len(sites) <= 3


def test_sites_start_at_onboarding_and_some_inactive(starter):
    _, ds = starter
    onboard = {s["supplier_id"]: s["onboarding_date"] for s in ds.suppliers}
    inactive = [s for s in ds.sites if s["site_status"] == "INACTIVE"]
    assert inactive
    assert 0.03 <= len(inactive) / len(ds.sites) <= 0.2
    for s in ds.sites:
        assert s["effective_from"] == onboard[s["supplier_id"]]
        if s["site_status"] == "INACTIVE":
            assert REF_START <= s["effective_to"] <= REF_END
        else:
            assert s["effective_to"] is None


def test_every_supplier_has_site_active_for_whole_period(starter):
    _, ds = starter
    ok = defaultdict(bool)
    for s in ds.sites:
        if (
            s["site_status"] == "ACTIVE"
            and s["effective_from"] <= REF_START
            and (s["effective_to"] is None or s["effective_to"] >= REF_END)
        ):
            ok[s["supplier_id"]] = True
    assert all(ok[s["supplier_id"]] for s in ds.suppliers)


def test_contracts_cover_reference_period_without_gap(starter):
    _, ds = starter
    per = defaultdict(list)
    for c in ds.contracts:
        per[c["supplier_id"]].append(c)
    assert set(per) == {s["supplier_id"] for s in ds.suppliers}
    renewed = 0
    for cs in per.values():
        assert 1 <= len(cs) <= 2
        cs.sort(key=lambda c: c["effective_from"])
        assert cs[0]["effective_from"] <= REF_START
        for a, b in zip(cs, cs[1:], strict=False):
            assert a["contract_status"] == "EXPIRED"
            assert REF_START <= a["effective_to"] < REF_END
            assert (b["effective_from"] - a["effective_to"]).days == 1
            renewed += 1
        last = cs[-1]
        assert last["effective_to"] is None or last["effective_to"] >= REF_END
        assert len({c["currency"] for c in cs}) == 1
    assert 0.05 <= renewed / len(per) <= 0.3


def test_contract_scope_and_terms(starter):
    _, ds = starter
    sites = {s["site_id"]: s for s in ds.sites}
    n_scoped = 0
    for c in ds.contracts:
        assert c["currency"] in {"USD", "EUR", "GBP", "INR"}
        assert c["capacity_unit"] == "units/month"
        if c["site_id"] is not None:
            n_scoped += 1
            assert sites[c["site_id"]]["supplier_id"] == c["supplier_id"]
    assert n_scoped < len(ds.contracts) / 2
    assert len({c["payment_terms"] for c in ds.contracts}) > 1


def test_some_contracts_have_unknown_capacity(starter):
    _, ds = starter
    unknown = [c for c in ds.contracts if c["contracted_capacity"] is None]
    assert unknown
    assert len(unknown) / len(ds.contracts) <= 0.12
    for c in ds.contracts:
        if c["contracted_capacity"] is not None:
            assert isinstance(c["contracted_capacity"], Decimal)
            assert c["contracted_capacity"] >= 0


def test_contracted_capacity_is_profile_free_capacity_with_peak_headroom(starter):
    """Rulings R33/R34: expected demand / profile demand_capacity_ratio x CONTRACT_PEAK_HEADROOM."""
    from supplier_reliability.services.generator.constants import CONTRACT_PEAK_HEADROOM

    assert Decimal("2.0") == CONTRACT_PEAK_HEADROOM
    cfg, ds = starter
    total_w = sum(cfg.profiles[p].po_weight for p in ds.supplier_profiles.values())
    checked = 0
    for c in ds.contracts:
        if c["contracted_capacity"] is None:
            continue
        p = cfg.profiles[ds.supplier_profiles[c["supplier_id"]]]
        demand = expected_monthly_demand(p, cfg.n_purchase_orders * p.po_weight / total_w, cfg)
        ratio = Decimal(str(p.demand_capacity_ratio))
        expected = (demand / ratio * CONTRACT_PEAK_HEADROOM).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )
        assert c["contracted_capacity"] == expected, c["contract_id"]
        checked += 1
    assert checked >= 0.85 * len(ds.contracts)


def test_each_contract_has_price_per_category(starter):
    _, ds = starter
    per = defaultdict(list)
    for p in ds.contract_prices:
        assert isinstance(p["unit_price"], Decimal) and p["unit_price"] > 0
        per[p["contract_id"]].append(p["product_category"])
    assert set(per) == {c["contract_id"] for c in ds.contracts}
    for cats in per.values():
        assert len(cats) == len(set(cats)) >= 2


def test_requirements_per_contract(starter):
    _, ds = starter
    per = defaultdict(list)
    for r in ds.compliance_requirements:
        per[r["contract_id"]].append(r)
    assert set(per) == {c["contract_id"] for c in ds.contracts}
    for rs in per.values():
        assert 2 <= len(rs) <= 4
        assert any(r["mandatory"] for r in rs)
        assert len({r["certification_type"] for r in rs}) == len(rs)
    scoped = [r for r in ds.compliance_requirements if r["scope"] == "SITE"]
    assert 0.05 <= len(scoped) / len(ds.compliance_requirements) <= 0.3


def test_site_scoped_requirement_has_site_of_same_supplier(starter):
    _, ds = starter
    sites = {s["site_id"]: s for s in ds.sites}
    contracts = {c["contract_id"]: c for c in ds.contracts}
    scoped = 0
    for r in ds.compliance_requirements:
        if r["scope"] == "SITE":
            scoped += 1
            assert sites[r["site_id"]]["supplier_id"] == contracts[r["contract_id"]]["supplier_id"]
        else:
            assert r["site_id"] is None
    assert scoped > 0


def test_capacity_declarations_nonnegative_free_mostly_and_some_unverified(starter):
    _, ds = starter
    rows = ds.capacity_declarations
    assert rows
    sup_with = {r["supplier_id"] for r in rows}
    assert 0 < 50 - len(sup_with) <= 5  # ~5% of suppliers have none
    unverified = [r for r in rows if r["verification_status"] == "UNVERIFIED"]
    assert 0.03 <= len(unverified) / len(rows) <= 0.2
    for r in rows:
        assert isinstance(r["available_capacity"], Decimal)
        assert r["unit"] == "units/month"
        assert r["period_start"].day == 1
        assert r["period_end"].month == r["period_start"].month
        assert (r["period_end"].replace(day=28) - r["period_start"]).days >= 27
        assert (r["period_end"] + (date(2000, 1, 2) - date(2000, 1, 1))).day == 1
        assert r["committed_capacity"] >= 0
        if r["verification_status"] == "VERIFIED":
            assert r["committed_capacity"] <= r["available_capacity"]


def test_capacity_rows_are_monthly_and_scoped_to_active_sites(starter):
    _, ds = starter
    sites = {s["site_id"]: s for s in ds.sites}
    seen = set()
    for r in ds.capacity_declarations:
        key = (r["site_id"], r["period_start"])
        assert key not in seen
        seen.add(key)
        s = sites[r["site_id"]]
        assert s["supplier_id"] == r["supplier_id"]
        assert s["effective_from"] <= r["period_end"]
        assert s["effective_to"] is None or s["effective_to"] >= r["period_start"]
    months = {r["period_start"] for r in ds.capacity_declarations}
    assert date(2025, 1, 1) in months and date(2026, 6, 1) in months and len(months) == 18


def test_realised_demand_capacity_ratio_tracks_profile(app_config, starter):
    cfg, ds = starter
    total_w = sum(cfg.profiles[p].po_weight for p in ds.supplier_profiles.values())
    free = defaultdict(lambda: defaultdict(Decimal))
    for r in ds.capacity_declarations:
        if r["verification_status"] != "VERIFIED":
            continue  # D6: scoring sees VERIFIED declarations only
        free[r["supplier_id"]][r["period_start"]] += (
            r["available_capacity"] - r["committed_capacity"]
        )
    by_profile = defaultdict(list)
    for sid, months in free.items():
        prof = cfg.profiles[ds.supplier_profiles[sid]]
        demand = expected_monthly_demand(
            prof, cfg.n_purchase_orders * prof.po_weight / total_w, cfg
        )
        for f in months.values():
            by_profile[ds.supplier_profiles[sid]].append(float(demand / f))
    mean = {k: sum(v) / len(v) for k, v in by_profile.items()}
    assert mean["capacity_constrained"] > 1.0
    assert mean["stable_high"] < 0.8
    for name, m in mean.items():
        assert m == pytest.approx(cfg.profiles[name].demand_capacity_ratio, rel=0.1)


def test_expected_monthly_demand_formula(app_config):
    cfg = GeneratorConfig.from_app_config(app_config, "starter")
    prof = cfg.profiles["average"]
    d = expected_monthly_demand(prof, 18.0, cfg)
    assert isinstance(d, Decimal)
    assert d == Decimal(str(round(18.0 * MEAN_LINES_PER_PO * float(MEAN_QTY_PER_LINE) / 18, 2)))


def test_certification_history_supersedes_existing_rows(starter):
    _, ds = starter
    by_id = {c["certification_id"]: c for c in ds.supplier_certifications}
    assert len(by_id) == len(ds.supplier_certifications)
    history = [c for c in ds.supplier_certifications if c["supersedes_certification_id"]]
    assert history
    for c in history:
        old = by_id[c["supersedes_certification_id"]]
        assert old["supplier_id"] == c["supplier_id"]
        assert old["certification_type"] == c["certification_type"]
        assert old["site_id"] == c["site_id"]
        assert c["recorded_at"] > old["recorded_at"]
    suppliers_with_history = {c["supplier_id"] for c in history}
    assert 0.15 <= len(suppliers_with_history) / 50 <= 0.8
    keys = [(c["supplier_id"], c["certificate_number"], c["recorded_at"]) for c in by_id.values()]
    assert len(keys) == len(set(keys))


def test_certification_lapse_modes_present(starter):
    _, ds = starter
    certs = ds.supplier_certifications
    assert any(c["valid_to"] < REF_END for c in certs if c["supersedes_certification_id"] is None)
    assert any(c["compliance_status"] == "NONCOMPLIANT" for c in certs)
    assert any(c["verification_status"] == "PENDING_VERIFICATION" for c in certs)
    for c in certs:
        assert c["valid_to"] >= c["valid_from"]
        assert (c["verified_at"] is None) == (c["verification_status"] == "PENDING_VERIFICATION")


def test_some_requirements_have_no_certification(starter):
    _, ds = starter
    contracts = {c["contract_id"]: c["supplier_id"] for c in ds.contracts}
    have = {
        (c["supplier_id"], c["certification_type"], c["site_id"])
        for c in ds.supplier_certifications
    }
    reqs = ds.compliance_requirements
    missing = [
        r
        for r in reqs
        if (contracts[r["contract_id"]], r["certification_type"], r["site_id"]) not in have
    ]
    assert 0.02 <= len(missing) / len(reqs) <= 0.16


def test_issuing_authority_and_evidence_are_synthetic(starter):
    _, ds = starter
    for c in ds.supplier_certifications:
        assert c["issuing_authority"].startswith("Synthetic Certification Body ")
        assert c["evidence_reference"].startswith("synthetic://cert/")


def test_profiles_not_in_supplier_rows(starter):
    _, ds = starter
    profiles = set(ds.supplier_profiles.values())
    for s in ds.suppliers:
        assert "profile" not in s
        assert not any(isinstance(v, str) and v in profiles for v in s.values())


def test_timestamps_are_utc_and_deterministic_offsets(starter):
    _, ds = starter
    onboard = {s["supplier_id"]: s["onboarding_date"] for s in ds.suppliers}
    for s in ds.suppliers:
        assert s["created_at"].tzinfo is UTC
        assert s["created_at"].date() >= s["onboarding_date"]
        assert s["updated_at"] >= s["created_at"]
    for s in ds.sites:
        assert s["created_at"].tzinfo is UTC
        assert s["created_at"].date() >= onboard[s["supplier_id"]]
    for c in ds.supplier_certifications:
        assert c["recorded_at"].tzinfo is UTC
    for r in ds.capacity_declarations:
        assert isinstance(r["recorded_at"], datetime) and r["recorded_at"].tzinfo is UTC


@pytest.mark.parametrize(
    ("table", "model"),
    [
        ("suppliers", Supplier),
        ("sites", Site),
        ("contracts", Contract),
        ("contract_prices", ContractPrice),
        ("compliance_requirements", ComplianceRequirement),
        ("supplier_certifications", SupplierCertification),
        ("capacity_declarations", CapacityDeclaration),
    ],
)
def test_row_keys_equal_model_columns(starter, table, model):
    _, ds = starter
    cols = {c.key for c in sa.inspect(model).columns}
    rows = getattr(ds, table)
    assert rows
    for row in rows:
        assert set(row) == cols


def test_untouched_tables_empty(starter):
    _, ds = starter
    for name in (
        "purchase_orders",
        "po_lines",
        "shipments",
        "shipment_lines",
        "delivery_events",
        "inspections",
        "invoices",
        "risk_signals",
    ):
        assert getattr(ds, name) == []


def test_master_data_deterministic(app_config):
    _, a = _run(app_config)
    _, b = _run(app_config)
    for t in (
        "suppliers",
        "sites",
        "contracts",
        "contract_prices",
        "compliance_requirements",
        "supplier_certifications",
        "capacity_declarations",
    ):
        assert getattr(a, t) == getattr(b, t)
    assert a.supplier_profiles == b.supplier_profiles
    _, c = _run(app_config, seed=1)
    assert c.suppliers != a.suppliers


def test_full_preset_counts(app_config):
    _, ds = _run(app_config, "full")
    assert len(ds.suppliers) == 1000


def test_certification_sites_match_supplier_and_requirement_site(starter):
    _, ds = starter
    sites = {s["site_id"]: s["supplier_id"] for s in ds.sites}
    for c in ds.supplier_certifications:
        if c["site_id"] is not None:
            assert sites[c["site_id"]] == c["supplier_id"]
    contracts = {c["contract_id"]: c["supplier_id"] for c in ds.contracts}
    certs = {
        (c["supplier_id"], c["certification_type"], c["site_id"])
        for c in ds.supplier_certifications
    }
    site_reqs = [r for r in ds.compliance_requirements if r["scope"] == "SITE"]
    matched = 0
    for r in site_reqs:
        sid = contracts[r["contract_id"]]
        if (sid, r["certification_type"], r["site_id"]) in certs:
            matched += 1
    assert matched > 0

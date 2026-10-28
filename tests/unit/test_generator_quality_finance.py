"""Task 8: inspections (D2), invoices (D3), risk signals (D6 dedup key) and generate_dataset."""

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from supplier_reliability.domain.enums import (
    DeliveryEventType,
    InspectionKind,
    InspectionType,
    ResolutionStatus,
    RiskSignalType,
    Severity,
    ShipmentStatus,
)
from supplier_reliability.models.transactions import Inspection, Invoice, RiskSignal
from supplier_reliability.services.generator.config import GeneratorCapacityError, GeneratorConfig
from supplier_reliability.services.generator.dataset import GeneratedDataset, generate_dataset
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.master_data import generate_master_data
from supplier_reliability.services.generator.quality_finance import (
    generate_inspections_and_invoices,
)
from supplier_reliability.services.generator.risk import (
    RISK_SOURCES,
    duplicate_source_pool,
    generate_risk_signals,
    plan_risk_signals,
    risk_dedup_key,
)
from supplier_reliability.services.generator.rng import RandomSource
from supplier_reliability.services.generator.transactions import generate_orders_and_shipments

E = DeliveryEventType
REF_START = datetime(2025, 1, 1, tzinfo=UTC)
REF_END = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
DELIVERED = {ShipmentStatus.DELIVERED, ShipmentStatus.PARTIALLY_RECEIVED}


@pytest.fixture(scope="module")
def starter(app_config):
    cfg = GeneratorConfig.from_app_config(app_config, "starter")
    return cfg, generate_dataset(cfg)


def _event(ds, etype):
    return {
        e["shipment_id"]: e for e in ds.delivery_events if e["event_type"] == etype
    }  # one ARRIVED / RECEIVED per shipment


def _lines(ds):
    return {a["shipment_line_id"]: a for a in ds.shipment_lines}


# --- generate_dataset ---------------------------------------------------------------------


def test_generate_dataset_runs_every_stage_with_exact_counts(starter):
    cfg, ds = starter
    assert len(ds.suppliers) == 50
    assert len(ds.purchase_orders) == 300
    assert len(ds.shipments) == 450
    assert len(ds.inspections) == 600
    assert ds.invoices and ds.risk_signals


def test_generate_dataset_equals_stage_by_stage_run(app_config, starter):
    cfg, a = starter
    rs, ids = RandomSource(cfg.seed), IdRegistry()
    b = GeneratedDataset(config=cfg)
    generate_master_data(cfg, rs, ids, b)
    generate_orders_and_shipments(cfg, rs, ids, b)
    generate_inspections_and_invoices(cfg, rs, ids, b)
    generate_risk_signals(cfg, rs, ids, b)
    assert a == b


def test_generate_dataset_deterministic_and_seed_sensitive(app_config, starter):
    cfg, a = starter
    assert generate_dataset(cfg) == a
    other = generate_dataset(GeneratorConfig.from_app_config(app_config, "starter", seed=7))
    assert other.inspections != a.inspections
    assert other.risk_signals != a.risk_signals


@pytest.mark.parametrize(
    ("table", "model"),
    [("inspections", Inspection), ("invoices", Invoice), ("risk_signals", RiskSignal)],
)
def test_row_keys_equal_model_columns(starter, table, model):
    _, ds = starter
    cols = {c.key for c in sa.inspect(model).columns}
    rows = getattr(ds, table)
    assert rows
    for row in rows:
        assert set(row) == cols


def test_id_formats(starter):
    _, ds = starter
    assert ds.inspections[0]["inspection_id"] == "INS-2026-000001"
    assert ds.invoices[0]["invoice_id"] == "INV-2026-000001"
    assert ds.risk_signals[0]["risk_signal_id"] == "RSK-2026-000001"


# --- inspections (D2) -----------------------------------------------------------------------


def test_inspection_count_exact(starter):
    _, ds = starter
    assert len(ds.inspections) == 600
    assert len({i["inspection_id"] for i in ds.inspections}) == 600


def test_inspection_targets_are_received_lines_of_delivered_shipments(starter):
    _, ds = starter
    shipments = {s["shipment_id"]: s for s in ds.shipments}
    lines = _lines(ds)
    for i in ds.inspections:
        line = lines[i["shipment_line_id"]]
        assert shipments[line["shipment_id"]]["shipment_status"] in DELIVERED
        assert line["received_quantity"] >= 1


def test_inspection_quantities_reconcile(starter):
    _, ds = starter
    lines = _lines(ds)
    for i in ds.inspections:
        received = lines[i["shipment_line_id"]]["received_quantity"]
        inspected = i["inspected_quantity"]
        assert i["accepted_quantity"] + i["rejected_quantity"] == inspected
        assert 0 <= i["rejected_quantity"] <= i["defect_quantity"] <= inspected
        assert inspected <= received
        if i["inspection_type"] == InspectionType.FULL:
            assert inspected == received
        else:
            assert 1 <= inspected < received
        for k in ("inspected_quantity", "accepted_quantity", "rejected_quantity"):
            assert isinstance(i[k], Decimal)
    sample = sum(i["inspection_type"] == InspectionType.SAMPLE for i in ds.inspections)
    assert 0.12 <= sample / len(ds.inspections) <= 0.28


def test_reinspections_reference_already_inspected_lines(starter):
    _, ds = starter
    initial = {}
    for i in ds.inspections:
        if i["inspection_kind"] == InspectionKind.INITIAL:
            assert i["shipment_line_id"] not in initial  # one INITIAL per line
            initial[i["shipment_line_id"]] = i
    re = [i for i in ds.inspections if i["inspection_kind"] == InspectionKind.REINSPECTION]
    assert len(re) == round(0.03 * 600)
    for r in re:
        first = initial[r["shipment_line_id"]]
        assert r["inspection_timestamp"] > first["inspection_timestamp"]


def test_inspection_timestamps_follow_original_arrival(starter):
    _, ds = starter
    arrived = _event(ds, E.ARRIVED)
    lines = _lines(ds)
    for i in ds.inspections:
        ts = i["inspection_timestamp"]
        assert ts.tzinfo is UTC
        assert i["inspection_timestamp"] <= i["recorded_at"] <= REF_END
        if i["inspection_kind"] != InspectionKind.INITIAL:
            continue
        sid = lines[i["shipment_line_id"]]["shipment_id"]
        # R12: the ORIGINAL ARRIVED event, not the corrected shipment row.
        assert timedelta(0) <= ts - arrived[sid]["event_timestamp"] <= timedelta(days=3)
    late = sum(
        i["recorded_at"] - i["inspection_timestamp"] > timedelta(days=1) for i in ds.inspections
    )
    assert late >= 1  # some inspections are recorded late, so the cutoff rule matters
    corrected = {e["shipment_id"] for e in ds.delivery_events if e["event_type"] == E.CORRECTION}
    assert any(lines[i["shipment_line_id"]]["shipment_id"] in corrected for i in ds.inspections)


def test_inspections_ignore_shipment_row_arrival(app_config):
    """R12: timestamps come from the ARRIVED event, so the (corrected) shipment row is unused."""
    cfg = GeneratorConfig.from_app_config(app_config, "starter")

    def run(scramble: bool):
        rs, ids, ds = RandomSource(cfg.seed), IdRegistry(), GeneratedDataset(config=cfg)
        generate_master_data(cfg, rs, ids, ds)
        generate_orders_and_shipments(cfg, rs, ids, ds)
        if scramble:
            for s in ds.shipments:
                if s["actual_delivery_timestamp"] is not None:
                    s["actual_delivery_timestamp"] = REF_START
        generate_inspections_and_invoices(cfg, rs, ids, ds)
        return ds.inspections

    assert run(scramble=True) == run(scramble=False)


def test_inspection_status_and_category_follow_defects(starter):
    _, ds = starter
    for i in ds.inspections:
        rate = i["defect_quantity"] / i["inspected_quantity"]
        want = (
            "FAILED"
            if rate > Decimal("0.05")
            else "CONDITIONAL"
            if rate > Decimal("0.02")
            else "PASSED"
        )
        assert i["inspection_status"] == want
        assert (i["defect_category"] == "NONE") == (i["defect_quantity"] == 0)
        assert i["inspection_source"].startswith("SYNTHETIC_")
    assert {i["inspection_status"] for i in ds.inspections} == {"PASSED", "CONDITIONAL", "FAILED"}


def test_defect_rate_tracks_profile(starter):
    _, ds = starter
    lines = _lines(ds)
    sup_of_shipment = {}
    po_sup = {p["po_id"]: p["supplier_id"] for p in ds.purchase_orders}
    for s in ds.shipments:
        sup_of_shipment[s["shipment_id"]] = po_sup[s["po_id"]]
    tot = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for i in ds.inspections:
        sup = sup_of_shipment[lines[i["shipment_line_id"]]["shipment_id"]]
        t = tot[ds.supplier_profiles[sup]]
        t[0] += i["defect_quantity"]
        t[1] += i["inspected_quantity"]
    rate = {p: float(d / n) for p, (d, n) in tot.items() if n}
    assert rate["quality_issues"] == pytest.approx(0.07, rel=0.3)
    assert rate["stable_high"] == pytest.approx(0.005, rel=0.5)
    assert rate["quality_issues"] > 5 * rate["stable_high"]


def test_not_enough_inspection_targets_raises(app_config):
    cfg = GeneratorConfig.from_app_config(app_config, "starter").model_copy(
        update={"n_inspections": 100_000}
    )
    with pytest.raises(GeneratorCapacityError):
        generate_dataset(cfg)


# --- invoices (D3) --------------------------------------------------------------------------


def test_invoice_expected_equals_received_times_price(starter):
    _, ds = starter
    price = {x["po_line_id"]: x["agreed_unit_price"] for x in ds.po_lines}
    expected = defaultdict(lambda: Decimal(0))
    for a in ds.shipment_lines:
        expected[a["shipment_id"]] += a["received_quantity"] * price[a["po_line_id"]]
    for inv in ds.invoices:
        want = expected[inv["shipment_id"]].quantize(Decimal("0.01"))
        assert inv["expected_amount"] == want
        assert inv["expected_amount"].as_tuple().exponent == -2


def test_invoice_variance_derived(starter):
    _, ds = starter
    over = under = 0
    for inv in ds.invoices:
        assert inv["variance_amount"] == inv["invoiced_amount"] - inv["expected_amount"]
        assert inv["expected_amount"] > 0
        want = (inv["variance_amount"] / inv["expected_amount"]).quantize(Decimal("0.000001"))
        assert inv["variance_percentage"] == want
        assert inv["invoiced_amount"] >= 0
        over += inv["variance_amount"] > 0
        under += inv["variance_amount"] < 0
    assert over > under > 0


def test_invoice_variance_frequency_tracks_profile(starter):
    _, ds = starter
    n = Counter()
    var = Counter()
    for inv in ds.invoices:
        p = ds.supplier_profiles[inv["supplier_id"]]
        n[p] += 1
        var[p] += inv["variance_amount"] != 0
    assert var["invoice_discrepancy"] / n["invoice_discrepancy"] > 0.25
    assert var["stable_high"] / n["stable_high"] < 0.10


def test_one_invoice_per_delivered_shipment_except_late_uninvoiced(starter):
    _, ds = starter
    received = _event(ds, E.RECEIVED)
    delivered = [s for s in ds.shipments if s["shipment_status"] in DELIVERED]
    invoiced = Counter(inv["shipment_id"] for inv in ds.invoices)
    assert max(invoiced.values()) == 1
    assert set(invoiced) <= {s["shipment_id"] for s in delivered}
    missing = [s for s in delivered if s["shipment_id"] not in invoiced]
    assert len(missing) == round(0.03 * len(delivered))
    last_invoiced = max(received[sid]["event_timestamp"] for sid in invoiced)
    assert all(received[s["shipment_id"]]["event_timestamp"] >= last_invoiced for s in missing)


def test_invoice_links_dates_and_numbers(starter):
    _, ds = starter
    received = _event(ds, E.RECEIVED)
    shipments = {s["shipment_id"]: s for s in ds.shipments}
    pos = {p["po_id"]: p for p in ds.purchase_orders}
    seen = set()
    for inv in ds.invoices:
        po = pos[shipments[inv["shipment_id"]]["po_id"]]
        assert inv["po_id"] == po["po_id"]
        assert inv["supplier_id"] == po["supplier_id"]
        assert inv["contract_id"] == po["contract_id"]
        assert inv["currency"] == po["currency"]
        got = received[inv["shipment_id"]]["event_timestamp"]
        assert got.date() <= inv["invoice_date"] <= REF_END.date()
        midnight = datetime.combine(inv["invoice_date"], datetime.min.time(), UTC)
        assert inv["recorded_at"] >= max(midnight, got)
        assert inv["recorded_at"] <= REF_END
        assert inv["created_at"] <= inv["recorded_at"]
        key = (inv["supplier_id"], inv["supplier_invoice_number"])
        assert key not in seen
        seen.add(key)


# --- risk signals ---------------------------------------------------------------------------


def test_risk_dedup_key_formula():
    # date(2025, 3, 15).toordinal() == 739325 -> week bucket 105617; 2025-03-16 starts 105618.
    sat = datetime(2025, 3, 15, 23, 0, tzinfo=UTC)
    sun = datetime(2025, 3, 16, 1, 0, tzinfo=UTC)
    assert (
        risk_dedup_key("SUP-000001", None, RiskSignalType.FACTORY_INCIDENT, sat)
        == "SUP-000001|*|FACTORY_INCIDENT|105617"
    )
    assert (
        risk_dedup_key("SUP-000001", "SITE-SUP-000001-01", "CARRIER_DELAY", sun)
        == "SUP-000001|SITE-SUP-000001-01|CARRIER_DELAY|105618"
    )
    with pytest.raises(ValueError):
        risk_dedup_key("SUP-000001", None, "CARRIER_DELAY", datetime(2025, 3, 15))


def test_risk_signals_are_synthetic(starter):
    _, ds = starter
    for r in ds.risk_signals:
        assert r["signal_source"].startswith("SYNTHETIC_")
        assert r["evidence_reference"] == f"synthetic://risk/{r['risk_signal_id']}"
        assert r["dedup_key"] == risk_dedup_key(
            r["supplier_id"], r["site_id"], r["signal_type"], r["observed_timestamp"]
        )


def test_risk_signal_times_and_resolution(starter):
    _, ds = starter
    sites = {s["site_id"]: s["supplier_id"] for s in ds.sites}
    for r in ds.risk_signals:
        assert REF_START <= r["observed_timestamp"] <= r["recorded_timestamp"] <= REF_END
        assert r["valid_from"] == r["observed_timestamp"]
        if r["resolution_status"] == ResolutionStatus.RESOLVED:
            assert r["valid_from"] <= r["valid_to"] <= REF_END
        else:
            assert r["valid_to"] is None
        if r["site_id"] is not None:
            assert sites[r["site_id"]] == r["supplier_id"]
    risks = [
        r for r in ds.risk_signals if r["signal_type"] != RiskSignalType.MONITORING_ATTESTATION
    ]
    statuses = Counter(r["resolution_status"] for r in risks)
    assert set(statuses) == set(ResolutionStatus)
    mid = REF_START + (REF_END - REF_START) / 2
    old = [r for r in risks if r["observed_timestamp"] < mid]
    new = [r for r in risks if r["observed_timestamp"] >= mid]

    def share(rows):
        return sum(r["resolution_status"] == ResolutionStatus.RESOLVED for r in rows) / len(rows)

    assert share(old) > share(new)


def test_monitoring_attestations_quarterly_for_about_70_percent(starter):
    _, ds = starter
    att = [r for r in ds.risk_signals if r["signal_type"] == RiskSignalType.MONITORING_ATTESTATION]
    assert all(r["severity"] == Severity.NONE for r in att)
    by_sup = defaultdict(set)
    for r in att:
        ts = r["observed_timestamp"]
        by_sup[r["supplier_id"]].add((ts.year, (ts.month - 1) // 3))
    assert len(by_sup) == round(0.7 * 50)
    assert all(len(q) == 6 for q in by_sup.values())  # every quarter 2025Q1..2026Q2
    others = [
        r for r in ds.risk_signals if r["signal_type"] != RiskSignalType.MONITORING_ATTESTATION
    ]
    assert all(r["severity"] != Severity.NONE for r in others)


def _strip(row):
    return {k: v for k, v in row.items() if k not in ("risk_signal_id", "evidence_reference")}


def _key(row):
    return tuple(sorted(_strip(row).items()))


@pytest.mark.parametrize("seed", [20261022, 7, 424242])
def test_duplicate_signals_pair_with_their_original(app_config, seed):
    """Duplicates are identified by the generator's own plan, so coincidental collisions of
    independent signals cannot break the assertions under any seed."""
    cfg = GeneratorConfig.from_app_config(app_config, "starter", seed=seed)
    rs, ids, ds = RandomSource(cfg.seed), IdRegistry(), GeneratedDataset(config=cfg)
    generate_master_data(cfg, rs, ids, ds)
    generate_orders_and_shipments(cfg, rs, ids, ds)
    generate_inspections_and_invoices(cfg, rs, ids, ds)
    plan = plan_risk_signals(cfg, rs, ds)

    assert len(plan.duplicates) == round(0.05 * len(plan.originals))
    assert len({i for i, _ in plan.duplicates}) == len(plan.duplicates)
    for i, dup in plan.duplicates:
        orig = plan.originals[i]
        for k in ("supplier_id", "site_id", "signal_type", "related_shipment_id"):
            assert dup[k] == orig[k], k
        assert dup["related_contract_id"] == orig["related_contract_id"]
        assert dup["dedup_key"] == orig["dedup_key"]
        assert timedelta(0) <= dup["observed_timestamp"] - orig["observed_timestamp"]
        assert dup["observed_timestamp"] - orig["observed_timestamp"] <= timedelta(days=2)
        assert dup["signal_source"] != orig["signal_source"]
        # Every type has >= 2 own sources, so the copy comes from the same type's sources.
        assert dup["signal_source"] in RISK_SOURCES[orig["signal_type"]]
        assert dup["signal_source"].startswith("SYNTHETIC_")

    # The plan is exactly what generate_dataset loads (ids aside).
    planned = [*plan.originals, *(d for _, d in plan.duplicates), *plan.attestations]
    generated = generate_dataset(cfg).risk_signals
    assert Counter(map(_key, generated)) == Counter(map(_key, planned))


def test_duplicate_source_pool_prefers_same_type_then_falls_back():
    t = RiskSignalType
    assert duplicate_source_pool(t.CARRIER_DELAY, "SYNTHETIC_CARRIER_FEED") == [
        "SYNTHETIC_LOGISTICS_MONITOR"
    ]
    sources = {
        t.CARRIER_DELAY: ("SYNTHETIC_A",),
        t.FACTORY_INCIDENT: ("SYNTHETIC_C", "SYNTHETIC_B"),
    }
    assert duplicate_source_pool(t.CARRIER_DELAY, "SYNTHETIC_A", sources) == [
        "SYNTHETIC_B",
        "SYNTHETIC_C",
    ]


def test_risk_rate_tracks_profile(starter):
    _, ds = starter
    n_sup = Counter(ds.supplier_profiles.values())
    n = Counter(
        ds.supplier_profiles[r["supplier_id"]]
        for r in ds.risk_signals
        if r["signal_type"] != RiskSignalType.MONITORING_ATTESTATION
    )
    per = {p: n[p] / n_sup[p] for p in n_sup}
    # 6/yr x 1.5 yr = 9 vs 0.5/yr x 1.5 yr = 0.75 expected signals per supplier
    assert per["disruption_prone"] > 4 * per["stable_high"]
    assert per["disruption_prone"] == pytest.approx(9 * 1.05, rel=0.5)

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from supplier_reliability.database import make_engine
from supplier_reliability.domain.enums import (
    DeliveryEventType,
    is_valid_transition,
    validate_sequence,
)
from supplier_reliability.domain.numeric import to_money
from supplier_reliability.models import Base
from supplier_reliability.models.master import Contract, ContractPrice, Site, Supplier
from supplier_reliability.models.transactions import (
    DeliveryEvent,
    POLine,
    PurchaseOrder,
    Shipment,
    ShipmentLineAllocation,
)
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.constants import PRODUCT_CATEGORIES
from supplier_reliability.services.generator.dataset import GeneratedDataset
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.master_data import (
    expected_monthly_demand,
    generate_master_data,
)
from supplier_reliability.services.generator.rng import RandomSource
from supplier_reliability.services.generator.transactions import generate_orders_and_shipments

E = DeliveryEventType
REF_START = datetime(2025, 1, 1, tzinfo=UTC)
REF_END = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


def _grace(ds):
    return timedelta(hours=ds.config.grace_hours)


def _run(app_config, preset="starter", seed=None, **overrides):
    cfg = GeneratorConfig.from_app_config(app_config, preset, seed)
    if overrides:
        cfg = cfg.model_copy(update=overrides)
    rs = RandomSource(cfg.seed)
    ids = IdRegistry()
    ds = GeneratedDataset(config=cfg)
    generate_master_data(cfg, rs, ids, ds)
    generate_orders_and_shipments(cfg, rs, ids, ds)
    return cfg, ds


@pytest.fixture(scope="module")
def starter(app_config):
    return _run(app_config)


@pytest.fixture(scope="module")
def full(app_config):
    return _run(app_config, "full")


def _events_by_shipment(ds):
    out = defaultdict(list)
    for e in ds.delivery_events:
        out[e["shipment_id"]].append(e)
    for evs in out.values():
        evs.sort(key=lambda e: (e["event_timestamp"], e["delivery_event_id"]))
    return out


def _late_rates(ds, profile):
    """Late share of delivered shipments by expected-delivery half-year, for one profile."""
    pos = {p["po_id"]: p for p in ds.purchase_orders}
    counts = defaultdict(lambda: [0, 0])  # half -> [late, delivered]
    for s in ds.shipments:
        sid = pos[s["po_id"]]["supplier_id"]
        if ds.supplier_profiles[sid] != profile or s["actual_delivery_timestamp"] is None:
            continue
        exp = s["expected_delivery_timestamp"]
        half = (exp.year, 1 if exp.month <= 6 else 2)
        counts[half][1] += 1
        if s["actual_delivery_timestamp"] > exp + _grace(ds):
            counts[half][0] += 1
    return {h: late / n for h, (late, n) in counts.items()}, counts


# --- brief tests ------------------------------------------------------------------------


def test_po_and_shipment_counts_exact(starter):
    cfg, ds = starter
    assert len(ds.purchase_orders) == 300 == cfg.n_purchase_orders
    assert len(ds.shipments) == 450 == cfg.n_shipments
    assert len({p["po_id"] for p in ds.purchase_orders}) == 300
    assert len({s["shipment_id"] for s in ds.shipments}) == 450


def test_full_preset_counts_exact(full):
    _, ds = full
    assert len(ds.purchase_orders) == 10_000
    assert len(ds.shipments) == 15_000


def test_cancelled_pos_have_no_shipments(starter):
    _, ds = starter
    n_ship = defaultdict(int)
    for s in ds.shipments:
        n_ship[s["po_id"]] += 1
    cancelled = [p for p in ds.purchase_orders if p["po_status"] == "CANCELLED"]
    assert cancelled
    assert 0.01 <= len(cancelled) / len(ds.purchase_orders) <= 0.03
    for p in ds.purchase_orders:
        if p["po_status"] == "CANCELLED":
            assert n_ship[p["po_id"]] == 0
        else:
            assert 1 <= n_ship[p["po_id"]] <= 4


def test_lines_per_po_between_1_and_5_with_mean_near_2_6(full):
    _, ds = full
    per = defaultdict(list)
    for line in ds.po_lines:
        per[line["po_id"]].append(line["line_number"])
    assert set(per) == {p["po_id"] for p in ds.purchase_orders}
    for numbers in per.values():
        assert sorted(numbers) == list(range(1, len(numbers) + 1)) and len(numbers) <= 5
    assert sum(len(v) for v in per.values()) / len(per) == pytest.approx(2.6, abs=0.1)


def test_line_amount_equals_qty_times_price(starter):
    _, ds = starter
    pos = {p["po_id"]: p for p in ds.purchase_orders}
    contracts = {c["contract_id"]: c for c in ds.contracts}
    suppliers = {s["supplier_id"]: s for s in ds.suppliers}
    prices = {(p["contract_id"], p["product_category"]): p for p in ds.contract_prices}
    assert ds.po_lines
    for line in ds.po_lines:
        po = pos[line["po_id"]]
        contract = contracts[po["contract_id"]]
        price = prices[(contract["contract_id"], line["product_category"])]
        assert isinstance(line["ordered_quantity"], Decimal)
        assert isinstance(line["agreed_unit_price"], Decimal)
        assert line["ordered_quantity"] > 0
        assert line["agreed_unit_price"] == price["unit_price"]
        assert line["unit_of_measure"] == price["unit_of_measure"]
        uoms = {
            p: u
            for p, u, _ in PRODUCT_CATEGORIES[suppliers[po["supplier_id"]]["supplier_category"]]
        }
        assert line["unit_of_measure"] == uoms[line["product_category"]]
        assert line["line_amount"] == to_money(line["ordered_quantity"] * line["agreed_unit_price"])
        assert po["currency"] == contract["currency"]


def test_po_uses_contract_active_at_order_date(starter):
    _, ds = starter
    contracts = {c["contract_id"]: c for c in ds.contracts}
    last_order = (REF_END - timedelta(days=10)).date()
    assert ds.purchase_orders
    for po in ds.purchase_orders:
        c = contracts[po["contract_id"]]
        assert c["supplier_id"] == po["supplier_id"]
        assert c["effective_from"] <= po["order_date"]
        assert c["effective_to"] is None or po["order_date"] <= c["effective_to"]
        assert REF_START.date() <= po["order_date"] <= last_order
        lead = (po["required_delivery_date"] - po["order_date"]).days
        assert 14 <= lead <= 45
        assert po["created_at"].date() == po["order_date"]


def test_shipped_never_exceeds_ordered_per_po_line(starter):
    _, ds = starter
    alloc = defaultdict(Decimal)
    shipped = defaultdict(Decimal)
    for a in ds.shipment_lines:
        alloc[a["po_line_id"]] += a["allocated_quantity"]
        shipped[a["po_line_id"]] += a["shipped_quantity"]
    cancelled = {p["po_id"] for p in ds.purchase_orders if p["po_status"] == "CANCELLED"}
    for line in ds.po_lines:
        assert shipped[line["po_line_id"]] <= alloc[line["po_line_id"]] <= line["ordered_quantity"]
        if line["po_id"] not in cancelled:
            assert alloc[line["po_line_id"]] > 0
    split = defaultdict(int)
    for a in ds.shipment_lines:
        split[a["po_line_id"]] += 1
    assert max(split.values()) <= 4
    assert any(n > 1 for n in split.values())


def test_allocations_reconcile_received_le_shipped_le_allocated(starter):
    _, ds = starter
    ships = {s["shipment_id"]: s for s in ds.shipments}
    evs = _events_by_shipment(ds)
    keys = set()
    for a in ds.shipment_lines:
        assert (a["shipment_id"], a["po_line_id"]) not in keys
        keys.add((a["shipment_id"], a["po_line_id"]))
        assert 0 <= a["received_quantity"] <= a["shipped_quantity"] <= a["allocated_quantity"]
        by_type = {e["event_type"]: e for e in evs[a["shipment_id"]]}
        assert a["allocated_at"] == by_type[E.BOOKED]["event_timestamp"]
        assert a["shipped_at"] == ships[a["shipment_id"]]["dispatch_timestamp"]
        if E.RECEIVED in by_type:
            assert a["received_at"] == by_type[E.RECEIVED]["event_timestamp"]
        else:
            assert a["received_at"] is None
            assert a["received_quantity"] == 0
    short = [a for a in ds.shipment_lines if a["shipped_quantity"] < a["allocated_quantity"]]
    assert 0.01 <= len(short) / len(ds.shipment_lines) <= 0.06


def test_allocation_uom_matches_po_line(starter):
    _, ds = starter
    lines = {x["po_line_id"]: x for x in ds.po_lines}
    ships = {s["shipment_id"]: s for s in ds.shipments}
    assert ds.shipment_lines
    for a in ds.shipment_lines:
        line = lines[a["po_line_id"]]
        assert a["unit_of_measure"] == line["unit_of_measure"]
        assert ships[a["shipment_id"]]["po_id"] == line["po_id"]


def test_shipment_totals_equal_sum_of_lines(starter):
    _, ds = starter
    shipped = defaultdict(Decimal)
    received = defaultdict(Decimal)
    n = defaultdict(int)
    assert ds.shipments
    for a in ds.shipment_lines:
        shipped[a["shipment_id"]] += a["shipped_quantity"]
        received[a["shipment_id"]] += a["received_quantity"]
        n[a["shipment_id"]] += 1
    for s in ds.shipments:
        assert n[s["shipment_id"]] >= 1
        assert s["shipped_quantity"] == shipped[s["shipment_id"]]
        assert s["received_quantity"] == received[s["shipment_id"]]


def test_corrections_reference_earlier_event_of_same_shipment(starter):
    _, ds = starter
    by_id = {e["delivery_event_id"]: e for e in ds.delivery_events}
    ships = {s["shipment_id"]: s for s in ds.shipments}
    evs = _events_by_shipment(ds)
    corrections = [e for e in ds.delivery_events if e["event_type"] == E.CORRECTION]
    delivered = [s for s in ds.shipments if s["actual_delivery_timestamp"] is not None]
    assert corrections
    assert len(corrections) <= max(2, round(0.03 * len(delivered)))
    for c in corrections:
        meta = c["event_metadata"]
        assert set(meta) == {"corrects_event_id", "field", "corrected_value"}
        target = by_id[meta["corrects_event_id"]]
        assert target["shipment_id"] == c["shipment_id"]
        assert target["event_type"] == E.ARRIVED
        assert meta["field"] == "event_timestamp"
        assert target["event_timestamp"] <= c["event_timestamp"]
        assert target["recorded_timestamp"] < c["recorded_timestamp"]
        corrected = datetime.fromisoformat(meta["corrected_value"])
        assert corrected.utcoffset() == timedelta(0)
        delta = abs(corrected - target["event_timestamp"])
        assert timedelta(hours=1) <= delta <= timedelta(hours=48)
        by_type = {e["event_type"]: e for e in evs[c["shipment_id"]]}
        assert corrected >= by_type[E.DISPATCHED]["event_timestamp"]
        assert corrected <= REF_END
        # Shipment row is the current-state view: it holds the corrected value.
        assert ships[c["shipment_id"]]["actual_delivery_timestamp"] == corrected


def test_deteriorating_profile_late_rate_increases(full):
    _, ds = full
    rates, counts = _late_rates(ds, "deteriorating_lead_time")
    assert counts[(2025, 1)][1] >= 100 and counts[(2026, 1)][1] >= 100
    assert rates[(2026, 1)] > rates[(2025, 1)] + 0.15


def test_recovering_profile_late_rate_decreases(full):
    _, ds = full
    rates, counts = _late_rates(ds, "recovering")
    assert counts[(2025, 1)][1] >= 50 and counts[(2026, 1)][1] >= 50
    assert rates[(2026, 1)] < rates[(2025, 1)] - 0.15


def test_stable_profile_late_rate_flat_and_low(full):
    _, ds = full
    rates, _ = _late_rates(ds, "stable_high")
    assert rates[(2025, 1)] < 0.08 and rates[(2026, 1)] < 0.08


def test_event_sequence_follows_transitions(starter):
    _, ds = starter
    ships = {s["shipment_id"]: s for s in ds.shipments}
    evs = _events_by_shipment(ds)
    assert ships
    assert set(evs) == set(ships)
    for sid, events in evs.items():
        types = [e["event_type"] for e in events]
        assert validate_sequence(types), (sid, types)
        prev = None
        for t in types:
            assert is_valid_transition(prev, t), (sid, types)
            if t is not E.CORRECTION:
                prev = t
        s = ships[sid]
        by_type = {e["event_type"]: e for e in events}
        assert by_type[E.DISPATCHED]["event_timestamp"] == s["dispatch_timestamp"]
        assert by_type[E.BOOKED]["event_timestamp"] >= s["created_at"]


def test_events_within_period_and_recorded_not_before_event(starter):
    _, ds = starter
    pos = {p["po_id"]: p for p in ds.purchase_orders}
    ships = {s["shipment_id"]: s for s in ds.shipments}
    lagged = 0
    for e in ds.delivery_events:
        assert e["event_timestamp"].tzinfo is UTC and e["recorded_timestamp"].tzinfo is UTC
        assert e["event_timestamp"] <= e["recorded_timestamp"] <= REF_END
        order_date = pos[ships[e["shipment_id"]]["po_id"]]["order_date"]
        assert e["event_timestamp"].date() >= order_date
        if e["recorded_timestamp"] - e["event_timestamp"] >= timedelta(days=1):
            lagged += 1
    assert 0.02 <= lagged / len(ds.delivery_events) <= 0.09


def _assert_delayed_matches_arrived_event(ds):
    """DELAYED exists iff the ARRIVED *event* (originally recorded arrival) is past grace."""
    grace = _grace(ds)
    ships = {s["shipment_id"]: s for s in ds.shipments}
    n_delayed = 0
    for sid, events in _events_by_shipment(ds).items():
        s = ships[sid]
        by_type = {e["event_type"]: e for e in events}
        if E.DELAYED in by_type:
            n_delayed += 1
            if E.ARRIVED in by_type:
                arrived = by_type[E.ARRIVED]["event_timestamp"]
                assert by_type[E.DELAYED]["event_timestamp"] < arrived
                assert arrived > s["expected_delivery_timestamp"] + grace
        elif E.ARRIVED in by_type:
            assert by_type[E.ARRIVED]["event_timestamp"] <= s["expected_delivery_timestamp"] + grace
    return n_delayed


def test_delayed_event_only_for_late_shipments(starter):
    _, ds = starter
    assert _assert_delayed_matches_arrived_event(ds) > 0


def test_in_transit_shipments_have_no_arrival(starter):
    _, ds = starter
    evs = _events_by_shipment(ds)
    in_transit = [s for s in ds.shipments if s["shipment_status"] == "IN_TRANSIT"]
    assert in_transit
    for s in ds.shipments:
        types = {e["event_type"] for e in evs[s["shipment_id"]]}
        if s["shipment_status"] == "IN_TRANSIT":
            assert s["actual_delivery_timestamp"] is None
            assert E.ARRIVED not in types and E.RECEIVED not in types
            assert s["received_quantity"] == 0
        else:
            assert s["shipment_status"] in {"DELIVERED", "PARTIALLY_RECEIVED"}
            assert {E.ARRIVED, E.RECEIVED} <= types
            assert s["actual_delivery_timestamp"] is not None
            expect = (
                "DELIVERED"
                if s["received_quantity"] == s["shipped_quantity"]
                else ("PARTIALLY_RECEIVED")
            )
            assert s["shipment_status"] == expect
        assert s["expected_delivery_timestamp"] > s["dispatch_timestamp"]
        transit = s["expected_delivery_timestamp"] - s["dispatch_timestamp"]
        assert timedelta(days=2) <= transit <= timedelta(days=10)


def test_dispatch_site_belongs_to_supplier_and_is_effective(starter):
    _, ds = starter
    sites = {s["site_id"]: s for s in ds.sites}
    pos = {p["po_id"]: p for p in ds.purchase_orders}
    assert ds.shipments
    for s in ds.shipments:
        site = sites[s["originating_site_id"]]
        assert site["supplier_id"] == pos[s["po_id"]]["supplier_id"]
        assert site["site_status"] == "ACTIVE"
        d = s["dispatch_timestamp"].date()
        assert site["effective_from"] <= d
        assert site["effective_to"] is None or d <= site["effective_to"]


def test_some_partial_deliveries_exist(starter):
    _, ds = starter
    received = defaultdict(Decimal)
    for a in ds.shipment_lines:
        received[a["po_line_id"]] += a["received_quantity"]
    ships_by_po = defaultdict(list)
    for s in ds.shipments:
        ships_by_po[s["po_id"]].append(s)
    lines_by_po = defaultdict(list)
    for line in ds.po_lines:
        lines_by_po[line["po_id"]].append(line)
    done_lines = short_lines = 0
    for po in ds.purchase_orders:
        ships = ships_by_po[po["po_id"]]
        if not ships:
            assert po["po_status"] == "CANCELLED"
            continue
        all_done = all(s["actual_delivery_timestamp"] is not None for s in ships)
        none_done = all(s["actual_delivery_timestamp"] is None for s in ships)
        shortfall = any(
            received[x["po_line_id"]] < x["ordered_quantity"] for x in lines_by_po[po["po_id"]]
        )
        if none_done:
            assert po["po_status"] == "OPEN"
        elif all_done and not shortfall:
            assert po["po_status"] == "RECEIVED"
        else:
            assert po["po_status"] == "PARTIALLY_RECEIVED"
        if all_done:
            for x in lines_by_po[po["po_id"]]:
                done_lines += 1
                short_lines += received[x["po_line_id"]] < x["ordered_quantity"]
    assert any(p["po_status"] == "PARTIALLY_RECEIVED" for p in ds.purchase_orders)
    assert 0.015 <= short_lines / done_lines <= 0.08


# --- controller-requested tests ---------------------------------------------------------


def test_realised_demand_matches_expected_monthly_demand(app_config, starter, full):
    for cfg, ds, min_pos in (starter + (0,), full + (200,)):
        total_w = sum(cfg.profiles[p].po_weight for p in ds.supplier_profiles.values())
        sup_profile = ds.supplier_profiles
        pos = {p["po_id"]: p["supplier_id"] for p in ds.purchase_orders}
        qty = defaultdict(Decimal)
        n_po = defaultdict(int)
        for line in ds.po_lines:
            qty[sup_profile[pos[line["po_id"]]]] += line["ordered_quantity"]
        for sid in pos.values():
            n_po[sup_profile[sid]] += 1
        n_sup = defaultdict(int)
        for p in sup_profile.values():
            n_sup[p] += 1
        n_months = 18
        if cfg.preset == "starter":
            # Starter has too few POs per profile (2-13 suppliers) for a per-profile check;
            # compare the whole population instead.
            realised = sum(qty.values()) / n_months
            expected = sum(
                expected_monthly_demand(
                    cfg.profiles[p],
                    cfg.n_purchase_orders * cfg.profiles[p].po_weight / total_w,
                    cfg,
                )
                for p in sup_profile.values()
            )
            assert float(realised / expected) == pytest.approx(1.0, rel=0.15)
            continue
        checked = 0
        for name, prof in cfg.profiles.items():
            if n_po[name] < min_pos:
                continue  # sparse_history (~50 POs in total) is too noisy for a 15% band
            expected = expected_monthly_demand(
                prof, cfg.n_purchase_orders * prof.po_weight / total_w, cfg
            )
            realised = qty[name] / n_months / n_sup[name]
            assert float(realised / expected) == pytest.approx(1.0, rel=0.15), name
            checked += 1
        assert checked >= 9


def test_row_keys_equal_model_columns(starter):
    _, ds = starter
    for table, model in (
        ("purchase_orders", PurchaseOrder),
        ("po_lines", POLine),
        ("shipments", Shipment),
        ("shipment_lines", ShipmentLineAllocation),
        ("delivery_events", DeliveryEvent),
    ):
        cols = {c.key for c in sa.inspect(model).columns}
        rows = getattr(ds, table)
        assert rows
        for row in rows:
            assert set(row) == cols, table


def test_id_formats(starter):
    _, ds = starter
    assert ds.purchase_orders[0]["po_id"] == "PO-2026-000001"
    assert ds.po_lines[0]["po_line_id"] == "POL-2026-000001"
    assert all(s["shipment_id"].startswith("SHP-2026-") for s in ds.shipments)
    assert all(a["shipment_line_id"].startswith("SHL-2026-") for a in ds.shipment_lines)
    assert all(e["delivery_event_id"].startswith("EVT-2026-") for e in ds.delivery_events)
    dates = [p["order_date"] for p in ds.purchase_orders]
    assert dates == sorted(dates)


def test_rows_satisfy_database_constraints(starter, tmp_path):
    _, ds = starter
    engine = make_engine(f"sqlite:///{tmp_path}/tx.db")  # FK pragma ON
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for model, rows in (
            (Supplier, ds.suppliers),
            (Site, ds.sites),
            (Contract, ds.contracts),
            (ContractPrice, ds.contract_prices),
            (PurchaseOrder, ds.purchase_orders),
            (POLine, ds.po_lines),
            (Shipment, ds.shipments),
            (ShipmentLineAllocation, ds.shipment_lines),
            (DeliveryEvent, ds.delivery_events),
        ):
            conn.execute(sa.insert(model), rows)
        assert conn.scalar(sa.select(sa.func.count()).select_from(DeliveryEvent)) == len(
            ds.delivery_events
        )
    engine.dispose()


def test_orders_and_shipments_deterministic(app_config, starter):
    _, a = starter
    _, b = _run(app_config)
    for t in ("purchase_orders", "po_lines", "shipments", "shipment_lines", "delivery_events"):
        assert getattr(a, t) == getattr(b, t), t
    _, c = _run(app_config, seed=7)
    assert c.shipments != a.shipments


# --- fix round 1 ------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["starter", "full"])
def test_no_zero_quantity_allocations_and_no_empty_shipments(request, preset):
    _, ds = request.getfixturevalue(preset)
    assert min(a["allocated_quantity"] for a in ds.shipment_lines) >= 1
    assert min(a["shipped_quantity"] for a in ds.shipment_lines) >= 1
    n = defaultdict(int)
    for a in ds.shipment_lines:
        n[a["shipment_id"]] += 1
    assert all(n[s["shipment_id"]] >= 1 for s in ds.shipments)


def test_line_memberships_respect_quantity_and_cover_every_shipment():
    import numpy as np

    from supplier_reliability.services.generator.transactions import plan_line_memberships

    rng = np.random.default_rng(5)
    cases = [([1], 1), ([1, 1], 2), ([1, 3], 4), ([2, 1, 1], 4), ([1, 1, 1, 1, 1], 4), ([4], 4)]
    for _ in range(300):
        qtys = [int(q) for q in rng.integers(1, 4, size=int(rng.integers(1, 6)))]
        cases.append((qtys, int(rng.integers(1, min(4, sum(qtys)) + 1))))
    for qtys, k in cases:
        members = plan_line_memberships(rng, qtys, k)
        assert len(members) == len(qtys)
        for q, m in zip(qtys, members, strict=True):
            assert 1 <= len(m) <= q
            assert len(set(m)) == len(m) and all(0 <= j < k for j in m)
        assert {j for m in members for j in m} == set(range(k)), (qtys, k, members)


def test_cap_shipment_counts_keeps_total_and_respects_caps():
    import numpy as np

    from supplier_reliability.services.generator.transactions import cap_shipment_counts

    rng = np.random.default_rng(3)
    counts = np.array([4, 4, 1, 2, 3])
    caps = np.array([1, 2, 4, 4, 4])
    out = cap_shipment_counts(counts, caps, rng)
    assert int(out.sum()) == int(counts.sum())
    assert (out >= 1).all() and (out <= caps).all()
    same = cap_shipment_counts(np.array([2, 3]), np.array([4, 4]), rng)
    assert same.tolist() == [2, 3]


def test_generator_config_grace_hours_comes_from_scoring_config(app_config):
    cfg = GeneratorConfig.from_app_config(app_config, "starter")
    assert cfg.grace_hours == app_config.scoring.grace_hours
    data = app_config.model_dump(mode="json")
    data["scoring"]["grace_hours"] = app_config.scoring.grace_hours + 24
    changed = GeneratorConfig.from_app_config(type(app_config).model_validate(data), "starter")
    assert changed.grace_hours == app_config.scoring.grace_hours + 24
    assert changed.config_hash() != cfg.config_hash()


def test_late_boundary_follows_configured_grace(app_config):
    grace_hours = app_config.scoring.grace_hours + 48
    _, ds = _run(app_config, grace_hours=grace_hours)
    assert ds.config.grace_hours == grace_hours
    assert _assert_delayed_matches_arrived_event(ds) > 0


def test_exact_subset_is_public_in_profiles():
    import numpy as np

    from supplier_reliability.services.generator.profiles import exact_subset

    a = exact_subset(np.random.default_rng(1), 20, 5)
    expected = sorted(int(i) for i in np.random.default_rng(1).permutation(20)[:5])
    assert a == expected


def test_corrections_can_cross_late_boundary_but_events_stay_consistent(full):
    _, ds = full
    grace = _grace(ds)
    by_id = {e["delivery_event_id"]: e for e in ds.delivery_events}
    ships = {s["shipment_id"]: s for s in ds.shipments}
    evs = _events_by_shipment(ds)
    crossed = 0
    for c in ds.delivery_events:
        if c["event_type"] != E.CORRECTION:
            continue
        s = ships[c["shipment_id"]]
        deadline = s["expected_delivery_timestamp"] + grace
        original = by_id[c["event_metadata"]["corrects_event_id"]]["event_timestamp"]
        corrected = s["actual_delivery_timestamp"]
        has_delayed = any(e["event_type"] == E.DELAYED for e in evs[s["shipment_id"]])
        assert has_delayed == (original > deadline)  # DELAYED follows the ARRIVED event
        if (original > deadline) != (corrected > deadline):
            crossed += 1
    assert crossed > 0
    _assert_delayed_matches_arrived_event(ds)


def test_end_of_period_dispatches_not_clustered(full):
    _, ds = full
    cap = REF_END - timedelta(days=1)
    window = timedelta(hours=72)
    start, end = REF_START + timedelta(days=60), REF_END - timedelta(days=60)
    mid = sum(start <= s["dispatch_timestamp"] < end for s in ds.shipments)
    per_window = mid / ((end - start) / window)
    last = sum(cap - window < s["dispatch_timestamp"] <= cap for s in ds.shipments)
    assert last <= 1.5 * per_window, (last, per_window)

"""Task 19: supplier, purchase-order and shipment endpoints over the seeded starter dataset.

Expected values come from independent SQL against the same database copy, never from the API
under test."""

import re
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from supplier_reliability.domain.enums import (
    Region,
    ReliabilityCategory,
    ShipmentStatus,
    SupplierCategory,
    SupplierStatus,
)
from supplier_reliability.models import PurchaseOrder, Shipment, Supplier, SupplierScore

Z_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
MONEY = re.compile(r"^-?\d+\.\d{2}$")


def session(client):
    return client.app.state.session_factory()


def all_pages(client, url, **params):
    items, offset = [], 0
    while True:
        body = client.get(url, params={**params, "limit": 500, "offset": offset}).json()
        items.extend(body["items"])
        offset += 500
        if offset >= body["total"]:
            return items


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- suppliers: list -------------------------------------------------------------------------


def test_list_suppliers_paginated(api_client):
    r = api_client.get("/api/v1/suppliers", params={"limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 10
    assert body["total"] == 50 and body["limit"] == 10 and body["offset"] == 0
    first = body["items"][0]
    assert first["supplier_id"] == "SUP-000001"  # default sort: supplier_id asc
    assert {"supplier_name", "supplier_category", "region", "supplier_status"} <= set(first)
    assert Z_TIMESTAMP.match(first["created_at"])


def test_default_page_size_is_50(api_client):
    body = api_client.get("/api/v1/suppliers").json()
    assert body["limit"] == 50 and len(body["items"]) == 50


def test_pages_do_not_overlap_and_cover_everything(api_client):
    ids = []
    for offset in range(0, 50, 20):
        page = api_client.get("/api/v1/suppliers", params={"limit": 20, "offset": offset})
        ids += [i["supplier_id"] for i in page.json()["items"]]
    assert len(ids) == 50 and len(set(ids)) == 50


def test_limit_above_max_is_422(api_client):
    r = api_client.get("/api/v1/suppliers", params={"limit": 501})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"][0]["loc"][-1] == "limit"


@pytest.mark.parametrize("params", [{"limit": 0}, {"offset": -1}, {"limit": "abc"}])
def test_bad_pagination_is_422(api_client, params):
    assert api_client.get("/api/v1/suppliers", params=params).status_code == 422


def test_offset_past_end_returns_empty_page(api_client):
    r = api_client.get("/api/v1/suppliers", params={"offset": 1000})
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 50, "limit": 50, "offset": 1000}


def test_unknown_sort_field_422(api_client):
    r = api_client.get("/api/v1/suppliers", params={"sort": "password:asc"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["details"][0]["loc"] == ["query", "sort"]
    assert "password" not in r.text  # raw input is never echoed


@pytest.mark.parametrize("sort", ["overall_score", "overall_score:sideways", "a:b:c"])
def test_malformed_sort_422(api_client, sort):
    assert api_client.get("/api/v1/suppliers", params={"sort": sort}).status_code == 422


def test_sort_by_overall_score_desc_with_stable_tiebreak(api_client):
    items = api_client.get(
        "/api/v1/suppliers", params={"sort": "overall_score:desc", "limit": 50}
    ).json()["items"]

    def score_of(item):
        return item["latest_score"] and item["latest_score"]["overall_score"]

    scored = [i for i in items if score_of(i) is not None]
    values = [score_of(i) for i in scored]
    assert values == sorted(values, reverse=True) and len(values) >= 40
    # ties on the score break by supplier_id ascending
    for a, b in zip(scored, scored[1:], strict=False):
        if score_of(a) == score_of(b):
            assert a["supplier_id"] < b["supplier_id"]
    # suppliers without a score are listed last
    assert items[len(scored) :] == [i for i in items if score_of(i) is None]


def test_sort_by_name_ascending(api_client):
    items = api_client.get(
        "/api/v1/suppliers", params={"sort": "supplier_name:asc", "limit": 50}
    ).json()["items"]
    names = [i["supplier_name"] for i in items]
    assert names == sorted(names)


def test_list_embeds_latest_current_score(api_client):
    with session(api_client) as s:
        expected = dict(
            s.execute(
                sa.text(
                    "SELECT supplier_id, score_id FROM supplier_scores sc WHERE is_current = 1 "
                    "AND site_id IS NULL AND data_cutoff_timestamp = ("
                    "SELECT MAX(data_cutoff_timestamp) FROM supplier_scores x "
                    "WHERE x.supplier_id = sc.supplier_id AND x.is_current = 1 "
                    "AND x.site_id IS NULL)"
                )
            ).all()
        )
    items = api_client.get("/api/v1/suppliers", params={"limit": 50}).json()["items"]
    got = {i["supplier_id"]: i["latest_score"]["score_id"] for i in items if i["latest_score"]}
    assert got == expected and len(got) > 0
    sample = next(i for i in items if i["latest_score"])["latest_score"]
    assert {"overall_score", "reliability_category", "decision", "data_cutoff"} <= set(sample)


def _supplier_rows(client):
    with session(client) as s:
        return s.execute(sa.select(Supplier)).scalars().all()


def test_filter_region_category_status(api_client):
    rows = _supplier_rows(api_client)
    first = rows[0]
    cases = [
        ("region", first.region, "region"),
        ("category", first.supplier_category, "supplier_category"),
        ("status", first.supplier_status, "supplier_status"),
    ]
    for param, value, attr in cases:
        body = api_client.get("/api/v1/suppliers", params={param: value.value}).json()
        want = sorted(r.supplier_id for r in rows if getattr(r, attr) == value)
        assert body["total"] == len(want) > 0
        assert sorted(i["supplier_id"] for i in body["items"]) == want


def test_filter_combines_with_and(api_client):
    rows = _supplier_rows(api_client)
    r0 = rows[0]
    body = api_client.get(
        "/api/v1/suppliers",
        params={"region": r0.region.value, "category": r0.supplier_category.value},
    ).json()
    want = [
        r for r in rows if r.region == r0.region and r.supplier_category == r0.supplier_category
    ]
    assert body["total"] == len(want)


def test_filter_by_reliability_category_uses_latest_score(api_client):
    items = api_client.get("/api/v1/suppliers", params={"limit": 50}).json()["items"]
    cats = {i["latest_score"]["reliability_category"] for i in items if i["latest_score"]}
    cat = sorted(cats)[0]
    body = api_client.get(
        "/api/v1/suppliers", params={"reliability_category": cat, "limit": 50}
    ).json()
    want = {
        i["supplier_id"]
        for i in items
        if i["latest_score"] and i["latest_score"]["reliability_category"] == cat
    }
    assert {i["supplier_id"] for i in body["items"]} == want
    assert body["total"] == len(want) > 0


def test_invalid_enum_filter_422(api_client):
    r = api_client.get("/api/v1/suppliers", params={"region": "MARS"})
    assert r.status_code == 422 and "MARS" not in r.text
    r = api_client.get("/api/v1/suppliers", params={"reliability_category": "SHINY"})
    assert r.status_code == 422


def test_q_searches_name_and_id_case_insensitively_and_escapes_wildcards(api_client):
    rows = _supplier_rows(api_client)
    word = rows[3].supplier_name.split()[0]
    want = sorted(r.supplier_id for r in rows if word.lower() in r.supplier_name.lower())
    body = api_client.get("/api/v1/suppliers", params={"q": word.upper(), "limit": 50}).json()
    assert sorted(i["supplier_id"] for i in body["items"]) == want
    by_id = api_client.get("/api/v1/suppliers", params={"q": "sup-000007"}).json()
    assert [i["supplier_id"] for i in by_id["items"]] == ["SUP-000007"]
    # '%' is a literal, not a wildcard
    assert api_client.get("/api/v1/suppliers", params={"q": "%"}).json()["total"] == 0


# --- suppliers: detail & sub-resources -------------------------------------------------------


def test_supplier_404_envelope(api_client):
    r = api_client.get("/api/v1/suppliers/SUP-999999")
    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == "not_found"
    assert "SUP-999999" in body["error"]["message"]
    for sub in ("performance", "scores", "explanation"):
        assert api_client.get(f"/api/v1/suppliers/SUP-999999/{sub}").status_code == 404


def test_supplier_detail_has_sites_contracts_and_score(api_client):
    r = api_client.get("/api/v1/suppliers/SUP-000001")
    assert r.status_code == 200
    body = r.json()
    assert body["supplier_id"] == "SUP-000001"
    assert len(body["sites"]) >= 1 and len(body["contracts"]) >= 1
    assert all(s["supplier_id"] == "SUP-000001" for s in body["sites"])
    contract = body["contracts"][0]
    assert {"contract_id", "currency", "effective_from", "contract_status"} <= set(contract)
    if contract["contracted_capacity"] is not None:
        assert MONEY.match(contract["contracted_capacity"])
    assert body["latest_score"] is not None
    with session(api_client) as s:
        n_sites = s.scalar(sa.text("SELECT COUNT(*) FROM sites WHERE supplier_id='SUP-000001'"))
        n_contracts = s.scalar(
            sa.text("SELECT COUNT(*) FROM contracts WHERE supplier_id='SUP-000001'")
        )
    assert len(body["sites"]) == n_sites and len(body["contracts"]) == n_contracts


def test_performance_lists_current_windows_in_order(api_client):
    body = api_client.get("/api/v1/suppliers/SUP-000001/performance").json()
    assert body["supplier_id"] == "SUP-000001"
    windows = body["windows"]
    assert len(windows) == 6
    ends = [w["evaluation_end"] for w in windows]
    assert ends == sorted(ends)
    w = windows[-1]
    assert {
        "delivery_score",
        "quality_score",
        "cost_score",
        "capacity_score",
        "compliance_score",
        "disruption_score",
        "overall_score",
        "evidence_coverage",
        "missing_reasons",
        "reliability_category",
    } <= set(w)
    with session(api_client) as s:
        stored = s.scalar(
            sa.select(SupplierScore.overall_score).where(SupplierScore.score_id == w["score_id"])
        )
    assert w["overall_score"] == pytest.approx(float(stored))


def test_scores_history_includes_superseded_ordered(api_client):
    with session(api_client) as s:
        victim_id = s.scalars(
            sa.select(SupplierScore.score_id)
            .where(SupplierScore.supplier_id == "SUP-000001")
            .order_by(SupplierScore.evaluation_end)
            .limit(1)
        ).one()
        # a superseded version: same window, no longer current
        s.execute(
            sa.update(SupplierScore)
            .where(SupplierScore.score_id == victim_id)
            .values(is_current=False)
        )
        s.commit()
    perf = api_client.get("/api/v1/suppliers/SUP-000001/performance").json()["windows"]
    assert victim_id not in {w["score_id"] for w in perf} and len(perf) == 5
    body = api_client.get("/api/v1/suppliers/SUP-000001/scores").json()
    assert body["total"] == 6
    assert victim_id in {i["score_id"] for i in body["items"]}
    keys = [(i["evaluation_end"], i["version_number"]) for i in body["items"]]
    assert keys == sorted(keys)
    flags = {i["score_id"]: i["is_current"] for i in body["items"]}
    assert flags[victim_id] is False and sum(flags.values()) == 5
    assert {"version_number", "superseded_by_score_id", "change_reason"} <= set(body["items"][0])


def test_scores_history_is_paginated(api_client):
    body = api_client.get(
        "/api/v1/suppliers/SUP-000001/scores", params={"limit": 2, "offset": 1}
    ).json()
    assert len(body["items"]) == 2 and body["total"] == 6 and body["offset"] == 1


def test_supplier_explanation_has_components_and_cutoff(api_client):
    r = api_client.get("/api/v1/suppliers/SUP-000001/explanation")
    assert r.status_code == 200
    body = r.json()
    assert "components" not in body  # lives once, inside `explanation`
    assert set(body["explanation"]["components"]) == {
        "delivery",
        "quality",
        "cost",
        "capacity",
        "compliance",
        "disruption",
    }
    assert body["data_cutoff"].endswith("Z") and body["supplier_id"] == "SUP-000001"
    assert body["explanation"]["data_cutoff"].startswith("2026-06-30")
    perf = api_client.get("/api/v1/suppliers/SUP-000001/performance").json()["windows"]
    assert body["score_id"] == perf[-1]["score_id"]  # the latest current score


def test_explanation_404_when_supplier_has_no_score(api_client):
    with session(api_client) as s:
        s.add(
            Supplier(
                supplier_id="SUP-777777",
                supplier_name="Unscored Supplier",
                supplier_category=SupplierCategory.PACKAGING,
                country="Canada",
                region=Region.NA,
                supplier_status=SupplierStatus.ACTIVE,
                onboarding_date=date(2025, 1, 1),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        s.commit()
    r = api_client.get("/api/v1/suppliers/SUP-777777/explanation")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    detail = api_client.get("/api/v1/suppliers/SUP-777777")
    assert detail.status_code == 200 and detail.json()["latest_score"] is None
    assert api_client.get("/api/v1/suppliers/SUP-777777/performance").json()["windows"] == []


def test_empty_database_lists_are_empty_pages(empty_api_client):
    body = empty_api_client.get("/api/v1/suppliers").json()
    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}
    assert empty_api_client.get("/api/v1/shipments").json()["total"] == 0
    assert empty_api_client.get("/api/v1/purchase-orders").json()["total"] == 0


# --- purchase orders ---------------------------------------------------------------------------


def test_list_purchase_orders_and_filters(api_client):
    body = api_client.get("/api/v1/purchase-orders", params={"limit": 5}).json()
    assert body["total"] == 300 and len(body["items"]) == 5
    assert body["items"][0]["po_id"] == "PO-2026-000001"
    with session(api_client) as s:
        pos = s.scalars(sa.select(PurchaseOrder)).all()
    sup = pos[0].supplier_id
    want = [p for p in pos if p.supplier_id == sup]
    body = api_client.get("/api/v1/purchase-orders", params={"supplier_id": sup}).json()
    assert body["total"] == len(want) and all(i["supplier_id"] == sup for i in body["items"])
    status = pos[0].po_status
    body = api_client.get("/api/v1/purchase-orders", params={"status": status.value}).json()
    assert body["total"] == len([p for p in pos if p.po_status == status])
    dates = sorted(p.order_date for p in pos)
    lo, hi = dates[100], dates[150]
    body = api_client.get(
        "/api/v1/purchase-orders",
        params={"order_date_from": lo.isoformat(), "order_date_to": hi.isoformat(), "limit": 500},
    ).json()
    assert body["total"] == len([p for p in pos if lo <= p.order_date <= hi])
    bad = api_client.get("/api/v1/purchase-orders", params={"order_date_from": "not-a-date"})
    assert bad.status_code == 422


def test_purchase_order_detail_has_lines_and_shipments(api_client):
    with session(api_client) as s:
        po_id = s.scalar(sa.text("SELECT po_id FROM shipments ORDER BY po_id LIMIT 1"))
        n_lines = s.scalar(sa.text("SELECT COUNT(*) FROM po_lines WHERE po_id=:p"), {"p": po_id})
        n_ship = s.scalar(sa.text("SELECT COUNT(*) FROM shipments WHERE po_id=:p"), {"p": po_id})
    r = api_client.get(f"/api/v1/purchase-orders/{po_id}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["lines"]) == n_lines and len(body["shipments"]) == n_ship >= 1
    line = body["lines"][0]
    assert MONEY.match(line["agreed_unit_price"]) and MONEY.match(line["line_amount"])
    assert isinstance(line["ordered_quantity"], str)  # decimals travel as strings
    numbers = [ln["line_number"] for ln in body["lines"]]
    assert numbers == sorted(numbers)
    assert body["shipments"][0]["po_id"] == po_id
    assert Z_TIMESTAMP.match(body["created_at"])


def test_purchase_order_404(api_client):
    r = api_client.get("/api/v1/purchase-orders/PO-2026-999999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# --- shipments ---------------------------------------------------------------------------------


def test_filter_shipments_by_site_and_date(api_client):
    with session(api_client) as s:
        ships = s.scalars(sa.select(Shipment)).all()
    per_site: dict[str, int] = {}
    for x in ships:
        per_site[x.originating_site_id] = per_site.get(x.originating_site_id, 0) + 1
    site = max(per_site, key=lambda k: (per_site[k], k))
    in_site = sorted(x.dispatch_timestamp for x in ships if x.originating_site_id == site)
    assert len(in_site) >= 4
    lo, hi = in_site[1], in_site[-2]
    want = sorted(
        x.shipment_id
        for x in ships
        if x.originating_site_id == site and lo <= x.dispatch_timestamp <= hi
    )
    body = api_client.get(
        "/api/v1/shipments",
        params={
            "site_id": site,
            "dispatch_from": iso_z(lo),
            "dispatch_to": iso_z(hi),
            "limit": 500,
        },
    ).json()
    assert [i["shipment_id"] for i in body["items"]] == want and body["total"] == len(want)
    assert all(i["originating_site_id"] == site for i in body["items"])


def test_dispatch_filter_converts_offsets_to_utc(api_client):
    with session(api_client) as s:
        ships = s.scalars(sa.select(Shipment).order_by(Shipment.dispatch_timestamp)).all()
    cut = ships[200].dispatch_timestamp
    plus5 = (cut.astimezone(UTC).replace(tzinfo=None) + timedelta(hours=5)).isoformat() + "+05:00"
    body = api_client.get("/api/v1/shipments", params={"dispatch_from": plus5, "limit": 500}).json()
    assert body["total"] == len([x for x in ships if x.dispatch_timestamp >= cut])


def test_naive_datetime_query_param_422(api_client):
    r = api_client.get("/api/v1/shipments", params={"dispatch_from": "2026-01-01T00:00:00"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["details"][0]["loc"] == ["query", "dispatch_from"]
    assert "2026-01-01" not in r.text
    r = api_client.get("/api/v1/shipments", params={"dispatch_to": "2026-01-01T00:00:00"})
    assert r.status_code == 422


def test_filter_shipments_by_supplier_and_status(api_client):
    with session(api_client) as s:
        rows = s.execute(
            sa.text(
                "SELECT s.shipment_id, p.supplier_id, s.shipment_status FROM shipments s "
                "JOIN purchase_orders p ON p.po_id = s.po_id"
            )
        ).all()
    sup, status = rows[0].supplier_id, rows[0].shipment_status
    body = api_client.get("/api/v1/shipments", params={"supplier_id": sup, "limit": 500}).json()
    assert sorted(i["shipment_id"] for i in body["items"]) == sorted(
        r.shipment_id for r in rows if r.supplier_id == sup
    )
    assert all(i["supplier_id"] == sup for i in body["items"])
    body = api_client.get("/api/v1/shipments", params={"status": status}).json()
    assert body["total"] == len([r for r in rows if r.shipment_status == status])


def _parse(value):
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _reference_end(app_config):
    return _parse(app_config.generator.reference_period["end"])


def _expected_late(item, grace, ref_end):
    """The R48 rule, restated independently from the response fields."""
    actual = _parse(item["actual_delivery_timestamp"])
    expected = _parse(item["expected_delivery_timestamp"])
    if actual is not None:
        return actual > expected + grace
    return item["shipment_status"] != "CANCELLED" and expected + grace < ref_end


def test_late_only_follows_r48_for_every_shipment(api_client, app_config):
    grace = timedelta(hours=app_config.scoring.grace_hours)
    ref_end = _reference_end(app_config)
    items = all_pages(api_client, "/api/v1/shipments")
    assert len(items) == 450
    want = sorted(i["shipment_id"] for i in items if _expected_late(i, grace, ref_end))
    late = all_pages(api_client, "/api/v1/shipments", late_only=True)
    rest = all_pages(api_client, "/api/v1/shipments", late_only=False)
    assert sorted(i["shipment_id"] for i in late) == want and 0 < len(want) < 450
    # late + not-late partitions the table: false never silently drops NULL-arrival rows
    assert len(late) + len(rest) == 450
    assert {i["shipment_id"] for i in rest}.isdisjoint(want)
    late_ids = set(want)
    for i in items:
        assert i["is_late"] is (i["shipment_id"] in late_ids)
        assert (i["delay_hours"] is not None) is i["is_late"]


def test_late_only_cases_arrived_overdue_cancelled_and_boundary(api_client, app_config):
    grace_h = app_config.scoring.grace_hours
    ref_end = _reference_end(app_config)
    with session(api_client) as s:
        ships = s.scalars(sa.select(Shipment).order_by(Shipment.shipment_id).limit(6)).all()
        arrived_late, at_boundary, overdue, cancelled, not_due, arrived_early = ships
        arrived_late.actual_delivery_timestamp = arrived_late.expected_delivery_timestamp + (
            timedelta(hours=grace_h, seconds=1)
        )
        at_boundary.actual_delivery_timestamp = at_boundary.expected_delivery_timestamp + (
            timedelta(hours=grace_h)
        )
        overdue.actual_delivery_timestamp = None
        overdue.expected_delivery_timestamp = ref_end - timedelta(days=10)
        overdue.shipment_status = ShipmentStatus.IN_TRANSIT
        cancelled.actual_delivery_timestamp = None
        cancelled.expected_delivery_timestamp = ref_end - timedelta(days=10)
        cancelled.shipment_status = ShipmentStatus.CANCELLED
        not_due.actual_delivery_timestamp = None
        not_due.expected_delivery_timestamp = ref_end - timedelta(hours=grace_h - 1)
        not_due.shipment_status = ShipmentStatus.IN_TRANSIT
        arrived_early.actual_delivery_timestamp = arrived_early.expected_delivery_timestamp - (
            timedelta(hours=5)
        )
        ids = [x.shipment_id for x in ships]
        overdue_expected = overdue.expected_delivery_timestamp
        s.commit()
    late = {i["shipment_id"] for i in all_pages(api_client, "/api/v1/shipments", late_only=True)}
    a, b, o, c, n, e = ids
    assert a in late and o in late
    assert not ({b, c, n, e} & late)  # boundary, CANCELLED, not yet due, early arrival
    by_id = {i["shipment_id"]: i for i in all_pages(api_client, "/api/v1/shipments")}
    assert by_id[a]["is_late"] and by_id[a]["delay_hours"] == f"{grace_h:.2f}"
    overdue_hours = (ref_end - overdue_expected).total_seconds() / 3600
    assert by_id[o]["is_late"] and by_id[o]["delay_hours"] == f"{overdue_hours:.2f}"
    for sid in (b, c, n, e):
        assert by_id[sid]["is_late"] is False and by_id[sid]["delay_hours"] is None
    detail = api_client.get(f"/api/v1/shipments/{o}").json()
    assert detail["is_late"] is True and detail["delay_hours"] == f"{overdue_hours:.2f}"


def test_shipment_detail_has_lines_and_supplier(api_client):
    r = api_client.get("/api/v1/shipments/SHP-2026-000001")
    assert r.status_code == 200
    body = r.json()
    assert body["shipment_id"] == "SHP-2026-000001" and body["supplier_id"].startswith("SUP-")
    assert len(body["lines"]) >= 1
    line = body["lines"][0]
    expected_keys = {"po_line_id", "allocated_quantity", "shipped_quantity", "received_quantity"}
    assert expected_keys <= set(line)
    assert isinstance(line["shipped_quantity"], str)
    assert Z_TIMESTAMP.match(body["dispatch_timestamp"])


def test_shipment_events_ordered(api_client):
    r = api_client.get("/api/v1/shipments/SHP-2026-000001/events")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 3 and body["total"] == len(body["items"])
    keys = [(e["event_timestamp"], e["delivery_event_id"]) for e in body["items"]]
    assert keys == sorted(keys)
    assert {e["shipment_id"] for e in body["items"]} == {"SHP-2026-000001"}
    assert {"event_type", "recorded_timestamp", "source_system"} <= set(body["items"][0])


def test_shipment_404s(api_client):
    for url in ("/api/v1/shipments/SHP-2026-999999", "/api/v1/shipments/SHP-2026-999999/events"):
        r = api_client.get(url)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_unknown_shipment_sort_422_and_valid_sort(api_client):
    assert api_client.get("/api/v1/shipments", params={"sort": "hack:asc"}).status_code == 422
    items = api_client.get(
        "/api/v1/shipments", params={"sort": "dispatch_timestamp:desc", "limit": 20}
    ).json()["items"]
    stamps = [i["dispatch_timestamp"] for i in items]
    assert stamps == sorted(stamps, reverse=True)


# --- no N+1 ------------------------------------------------------------------------------------


class StatementCounter:
    def __init__(self, engine):
        self.engine, self.count = engine, 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._on)

    def _on(self, *args):
        self.count += 1


def count_statements(client, url, **params):
    with StatementCounter(client.app.state.engine) as c:
        r = client.get(url, params=params)
    assert r.status_code == 200
    return c.count, r.json()


def test_supplier_list_statement_count_is_constant(api_client):
    n5, body5 = count_statements(api_client, "/api/v1/suppliers", limit=5)
    n50, body50 = count_statements(api_client, "/api/v1/suppliers", limit=50)
    assert len(body5["items"]) == 5 and len(body50["items"]) == 50
    assert n5 == n50 and n50 <= 3  # one count + one page (scores joined in the same query)


def _extremes(client, sql):
    """(id with the most child rows, id with the fewest) from independent SQL."""
    with session(client) as s:
        rows = s.execute(sa.text(sql)).all()
    rows.sort(key=lambda r: (r[1], r[0]))
    assert rows[-1][1] > rows[0][1]  # differing collection sizes, or the test proves nothing
    return rows[-1][0], rows[0][0]


def test_detail_and_transaction_lists_have_constant_statement_counts(api_client):
    for url in ("/api/v1/shipments", "/api/v1/purchase-orders"):
        n5, _ = count_statements(api_client, url, limit=5)
        n50, _ = count_statements(api_client, url, limit=50)
        assert n5 == n50 <= 3
    big, small = _extremes(
        api_client,
        "SELECT s.supplier_id, COUNT(DISTINCT c.contract_id) + COUNT(DISTINCT x.site_id) "
        "FROM suppliers s LEFT JOIN contracts c ON c.supplier_id = s.supplier_id "
        "LEFT JOIN sites x ON x.supplier_id = s.supplier_id GROUP BY s.supplier_id",
    )
    n_big, _ = count_statements(api_client, f"/api/v1/suppliers/{big}")
    n_small, _ = count_statements(api_client, f"/api/v1/suppliers/{small}")
    assert n_big == n_small <= 5
    big, small = _extremes(api_client, "SELECT po_id, COUNT(*) FROM po_lines GROUP BY po_id")
    n_big, _ = count_statements(api_client, f"/api/v1/purchase-orders/{big}")
    n_small, _ = count_statements(api_client, f"/api/v1/purchase-orders/{small}")
    assert n_big == n_small <= 5
    big, small = _extremes(
        api_client, "SELECT shipment_id, COUNT(*) FROM shipment_lines GROUP BY shipment_id"
    )
    n_big, _ = count_statements(api_client, f"/api/v1/shipments/{big}")
    n_small, _ = count_statements(api_client, f"/api/v1/shipments/{small}")
    assert n_big == n_small <= 3
    big, small = _extremes(
        api_client, "SELECT shipment_id, COUNT(*) FROM delivery_events GROUP BY shipment_id"
    )
    n_big, _ = count_statements(api_client, f"/api/v1/shipments/{big}/events")
    n_small, _ = count_statements(api_client, f"/api/v1/shipments/{small}/events")
    assert n_big == n_small


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/suppliers/{}",
        "/api/v1/suppliers/{}/performance",
        "/api/v1/suppliers/{}/scores",
        "/api/v1/suppliers/{}/explanation",
        "/api/v1/purchase-orders/{}",
        "/api/v1/shipments/{}",
        "/api/v1/shipments/{}/events",
    ],
)
@pytest.mark.parametrize("bad_id", ["X" * 60, "sup-000001", "SUP_000001", "SUP.1%3Bdrop"])
def test_malformed_path_ids_are_422_without_echo(api_client, path, bad_id):
    r = api_client.get(path.format(bad_id))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert bad_id not in r.text and "drop" not in r.text


def test_reliability_category_values_listed_in_openapi(api_client):
    spec = api_client.get("/openapi.json").json()
    params = spec["paths"]["/api/v1/suppliers"]["get"]["parameters"]
    cat = next(p for p in params if p["name"] == "reliability_category")
    ref = next(s["$ref"] for s in cat["schema"]["anyOf"] if "$ref" in s)
    enum = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]["enum"]
    assert enum == [c.value for c in ReliabilityCategory]

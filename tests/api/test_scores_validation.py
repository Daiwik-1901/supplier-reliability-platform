"""Task 20: score, validation and exception endpoints over the seeded starter dataset.

Expected values come from independent SQL against the same database copy, never from the API
under test. Every test has its own database copy (tests/api/conftest.py)."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from supplier_reliability.domain.enums import (
    DeliveryEventType,
    ReliabilityCategory,
    Severity,
    ShipmentStatus,
)
from supplier_reliability.models import (
    DataException,
    DeliveryEvent,
    PurchaseOrder,
    ScoreEvidence,
    Shipment,
    Supplier,
    SupplierScore,
    ValidationFinding,
    ValidationRun,
)
from supplier_reliability.services.validation import REGISTRY

SCORES = "/api/v1/scores"
# A window the starter pipeline never scored (it scores the 6 trailing-12-month scorecards).
NEW_WINDOW = {
    "evaluation_start": "2025-04-01T00:00:00Z",
    "evaluation_end": "2025-06-30T23:59:59Z",
}


def session(client):
    return client.app.state.session_factory()


def supplier_ids(client, n):
    with session(client) as s:
        return list(s.scalars(sa.select(Supplier.supplier_id).order_by(Supplier.supplier_id)))[:n]


def last_score(client):
    """A current score of the final scorecard window (the pipeline's latest)."""
    with session(client) as s:
        row = s.scalars(
            sa.select(SupplierScore)
            .where(SupplierScore.is_current.is_(True))
            .order_by(SupplierScore.evaluation_end.desc(), SupplierScore.score_id)
        ).first()
        s.expunge(row)
        return row


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_body(score: SupplierScore, **extra) -> dict:
    return {
        "evaluation_start": iso(score.evaluation_start),
        "evaluation_end": iso(score.evaluation_end),
        **extra,
    }


def add_late_arrival(client, window_score: SupplierScore) -> str:
    """Insert an ARRIVED event (recorded before the cutoff) for a due shipment of some supplier
    that had none known at the cutoff, so recalculating that window changes the evidence."""
    w = window_score
    with session(client) as s:
        known = set(
            s.scalars(
                sa.select(DeliveryEvent.shipment_id).where(
                    DeliveryEvent.event_type.in_(
                        [DeliveryEventType.ARRIVED, DeliveryEventType.CORRECTION]
                    )
                )
            )
        )
        rows = s.execute(
            sa.select(
                Shipment.shipment_id,
                PurchaseOrder.supplier_id,
                Shipment.expected_delivery_timestamp,
            )
            .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
            .where(
                Shipment.expected_delivery_timestamp.between(
                    w.evaluation_start, w.data_cutoff_timestamp
                ),
                Shipment.created_at <= w.data_cutoff_timestamp,
                Shipment.shipment_status != ShipmentStatus.CANCELLED,
            )
            .order_by(Shipment.shipment_id)
        ).all()
        for shipment_id, supplier_id, expected in rows:
            if shipment_id in known:
                continue
            digest = hashlib.sha256(shipment_id.encode()).hexdigest()[:8]
            s.add(
                DeliveryEvent(
                    delivery_event_id=f"EVT-9999-{digest}",
                    shipment_id=shipment_id,
                    event_type=DeliveryEventType.ARRIVED,
                    event_timestamp=expected - timedelta(hours=2),
                    recorded_timestamp=expected - timedelta(hours=1),
                    location_id=None,
                    source_system="SYNTHETIC_TMS",
                    event_metadata=None,
                )
            )
            s.commit()
            return supplier_id
    raise AssertionError("no shipment without a known arrival in the window")


# --- calculate -------------------------------------------------------------------------------


def test_calculate_then_repeat_is_idempotent(api_client):
    ids = supplier_ids(api_client, 3)
    body = {**NEW_WINDOW, "supplier_ids": ids}
    first = api_client.post(f"{SCORES}/calculate", json=body)
    assert first.status_code == 201
    out = first.json()
    assert len(out["created"]) == 3
    assert out["existing"] == out["unchanged"] == [] and out["superseded"] == {}
    with session(api_client) as s:
        assert sorted(
            s.scalars(
                sa.select(SupplierScore.score_id).where(SupplierScore.score_id.in_(out["created"]))
            )
        ) == sorted(out["created"])
    second = api_client.post(f"{SCORES}/calculate", json=body)
    assert second.status_code == 200
    assert second.json()["created"] == []
    assert sorted(second.json()["existing"]) == sorted(out["created"])
    with session(api_client) as s:  # nothing was written twice
        n = s.scalar(
            sa.select(sa.func.count())
            .select_from(SupplierScore)
            .where(
                SupplierScore.evaluation_start == datetime(2025, 4, 1, tzinfo=UTC),
                SupplierScore.evaluation_end == datetime(2025, 6, 30, 23, 59, 59, tzinfo=UTC),
            )
        )
    assert n == 3


def test_calculate_dedups_supplier_ids_in_body(api_client):
    sid = supplier_ids(api_client, 1)[0]
    r = api_client.post(f"{SCORES}/calculate", json={**NEW_WINDOW, "supplier_ids": [sid, sid, sid]})
    assert r.status_code == 201 and len(r.json()["created"]) == 1
    again = api_client.post(f"{SCORES}/calculate", json={**NEW_WINDOW, "supplier_ids": [sid, sid]})
    assert again.status_code == 200 and again.json()["created"] == []


def test_calculate_defaults_cutoff_to_window_end(api_client):
    sid = supplier_ids(api_client, 1)[0]
    r = api_client.post(f"{SCORES}/calculate", json={**NEW_WINDOW, "supplier_ids": [sid]})
    with session(api_client) as s:
        row = s.get(SupplierScore, r.json()["created"][0])
        assert row.data_cutoff_timestamp == row.evaluation_end
        # wall-clock calculated_at (documented): recent, not a pipeline's deterministic clock
        assert datetime.now(UTC) - row.calculated_at < timedelta(minutes=5)


@pytest.mark.parametrize(
    "patch",
    [
        {"evaluation_end": "2025-03-31T23:59:59Z"},  # end < start
        {"data_cutoff": "2025-03-31T00:00:00Z"},  # cutoff < start
        {"evaluation_start": "2025-04-01T00:00:00"},  # naive
        {"evaluation_end": "2025-06-30T23:59:59"},  # naive
        {"data_cutoff": "2025-06-30T23:59:59"},  # naive
        {"supplier_ids": []},
        {"supplier_ids": ["sup 1"]},
        {"supplier_ids": [f"SUP-{i:06d}" for i in range(1001)]},
    ],
)
def test_calculate_invalid_period_422(api_client, patch):
    r = api_client.post(f"{SCORES}/calculate", json={**NEW_WINDOW, **patch})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    with session(api_client) as s:  # nothing written
        assert s.scalar(sa.select(sa.func.count()).select_from(SupplierScore)) == 300


def test_calculate_unknown_supplier_404(api_client):
    real = supplier_ids(api_client, 1)[0]
    r = api_client.post(
        f"{SCORES}/calculate",
        json={**NEW_WINDOW, "supplier_ids": [real, "SUP-999998", "SUP-999999"]},
    )
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert "SUP-999998" in err["message"] and "SUP-999999" in err["message"]
    assert real not in err["message"]
    with session(api_client) as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(SupplierScore)) == 300


def test_unknown_supplier_listing_is_bounded(api_client):
    ids = [f"SUP-9{i:05d}" for i in range(200)]
    r = api_client.post(f"{SCORES}/calculate", json={**NEW_WINDOW, "supplier_ids": ids})
    assert r.status_code == 404
    assert len(r.text) < 1000 and "more" in r.json()["error"]["message"]


def test_scoring_version_mismatch_is_409(api_client, app_config):
    body = {**NEW_WINDOW, "supplier_ids": supplier_ids(api_client, 1)}
    r = api_client.post(f"{SCORES}/calculate", json={**body, "scoring_version": "9.9.9"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert "scoring version not available" in r.json()["error"]["message"]
    ok = api_client.post(
        f"{SCORES}/calculate", json={**body, "scoring_version": app_config.scoring.scoring_version}
    )
    assert ok.status_code == 201
    r = api_client.post(
        f"{SCORES}/recalculate", json={**body, "scoring_version": "9.9.9", "reason": "x"}
    )
    assert r.status_code == 409


# --- recalculate -----------------------------------------------------------------------------


def test_recalculate_requires_reason_422(api_client):
    sid = supplier_ids(api_client, 1)[0]
    body = {**NEW_WINDOW, "supplier_ids": [sid]}
    for extra in ({}, {"reason": ""}, {"reason": "x" * 501}):
        r = api_client.post(f"{SCORES}/recalculate", json={**body, **extra})
        assert r.status_code == 422, extra
        assert r.json()["error"]["details"][0]["loc"][-1] == "reason"


def test_recalculate_unchanged_evidence_creates_no_version(api_client):
    score = last_score(api_client)
    body = window_body(
        score, data_cutoff=iso(score.data_cutoff_timestamp), supplier_ids=[score.supplier_id]
    )
    r = api_client.post(f"{SCORES}/recalculate", json={**body, "reason": "routine check"})
    assert r.status_code == 200
    assert r.json() == {
        "created": [],
        "existing": [],
        "unchanged": [score.score_id],
        "superseded": {},
    }


def test_recalculate_preserves_history(api_client):
    score = last_score(api_client)
    supplier_id = add_late_arrival(api_client, score)
    with session(api_client) as s:
        old = s.scalars(
            sa.select(SupplierScore).where(
                SupplierScore.supplier_id == supplier_id,
                SupplierScore.evaluation_end == score.evaluation_end,
                SupplierScore.is_current.is_(True),
            )
        ).one()
        old_id, old_overall = old.score_id, old.overall_score
    body = window_body(
        score,
        data_cutoff=iso(score.data_cutoff_timestamp),
        supplier_ids=[supplier_id],
        reason="late ARRIVED loaded",
    )
    r = api_client.post(f"{SCORES}/recalculate", json=body)
    assert r.status_code == 200
    out = r.json()
    assert list(out["superseded"]) == [old_id]
    new_id = out["superseded"][old_id]
    history = api_client.get(f"/api/v1/suppliers/{supplier_id}/scores").json()["items"]
    by_id = {h["score_id"]: h for h in history}
    assert by_id[old_id]["is_current"] is False
    assert by_id[old_id]["superseded_by_score_id"] == new_id
    assert by_id[old_id]["version_number"] == 1
    assert by_id[new_id]["is_current"] is True and by_id[new_id]["version_number"] == 2
    assert by_id[new_id]["change_reason"] == "late ARRIVED loaded"
    with session(api_client) as s:  # old version untouched
        assert s.get(SupplierScore, old_id).overall_score == old_overall
    # a repeat finds nothing more to change
    again = api_client.post(f"{SCORES}/recalculate", json=body).json()
    assert again["superseded"] == {} and again["unchanged"] == [new_id]


# --- restate ---------------------------------------------------------------------------------


def test_restate_links_to_the_earlier_score(api_client):
    score = last_score(api_client)
    later = score.data_cutoff_timestamp + timedelta(days=30)
    body = window_body(
        score, data_cutoff=iso(later), supplier_ids=[score.supplier_id], reason="late data"
    )
    r = api_client.post(f"{SCORES}/restate", json=body)
    assert r.status_code == 201
    new_id = r.json()["created"][0]
    detail = api_client.get(f"{SCORES}/{new_id}").json()
    assert detail["restates_score_id"] == score.score_id
    assert detail["data_cutoff_timestamp"] == iso(later)
    assert detail["change_reason"] == "late data"
    assert api_client.get(f"{SCORES}/{score.score_id}").json()["is_current"] is True
    repeat = api_client.post(f"{SCORES}/restate", json=body)
    assert repeat.status_code == 201 and repeat.json()["created"] == []
    assert repeat.json()["existing"] == [new_id]


def test_restate_without_an_earlier_score_is_409(api_client):
    sid = supplier_ids(api_client, 1)[0]
    body = {
        **NEW_WINDOW,
        "data_cutoff": "2025-07-15T00:00:00Z",
        "supplier_ids": [sid],
        "reason": "nothing to restate",
    }
    r = api_client.post(f"{SCORES}/restate", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    with session(api_client) as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(SupplierScore)) == 300


def test_restate_validates_body(api_client):
    sid = supplier_ids(api_client, 1)[0]
    base = {**NEW_WINDOW, "supplier_ids": [sid], "reason": "r"}
    assert api_client.post(f"{SCORES}/restate", json=base).status_code == 422  # cutoff required
    cutoff = {**base, "data_cutoff": "2025-07-15T00:00:00Z"}
    assert api_client.post(f"{SCORES}/restate", json={**cutoff, "reason": ""}).status_code == 422
    naive = {**cutoff, "data_cutoff": "2025-07-15T00:00:00"}
    assert api_client.post(f"{SCORES}/restate", json=naive).status_code == 422
    unknown = {**cutoff, "supplier_ids": ["SUP-999999"]}
    assert api_client.post(f"{SCORES}/restate", json=unknown).status_code == 404


# --- reads -----------------------------------------------------------------------------------


def test_filter_scores_by_category(api_client):
    with session(api_client) as s:
        counts = dict(
            s.execute(
                sa.select(SupplierScore.reliability_category, sa.func.count())
                .where(SupplierScore.is_current.is_(True))
                .group_by(SupplierScore.reliability_category)
            ).all()
        )
    category = max(counts, key=counts.get)
    body = api_client.get(
        SCORES, params={"reliability_category": category.value, "limit": 500}
    ).json()
    assert body["total"] == counts[category]
    assert body["items"] and all(i["reliability_category"] == category.value for i in body["items"])
    other = next(c for c in ReliabilityCategory if c not in counts)
    assert api_client.get(SCORES, params={"reliability_category": other.value}).json()["total"] == 0
    assert api_client.get(SCORES, params={"reliability_category": "NOPE"}).status_code == 422


def test_list_scores_filters_sort_and_current_only(api_client):
    score = last_score(api_client)
    add_late_arrival(api_client, score)
    api_client.post(
        f"{SCORES}/recalculate",
        json=window_body(score, data_cutoff=iso(score.data_cutoff_timestamp), reason="r"),
    )
    with session(api_client) as s:
        total_all = s.scalar(sa.select(sa.func.count()).select_from(SupplierScore))
        total_current = s.scalar(
            sa.select(sa.func.count()).select_from(SupplierScore).where(SupplierScore.is_current)
        )
    assert total_all == total_current + 1
    assert api_client.get(SCORES).json()["total"] == total_current
    assert api_client.get(SCORES, params={"current_only": "false"}).json()["total"] == total_all
    page = api_client.get(
        SCORES,
        params={
            "supplier_id": score.supplier_id,
            "evaluation_end": iso(score.evaluation_end),
            "evaluation_start": iso(score.evaluation_start),
            "sort": "overall_score:desc",
        },
    ).json()
    assert page["items"] and all(i["supplier_id"] == score.supplier_id for i in page["items"])
    values = [i["overall_score"] for i in page["items"] if i["overall_score"] is not None]
    assert values == sorted(values, reverse=True)
    status = page["items"][0]["calculation_status"]
    by_status = api_client.get(SCORES, params={"calculation_status": status, "limit": 500}).json()
    assert all(i["calculation_status"] == status for i in by_status["items"])
    assert api_client.get(SCORES, params={"sort": "calculated_at:asc"}).status_code == 422
    assert (
        api_client.get(SCORES, params={"evaluation_end": "2025-06-30T00:00:00"}).status_code == 422
    )


def test_get_score_has_explanation_and_lineage_counts(api_client):
    score = last_score(api_client)
    body = api_client.get(f"{SCORES}/{score.score_id}").json()
    assert body["score_id"] == score.score_id
    assert body["explanation"] == score_explanation(api_client, score.score_id)
    with session(api_client) as s:
        rows = s.execute(
            sa.select(ScoreEvidence.component, ScoreEvidence.included, sa.func.count())
            .where(ScoreEvidence.score_id == score.score_id)
            .group_by(ScoreEvidence.component, ScoreEvidence.included)
        ).all()
    expected: dict[str, dict[str, int]] = {}
    for component, included, n in rows:
        expected.setdefault(component.value, {"included": 0, "excluded": 0})[
            "included" if included else "excluded"
        ] = n
    assert body["lineage_summary"] == expected
    assert "lineage" not in body  # summary counts only, never the rows


def score_explanation(client, score_id):
    with session(client) as s:
        return s.get(SupplierScore, score_id).explanation


def test_get_score_404_and_bad_id_422(api_client):
    assert api_client.get(f"{SCORES}/SCORE-2026-999999").status_code == 404
    assert api_client.get(f"{SCORES}/score-1").status_code == 422


# --- validation ------------------------------------------------------------------------------


def test_validation_run_and_results(api_client):
    rules = sorted(REGISTRY)[:3]
    r = api_client.post("/api/v1/validation/run", json={"rule_ids": rules})
    assert r.status_code == 201
    run = r.json()
    assert sorted(run["counts_by_rule"]) == rules
    assert sorted(run["checked_counts_by_rule"]) == rules
    assert set(run["counts_by_severity"]) == {s.value for s in Severity}
    total = sum(run["counts_by_rule"].values())
    assert sum(run["counts_by_severity"].values()) == total
    defect_sev = {Severity.MEDIUM.value, Severity.HIGH.value, Severity.CRITICAL.value}
    assert run["defects"]["total"] == sum(
        n for sev, n in run["counts_by_severity"].items() if sev in defect_sev
    )
    assert run["informational"]["total"] == total - run["defects"]["total"]
    with session(api_client) as s:
        stored = s.get(ValidationRun, run["run_id"])
        assert stored is not None
        assert (
            s.scalar(
                sa.select(sa.func.count())
                .select_from(ValidationFinding)
                .where(ValidationFinding.run_id == run["run_id"])
            )
            == total
        )
    results = api_client.get("/api/v1/validation/results", params={"run_id": run["run_id"]})
    assert results.status_code == 200 and results.json()["total"] == total
    assert all(i["run_id"] == run["run_id"] for i in results.json()["items"])


def test_validation_run_all_rules_matches_registry(api_client):
    run = api_client.post("/api/v1/validation/run", json={}).json()
    assert sorted(run["counts_by_rule"]) == sorted(REGISTRY)
    run2 = api_client.post("/api/v1/validation/run").json()  # no body at all is also "all"
    assert run2["run_id"] != run["run_id"] and run2["counts_by_rule"] == run["counts_by_rule"]


def test_validation_unknown_rule_ids_422(api_client):
    r = api_client.post("/api/v1/validation/run", json={"rule_ids": ["NO-001", "NO-002"]})
    assert r.status_code == 422
    msg = r.json()["error"]["details"][0]["msg"]
    assert "NO-001" in msg and "NO-002" in msg
    many = [f"NO-{i:03d}" for i in range(100)]
    r = api_client.post("/api/v1/validation/run", json={"rule_ids": many})
    assert r.status_code == 422 and len(r.text) < 1500


def test_validation_results_default_to_latest_run_and_filter(api_client):
    first = api_client.post("/api/v1/validation/run", json={}).json()
    second = api_client.post("/api/v1/validation/run", json={}).json()
    assert second["run_id"] > first["run_id"]
    body = api_client.get("/api/v1/validation/results", params={"limit": 500}).json()
    assert body["total"] == sum(second["counts_by_rule"].values())
    assert {i["run_id"] for i in body["items"]} <= {second["run_id"]}
    busiest = max(second["counts_by_rule"], key=second["counts_by_rule"].get)
    by_rule = api_client.get(
        "/api/v1/validation/results", params={"rule_id": busiest, "limit": 500}
    ).json()
    assert by_rule["total"] == second["counts_by_rule"][busiest]
    assert all(i["rule_id"] == busiest for i in by_rule["items"])
    sev = next(s for s, n in second["counts_by_severity"].items() if n)
    by_sev = api_client.get("/api/v1/validation/results", params={"severity": sev}).json()
    assert by_sev["total"] == second["counts_by_severity"][sev]
    old = api_client.get("/api/v1/validation/results", params={"run_id": first["run_id"]}).json()
    assert old["total"] == sum(first["counts_by_rule"].values())


def test_validation_results_404_without_runs_or_unknown_run(empty_api_client, api_client):
    r = empty_api_client.get("/api/v1/validation/results")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert (
        api_client.get("/api/v1/validation/results", params={"run_id": "VAL-999999"}).status_code
        == 404
    )
    assert (
        api_client.get("/api/v1/validation/results", params={"severity": "BAD"}).status_code == 422
    )


# --- exceptions ------------------------------------------------------------------------------


def test_exceptions_filter_by_severity(api_client):
    with session(api_client) as s:
        counts = dict(
            s.execute(
                sa.select(DataException.severity, sa.func.count()).group_by(DataException.severity)
            ).all()
        )
    assert counts, "starter pipeline must have produced data exceptions"
    severity = max(counts, key=counts.get)
    body = api_client.get(
        "/api/v1/exceptions", params={"severity": severity.value, "limit": 500}
    ).json()
    assert body["total"] == counts[severity]
    assert all(i["severity"] == severity.value for i in body["items"])
    ids = [i["exception_id"] for i in body["items"]]
    assert ids == sorted(ids)
    assert "evidence" not in body["items"][0]
    first = body["items"][0]
    for key in ("issue_type", "supplier_id", "disposition"):
        value = first[key]
        if value is None:
            continue
        got = api_client.get("/api/v1/exceptions", params={key: value, "limit": 500}).json()
        assert got["items"] and all(i[key] == value for i in got["items"])
    assert api_client.get("/api/v1/exceptions", params={"severity": "BAD"}).status_code == 422


def test_exception_detail_has_evidence(api_client):
    with session(api_client) as s:
        row = s.scalars(sa.select(DataException).order_by(DataException.exception_id)).first()
        expected = (row.exception_id, row.evidence, row.related_entity_ids)
    r = api_client.get(f"/api/v1/exceptions/{expected[0]}")
    assert r.status_code == 200
    body = r.json()
    assert body["exception_id"] == expected[0]
    assert body["evidence"] == expected[1]
    assert body["related_entity_ids"] == expected[2]


def test_exception_404(api_client):
    r = api_client.get("/api/v1/exceptions/EXC-999999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert api_client.get("/api/v1/exceptions/exc_1").status_code == 422


# --- OpenAPI contract ------------------------------------------------------------------------

NEW_ROUTES = {
    ("/api/v1/scores", "get"): {"422"},
    ("/api/v1/scores/{score_id}", "get"): {"404", "422"},
    ("/api/v1/scores/calculate", "post"): {"404", "409", "422"},
    ("/api/v1/scores/recalculate", "post"): {"404", "409", "422"},
    ("/api/v1/scores/restate", "post"): {"404", "409", "422"},
    ("/api/v1/validation/run", "post"): {"422"},
    ("/api/v1/validation/results", "get"): {"404", "422"},
    ("/api/v1/exceptions", "get"): {"422"},
    ("/api/v1/exceptions/{exception_id}", "get"): {"404", "422"},
}


def test_new_routes_are_documented(api_client):
    spec = api_client.get("/openapi.json").json()
    for (path, method), statuses in NEW_ROUTES.items():
        op = spec["paths"][path][method]
        assert op.get("summary") and op.get("description"), (path, method)
        ok = [c for c in ("200", "201") if c in op["responses"]]
        assert ok and "schema" in op["responses"][ok[0]]["content"]["application/json"]
        assert statuses <= set(op["responses"]), (path, method)
        for code in statuses:
            ref = op["responses"][code]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith("/ErrorEnvelope"), (path, method, code)
    assert "201" in spec["paths"]["/api/v1/scores/calculate"]["post"]["responses"]
    assert "200" in spec["paths"]["/api/v1/scores/calculate"]["post"]["responses"]


@pytest.mark.parametrize(
    "model", ["CalculateRequest", "RecalculateRequest", "RestateRequest", "ValidationRunRequest"]
)
def test_request_models_carry_valid_examples(api_client, model):
    schema = api_client.get("/openapi.json").json()["components"]["schemas"][model]
    assert schema.get("examples")


def test_documented_request_examples_are_accepted(api_client):
    schemas = api_client.get("/openapi.json").json()["components"]["schemas"]
    routes = {
        "CalculateRequest": f"{SCORES}/calculate",
        "RecalculateRequest": f"{SCORES}/recalculate",
        "RestateRequest": f"{SCORES}/restate",
        "ValidationRunRequest": "/api/v1/validation/run",
    }
    for model, url in routes.items():
        for example in schemas[model]["examples"]:
            r = api_client.post(url, json=example)
            # examples are well-formed: never a 422 (they may legitimately be 404/409 here)
            assert r.status_code != 422, (model, r.text)

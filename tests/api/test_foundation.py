"""Task 19: error envelope, readiness, metadata, request-size limit, CORS and OpenAPI contract."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from supplier_reliability import GENERATOR_VERSION, SCHEMA_VERSION, __version__
from supplier_reliability.api.middleware import RequestSizeLimitMiddleware
from supplier_reliability.config import Settings
from supplier_reliability.database import alembic_config
from supplier_reliability.main import create_app
from supplier_reliability.models import DatasetManifest

HEAD = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


# --- health / ready ----------------------------------------------------------------------------


def test_health_and_ready(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    r = api_client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "database": "ok", "schema_version": HEAD}


def test_health_does_not_touch_the_database(tmp_path, config_path):
    bad = Settings(
        database_url=f"sqlite:///{tmp_path}/missing-dir/x.db",
        config_path=config_path,
    )
    assert TestClient(create_app(bad)).get("/health").status_code == 200


def test_ready_503_when_db_unreachable(tmp_path, config_path):
    bad = Settings(
        database_url=f"sqlite:///{tmp_path}/no/such/dir/x.db",
        config_path=config_path,
    )
    r = TestClient(create_app(bad)).get("/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "database": "unavailable"}
    assert "no/such" not in r.text and "sqlite" not in r.text.lower()


def test_unusable_url_logs_only_the_exception_type(config_path, capsys):
    secret = "p4ssw0rd-in-url"
    create_app(Settings(database_url=f"{secret} not a url", config_path=config_path))
    logged = capsys.readouterr().err
    assert "ArgumentError" in logged and secret not in logged


def test_ready_503_when_url_cannot_even_be_parsed(config_path):
    bad = Settings(database_url="not a url at all", config_path=config_path)
    r = TestClient(create_app(bad)).get("/ready")
    assert r.status_code == 503 and r.json()["database"] == "unavailable"


def test_ready_does_not_create_a_missing_sqlite_file(tmp_path, config_path):
    db = tmp_path / "absent.db"
    bad = Settings(database_url=f"sqlite:///{db}", config_path=config_path)
    r = TestClient(create_app(bad)).get("/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "database": "unavailable"}
    assert not db.exists()


def test_ready_503_when_not_migrated(client, tmp_settings):
    # an existing but empty (never migrated) SQLite file
    Path(tmp_settings.database_url.removeprefix("sqlite:///")).write_bytes(b"")
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "database": "not_migrated"}


def test_ready_503_when_revision_is_behind_head(api_client):
    with api_client.app.state.session_factory() as s:
        s.execute(sa.text("UPDATE alembic_version SET version_num = '0000_old'"))
        s.commit()
    r = api_client.get("/ready")
    assert r.status_code == 503 and r.json()["database"] == "not_migrated"
    assert "0000_old" not in r.text


# --- metadata ----------------------------------------------------------------------------------


def test_metadata_contents(api_client, app_config):
    r = api_client.get("/api/v1/metadata")
    assert r.status_code == 200
    body = r.json()
    assert body["app_version"] == __version__
    assert body["generator_version"] == GENERATOR_VERSION
    assert body["scoring_version"] == app_config.scoring.scoring_version
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["scoring_config_hash"] == _expected_scoring_hash(app_config)
    assert "config_hash" not in body
    assert body["weights"] == app_config.scoring.weights
    assert set(body["category_thresholds"]) == {"HIGH", "ACCEPTABLE", "AT_RISK", "CRITICAL"}
    assert body["category_thresholds"]["HIGH"] == {"lower": 85, "upper": 100}
    assert body["reference_period"] == {
        "start": "2025-01-01T00:00:00Z",
        "end": "2026-06-30T23:59:59Z",
    }
    with api_client.app.state.session_factory() as s:
        latest = s.scalar(
            sa.select(DatasetManifest.dataset_id).order_by(DatasetManifest.created_at.desc())
        )
    assert body["dataset_id"] == latest and latest is not None


def _expected_scoring_hash(app_config):
    import hashlib
    import json

    canonical = json.dumps(
        app_config.scoring.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def test_metadata_has_no_paths(api_client):
    body = api_client.get("/api/v1/metadata").json()
    for text in _strings(body):
        assert ":\\" not in text and "\\" not in text
        assert "/" not in text, text
        assert "sqlite" not in text.lower() and ".db" not in text


def test_metadata_without_dataset_has_null_dataset_id(empty_api_client):
    body = empty_api_client.get("/api/v1/metadata").json()
    assert body["dataset_id"] is None


# --- error envelope ----------------------------------------------------------------------------


def test_unknown_route_uses_the_envelope(api_client):
    r = api_client.get("/api/v1/nope")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "not_found", "message": "Not Found", "details": None}}


def test_validation_error_details_do_not_echo_input(api_client):
    secret = "hunter2-secret-value"
    r = api_client.get("/api/v1/suppliers", params={"limit": secret, "offset": secret})
    assert r.status_code == 422
    assert secret not in r.text
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["message"]
    assert len(err["details"]) == 2
    for d in err["details"]:
        assert set(d) == {"loc", "msg", "type"} and isinstance(d["loc"], list)


def test_unhandled_exception_is_500_internal_error_and_logged(config_path, tmp_path, capsys):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/x.db", config_path=config_path))

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret-db-password=abc123 at C:\\private\\path")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "internal_error" and err["details"] is None
    assert "abc123" not in r.text and "private" not in r.text and "Traceback" not in r.text
    logged = capsys.readouterr().err  # the real cause goes to the server log only
    assert "unhandled error on GET /boom" in logged and "abc123" in logged


def test_domain_errors_map_to_404_and_409(config_path, tmp_path):
    from supplier_reliability.api.errors import ConflictError, NotFoundError

    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/x.db", config_path=config_path))

    @app.get("/nf")
    def nf():
        raise NotFoundError("Widget", "W-1")

    @app.get("/cf")
    def cf():
        raise ConflictError("already exists")

    client = TestClient(app)
    r = client.get("/nf")
    assert r.status_code == 404
    assert r.json()["error"] == {
        "code": "not_found",
        "message": "Widget 'W-1' not found",
        "details": None,
    }
    r = client.get("/cf")
    assert r.status_code == 409
    assert r.json()["error"] == {"code": "conflict", "message": "already exists", "details": None}


# --- request size ------------------------------------------------------------------------------


def test_content_length_over_1mb_is_413(api_client):
    r = api_client.post("/api/v1/suppliers", content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
    assert r.json()["error"]["details"] is None


def test_body_at_the_limit_is_not_rejected_by_the_middleware(api_client):
    r = api_client.post("/api/v1/suppliers", content=b"x" * 1024)
    assert r.status_code == 405  # reaches routing: no POST route exists yet


def _echo_app(limit: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=limit)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"size": len(body)}

    return app


def test_chunked_body_over_limit_is_413_without_content_length():
    client = TestClient(_echo_app(1000))

    def chunks():
        for _ in range(20):
            yield b"y" * 100

    r = client.post("/echo", content=chunks())
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


def test_chunked_body_within_limit_passes():
    client = TestClient(_echo_app(1000))

    def chunks():
        for _ in range(5):
            yield b"y" * 100

    r = client.post("/echo", content=chunks())
    assert r.status_code == 200 and r.json() == {"size": 500}


# --- CORS --------------------------------------------------------------------------------------


def _preflight(client):
    return client.options(
        "/health",
        headers={"Origin": "https://ui.example", "Access-Control-Request-Method": "GET"},
    )


def test_cors_off_by_default(tmp_settings):
    r = _preflight(TestClient(create_app(tmp_settings)))
    assert "access-control-allow-origin" not in r.headers


def test_cors_enabled_only_for_configured_origins(tmp_settings):
    settings = tmp_settings.model_copy(update={"cors_origins": ["https://ui.example"]})
    client = TestClient(create_app(settings))
    r = _preflight(client)
    assert r.headers["access-control-allow-origin"] == "https://ui.example"
    other = client.options(
        "/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in other.headers


def test_max_page_size_comes_from_settings(tmp_settings):
    settings = tmp_settings.model_copy(update={"max_page_size": 7})
    app = create_app(settings)
    # no DB access happens: validation fails first
    r = TestClient(app).get("/api/v1/suppliers", params={"limit": 8})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"][-1] == "limit"


# --- OpenAPI contract --------------------------------------------------------------------------


def test_every_api_route_is_documented(api_client):
    spec = api_client.get("/openapi.json").json()
    api_ops = [
        (path, method, op)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if path.startswith("/api/v1") or path in {"/health", "/ready"}
    ]
    assert len(api_ops) == 13  # 3 system + 5 supplier + 2 PO + 3 shipment routes
    for path, method, op in api_ops:
        assert op.get("summary"), (path, method)
        ok = op["responses"].get("200")
        assert ok and "schema" in ok["content"]["application/json"], (path, method)
        if "{" in path:
            assert "404" in op["responses"], (path, method)
        if op.get("parameters"):
            assert "422" in op["responses"], (path, method)
        for code in ("404", "422"):
            if code in op["responses"]:
                ref = op["responses"][code]["content"]["application/json"]["schema"]
                assert ref["$ref"].endswith("/ErrorEnvelope"), (path, method, code)
    ready = spec["paths"]["/ready"]["get"]["responses"]
    assert "503" in ready


def test_list_endpoints_document_examples(api_client):
    spec = api_client.get("/openapi.json").json()
    params = spec["paths"]["/api/v1/suppliers"]["get"]["parameters"]
    by_name = {p["name"]: p for p in params}
    assert by_name["sort"]["schema"].get("examples") or by_name["sort"].get("examples")
    assert "ErrorEnvelope" in spec["components"]["schemas"]
    assert spec["components"]["schemas"]["ErrorEnvelope"].get("examples")


@pytest.mark.parametrize("path", ["/api/v1/suppliers", "/api/v1/shipments"])
def test_no_filesystem_paths_in_list_responses(api_client, path):
    text = api_client.get(path, params={"limit": 50}).text
    assert str(Path.home()) not in text and ":\\\\" not in text


def test_openapi_exposes_the_configured_limit_maximum(tmp_settings):
    app = create_app(tmp_settings.model_copy(update={"max_page_size": 123}))
    spec = TestClient(app).get("/openapi.json").json()
    seen = 0
    for path, item in spec["paths"].items():
        for op in item.values():
            for p in op.get("parameters", []):
                if p["name"] == "limit":
                    seen += 1
                    assert p["schema"]["maximum"] == 123, path
    assert seen >= 5

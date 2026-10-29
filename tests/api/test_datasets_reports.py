"""Task 21 API tests: dataset generation jobs, manifests, live reports and evaluation."""

import hashlib
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from supplier_reliability.api.dependencies import get_job_runner
from supplier_reliability.config import AppConfig, Settings
from supplier_reliability.database import run_migrations
from supplier_reliability.domain.enums import JobStatus
from supplier_reliability.main import create_app
from supplier_reliability.models import Base, DatasetJob, DatasetManifest
from supplier_reliability.repositories import jobs as jobs_repo
from supplier_reliability.services.anomalies import batch_id_for
from supplier_reliability.services.anomalies.injector import GROUND_TRUTH_FILE
from supplier_reliability.services.datasets import relativise_paths
from supplier_reliability.services.evaluation import evaluate_anomalies
from supplier_reliability.services.jobs import InlineRunner
from supplier_reliability.services.pipeline import PipelineStageError

GENERATE = "/api/v1/datasets/generate"
JOBS = "/api/v1/datasets/jobs"
MANIFESTS = "/api/v1/datasets/manifests"
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def _inline(client: TestClient) -> TestClient:
    client.app.dependency_overrides[get_job_runner] = lambda: InlineRunner()
    return client


def _add_job(client: TestClient, job_id: str, status: JobStatus, **extra) -> None:
    with client.app.state.session_factory() as s:
        s.add(
            DatasetJob(
                job_id=job_id, status=status, preset="starter", seed=1, requested_at=NOW, **extra
            )
        )
        s.commit()


def _job(client: TestClient, job_id: str) -> dict:
    r = client.get(f"{JOBS}/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def empty_inline(empty_api_client) -> TestClient:
    return _inline(empty_api_client)


@pytest.fixture
def evaluated_client(
    api_template_db: Path, api_settings: Settings, api_client: TestClient
) -> TestClient:
    """api_client plus the template run's generated/ground-truth files in this test's data_dir."""
    shutil.copytree(api_template_db.parent / "data", api_settings.data_dir, dirs_exist_ok=True)
    return api_client


# --- generate: job lifecycle ------------------------------------------------------------------


def test_generate_job_lifecycle(empty_inline):
    r = empty_inline.post(GENERATE, json={"preset": "starter"})
    assert r.status_code == 202
    assert r.json() == {"job_id": "JOB-000001", "status": "QUEUED"}
    assert r.headers["location"] == f"{JOBS}/JOB-000001"

    job = _job(empty_inline, "JOB-000001")
    assert job["status"] == "SUCCEEDED"
    assert job["preset"] == "starter" and job["seed"] == 20261022
    assert job["error_message"] is None
    assert job["started_at"].endswith("Z") and job["finished_at"].endswith("Z")
    assert job["started_at"] <= job["finished_at"]
    assert job["current_stage"] is None  # stage entries are dropped at a terminal state
    dataset_id = job["dataset_id"]
    assert dataset_id.startswith("DS-starter-20261022-")

    m = empty_inline.get(f"{MANIFESTS}/{dataset_id}")
    assert m.status_code == 200
    manifest = m.json()
    assert manifest["dataset_id"] == dataset_id and manifest["files"]
    assert all(not Path(f["path"]).is_absolute() for f in manifest["files"])

    # the dataset now exists: a second request without replace is a conflict
    again = empty_inline.post(GENERATE, json={"preset": "starter"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"
    assert again.json()["error"]["message"] == (
        "a dataset is already loaded; set replace_existing to true to replace it"
    )


# --- generate: validation, conflicts ------------------------------------------------------------


def test_generate_conflict_when_data_exists_without_replace(api_client):
    r = _inline(api_client).post(GENERATE, json={"preset": "starter"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert api_client.get(f"{JOBS}/JOB-000001").status_code == 404  # no job row was created


@pytest.mark.parametrize("replace", [False, True])
def test_generate_conflict_when_job_running(empty_inline, replace):
    _add_job(empty_inline, "JOB-000007", JobStatus.RUNNING, started_at=NOW)
    r = empty_inline.post(GENERATE, json={"preset": "starter", "replace_existing": replace})
    assert r.status_code == 409
    assert r.json()["error"]["message"] == ("a dataset generation job is already queued or running")


def test_generate_conflict_when_job_queued(empty_inline):
    _add_job(empty_inline, "JOB-000002", JobStatus.QUEUED)
    assert empty_inline.post(GENERATE, json={"preset": "starter"}).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"preset": "gigantic"},
        {"preset": "starter", "seed": -1},
        {"preset": "starter", "seed": "abc"},
        {"preset": "starter", "replace_existing": "maybe"},
        {},
    ],
)
def test_generate_rejects_invalid_body(empty_inline, body):
    r = empty_inline.post(GENERATE, json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_generate_passes_options_and_numbers_jobs(api_client, monkeypatch):
    calls, seen = [], []

    def fake(settings, app_config, preset, seed, replace_existing, on_progress=None):
        calls.append((preset, seed, replace_existing))
        on_progress("generate")
        seen.append(api_client.app.state.job_stages.get("JOB-000001"))
        return SimpleNamespace(dataset_id="DS-fake-1-abcd1234")

    monkeypatch.setattr("supplier_reliability.services.jobs.run_pipeline", fake)
    _inline(api_client)
    r = api_client.post(GENERATE, json={"preset": "full", "seed": 7, "replace_existing": True})
    assert r.status_code == 202 and r.json()["job_id"] == "JOB-000001"
    assert calls == [("full", 7, True)]
    assert seen == ["generate"]  # visible to the job endpoint while running
    assert "JOB-000001" not in api_client.app.state.job_stages
    job = _job(api_client, "JOB-000001")
    assert job["status"] == "SUCCEEDED" and job["dataset_id"] == "DS-fake-1-abcd1234"
    assert job["current_stage"] is None
    second = api_client.post(GENERATE, json={"preset": "starter", "replace_existing": True})
    assert second.json()["job_id"] == "JOB-000002"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            PipelineStageError("load", ValueError("boom C:\\secret\\x.db sqlite:///pw@host")),
            "stage 'load' failed: ValueError",
        ),
        (RuntimeError("/etc/passwd Traceback (most recent call last)"), "RuntimeError"),
    ],
)
def test_failed_job_reports_sanitized_error(empty_inline, monkeypatch, exc, expected):
    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr("supplier_reliability.services.jobs.run_pipeline", boom)
    assert empty_inline.post(GENERATE, json={"preset": "starter"}).status_code == 202
    job = _job(empty_inline, "JOB-000001")
    assert job["status"] == "FAILED"
    assert job["error_message"] == expected
    assert job["dataset_id"] is None and job["finished_at"] is not None
    # a failed job no longer blocks a new request
    assert empty_inline.post(GENERATE, json={"preset": "starter"}).status_code == 202


# --- terminal-state writes never strand a job ------------------------------------------------


def _fake_pipeline(monkeypatch):
    monkeypatch.setattr(
        "supplier_reliability.services.jobs.run_pipeline",
        lambda *a, **k: SimpleNamespace(dataset_id="DS-fake-1-abcd1234"),
    )


def _failing_pipeline(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("original pipeline error /secret/path")

    monkeypatch.setattr("supplier_reliability.services.jobs.run_pipeline", boom)


def _flaky(real, failures: int):
    state = {"left": failures}

    def wrapper(*args, **kwargs):
        if state["left"] > 0:
            state["left"] -= 1
            raise RuntimeError("database is locked secret-token")
        return real(*args, **kwargs)

    return wrapper


def _accepts_new_request(client: TestClient) -> bool:
    return (
        client.post(GENERATE, json={"preset": "starter", "replace_existing": True}).status_code
        == 202
    )


def test_success_write_failing_once_is_retried(empty_inline, monkeypatch):
    _fake_pipeline(monkeypatch)
    monkeypatch.setattr(jobs_repo, "mark_succeeded", _flaky(jobs_repo.mark_succeeded, 1))
    assert empty_inline.post(GENERATE, json={"preset": "starter"}).status_code == 202
    job = _job(empty_inline, "JOB-000001")
    assert job["status"] == "SUCCEEDED" and job["dataset_id"] == "DS-fake-1-abcd1234"
    assert _accepts_new_request(empty_inline)


def test_success_write_failing_twice_falls_through_to_failed(empty_inline, monkeypatch):
    _fake_pipeline(monkeypatch)
    monkeypatch.setattr(jobs_repo, "mark_succeeded", _flaky(jobs_repo.mark_succeeded, 99))
    assert empty_inline.post(GENERATE, json={"preset": "starter"}).status_code == 202
    job = _job(empty_inline, "JOB-000001")
    assert job["status"] == "FAILED"  # never left RUNNING
    assert "secret-token" not in str(job)
    assert _accepts_new_request(empty_inline)  # no permanent 409


def test_failed_write_failing_once_still_ends_failed(empty_inline, monkeypatch, caplog):
    _failing_pipeline(monkeypatch)
    monkeypatch.setattr(jobs_repo, "mark_failed", _flaky(jobs_repo.mark_failed, 1))
    with caplog.at_level("ERROR"):
        assert empty_inline.post(GENERATE, json={"preset": "starter"}).status_code == 202
    job = _job(empty_inline, "JOB-000001")
    assert job["status"] == "FAILED" and job["error_message"] == "ValueError"
    # the original error's class is logged (message only: no str(exc)) before any write
    messages = [r.getMessage() for r in caplog.records]
    assert any("ValueError" in m for m in messages)
    assert not any("/secret/path" in m for m in messages)
    assert _accepts_new_request(empty_inline)


def test_failed_write_failing_always_logs_critical_without_secrets(
    empty_inline, monkeypatch, caplog
):
    _failing_pipeline(monkeypatch)
    monkeypatch.setattr(jobs_repo, "mark_failed", _flaky(jobs_repo.mark_failed, 99))
    assert empty_inline.post(GENERATE, json={"preset": "starter"}).status_code == 202
    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert critical and "JOB-000001" in critical[0].getMessage()
    assert "secret-token" not in critical[0].getMessage()


# --- jobs and manifests: lookups ----------------------------------------------------------------


def test_job_and_manifest_lookup_errors(empty_api_client):
    assert empty_api_client.get(f"{JOBS}/JOB-999999").status_code == 404
    assert empty_api_client.get(f"{JOBS}/job_1;drop").status_code == 422
    assert empty_api_client.get(f"{MANIFESTS}/DS-starter-1-abcd1234").status_code == 404
    assert empty_api_client.get(f"{MANIFESTS}/{'A' * 80}").status_code == 422
    # an encoded slash decodes into extra path segments, so no route matches: 404 (not 422)
    assert empty_api_client.get(f"{MANIFESTS}/..%2F..%2Fetc").status_code == 404


def test_manifest_paths_are_relative(api_client, api_settings):
    data = Path(api_settings.data_dir)
    manifest = {
        "dataset_id": "DS-starter-1-abcd1234",
        "files": [
            {"path": "suppliers.csv", "rows": 1, "sha256": "x"},
            {"path": str(data / "generated" / "starter" / "po.csv"), "rows": 1, "sha256": "y"},
            {"path": "C:\\Users\\someone\\elsewhere\\other.csv", "rows": 1, "sha256": "z"},
        ],
        "output": {"dir": "/var/lib/srp/generated"},
        "note": "plain text, not a path",
    }
    with api_client.app.state.session_factory() as s:
        s.add(
            DatasetManifest(dataset_id="DS-starter-1-abcd1234", manifest=manifest, created_at=NOW)
        )
        s.commit()
    body = api_client.get(f"{MANIFESTS}/DS-starter-1-abcd1234").json()
    paths = [f["path"] for f in body["files"]]
    assert paths == ["suppliers.csv", "generated/starter/po.csv", "other.csv"]
    assert body["output"]["dir"] == "generated"
    assert body["note"] == "plain text, not a path"
    text = api_client.get(f"{MANIFESTS}/DS-starter-1-abcd1234").text
    assert str(data) not in text and "someone" not in text and "/var/lib" not in text


def test_relativise_paths_only_rewrites_path_leading_strings(tmp_path):
    """Documented limit: a string is rewritten only when it STARTS with an absolute path;
    paths embedded in prose and dictionary keys are left alone (the pipeline stores relative
    paths, so this is defence in depth, not a scrubber)."""
    prose = "see C:\\Users\\x\\file.csv for details"
    key = "C:\\keys\\are\\not\\rewritten"
    out = relativise_paths({key: prose, "lead": str(tmp_path / "generated" / "a.csv")}, tmp_path)
    assert out[key] == prose
    assert out["lead"] == "generated/a.csv"


def test_real_manifest_exposes_no_filesystem_paths(api_client, api_settings):
    ds = api_client.get("/api/v1/metadata").json()["dataset_id"]
    r = api_client.get(f"{MANIFESTS}/{ds}")
    assert r.status_code == 200
    assert str(Path(api_settings.data_dir)) not in r.text
    assert str(Path(api_settings.data_dir).parent) not in r.text


# --- startup recovery ---------------------------------------------------------------------------


def test_startup_marks_unfinished_jobs_failed(config_path, tmp_path, monkeypatch):
    db = tmp_path / "recover.db"
    run_migrations(f"sqlite:///{db}")
    settings = Settings(
        database_url=f"sqlite:///{db}",
        config_path=config_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )
    with TestClient(create_app(settings)) as first:
        _add_job(first, "JOB-000001", JobStatus.RUNNING, started_at=NOW)
        _add_job(first, "JOB-000002", JobStatus.QUEUED)
        _add_job(first, "JOB-000003", JobStatus.SUCCEEDED, started_at=NOW, finished_at=NOW)
    with TestClient(create_app(settings)) as restarted:
        for job_id in ("JOB-000001", "JOB-000002"):
            job = _job(restarted, job_id)
            assert job["status"] == "FAILED"
            assert job["error_message"] == "interrupted by restart"
            assert job["finished_at"] is not None
        assert _job(restarted, "JOB-000003")["status"] == "SUCCEEDED"
        # nothing is blocking a new request any more
        monkeypatch.setattr(
            "supplier_reliability.services.jobs.run_pipeline",
            lambda *a, **k: SimpleNamespace(dataset_id="DS-x-1-abcd1234"),
        )
        _inline(restarted)
        assert restarted.post(GENERATE, json={"preset": "starter"}).status_code == 202


def test_startup_survives_unmigrated_database(config_path, tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'blank.db'}",
        config_path=config_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200


# --- reports -----------------------------------------------------------------------------------


def test_reports_endpoints_return_metrics(api_client):
    summary = api_client.get("/api/v1/reports/summary")
    assert summary.status_code == 200
    s = summary.json()
    assert s["report"] == "summary" and s["metrics"]["suppliers"]["value"] > 0
    rankings = api_client.get("/api/v1/reports/supplier-rankings")
    assert rankings.status_code == 200
    assert rankings.json()["report"] == "supplier_rankings"
    assert rankings.json()["metrics"]["ranked_suppliers"]["value"] > 0
    quality = api_client.get("/api/v1/reports/data-quality")
    assert quality.status_code == 200
    assert quality.json()["report"] == "data_quality"
    assert "defects" in quality.json()["metrics"]


def test_reports_are_404_on_an_empty_database(empty_api_client):
    for name in ("summary", "supplier-rankings", "data-quality", "anomaly-evaluation"):
        r = empty_api_client.get(f"/api/v1/reports/{name}")
        assert r.status_code == 404, name
        assert r.json()["error"]["code"] == "not_found"


def _tree(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _row_counts(client: TestClient) -> dict[str, int]:
    with client.app.state.session_factory() as s:
        return {
            t.name: s.scalar(sa.select(sa.func.count()).select_from(t))
            for t in Base.metadata.sorted_tables
        }


def test_reports_and_evaluation_write_nothing(evaluated_client, api_settings):
    data, reports = Path(api_settings.data_dir), Path(api_settings.reports_dir)
    files_before = (_tree(data), _tree(reports))
    rows_before = _row_counts(evaluated_client)
    assert files_before[0]  # ground truth and exports are really there
    for name in ("summary", "supplier-rankings", "data-quality", "anomaly-evaluation"):
        assert evaluated_client.get(f"/api/v1/reports/{name}").status_code == 200, name
    assert evaluated_client.post("/api/v1/evaluation/run").status_code == 200
    assert (_tree(data), _tree(reports)) == files_before
    assert not reports.exists()
    assert _row_counts(evaluated_client) == rows_before


def test_anomaly_evaluation_404_before_run(empty_api_client, api_client):
    r = empty_api_client.get("/api/v1/reports/anomaly-evaluation")
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "no evaluation available"
    # data and manifest exist but this data_dir holds no ground-truth file
    r = api_client.get("/api/v1/reports/anomaly-evaluation")
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "no evaluation available"


def test_anomaly_evaluation_report_when_ground_truth_exists(evaluated_client, api_settings):
    r = evaluated_client.get("/api/v1/reports/anomaly-evaluation")
    assert r.status_code == 200
    body = r.json()
    assert body["report"] == "anomaly_evaluation"
    assert len(body["metrics"]["by_class"]) == 5
    assert str(api_settings.data_dir) not in r.text


# --- evaluation run ----------------------------------------------------------------------------


def test_evaluation_run_returns_per_class_metrics(
    evaluated_client, api_settings, app_config: AppConfig
):
    r = evaluated_client.post("/api/v1/evaluation/run")
    assert r.status_code == 200, r.text
    body = r.json()
    classes = body["anomaly_evaluation"]["per_class"]
    assert len(classes) == 5
    for cell in classes.values():
        assert set(cell) >= {"tp", "fp", "fn", "tn", "precision", "recall", "f1"}

    manifest_id = body["dataset_id"]
    with evaluated_client.app.state.session_factory() as s:
        direct = evaluate_anomalies(
            s,
            batch_id_for(manifest_id),
            Path(api_settings.data_dir) / "ground_truth" / "starter" / GROUND_TRUTH_FILE,
        )
    micro = body["anomaly_evaluation"]["micro"]
    assert (micro["tp"], micro["fp"], micro["fn"], micro["tn"]) == (
        direct.micro.tp,
        direct.micro.fp,
        direct.micro.fn,
        direct.micro.tn,
    )
    assert micro["tp"] > 0
    assert {k: classes[k]["tp"] for k in classes} == {
        i.value: c.tp for i, c in direct.per_class.items()
    }

    rec = body["reconciliation"]
    assert rec["checked"] > 0 and rec["matched"] == rec["checked"]
    assert rec["mismatches_count"] == 0 and rec["mismatches"] == []
    assert rec["missing_persisted"] == 0
    assert rec["tolerance"] == str(Decimal(0))
    assert str(api_settings.data_dir) not in r.text


def test_evaluation_run_404_without_dataset_or_export(empty_api_client, api_client):
    for client in (empty_api_client, api_client):  # api_client: no export dir in its data_dir
        r = client.post("/api/v1/evaluation/run")
        assert r.status_code == 404
        assert r.json()["error"]["message"] == "no evaluation available"


# --- OpenAPI -----------------------------------------------------------------------------------

SPEC_12_1 = {
    ("/health", "get"),
    ("/ready", "get"),
    ("/api/v1/metadata", "get"),
    ("/api/v1/suppliers", "get"),
    ("/api/v1/suppliers/{supplier_id}", "get"),
    ("/api/v1/suppliers/{supplier_id}/performance", "get"),
    ("/api/v1/suppliers/{supplier_id}/scores", "get"),
    ("/api/v1/suppliers/{supplier_id}/explanation", "get"),
    ("/api/v1/scores", "get"),
    ("/api/v1/scores/{score_id}", "get"),
    ("/api/v1/scores/calculate", "post"),
    ("/api/v1/scores/recalculate", "post"),
    ("/api/v1/purchase-orders", "get"),
    ("/api/v1/purchase-orders/{po_id}", "get"),
    ("/api/v1/shipments", "get"),
    ("/api/v1/shipments/{shipment_id}", "get"),
    ("/api/v1/shipments/{shipment_id}/events", "get"),
    ("/api/v1/validation/run", "post"),
    ("/api/v1/validation/results", "get"),
    ("/api/v1/exceptions", "get"),
    ("/api/v1/exceptions/{exception_id}", "get"),
    ("/api/v1/datasets/generate", "post"),
    ("/api/v1/datasets/jobs/{job_id}", "get"),
    ("/api/v1/datasets/manifests/{dataset_id}", "get"),
    ("/api/v1/reports/summary", "get"),
    ("/api/v1/reports/supplier-rankings", "get"),
    ("/api/v1/reports/data-quality", "get"),
    ("/api/v1/reports/anomaly-evaluation", "get"),
    ("/api/v1/evaluation/run", "post"),
}


def test_openapi_lists_all_spec_endpoints(api_client):
    spec = api_client.get("/openapi.json").json()
    missing = [(p, m) for p, m in SPEC_12_1 if m not in spec["paths"].get(p, {})]
    assert missing == []


NEW_ROUTES = {
    ("/api/v1/datasets/generate", "post"): ("202", {"409", "422"}),
    ("/api/v1/datasets/jobs/{job_id}", "get"): ("200", {"404", "422"}),
    ("/api/v1/datasets/manifests/{dataset_id}", "get"): ("200", {"404", "422"}),
    ("/api/v1/reports/summary", "get"): ("200", {"404"}),
    ("/api/v1/reports/supplier-rankings", "get"): ("200", {"404"}),
    ("/api/v1/reports/data-quality", "get"): ("200", {"404"}),
    ("/api/v1/reports/anomaly-evaluation", "get"): ("200", {"404"}),
    ("/api/v1/evaluation/run", "post"): ("200", {"404"}),
}


def test_new_routes_are_documented(api_client):
    spec = api_client.get("/openapi.json").json()
    for (path, method), (success, errors) in NEW_ROUTES.items():
        op = spec["paths"][path][method]
        assert op.get("summary") and op.get("description"), (path, method)
        assert success in op["responses"], (path, method)
        assert "schema" in op["responses"][success]["content"]["application/json"]
        assert not ({"200", "201", "202"} - {success}) & set(op["responses"]), (path, method)
        for code in errors:
            ref = op["responses"][code]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith("/ErrorEnvelope"), (path, method, code)

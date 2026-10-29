"""Task 18: end-to-end pipeline orchestration and the Typer CLI (`srp`).

One starter pipeline run is shared by the module (it takes the bulk of the runtime); every CLI
invocation points SRP_* at tmp_path so nothing touches ./data, ./reports or a developer DB."""

import csv
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from supplier_reliability.cli import app
from supplier_reliability.config import Settings, load_app_config
from supplier_reliability.database import make_engine
from supplier_reliability.domain.enums import IssueType
from supplier_reliability.models import DatasetManifest, Supplier, SupplierScore
from supplier_reliability.services import pipeline
from supplier_reliability.services.pipeline import (
    PipelineStageError,
    remove_output_dir,
    run_pipeline,
)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
SEED = 20261022
REPORT_NAMES = {
    "entity_counts",
    "supplier_rankings",
    "score_distribution",
    "delivery_quality_trends",
    "invoice_variance_distribution",
    "capacity_risk",
    "compliance_exceptions",
    "disruption_exposure",
    "missing_data_rates",
    "deteriorating_suppliers",
    "data_quality",
    "anomaly_evaluation",
    "reconciliation",
    "summary",
}

runner = CliRunner()


def _env(root: Path) -> dict[str, str]:
    return {
        "SRP_DATABASE_URL": f"sqlite:///{(root / 'srp.db').as_posix()}",
        "SRP_CONFIG_PATH": str(CONFIG_PATH),
        "SRP_DATA_DIR": str(root / "data"),
        "SRP_REPORTS_DIR": str(root / "reports"),
    }


def _settings(root: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(root / 'srp.db').as_posix()}",
        config_path=CONFIG_PATH,
        data_dir=root / "data",
        reports_dir=root / "reports",
    )


def _invoke(root: Path, *args: str):
    return runner.invoke(app, list(args), env=_env(root))


def _json(output: str) -> dict:
    return json.loads(output)


@pytest.fixture(scope="module")
def starter(tmp_path_factory) -> tuple[Path, dict, dict]:
    """(root, printed summary, manifest file) of one `srp generate --preset starter` run."""
    root = tmp_path_factory.mktemp("pipeline")
    result = _invoke(root, "generate", "--preset", "starter", "--seed", str(SEED))
    assert result.exit_code == 0, result.output
    printed = _json(result.stdout)
    manifest_files = list((root / "data" / "manifests").glob("*.json"))
    assert len(manifest_files) == 1
    manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
    return root, printed, manifest


def test_cli_generate_starter_end_to_end(starter):
    root, printed, manifest = starter
    counts = printed["counts"]
    assert (counts["suppliers"], counts["purchase_orders"], counts["shipments"]) == (50, 300, 450)
    assert counts["inspections"] == 600

    dataset_id = printed["dataset_id"]
    assert manifest["dataset_id"] == dataset_id
    assert manifest["row_counts"]["suppliers"] == 50
    meta = manifest["execution_metadata"]
    assert {"generated_at", "duration_seconds", "git_sha", "hostname", "python_version"} <= set(
        meta
    )
    assert (root / "data" / "manifests" / f"{dataset_id}.json").is_file()

    engine = make_engine(_env(root)["SRP_DATABASE_URL"])
    with engine.connect() as conn:
        row = conn.execute(sa.select(DatasetManifest.manifest)).one()
        assert row.manifest["content_checksum"] == manifest["content_checksum"]
        assert conn.scalar(sa.select(sa.func.count()).select_from(Supplier)) == 50
    engine.dispose()

    export = root / "data" / "generated" / "starter"
    for entry in manifest["files"]:
        assert (export / entry["path"]).is_file()
    assert (export / "supplier_scores.csv").is_file()
    assert (export / "raw_ingestion_records.csv").is_file()

    gt = root / "data" / "ground_truth" / "starter"
    with (gt / "anomaly_ground_truth.csv").open(encoding="utf-8", newline="") as fh:
        cases = list(csv.DictReader(fh))
    assert len(cases) == 25
    assert (gt / "supplier_profiles.csv").is_file()

    reports = root / "reports" / "starter"
    for name in REPORT_NAMES:
        assert (reports / f"{name}.json").is_file(), name
        assert (reports / f"{name}.md").is_file(), name
    rec = json.loads((reports / "reconciliation.json").read_text(encoding="utf-8"))
    assert rec["metrics"]["mismatched_scores"]["value"] == 0
    assert rec["metrics"]["checked"]["value"] == 300  # 50 suppliers x 6 scorecard windows
    ev = json.loads((reports / "anomaly_evaluation.json").read_text(encoding="utf-8"))
    for issue in IssueType:
        assert ev["metrics"]["by_class"][issue.value]["tp"]["value"] == 5, issue
    summary = json.loads((reports / "summary.json").read_text(encoding="utf-8"))
    assert "generated_at" not in summary  # wall clock only in the manifest

    # printed summary: detection per class, reconciliation, validation, repo-relative paths
    for issue in IssueType:
        assert printed["detection"][issue.value]["tp"] == 5
    assert printed["reconciliation"] == {"checked": 300, "matched": 300}
    assert set(printed["validation"]) == {"run_id", "defects", "informational"}
    assert printed["paths"]["manifest"].endswith(f"data/manifests/{dataset_id}.json")


def test_cli_generate_refuses_without_replace(starter):
    root, _, manifest = starter
    result = _invoke(root, "generate", "--preset", "starter", "--seed", str(SEED))
    assert result.exit_code == 2
    assert "dataset already loaded; pass --replace" in result.output
    assert "Traceback" not in result.output
    path = root / "data" / "manifests" / f"{manifest['dataset_id']}.json"
    assert json.loads(path.read_text(encoding="utf-8")) == manifest  # nothing touched


def test_cli_generate_replace_reproduces_checksums(starter):
    root, _, manifest = starter
    stale = root / "reports" / "starter" / "stale.json"
    stale.write_text("{}", encoding="utf-8")
    result = _invoke(root, "generate", "--preset", "starter", "--seed", str(SEED), "--replace")
    assert result.exit_code == 0, result.output
    again = json.loads(
        (root / "data" / "manifests" / f"{manifest['dataset_id']}.json").read_text(encoding="utf-8")
    )
    assert again["content_checksum"] == manifest["content_checksum"]
    assert again["files"] == manifest["files"]
    assert not stale.exists()  # the preset's report directory was cleared
    engine = make_engine(_env(root)["SRP_DATABASE_URL"])
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(Supplier)) == 50
        assert conn.scalar(sa.select(sa.func.count()).select_from(DatasetManifest)) == 1
    engine.dispose()


def test_cli_score_idempotent(starter):
    root, _, _ = starter
    args = ("score", "--start", "2025-04-01T00:00:00Z", "--end", "2025-06-30T23:59:59Z")
    first = _invoke(root, *args)
    assert first.exit_code == 0, first.output
    assert _json(first.stdout)["created"] == 50
    second = _invoke(root, *args)
    assert second.exit_code == 0, second.output
    out = _json(second.stdout)
    assert out["created"] == 0
    assert out["existing"] == 50
    engine = make_engine(_env(root)["SRP_DATABASE_URL"])
    with engine.connect() as conn:
        ids = conn.scalars(
            sa.select(SupplierScore.score_id).where(SupplierScore.score_id.in_(out["score_ids"]))
        ).all()
    engine.dispose()
    assert sorted(ids) == sorted(_json(first.stdout)["score_ids"])


def test_cli_score_rejects_naive_timestamp(starter):
    root, _, _ = starter
    result = _invoke(root, "score", "--start", "2025-04-01T00:00:00", "--end", "2025-06-30")
    assert result.exit_code == 1
    assert "UTC offset" in result.output
    assert "Traceback" not in result.output


def test_failing_stage_exits_1_without_traceback(tmp_path, monkeypatch):
    def boom(cfg):
        raise RuntimeError("synthetic generator failure")

    monkeypatch.setattr(pipeline, "generate_dataset", boom)
    result = _invoke(tmp_path, "generate", "--preset", "starter")
    assert result.exit_code == 1
    assert "generate" in result.output
    assert "synthetic generator failure" in result.output
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_run_pipeline_reports_progress_and_stage_on_failure(tmp_path, monkeypatch):
    def boom(cfg):
        raise RuntimeError("synthetic generator failure")

    monkeypatch.setattr(pipeline, "generate_dataset", boom)
    stages: list[str] = []
    with pytest.raises(PipelineStageError) as info:
        run_pipeline(
            _settings(tmp_path),
            load_app_config(CONFIG_PATH),
            "starter",
            SEED,
            replace_existing=False,
            on_progress=stages.append,
        )
    assert info.value.stage == "generate"
    assert isinstance(info.value.cause, RuntimeError)
    assert stages == ["configure", "migrate", "replace_guard", "generate"]


def test_run_pipeline_rejects_unknown_preset_before_touching_anything(tmp_path):
    with pytest.raises(PipelineStageError) as info:
        run_pipeline(_settings(tmp_path), load_app_config(CONFIG_PATH), "huge", SEED, False)
    assert "unknown preset" in str(info.value)
    assert not (tmp_path / "srp.db").exists()


def test_remove_output_dir_refuses_paths_outside_base(tmp_path):
    base = tmp_path / "data"
    inside = base / "generated" / "starter"
    inside.mkdir(parents=True)
    (inside / "x.csv").write_text("a\n", encoding="utf-8")
    remove_output_dir(inside, base)
    assert not inside.exists()
    remove_output_dir(inside, base)  # already gone: no error

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ValueError, match="outside"):
        remove_output_dir(outside, base)
    with pytest.raises(ValueError, match="outside"):
        remove_output_dir(base / ".." / "elsewhere", base)
    with pytest.raises(ValueError, match="outside"):
        remove_output_dir(base, base)  # the base itself is never removed
    assert outside.exists()

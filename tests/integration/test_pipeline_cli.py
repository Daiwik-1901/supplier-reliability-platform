"""Task 18: end-to-end pipeline orchestration and the Typer CLI (`srp`).

One starter pipeline run is shared by the module (it takes the bulk of the runtime); every CLI
invocation points SRP_* at tmp_path so nothing touches ./data, ./reports or a developer DB."""

import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from supplier_reliability.cli import app
from supplier_reliability.config import Settings, load_app_config
from supplier_reliability.database import make_engine, make_session_factory, session_scope
from supplier_reliability.domain.enums import IssueType, JobStatus
from supplier_reliability.models import DatasetJob, DatasetManifest, Supplier, SupplierScore
from supplier_reliability.services import pipeline
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.pipeline import (
    PipelineStageError,
    remove_output_dir,
    run_pipeline,
)
from supplier_reliability.services.scoring.evidence import quarter_windows, scorecard_windows

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
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


class Starter(NamedTuple):
    root: Path  # shared by the module; tests that only read it may use it directly
    printed: dict
    manifest: dict
    snapshot: Path  # untouched copy of root taken right after the run


@pytest.fixture(scope="module")
def starter(tmp_path_factory) -> Starter:
    """One `srp generate --preset starter` run (root, printed summary, manifest, snapshot)."""
    root = tmp_path_factory.mktemp("pipeline")
    result = _invoke(root, "generate", "--preset", "starter", "--seed", str(SEED))
    assert result.exit_code == 0, result.output
    printed = _json(result.stdout)
    manifest_files = list((root / "data" / "manifests").glob("*.json"))
    assert len(manifest_files) == 1
    manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
    snapshot = tmp_path_factory.mktemp("pipeline-snapshot") / "root"
    shutil.copytree(root, snapshot)
    return Starter(root, printed, manifest, snapshot)


def _fresh(starter: Starter, tmp_path: Path) -> Path:
    """A private copy of the freshly generated starter root (DB + data + reports)."""
    root = tmp_path / "root"
    shutil.copytree(starter.snapshot, root)
    return root


def _count(root: Path, model) -> int:
    engine = make_engine(_env(root)["SRP_DATABASE_URL"])
    try:
        with engine.connect() as conn:
            return conn.scalar(sa.select(sa.func.count()).select_from(model))
    finally:
        engine.dispose()


def _link_dir(link: Path, target: Path) -> None:
    """Directory symlink, or a Windows junction when symlinks need a privilege."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if sys.platform != "win32":
            raise
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_cli_generate_starter_end_to_end(starter):
    root, printed, manifest = starter.root, starter.printed, starter.manifest
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
    root, manifest = starter.root, starter.manifest
    result = _invoke(root, "generate", "--preset", "starter", "--seed", str(SEED))
    assert result.exit_code == 2
    assert "dataset already loaded; pass --replace" in result.output
    assert "Traceback" not in result.output
    path = root / "data" / "manifests" / f"{manifest['dataset_id']}.json"
    assert json.loads(path.read_text(encoding="utf-8")) == manifest  # nothing touched


def test_cli_generate_replace_reproduces_checksums(starter):
    root, manifest = starter.root, starter.manifest
    stale = root / "reports" / "starter" / "stale.json"
    stale.write_text("{}", encoding="utf-8")
    other_manifest = root / "data" / "manifests" / "DS-full-1-deadbeef.json"
    other_manifest.write_text('{"preset": "full"}\n', encoding="utf-8")
    engine = make_engine(_env(root)["SRP_DATABASE_URL"])
    with session_scope(make_session_factory(engine)) as s:
        s.add(
            DatasetJob(
                job_id="JOB-000001",
                status=JobStatus.RUNNING,
                preset="starter",
                seed=SEED,
                requested_at=datetime(2026, 10, 29, tzinfo=UTC),
            )
        )
    engine.dispose()

    result = _invoke(root, "generate", "--preset", "starter", "--seed", str(SEED), "--replace")
    assert result.exit_code == 0, result.output
    again = json.loads(
        (root / "data" / "manifests" / f"{manifest['dataset_id']}.json").read_text(encoding="utf-8")
    )
    assert again["content_checksum"] == manifest["content_checksum"]
    assert again["files"] == manifest["files"]
    assert not stale.exists()  # the preset's report directory was cleared
    assert _count(root, Supplier) == 50
    assert _count(root, DatasetManifest) == 1
    # R47: job history and other datasets' manifest files survive a replace
    assert _count(root, DatasetJob) == 1
    assert other_manifest.read_text(encoding="utf-8") == '{"preset": "full"}\n'


def test_replace_checks_output_dirs_before_emptying_the_database(starter, tmp_path):
    root = _fresh(starter, tmp_path)
    export = root / "data" / "generated" / "starter"
    shutil.rmtree(export)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.csv").write_text("x\n", encoding="utf-8")
    _link_dir(export, outside)  # data/generated/starter now resolves outside data_dir

    result = _invoke(root, "generate", "--preset", "starter", "--replace")
    assert result.exit_code == 1
    assert "replace_guard" in result.output
    assert "outside" in result.output
    assert "Traceback" not in result.output
    assert _count(root, Supplier) == 50  # nothing deleted
    assert _count(root, DatasetManifest) == 1
    assert (outside / "keep.csv").is_file()
    assert (root / "reports" / "starter" / "summary.json").is_file()
    assert (root / "data" / "ground_truth" / "starter" / "anomaly_ground_truth.csv").is_file()


def _non_scorecard_quarter():
    """A calendar quarter of the reference period that is not one of the scorecard windows."""
    app_config = load_app_config(CONFIG_PATH)
    cfg = GeneratorConfig.from_app_config(app_config, "starter")
    windows = scorecard_windows(
        cfg.reference_start, cfg.reference_end, app_config.scoring.window_months
    )
    taken = {(w.start, w.end, w.cutoff) for w in windows}
    quarters = quarter_windows(cfg.reference_start, cfg.reference_end)
    return next(w for w in quarters if (w.start, w.end, w.cutoff) not in taken)


def test_cli_score_idempotent(starter, tmp_path):
    root = _fresh(starter, tmp_path)
    window = _non_scorecard_quarter()
    n_suppliers = load_app_config(CONFIG_PATH).generator.presets["starter"]["suppliers"]
    args = ("score", "--start", window.start.isoformat(), "--end", window.end.isoformat())
    first = _invoke(root, *args)
    assert first.exit_code == 0, first.output
    assert _json(first.stdout)["created"] == n_suppliers
    before = _count(root, SupplierScore)
    second = _invoke(root, *args)
    assert second.exit_code == 0, second.output
    out = _json(second.stdout)
    assert out["created"] == 0
    assert out["existing"] == n_suppliers
    assert out["score_ids"] == _json(first.stdout)["score_ids"]
    assert _count(root, SupplierScore) == before  # the second call wrote nothing


def _last_scorecard_args() -> list[str]:
    app_config = load_app_config(CONFIG_PATH)
    cfg = GeneratorConfig.from_app_config(app_config, "starter")
    w = scorecard_windows(cfg.reference_start, cfg.reference_end, app_config.scoring.window_months)[
        -1
    ]
    return ["--start", w.start.isoformat(), "--end", w.end.isoformat()]


@pytest.mark.parametrize(
    ("args", "check"),
    [
        (["init-db"], lambda out: out["status"] == "migrated"),
        (
            ["recalculate", *_last_scorecard_args(), "--reason", "smoke"],
            lambda out: out["created"] == 0 and out["unchanged"] + out["existing"] == 50,
        ),
        (["validate"], lambda out: out["run_id"] == "VAL-000002" and out["defects"] == 0),
        (["detect"], lambda out: out["new_exceptions"] == 0),
        (
            ["evaluate"],
            lambda out: out["micro"]["tp"] == 25 and out["reconciliation"]["matched"] == 300,
        ),
        (["report"], lambda out: len(out["reports"]) == 14),
    ],
    ids=["init-db", "recalculate", "validate", "detect", "evaluate", "report"],
)
def test_cli_commands_smoke(starter, tmp_path, args, check):
    root = _fresh(starter, tmp_path)
    result = _invoke(root, *args)
    assert result.exit_code == 0, result.output
    assert check(_json(result.stdout)), result.stdout


def test_detect_uses_the_injected_batch_id(starter, tmp_path):
    from supplier_reliability.services.anomalies import batch_id_for

    root = _fresh(starter, tmp_path)
    result = _invoke(root, "detect")
    assert result.exit_code == 0, result.output
    assert _json(result.stdout)["batch_id"] == batch_id_for(starter.manifest["dataset_id"])


@pytest.mark.parametrize(
    ("script", "args", "code", "expected"),
    [
        ("validate_data.py", [], 0, '"run_id": "VAL-000002"'),
        ("generate_data.py", ["--preset", "starter"], 2, "dataset already loaded; pass --replace"),
    ],
)
def test_scripts_wrap_the_cli(starter, tmp_path, script, args, code, expected):
    root = _fresh(starter, tmp_path)
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
        env=os.environ | _env(root),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == code, done.stderr
    assert expected in done.stdout + done.stderr


def test_cli_score_rejects_naive_timestamp(starter):
    result = _invoke(starter.root, "score", "--start", "2025-04-01T00:00:00", "--end", "2025-06-30")
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


@pytest.mark.parametrize("preset", ["huge", "../x", "starter/../full", "a\\b", "../../reports"])
def test_run_pipeline_rejects_unknown_preset_before_touching_anything(tmp_path, preset):
    with pytest.raises(PipelineStageError) as info:
        run_pipeline(_settings(tmp_path), load_app_config(CONFIG_PATH), preset, SEED, True)
    assert info.value.stage == "configure"
    assert "unknown preset" in str(info.value)
    assert sorted(p.name for p in tmp_path.iterdir()) == []  # no DB, data or reports dirs


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

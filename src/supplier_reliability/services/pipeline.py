"""End-to-end pipeline (Ruling R46): the one orchestration used by the CLI and the API job.

Stages, in order (each name is passed to ``on_progress`` when the stage starts):
configure -> migrate -> replace_guard -> generate -> load -> score -> inject -> detect ->
validate -> export -> ground_truth -> evaluate -> reports -> manifest.

- score: every supplier for the 6 scorecard windows, calculated_at = cutoff + 1 day.
- detect and validate run with the fixed clock reference_end + 2 days.
- export: every default table to ``<data_dir>/generated/<preset>/``; ground truth to
  ``<data_dir>/ground_truth/<preset>/``; reports to ``<reports_dir>/<preset>/`` (no
  generated_at); manifest to ``<data_dir>/manifests/<dataset_id>.json`` plus a DatasetManifest
  row. Wall-clock time appears only in the manifest's ``execution_metadata`` (and the row's
  created_at, which is that same instant).

Replace guard: if the database already holds data and ``replace_existing`` is False,
``DatasetAlreadyLoadedError`` is raised. With ``replace_existing`` every table except
``dataset_jobs`` (operational job history, which the API job that started this run is itself
recorded in) is emptied in reverse FK order in ONE transaction, and the preset's output
directories are removed (only inside ``data_dir`` / ``reports_dir``).

Transactions: each stage commits on success; the pipeline as a whole is NOT one transaction.
A failing stage raises ``PipelineStageError(stage, cause)`` and leaves the database as the
previous stages left it (rerun with ``replace_existing=True`` to start over).
"""

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from supplier_reliability.config import AppConfig, Settings
from supplier_reliability.database import (
    make_engine,
    make_session_factory,
    run_migrations,
    session_scope,
)
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import Base, DatasetManifest
from supplier_reliability.services.anomalies import inject_anomalies, write_ground_truth
from supplier_reliability.services.anomalies.injector import GROUND_TRUTH_FILE
from supplier_reliability.services.anomaly_detection import detect_raw_batch
from supplier_reliability.services.evaluation import (
    AnomalyEvaluation,
    evaluate_anomalies,
    reconcile_scores,
)
from supplier_reliability.services.export import (
    build_manifest,
    execution_metadata,
    export_tables,
)
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import (
    DatasetAlreadyLoadedError,
    load_dataset,
)
from supplier_reliability.services.generator.rng import RandomSource
from supplier_reliability.services.reporting import (
    anomaly_evaluation_report,
    capacity_risk,
    compliance_exceptions,
    data_quality,
    delivery_quality_trends,
    deteriorating_suppliers,
    disruption_exposure,
    entity_counts,
    invoice_variance_distribution,
    missing_data_rates,
    reconciliation_report,
    score_distribution,
    summary,
    supplier_rankings,
    write_report,
)
from supplier_reliability.services.reporting.quality_reports import DEFECT_SEVERITIES
from supplier_reliability.services.scoring.evidence import EvaluationWindow, scorecard_windows
from supplier_reliability.services.scoring.service import ScoringService
from supplier_reliability.services.validation import run_validation

__all__ = [
    "KEPT_TABLES",
    "STAGES",
    "DatasetAlreadyLoadedError",
    "OutputPaths",
    "PipelineResult",
    "PipelineStageError",
    "current_manifest",
    "git_sha",
    "output_paths",
    "remove_output_dir",
    "run_pipeline",
    "validation_counts",
    "write_reports",
]

STAGES = (
    "configure",
    "migrate",
    "replace_guard",
    "generate",
    "load",
    "score",
    "inject",
    "detect",
    "validate",
    "export",
    "ground_truth",
    "evaluate",
    "reports",
    "manifest",
)
KEPT_TABLES = frozenset({"dataset_jobs"})  # never emptied by the replace guard
_REPO_ROOT = Path(__file__).resolve().parents[3]


class PipelineStageError(RuntimeError):
    """A pipeline stage failed; `cause` is the original exception."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"pipeline stage {stage!r} failed: {type(cause).__name__}: {cause}")
        self.stage = stage
        self.cause = cause


@dataclass(frozen=True)
class OutputPaths:
    export_dir: Path
    ground_truth_dir: Path
    reports_dir: Path
    manifests_dir: Path


@dataclass(frozen=True)
class PipelineResult:
    dataset_id: str
    counts: dict[str, int]  # rows loaded per canonical table
    manifest_path: Path
    validation_run_id: str
    batch_id: str
    evaluation: AnomalyEvaluation
    report_paths: dict[str, tuple[Path, Path]]  # report name -> (json, md)
    validation: dict[str, int]  # {"defects", "informational"}
    reconciliation: dict[str, int]  # {"checked", "matched", "mismatched", "missing_persisted"}
    content_checksum: str
    paths: OutputPaths


def output_paths(settings: Settings, preset: str) -> OutputPaths:
    data = Path(settings.data_dir)
    return OutputPaths(
        export_dir=data / "generated" / preset,
        ground_truth_dir=data / "ground_truth" / preset,
        reports_dir=Path(settings.reports_dir) / preset,
        manifests_dir=data / "manifests",
    )


def remove_output_dir(path: Path, base: Path) -> None:
    """Delete directory `path` (if present) only when it resolves strictly inside `base`."""
    resolved, root = Path(path).resolve(), Path(base).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"refusing to remove {path}: outside {base}")
    if resolved.exists():
        shutil.rmtree(resolved)


def git_sha() -> str:
    """`git rev-parse HEAD` of the repository, or "unknown"."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else "unknown"


def validation_counts(counts_by_severity: dict[str, int]) -> dict[str, int]:
    """Defects (MEDIUM and worse) vs informational (LOW / NONE) findings of a validation run."""
    defect = {s.value for s in DEFECT_SEVERITIES}
    return {
        "defects": sum(n for s, n in counts_by_severity.items() if s in defect),
        "informational": sum(n for s, n in counts_by_severity.items() if s not in defect),
    }


def current_manifest(session: Session) -> dict[str, Any]:
    """The manifest of the dataset loaded in this database."""
    rows = session.scalars(sa.select(DatasetManifest).order_by(DatasetManifest.dataset_id)).all()
    if not rows:
        raise LookupError("no dataset manifest in the database; run `srp generate` first")
    if len(rows) > 1:
        raise LookupError(f"{len(rows)} dataset manifests in the database; expected one")
    return rows[0].manifest


def write_reports(
    session: Session,
    app_config: AppConfig,
    out_dir: Path,
    *,
    validation_run_id: str,
    batch_id: str,
    ground_truth_path: Path,
    export_dir: Path,
    windows: Sequence[EvaluationWindow],
    tolerance: Decimal = Decimal(0),
) -> dict[str, tuple[Path, Path]]:
    """Build every report from persisted outputs and write `<name>.json` + `<name>.md` to
    `out_dir` (deterministic: no generated_at)."""
    scoring = app_config.scoring
    threshold = scoring.deteriorating_threshold
    built: dict[str, dict[str, Any]] = {
        "entity_counts": entity_counts(session),
        "supplier_rankings": supplier_rankings(session),
        "score_distribution": score_distribution(session),
        "delivery_quality_trends": delivery_quality_trends(session, scoring.grace_hours),
        "invoice_variance_distribution": invoice_variance_distribution(session),
        "capacity_risk": capacity_risk(session),
        "compliance_exceptions": compliance_exceptions(session),
        "disruption_exposure": disruption_exposure(session, lookback_days=scoring.lookback_days),
        "missing_data_rates": missing_data_rates(session),
        "deteriorating_suppliers": deteriorating_suppliers(session, threshold=threshold),
        "data_quality": data_quality(session, validation_run_id),
        "anomaly_evaluation": anomaly_evaluation_report(session, batch_id, ground_truth_path),
        "reconciliation": reconciliation_report(session, export_dir, windows, tolerance),
    }
    built["summary"] = summary(session, validation_run_id, reports=built, threshold=threshold)
    return {name: write_report(name, payload, out_dir) for name, payload in built.items()}


@contextmanager
def _stage(name: str, on_progress: Callable[[str], None] | None) -> Iterator[None]:
    if on_progress is not None:
        on_progress(name)
    try:
        yield
    except DatasetAlreadyLoadedError:
        raise
    except Exception as exc:
        raise PipelineStageError(name, exc) from exc


def _has_data(session: Session) -> bool:
    return any(
        session.execute(sa.select(sa.literal(1)).select_from(t).limit(1)).first() is not None
        for t in Base.metadata.sorted_tables
        if t.name not in KEPT_TABLES
    )


def _delete_all(session: Session) -> None:
    for table in reversed(Base.metadata.sorted_tables):
        if table.name not in KEPT_TABLES:
            session.execute(table.delete())


def _ensure_sqlite_dir(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def run_pipeline(
    settings: Settings,
    app_config: AppConfig,
    preset: str,
    seed: int,
    replace_existing: bool,
    on_progress: Callable[[str], None] | None = None,
) -> PipelineResult:
    """Run every stage (see the module docstring) and return what was produced."""
    started_wall, started = datetime.now(UTC), time.perf_counter()

    with _stage("configure", on_progress):
        cfg = GeneratorConfig.from_app_config(app_config, preset, seed)
        paths = output_paths(settings, preset)
        windows = scorecard_windows(
            cfg.reference_start, cfg.reference_end, app_config.scoring.window_months
        )
        scored_at = windows[-1].cutoff + timedelta(days=1)
        detected_at = ensure_utc(cfg.reference_end) + timedelta(days=2)

    with _stage("migrate", on_progress):
        _ensure_sqlite_dir(settings.database_url)
        run_migrations(settings.database_url)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        with _stage("replace_guard", on_progress):
            with session_scope(factory) as s:
                if _has_data(s):
                    if not replace_existing:
                        raise DatasetAlreadyLoadedError("dataset already loaded; pass --replace")
                    _delete_all(s)
            if replace_existing:
                remove_output_dir(paths.export_dir, settings.data_dir)
                remove_output_dir(paths.ground_truth_dir, settings.data_dir)
                remove_output_dir(paths.reports_dir, settings.reports_dir)

        with _stage("generate", on_progress):
            ds = generate_dataset(cfg)

        with _stage("load", on_progress), session_scope(factory) as s:
            counts = load_dataset(s, ds)

        with _stage("score", on_progress), factory() as s:
            ScoringService(s, app_config.scoring, lambda: scored_at).score_history(windows)

        with _stage("inject", on_progress), session_scope(factory) as s:
            injected = inject_anomalies(s, cfg, RandomSource(cfg.seed), scoring=app_config.scoring)

        with _stage("detect", on_progress), factory() as s:
            service = ScoringService(s, app_config.scoring, lambda: detected_at)
            detect_raw_batch(s, injected.batch_id, service, lambda: detected_at)

        with _stage("validate", on_progress), factory() as s:
            validation = run_validation(s, clock=lambda: detected_at)
            if validation.run_id is None:
                raise RuntimeError("validation run was not persisted")
            run_id = validation.run_id

        with _stage("export", on_progress), factory() as s:
            entries = export_tables(s, paths.export_dir)

        with _stage("ground_truth", on_progress):
            write_ground_truth(injected.cases, dict(ds.supplier_profiles), paths.ground_truth_dir)
            ground_truth_path = paths.ground_truth_dir / GROUND_TRUTH_FILE

        with _stage("evaluate", on_progress), factory() as s:
            evaluation = evaluate_anomalies(s, injected.batch_id, ground_truth_path)
            rec = reconcile_scores(s, paths.export_dir, windows, tolerance=Decimal(0))

        with _stage("reports", on_progress), factory() as s:
            report_paths = write_reports(
                s,
                app_config,
                paths.reports_dir,
                validation_run_id=run_id,
                batch_id=injected.batch_id,
                ground_truth_path=ground_truth_path,
                export_dir=paths.export_dir,
                windows=windows,
            )

        with _stage("manifest", on_progress):
            meta = execution_metadata(started_wall, round(time.perf_counter() - started, 3))
            meta["git_sha"] = git_sha()
            manifest = build_manifest(cfg, entries, counts, meta)
            manifest_path = paths.manifests_dir / f"{cfg.dataset_id}.json"
            _write_json(manifest_path, manifest)
            with session_scope(factory) as s:
                s.add(
                    DatasetManifest(
                        dataset_id=cfg.dataset_id, manifest=manifest, created_at=started_wall
                    )
                )
    finally:
        engine.dispose()

    return PipelineResult(
        dataset_id=cfg.dataset_id,
        counts=counts,
        manifest_path=manifest_path,
        validation_run_id=run_id,
        batch_id=injected.batch_id,
        evaluation=evaluation,
        report_paths=report_paths,
        validation=validation_counts(validation.counts_by_severity),
        reconciliation={
            "checked": rec.checked,
            "matched": rec.matched,
            "mismatched": rec.checked - rec.matched,
            "missing_persisted": len(rec.missing_persisted),
        },
        content_checksum=manifest["content_checksum"],
        paths=paths,
    )

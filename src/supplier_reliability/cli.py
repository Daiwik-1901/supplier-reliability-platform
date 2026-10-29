"""`srp` command-line interface (Typer). Every command prints a short JSON summary to stdout.

Exit codes: 0 success; 1 any failure (one line on stderr, no stack trace); 2 the database already
holds a dataset and `generate` was not given --replace (or a usage error).
Settings come from SRP_* environment variables (`Settings`), read fresh for every command.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, NoReturn

import sqlalchemy as sa
import typer
from sqlalchemy.orm import Session

from supplier_reliability.config import AppConfig, Settings, load_app_config
from supplier_reliability.database import make_engine, make_session_factory, run_migrations
from supplier_reliability.models import ValidationRun
from supplier_reliability.services.anomalies import batch_id_for
from supplier_reliability.services.anomalies.injector import GROUND_TRUTH_FILE
from supplier_reliability.services.anomaly_detection import detect_raw_batch
from supplier_reliability.services.evaluation import (
    AnomalyEvaluation,
    evaluate_anomalies,
    reconcile_scores,
)
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.pipeline import (
    DatasetAlreadyLoadedError,
    PipelineStageError,
    current_manifest,
    output_paths,
    run_pipeline,
    validation_counts,
    write_reports,
)
from supplier_reliability.services.scoring.evidence import EvaluationWindow, scorecard_windows
from supplier_reliability.services.scoring.service import (
    CalculationOutcome,
    CalculationRequest,
    ScoringService,
)
from supplier_reliability.services.validation import run_validation

app = typer.Typer(
    name="srp",
    help="Supplier Reliability Scoring Platform.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
ALREADY_LOADED = "dataset already loaded; pass --replace"


# --- helpers --------------------------------------------------------------------------------


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(f"error: {_one_line(message)}", err=True)
    raise typer.Exit(code)


@contextmanager
def _guard() -> Iterator[None]:
    """Turn every failure into a one-line message and a non-zero exit code."""
    try:
        yield
    except (typer.Exit, typer.Abort):
        raise
    except DatasetAlreadyLoadedError:
        _fail(ALREADY_LOADED, 2)
    except PipelineStageError as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"{type(exc).__name__}: {exc}")


def _emit(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _display(path: Path) -> str:
    """Path relative to the repository root when inside it, else absolute (forward slashes)."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _timestamp(value: str, option: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{option}: not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{option}: timestamp needs a UTC offset (e.g. Z or +00:00): {value!r}")
    return parsed.astimezone(UTC)


def _config(settings: Settings) -> AppConfig:
    return load_app_config(settings.config_path)


@contextmanager
def _session(settings: Settings) -> Iterator[Session]:
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _window(start: str, end: str, cutoff: str | None) -> EvaluationWindow:
    end_ts = _timestamp(end, "--end")
    return EvaluationWindow(
        _timestamp(start, "--start"),
        end_ts,
        end_ts if cutoff is None else _timestamp(cutoff, "--cutoff"),
    )


def _window_json(w: EvaluationWindow) -> dict[str, str]:
    return {k: getattr(w, k).isoformat() for k in ("start", "end", "cutoff")}


def _detection(evaluation: AnomalyEvaluation) -> dict[str, dict[str, int]]:
    return {
        issue.value: {"tp": c.tp, "fp": c.fp, "fn": c.fn}
        for issue, c in sorted(evaluation.per_class.items(), key=lambda kv: kv[0].value)
    }


def _dataset_windows(app_config: AppConfig, preset: str) -> list[EvaluationWindow]:
    cfg = GeneratorConfig.from_app_config(app_config, preset)
    return scorecard_windows(
        cfg.reference_start, cfg.reference_end, app_config.scoring.window_months
    )


# --- commands -------------------------------------------------------------------------------

StartOpt = Annotated[str, typer.Option("--start", help="Window start, ISO-8601 with offset.")]
EndOpt = Annotated[str, typer.Option("--end", help="Window end, ISO-8601 with offset.")]
CutoffOpt = Annotated[
    str | None, typer.Option("--cutoff", help="Data cutoff (default: --end), with offset.")
]
SupplierOpt = Annotated[
    list[str] | None, typer.Option("--supplier", help="Supplier id (repeatable; default all).")
]
BatchOpt = Annotated[
    str | None, typer.Option("--batch-id", help="Raw batch (default: the loaded dataset's).")
]


@app.command("init-db")
def init_db() -> None:
    """Create or upgrade the database schema (alembic upgrade head)."""
    with _guard():
        settings = Settings()
        run_migrations(settings.database_url)
        _emit({"status": "migrated", "revision": "head"})


@app.command()
def generate(
    preset: Annotated[str, typer.Option(help="Dataset preset: starter or full.")] = "starter",
    seed: Annotated[int | None, typer.Option(help="Seed (default: config, 20261022).")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Replace a loaded dataset.")] = False,
) -> None:
    """Run the full pipeline: generate, load, score, inject, detect, validate, export, report."""
    with _guard():
        settings = Settings()
        app_config = _config(settings)
        result = run_pipeline(
            settings,
            app_config,
            preset,
            app_config.generator.seed if seed is None else seed,
            replace,
        )
        _emit(
            {
                "dataset_id": result.dataset_id,
                "content_checksum": result.content_checksum,
                "counts": result.counts,
                "validation": {"run_id": result.validation_run_id, **result.validation},
                "detection": _detection(result.evaluation),
                "reconciliation": {
                    "checked": result.reconciliation["checked"],
                    "matched": result.reconciliation["matched"],
                },
                "paths": {
                    "manifest": _display(result.manifest_path),
                    "export_dir": _display(result.paths.export_dir),
                    "ground_truth_dir": _display(result.paths.ground_truth_dir),
                    "reports_dir": _display(result.paths.reports_dir),
                },
            }
        )


def _outcome_json(window: EvaluationWindow, out: CalculationOutcome) -> dict[str, Any]:
    return {
        "window": _window_json(window),
        "created": len(out.created),
        "existing": len(out.existing),
        "unchanged": len(out.unchanged),
        "superseded": len(out.superseded),
        "score_ids": sorted(out.created + out.existing + out.unchanged),
    }


@app.command()
def score(
    start: StartOpt, end: EndOpt, cutoff: CutoffOpt = None, supplier: SupplierOpt = None
) -> None:
    """Calculate scores for a window (idempotent: existing scores are returned, not redone)."""
    with _guard():
        settings = Settings()
        window = _window(start, end, cutoff)
        with _session(settings) as s:
            service = ScoringService(s, _config(settings).scoring, _utcnow)
            out = service.calculate(CalculationRequest(window, supplier or None))
        _emit(_outcome_json(window, out))


@app.command()
def recalculate(
    start: StartOpt,
    end: EndOpt,
    reason: Annotated[str, typer.Option("--reason", help="Why the scores are recalculated.")],
    cutoff: CutoffOpt = None,
    supplier: SupplierOpt = None,
) -> None:
    """Recompute scores; a new version is stored only where the eligible evidence changed."""
    with _guard():
        settings = Settings()
        window = _window(start, end, cutoff)
        with _session(settings) as s:
            service = ScoringService(s, _config(settings).scoring, _utcnow)
            out = service.recalculate(CalculationRequest(window, supplier or None), reason)
        _emit(_outcome_json(window, out))


@app.command()
def validate() -> None:
    """Run every validation rule and persist the run."""
    with _guard():
        settings = Settings()
        with _session(settings) as s:
            result = run_validation(s)
        _emit(
            {
                "run_id": result.run_id,
                **validation_counts(result.counts_by_severity),
                "by_severity": result.counts_by_severity,
            }
        )


@app.command()
def detect(batch_id: BatchOpt = None) -> None:
    """Detect anomalies in a raw batch (re-running adds nothing already detected)."""
    with _guard():
        settings = Settings()
        with _session(settings) as s:
            batch = batch_id or batch_id_for(current_manifest(s)["dataset_id"])
            service = ScoringService(s, _config(settings).scoring, _utcnow)
            result = detect_raw_batch(s, batch, service, _utcnow)
        _emit(
            {
                "batch_id": batch,
                "new_exceptions": len(result.exceptions),
                "by_issue_type": result.by_issue_type,
                "corrected_scores": len(result.corrected_scores),
                "skipped": len(result.skipped),
            }
        )


GroundTruthOpt = Annotated[
    Path | None, typer.Option("--ground-truth", help="anomaly_ground_truth.csv (default: preset).")
]
ExportOpt = Annotated[
    Path | None, typer.Option("--export-dir", help="Exported tables (default: preset).")
]


def _context(
    s: Session,
    settings: Settings,
    batch_id: str | None,
    ground_truth: Path | None,
    export_dir: Path | None,
) -> tuple[str, str, Path, Path]:
    """(preset, batch_id, ground truth csv, export dir), defaulting to the loaded dataset's."""
    manifest = current_manifest(s)
    paths = output_paths(settings, manifest["preset"])
    return (
        manifest["preset"],
        batch_id or batch_id_for(manifest["dataset_id"]),
        ground_truth or paths.ground_truth_dir / GROUND_TRUTH_FILE,
        export_dir or paths.export_dir,
    )


def _tolerance(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"--tolerance: not a decimal: {value!r}") from exc


@app.command()
def evaluate(
    batch_id: BatchOpt = None,
    ground_truth: GroundTruthOpt = None,
    export_dir: ExportOpt = None,
    tolerance: Annotated[str, typer.Option(help="Reconciliation tolerance.")] = "0",
) -> None:
    """Anomaly detection against ground truth, and scores against the independent recomputation."""
    with _guard():
        settings = Settings()
        app_config = _config(settings)
        with _session(settings) as s:
            preset, batch, gt, export = _context(s, settings, batch_id, ground_truth, export_dir)
            evaluation = evaluate_anomalies(s, batch, gt)
            windows = _dataset_windows(app_config, preset)
            rec = reconcile_scores(s, export, windows, tolerance=_tolerance(tolerance))
        _emit(
            {
                "batch_id": batch,
                "detection": _detection(evaluation),
                "micro": {
                    "tp": evaluation.micro.tp,
                    "fp": evaluation.micro.fp,
                    "fn": evaluation.micro.fn,
                },
                "reconciliation": {
                    "checked": rec.checked,
                    "matched": rec.matched,
                    "missing_persisted": len(rec.missing_persisted),
                },
            }
        )


@app.command()
def report(
    validation_run_id: Annotated[
        str | None, typer.Option("--validation-run-id", help="Default: the latest run.")
    ] = None,
    batch_id: BatchOpt = None,
    ground_truth: GroundTruthOpt = None,
    export_dir: ExportOpt = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Output dir (default: reports/<preset>).")
    ] = None,
) -> None:
    """Write every JSON + Markdown report from the persisted outputs."""
    with _guard():
        settings = Settings()
        app_config = _config(settings)
        with _session(settings) as s:
            preset, batch, gt, export = _context(s, settings, batch_id, ground_truth, export_dir)
            run_id = validation_run_id or s.scalar(sa.select(sa.func.max(ValidationRun.run_id)))
            if run_id is None:
                raise LookupError("no validation run in the database; run `srp validate` first")
            target = out or output_paths(settings, preset).reports_dir
            written = write_reports(
                s,
                app_config,
                target,
                validation_run_id=run_id,
                batch_id=batch,
                ground_truth_path=gt,
                export_dir=export,
                windows=_dataset_windows(app_config, preset),
            )
        _emit(
            {
                "validation_run_id": run_id,
                "reports_dir": _display(target),
                "reports": sorted(written),
            }
        )


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
) -> None:
    """Run the API with uvicorn."""
    import uvicorn

    uvicorn.run("supplier_reliability.main:app", host=host, port=port)


__all__ = ["ALREADY_LOADED", "app"]

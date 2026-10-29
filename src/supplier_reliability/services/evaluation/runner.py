"""Live evaluation of the latest dataset (Task 21): locate its ground truth and exported tables
under the data directory, then compute anomaly metrics and the score reconciliation.

Read-only: nothing is written. `NoEvaluationAvailable` means there is no dataset, or its
ground-truth file / export directory is not present in the data directory."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from supplier_reliability.config import AppConfig
from supplier_reliability.repositories import jobs as jobs_repo
from supplier_reliability.services.anomalies import batch_id_for
from supplier_reliability.services.anomalies.injector import GROUND_TRUTH_FILE
from supplier_reliability.services.evaluation.anomaly_eval import (
    AnomalyEvaluation,
    evaluate_anomalies,
)
from supplier_reliability.services.evaluation.reconciliation import (
    ReconciliationReport,
    reconcile_scores,
)
from supplier_reliability.services.reporting import anomaly_evaluation_report
from supplier_reliability.services.scoring.evidence import scorecard_windows

_PRESET = re.compile(r"^[a-z0-9_-]{1,32}$")
RECONCILIATION_TOLERANCE = Decimal(0)


class NoEvaluationAvailable(Exception):
    """No dataset, ground truth or export is available to evaluate."""


@dataclass(frozen=True)
class EvaluationInputs:
    dataset_id: str
    batch_id: str
    ground_truth: Path
    export_dir: Path


@dataclass(frozen=True)
class EvaluationRun:
    dataset_id: str
    anomalies: AnomalyEvaluation
    reconciliation: ReconciliationReport
    tolerance: Decimal


def resolve_inputs(session: Session, data_dir: Path) -> EvaluationInputs:
    """Ground truth and export directory of the latest dataset (by manifest)."""
    row = jobs_repo.latest_manifest(session)
    preset = None if row is None else row.manifest.get("preset")
    if row is None or not isinstance(preset, str) or not _PRESET.match(preset):
        raise NoEvaluationAvailable
    return EvaluationInputs(
        dataset_id=row.dataset_id,
        batch_id=batch_id_for(row.dataset_id),
        ground_truth=Path(data_dir) / "ground_truth" / preset / GROUND_TRUTH_FILE,
        export_dir=Path(data_dir) / "generated" / preset,
    )


def _with_ground_truth[T](inputs: EvaluationInputs, call: Callable[[], T]) -> T:
    if not inputs.ground_truth.is_file():
        raise NoEvaluationAvailable
    try:
        return call()
    except ValueError as exc:  # empty or unknown raw batch
        raise NoEvaluationAvailable from exc


def anomaly_report(session: Session, inputs: EvaluationInputs) -> dict[str, Any]:
    return _with_ground_truth(
        inputs,
        lambda: anomaly_evaluation_report(session, inputs.batch_id, inputs.ground_truth),
    )


def run_evaluation(
    session: Session, app_config: AppConfig, inputs: EvaluationInputs
) -> EvaluationRun:
    anomalies = _with_ground_truth(
        inputs, lambda: evaluate_anomalies(session, inputs.batch_id, inputs.ground_truth)
    )
    if not inputs.export_dir.is_dir():
        raise NoEvaluationAvailable
    period = app_config.generator.reference_period
    windows = scorecard_windows(
        datetime.fromisoformat(period["start"]),
        datetime.fromisoformat(period["end"]),
        app_config.scoring.window_months,
    )
    rec = reconcile_scores(session, inputs.export_dir, windows, tolerance=RECONCILIATION_TOLERANCE)
    return EvaluationRun(inputs.dataset_id, anomalies, rec, RECONCILIATION_TOLERANCE)

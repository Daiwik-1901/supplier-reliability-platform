"""Report and evaluation endpoints.

Reports are computed LIVE from the database with the Task 17 builders and nothing is written
(no files, no rows). Ground truth and the exported tables are looked up under the configured
data directory for the latest dataset (`generated/<preset>/`, `ground_truth/<preset>/`); the
paths themselves are never returned. "No evaluation available" (404) means there is no dataset,
or its ground-truth file / export directory is not present in this server's data directory."""

import contextlib
from typing import Any

from fastapi import APIRouter, HTTPException

from supplier_reliability.api.dependencies import AppConfigDep, DbDep, SettingsDep
from supplier_reliability.api.errors import MAX_LISTED_IDS, error_responses
from supplier_reliability.repositories import validation as validation_repo
from supplier_reliability.schemas.reports import (
    AnomalyEvaluationOut,
    ConfusionOut,
    EvaluationRunOut,
    ReconciliationOut,
)
from supplier_reliability.services.evaluation import ConfusionCounts
from supplier_reliability.services.evaluation.runner import (
    NoEvaluationAvailable,
    anomaly_report,
    resolve_inputs,
    run_evaluation,
)
from supplier_reliability.services.reporting import (
    data_quality,
    deteriorating_suppliers,
    entity_counts,
    summary,
    supplier_rankings,
)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
evaluation_router = APIRouter(prefix="/api/v1/evaluation", tags=["evaluation"])

NO_EVALUATION = "no evaluation available"


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def _confusion(cc: ConfusionCounts) -> ConfusionOut:
    return ConfusionOut(
        tp=cc.tp, fp=cc.fp, fn=cc.fn, tn=cc.tn, precision=cc.precision, recall=cc.recall, f1=cc.f1
    )


@router.get(
    "/summary",
    response_model=dict[str, Any],
    summary="Headline summary report",
    description="Headline numbers computed live from the database. Data-quality figures are "
    "included when a validation run exists and anomaly precision/recall when the latest "
    "dataset's ground truth is available; 404 when there are no scores yet.",
    responses=error_responses(404),
)
def get_summary(db: DbDep, config: AppConfigDep, settings: SettingsDep) -> dict[str, Any]:
    threshold = config.scoring.deteriorating_threshold
    try:
        built: dict[str, dict[str, Any]] = {
            "entity_counts": entity_counts(db),
            "supplier_rankings": supplier_rankings(db),
            "deteriorating_suppliers": deteriorating_suppliers(db, threshold=threshold),
        }
        run_id = validation_repo.latest_run_id(db)
        # no ground truth here: the summary simply omits the anomaly figures
        with contextlib.suppress(NoEvaluationAvailable):
            built["anomaly_evaluation"] = anomaly_report(db, resolve_inputs(db, settings.data_dir))
        return summary(db, run_id, reports=built, threshold=threshold)
    except LookupError as exc:
        raise _not_found("no data available for this report") from exc


@router.get(
    "/supplier-rankings",
    response_model=dict[str, Any],
    summary="Supplier rankings report",
    description="Suppliers ranked by overall score in the latest scored window, computed live. "
    "Suppliers with insufficient evidence are listed separately. 404 when nothing is scored.",
    responses=error_responses(404),
)
def get_supplier_rankings(db: DbDep) -> dict[str, Any]:
    try:
        return supplier_rankings(db)
    except LookupError as exc:
        raise _not_found("no data available for this report") from exc


@router.get(
    "/data-quality",
    response_model=dict[str, Any],
    summary="Data-quality report",
    description="Findings of the latest validation run, split into defects (MEDIUM and worse) "
    "and informational (LOW, NONE). 404 when no validation run exists.",
    responses=error_responses(404),
)
def get_data_quality(db: DbDep) -> dict[str, Any]:
    run_id = validation_repo.latest_run_id(db)
    if run_id is None:
        raise _not_found("no validation run available")
    return data_quality(db, run_id)


@router.get(
    "/anomaly-evaluation",
    response_model=dict[str, Any],
    summary="Anomaly evaluation report",
    description="Detected data exceptions of the latest dataset against its ground truth, per "
    "issue class and micro-averaged. 404 `no evaluation available` when no dataset exists or "
    "its ground-truth file is not available on this server.",
    responses=error_responses(404),
)
def get_anomaly_evaluation(db: DbDep, settings: SettingsDep) -> dict[str, Any]:
    try:
        return anomaly_report(db, resolve_inputs(db, settings.data_dir))
    except NoEvaluationAvailable as exc:
        raise _not_found(NO_EVALUATION) from exc


@evaluation_router.post(
    "/run",
    response_model=EvaluationRunOut,
    summary="Run the evaluation",
    description="Evaluates anomaly detection against ground truth (per class and micro) and "
    "reconciles the persisted scores of the scorecard windows against an independent "
    "recomputation from the latest dataset's exported tables (tolerance 0). Computed live; "
    f"nothing is stored. `reconciliation.mismatches` lists at most {MAX_LISTED_IDS} entries. "
    "404 `no evaluation available` when there is no dataset, ground truth or export.",
    responses=error_responses(404),
)
def run(db: DbDep, config: AppConfigDep, settings: SettingsDep) -> EvaluationRunOut:
    try:
        result = run_evaluation(db, config, resolve_inputs(db, settings.data_dir))
    except NoEvaluationAvailable as exc:
        raise _not_found(NO_EVALUATION) from exc
    anomalies, rec = result.anomalies, result.reconciliation
    return EvaluationRunOut(
        dataset_id=result.dataset_id,
        anomaly_evaluation=AnomalyEvaluationOut(
            unit=anomalies.unit,
            per_class={i.value: _confusion(c) for i, c in anomalies.per_class.items()},
            micro=_confusion(anomalies.micro),
            negative_population={i.value: n for i, n in anomalies.negative_population.items()},
        ),
        reconciliation=ReconciliationOut(
            tolerance=str(result.tolerance),
            checked=rec.checked,
            matched=rec.matched,
            mismatches_count=rec.checked - rec.matched,
            mismatches=[dict(m) for m in rec.mismatches[:MAX_LISTED_IDS]],
            missing_persisted=len(rec.missing_persisted),
        ),
    )

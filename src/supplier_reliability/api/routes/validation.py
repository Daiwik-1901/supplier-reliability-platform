"""Validation endpoints: run the rule registry and read stored findings.

A run stamps its findings with the wall-clock UTC time (the pipeline uses deterministic
clocks). `run_validation` commits its own transaction, so the handler never commits."""

from typing import Annotated

from fastapi import APIRouter, Body, Query

from supplier_reliability.api.dependencies import DbDep, PageParams, wall_clock
from supplier_reliability.api.errors import (
    InvalidRequestError,
    NotFoundError,
    bounded_list,
    error_responses,
)
from supplier_reliability.domain.enums import Severity
from supplier_reliability.repositories import validation as repo
from supplier_reliability.schemas.common import IdStr, Page
from supplier_reliability.schemas.validation import (
    FindingOut,
    RuleId,
    ValidationRunOut,
    ValidationRunRequest,
)
from supplier_reliability.services.reporting.quality_reports import DEFECT_SEVERITIES
from supplier_reliability.services.validation import REGISTRY, ValidationRunResult, run_validation

router = APIRouter(prefix="/api/v1/validation", tags=["validation"])


def _summary(result: ValidationRunResult) -> ValidationRunOut:
    """Defects are MEDIUM and worse; LOW and NONE are informational (same split as the data
    quality report)."""
    defect_rule: dict[str, int] = {}
    defect_sev: dict[str, int] = {}
    info_rule: dict[str, int] = {}
    for f in result.findings:
        if f.severity in DEFECT_SEVERITIES:
            defect_rule[f.rule_id] = defect_rule.get(f.rule_id, 0) + 1
            defect_sev[f.severity.value] = defect_sev.get(f.severity.value, 0) + 1
        else:
            info_rule[f.rule_id] = info_rule.get(f.rule_id, 0) + 1
    assert result.run_id is not None  # persisted runs only
    return ValidationRunOut(
        run_id=result.run_id,
        counts_by_rule=result.counts_by_rule,
        counts_by_severity=result.counts_by_severity,
        checked_counts_by_rule=result.checked_counts_by_rule,
        total_findings=len(result.findings),
        expected_anomalies=sum(f.is_expected_anomaly for f in result.findings),
        defects={
            "total": sum(defect_rule.values()),
            "by_rule": defect_rule,
            "by_severity": defect_sev,
        },
        informational={"total": sum(info_rule.values()), "by_rule": info_rule},
    )


@router.post(
    "/run",
    response_model=ValidationRunOut,
    status_code=201,
    summary="Run validation rules",
    description="Runs the selected rules (default all), stores the run and its findings and "
    "returns the summary. `defects` counts MEDIUM, HIGH and CRITICAL findings; `informational` "
    "counts LOW and NONE separately. Findings are stamped with the server's wall-clock UTC "
    "time. Unknown rule ids are a 422.",
    responses=error_responses(422),
)
def run(
    db: DbDep,
    body: Annotated[ValidationRunRequest | None, Body()] = None,
) -> ValidationRunOut:
    rule_ids = None if body is None else body.rule_ids
    if rule_ids is not None:
        unknown = sorted(set(rule_ids) - set(REGISTRY))
        if unknown:
            raise InvalidRequestError(
                ["body", "rule_ids"], f"Unknown rule ids: {bounded_list(unknown)}"
            )
    return _summary(run_validation(db, rule_ids=rule_ids, persist=True, clock=wall_clock))


@router.get(
    "/results",
    response_model=Page[FindingOut],
    summary="List validation findings",
    description="Findings of one run, ordered by finding id. Without `run_id` the latest run is "
    "used; 404 if no run exists or the given run is unknown.",
    responses=error_responses(404, 422),
)
def results(
    db: DbDep,
    page: PageParams,
    run_id: Annotated[IdStr | None, Query(examples=["VAL-000001"])] = None,
    severity: Annotated[Severity | None, Query(examples=["HIGH"])] = None,
    rule_id: Annotated[RuleId | None, Query(examples=["RI-001"])] = None,
) -> Page[FindingOut]:
    if run_id is None:
        run_id = repo.latest_run_id(db)
        if run_id is None:
            raise NotFoundError("Validation run", "latest")
    elif not repo.run_exists(db, run_id):
        raise NotFoundError("Validation run", run_id)
    rows, total = repo.list_findings(db, run_id, severity, rule_id, page.limit, page.offset)
    items = [FindingOut.model_validate(r) for r in rows]
    return Page[FindingOut](items=items, total=total, limit=page.limit, offset=page.offset)

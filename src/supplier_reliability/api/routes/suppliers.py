"""Supplier endpoints: list, detail, per-window performance, score history, explanation."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from supplier_reliability.api.dependencies import DbDep, PageParams, Sort, sort_dependency
from supplier_reliability.api.errors import NotFoundError, error_responses
from supplier_reliability.domain.enums import (
    Region,
    ReliabilityCategory,
    SupplierCategory,
    SupplierStatus,
)
from supplier_reliability.models import SupplierScore
from supplier_reliability.repositories import suppliers as repo
from supplier_reliability.schemas.common import Page
from supplier_reliability.schemas.suppliers import (
    ExplanationOut,
    PerformanceOut,
    ScoreBrief,
    ScoreVersionOut,
    SupplierDetail,
    SupplierSummary,
    WindowPerformance,
)

router = APIRouter(prefix="/api/v1/suppliers", tags=["suppliers"])

SupplierSort = Annotated[
    Sort, Depends(sort_dependency(repo.SUPPLIER_SORT_FIELDS, default="supplier_id:asc"))
]


def _require_supplier(db: DbDep, supplier_id: str) -> None:
    if not repo.supplier_exists(db, supplier_id):
        raise NotFoundError("Supplier", supplier_id)


@router.get(
    "",
    response_model=Page[SupplierSummary],
    summary="List suppliers",
    description="Paginated suppliers, each with its latest current score (joined in one query). "
    "Filters are ANDed; `reliability_category` applies to the latest current score. `q` is a "
    "case-insensitive substring match on name or id.",
    responses=error_responses(422),
)
def list_suppliers(
    db: DbDep,
    page: PageParams,
    sort: SupplierSort,
    region: Annotated[Region | None, Query(examples=["EMEA"])] = None,
    category: Annotated[SupplierCategory | None, Query(examples=["ELECTRONICS"])] = None,
    status: Annotated[SupplierStatus | None, Query(examples=["ACTIVE"])] = None,
    reliability_category: Annotated[ReliabilityCategory | None, Query(examples=["AT_RISK"])] = None,
    q: Annotated[str | None, Query(max_length=100, examples=["northwind"])] = None,
) -> Page[SupplierSummary]:
    filters = repo.SupplierFilters(
        region=region,
        category=category,
        status=status,
        reliability_category=reliability_category,
        q=q,
    )
    rows, total = repo.list_suppliers(
        db, filters, sort.field, sort.descending, page.limit, page.offset
    )
    items = [
        SupplierSummary.model_validate(supplier).model_copy(update={"latest_score": _brief(score)})
        for supplier, score in rows
    ]
    return Page[SupplierSummary](items=items, total=total, limit=page.limit, offset=page.offset)


def _brief(score: SupplierScore | None) -> ScoreBrief | None:
    return None if score is None else ScoreBrief.model_validate(score)


@router.get(
    "/{supplier_id}",
    response_model=SupplierDetail,
    summary="Get a supplier",
    description="Supplier with its sites, contracts and latest current score.",
    responses=error_responses(404, 422),
)
def get_supplier(supplier_id: str, db: DbDep) -> SupplierDetail:
    supplier = repo.get_supplier(db, supplier_id)
    if supplier is None:
        raise NotFoundError("Supplier", supplier_id)
    detail = SupplierDetail.model_validate(supplier)
    return detail.model_copy(
        update={"latest_score": _brief(repo.latest_current_score(db, supplier_id))}
    )


@router.get(
    "/{supplier_id}/performance",
    response_model=PerformanceOut,
    summary="Per-window component scores",
    description="Component scores of the supplier's current score for every window, ordered by "
    "window. A component without enough evidence is null; `missing_reasons` says why.",
    responses=error_responses(404, 422),
)
def get_performance(supplier_id: str, db: DbDep) -> PerformanceOut:
    _require_supplier(db, supplier_id)
    windows = []
    for score in repo.current_scores(db, supplier_id):
        reasons = score.explanation.get("missing_data_reasons", {})
        windows.append(
            WindowPerformance.model_validate(score).model_copy(update={"missing_reasons": reasons})
        )
    return PerformanceOut(supplier_id=supplier_id, windows=windows)


@router.get(
    "/{supplier_id}/scores",
    response_model=Page[ScoreVersionOut],
    summary="Score version history",
    description="Every stored score version, superseded ones included, ordered by window end "
    "and version number.",
    responses=error_responses(404, 422),
)
def get_score_history(supplier_id: str, db: DbDep, page: PageParams) -> Page[ScoreVersionOut]:
    _require_supplier(db, supplier_id)
    rows, total = repo.score_history(db, supplier_id, page.limit, page.offset)
    items = [ScoreVersionOut.model_validate(r) for r in rows]
    return Page[ScoreVersionOut](items=items, total=total, limit=page.limit, offset=page.offset)


@router.get(
    "/{supplier_id}/explanation",
    response_model=ExplanationOut,
    summary="Explain the latest score",
    description="The latest current score's stored explanation: per-component numerator, "
    "denominator, weights, missing-data reasons, evaluation period and data cutoff. "
    "404 if the supplier has no score yet.",
    responses=error_responses(404, 422),
)
def get_explanation(supplier_id: str, db: DbDep) -> ExplanationOut:
    _require_supplier(db, supplier_id)
    score = repo.latest_current_score(db, supplier_id)
    if score is None:
        raise NotFoundError("Score for supplier", supplier_id)
    return ExplanationOut(
        supplier_id=supplier_id,
        score_id=score.score_id,
        evaluation_start=score.evaluation_start,
        evaluation_end=score.evaluation_end,
        data_cutoff=score.data_cutoff_timestamp,
        overall_score=score.overall_score,
        reliability_category=score.reliability_category,
        decision=score.decision,
        components=score.explanation.get("components", {}),
        explanation=score.explanation,
    )

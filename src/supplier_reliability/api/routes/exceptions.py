"""Data exception endpoints: filtered list and full detail."""

from typing import Annotated

from fastapi import APIRouter, Query

from supplier_reliability.api.dependencies import DbDep, IdPath, PageParams
from supplier_reliability.api.errors import NotFoundError, error_responses
from supplier_reliability.domain.enums import Disposition, IssueType, Severity
from supplier_reliability.repositories import exceptions as repo
from supplier_reliability.schemas.common import IdStr, Page
from supplier_reliability.schemas.exceptions import ExceptionOut, ExceptionSummary

router = APIRouter(prefix="/api/v1/exceptions", tags=["exceptions"])


@router.get(
    "",
    response_model=Page[ExceptionSummary],
    summary="List data exceptions",
    description="Paginated data exceptions ordered by id; filters are ANDed. Evidence is "
    "returned by the detail endpoint only.",
    responses=error_responses(422),
)
def list_exceptions(
    db: DbDep,
    page: PageParams,
    severity: Annotated[Severity | None, Query(examples=["HIGH"])] = None,
    issue_type: Annotated[IssueType | None, Query(examples=["SHIPMENT_MISSING_PO"])] = None,
    supplier_id: Annotated[IdStr | None, Query(examples=["SUP-000001"])] = None,
    disposition: Annotated[Disposition | None, Query(examples=["QUARANTINED"])] = None,
) -> Page[ExceptionSummary]:
    filters = repo.ExceptionFilters(
        severity=severity,
        issue_type=issue_type,
        supplier_id=supplier_id,
        disposition=disposition,
    )
    rows, total = repo.list_exceptions(db, filters, page.limit, page.offset)
    items = [ExceptionSummary.model_validate(r) for r in rows]
    return Page[ExceptionSummary](items=items, total=total, limit=page.limit, offset=page.offset)


@router.get(
    "/{exception_id}",
    response_model=ExceptionOut,
    summary="Get a data exception",
    description="One exception with its evidence JSON and related entity ids.",
    responses=error_responses(404, 422),
)
def get_exception(exception_id: IdPath, db: DbDep) -> ExceptionOut:
    row = repo.get_exception(db, exception_id)
    if row is None:
        raise NotFoundError("Exception", exception_id)
    return ExceptionOut.model_validate(row)

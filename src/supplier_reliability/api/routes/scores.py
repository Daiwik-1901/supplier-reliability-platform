"""Score endpoints: list, detail, calculate, recalculate, restate.

The scoring service owns its transaction (it commits per call and refuses a session with pending
changes), so these handlers only read before calling it and never commit afterwards.
Scores calculated through the API stamp `calculated_at` with the wall clock (UTC); the pipeline
uses deterministic clocks instead.

Concurrent submissions (Ruling R50): the score-writing routes (calculate, recalculate, restate)
hold one process-wide lock around the service call, so a double-submit is serialised and the
second request finds the first one's rows (`existing`/`unchanged`). A unique-constraint
IntegrityError from score insertion is retried once (the service rolled back; the retry sees the
competing commit). Limitation: this is a single-process guarantee. Several workers or instances
need database-level locking or sequences for score ids (PostgreSQL: UNVERIFIED, not available
on the build machine); a second IntegrityError surfaces as a 500."""

import threading
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from supplier_reliability.api.dependencies import (
    AppConfigDep,
    DbDep,
    IdPath,
    PageParams,
    Sort,
    sort_dependency,
    wall_clock,
)
from supplier_reliability.api.errors import (
    ConflictError,
    NotFoundError,
    bounded_list,
    error_responses,
)
from supplier_reliability.config import AppConfig
from supplier_reliability.domain.enums import CalculationStatus, ReliabilityCategory
from supplier_reliability.repositories import scores as repo
from supplier_reliability.repositories.suppliers import existing_supplier_ids
from supplier_reliability.schemas.common import IdStr, Page
from supplier_reliability.schemas.scores import (
    CalculateRequest,
    CalculationOutcomeOut,
    LineageCounts,
    RecalculateRequest,
    RestateRequest,
    ScoreDetail,
    ScoreOut,
)
from supplier_reliability.services.scoring.evidence import EvaluationWindow
from supplier_reliability.services.scoring.service import (
    CalculationOutcome,
    CalculationRequest,
    NothingToRestateError,
    ScoringService,
)

router = APIRouter(prefix="/api/v1/scores", tags=["scores"])

SCORE_SORT_FIELDS = ("overall_score", "evaluation_end", "supplier_id", "score_id")
ScoreSort = Annotated[Sort, Depends(sort_dependency(SCORE_SORT_FIELDS, default="score_id:asc"))]
AWARE_NOTE = "ISO-8601 with a UTC offset (for example `Z`); a naive datetime is a 422."
SERIALISATION_NOTE = (
    " Writes are serialised per server process (a concurrent duplicate request returns the "
    "first one's result); this does not hold across several workers or instances."
)

_WRITE_LOCK = threading.Lock()


def _serialised[T](call: Callable[[], T]) -> T:
    """Run a score-writing service call under the process-wide lock; retry once on IntegrityError
    (the service has rolled back, so the retry sees whatever a competing writer committed)."""
    with _WRITE_LOCK:
        try:
            return call()
        except IntegrityError:
            return call()


def _unique_known_suppliers(db: Session, supplier_ids: list[str] | None) -> list[str] | None:
    """De-duplicated ids (sorted), or None for "every supplier"; 404 if any id is unknown."""
    if supplier_ids is None:
        return None
    unique = sorted(set(supplier_ids))
    unknown = sorted(set(unique) - existing_supplier_ids(db, unique))
    if unknown:
        raise NotFoundError("Suppliers", bounded_list(unknown))
    return unique


def _outcome(result: CalculationOutcome) -> CalculationOutcomeOut:
    return CalculationOutcomeOut(
        created=result.created,
        existing=result.existing,
        unchanged=result.unchanged,
        superseded=result.superseded,
    )


def _service(db: Session, config: AppConfig) -> ScoringService:
    return ScoringService(db, config.scoring, wall_clock)


def _require_version(config: AppConfig, requested: str | None) -> None:
    if requested is not None and requested != config.scoring.scoring_version:
        raise ConflictError("scoring version not available")


def _request(body: CalculateRequest, ids: list[str] | None) -> CalculationRequest:
    cutoff = body.data_cutoff or body.evaluation_end
    window = EvaluationWindow(body.evaluation_start, body.evaluation_end, cutoff)
    return CalculationRequest(window, ids, body.scoring_version)


@router.get(
    "",
    response_model=Page[ScoreOut],
    summary="List scores",
    description="Paginated score rows. Filters are ANDed; `evaluation_start` keeps windows "
    "starting at or after it and `evaluation_end` windows ending at or before it. "
    "`current_only` (default true) hides superseded versions.",
    responses=error_responses(422),
)
def list_scores(
    db: DbDep,
    page: PageParams,
    sort: ScoreSort,
    supplier_id: Annotated[
        IdStr | None, Query(pattern=r"^[A-Z0-9-]{1,40}$", examples=["SUP-000001"])
    ] = None,
    reliability_category: Annotated[ReliabilityCategory | None, Query(examples=["AT_RISK"])] = None,
    calculation_status: Annotated[CalculationStatus | None, Query(examples=["CALCULATED"])] = None,
    evaluation_start: Annotated[
        AwareDatetime | None, Query(description=AWARE_NOTE, examples=["2025-01-01T00:00:00Z"])
    ] = None,
    evaluation_end: Annotated[
        AwareDatetime | None, Query(description=AWARE_NOTE, examples=["2025-09-30T23:59:59Z"])
    ] = None,
    current_only: Annotated[bool, Query(description="Hide superseded versions.")] = True,
) -> Page[ScoreOut]:
    filters = repo.ScoreFilters(
        supplier_id=supplier_id,
        reliability_category=reliability_category,
        calculation_status=calculation_status,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        current_only=current_only,
    )
    order = f"{'-' if sort.descending else ''}{sort.field}"
    rows, total = repo.list_scores(db, filters, page.limit, page.offset, order)
    items = [ScoreOut.model_validate(r) for r in rows]
    return Page[ScoreOut](items=items, total=total, limit=page.limit, offset=page.offset)


@router.post(
    "/calculate",
    response_model=CalculationOutcomeOut,
    status_code=200,
    summary="Calculate scores for a window",
    description="Scores the window for the given suppliers (default all). Idempotent: an "
    "already scored window/cutoff/version is returned under `existing`. 201 when any score was "
    "created, otherwise 200. `calculated_at` is the server's wall-clock UTC time. A "
    "`scoring_version` other than the configured one is a 409." + SERIALISATION_NOTE,
    responses={201: {"model": CalculationOutcomeOut, "description": "Scores were created"}}
    | error_responses(404, 409, 422),
)
def calculate(
    body: CalculateRequest, response: Response, db: DbDep, config: AppConfigDep
) -> CalculationOutcomeOut:
    _require_version(config, body.scoring_version)
    ids = _unique_known_suppliers(db, body.supplier_ids)
    service, req = _service(db, config), _request(body, ids)
    result = _serialised(lambda: service.calculate(req))
    response.status_code = 201 if result.created else 200
    return _outcome(result)


@router.post(
    "/recalculate",
    response_model=CalculationOutcomeOut,
    summary="Recalculate scores for a window",
    description="Recomputes the window against the evidence known at its cutoff. Unchanged "
    "evidence writes nothing (`unchanged`); changed evidence supersedes the current score with "
    "a new version (`superseded`: old id to new id) and the old version stays readable. "
    "Always 200. `reason` (1-500 characters) is stored as the version's change reason."
    + SERIALISATION_NOTE,
    responses=error_responses(404, 409, 422),
)
def recalculate(body: RecalculateRequest, db: DbDep, config: AppConfigDep) -> CalculationOutcomeOut:
    _require_version(config, body.scoring_version)
    ids = _unique_known_suppliers(db, body.supplier_ids)
    service, req = _service(db, config), _request(body, ids)
    return _outcome(_serialised(lambda: service.recalculate(req, body.reason)))


@router.post(
    "/restate",
    response_model=CalculationOutcomeOut,
    status_code=201,
    summary="Restate a window with a later cutoff",
    description="Scores the same window with a later `data_cutoff` as a new evaluation that "
    "links to the earlier score (`restates_score_id`) and records the differences. The cutoff "
    "must be later than an existing score's cutoff for that window and supplier; otherwise "
    "409. Repeating the request also returns 201, with `created` empty and the stored "
    "restatement under `existing`." + SERIALISATION_NOTE,
    responses=error_responses(404, 409, 422),
)
def restate(body: RestateRequest, db: DbDep, config: AppConfigDep) -> CalculationOutcomeOut:
    ids = _unique_known_suppliers(db, body.supplier_ids)
    window = EvaluationWindow(body.evaluation_start, body.evaluation_end, body.data_cutoff)
    service = _service(db, config)
    try:
        return _outcome(_serialised(lambda: service.restate(window, ids, body.reason)))
    except NothingToRestateError as exc:  # any other ValueError is a fault: 500
        raise ConflictError(
            f"no earlier score with a cutoff before {exc.cutoff} to restate for "
            f"{bounded_list(exc.missing) or 'any supplier'}"
        ) from exc


@router.get(
    "/{score_id}",
    response_model=ScoreDetail,
    summary="Get a score",
    description="One score version with its stored explanation and per-component lineage "
    "counts (included/excluded records). The lineage rows themselves are not returned.",
    responses=error_responses(404, 422),
)
def get_score(score_id: IdPath, db: DbDep) -> ScoreDetail:
    score = repo.get_score(db, score_id)
    if score is None:
        raise NotFoundError("Score", score_id)
    summary = {
        component: LineageCounts(**counts)
        for component, counts in repo.lineage_counts(db, score_id).items()
    }
    return ScoreDetail(
        **ScoreOut.model_validate(score).model_dump(),
        explanation=score.explanation,
        lineage_summary=summary,
    )

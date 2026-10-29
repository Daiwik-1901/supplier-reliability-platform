"""Dataset endpoints: start a generation job, poll it, read a dataset manifest.

Generation runs the whole pipeline as a PROCESS-LOCAL background task (see
`supplier_reliability.services.jobs` for the limitations: not durable, not distributed; jobs left
unfinished by a restart are marked FAILED at startup). Responses never contain filesystem paths:
manifest paths are made relative to the data directory and job errors are sanitised."""

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi import Path as PathParam

from supplier_reliability.api.dependencies import (
    DbDep,
    IdPath,
    SettingsDep,
    get_job_context,
    get_job_runner,
)
from supplier_reliability.api.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    error_responses,
)
from supplier_reliability.domain.enums import JobStatus
from supplier_reliability.repositories import jobs as repo
from supplier_reliability.schemas.datasets import GenerateRequest, JobAcceptedOut, JobOut
from supplier_reliability.services.datasets import relativise_paths
from supplier_reliability.services.jobs import (
    JobConflictError,
    JobContext,
    JobRunner,
    request_generation,
)

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])

# DS-<preset>-<seed>-<hash>: letters, digits and dashes only, bounded.
DatasetIdPath = Annotated[
    str,
    PathParam(
        pattern=r"^[A-Za-z0-9-]{1,64}$", max_length=64, examples=["DS-starter-20261022-1a2b3c4d"]
    ),
]


@router.post(
    "/generate",
    response_model=JobAcceptedOut,
    status_code=202,
    summary="Start a dataset generation job",
    description="Queues the full pipeline (generate, load, score, inject, detect, validate, "
    "export, evaluate, report) as a process-local background task and returns 202 with the job "
    "id and a `Location` header pointing at the job. Poll `GET /api/v1/datasets/jobs/{job_id}`. "
    "This is NOT a durable or distributed job system: if the server restarts the job is marked "
    "FAILED. 409 when another job is QUEUED or RUNNING, or when data already exists and "
    "`replace_existing` is false. `preset` must be one configured in the YAML config; `seed` "
    "defaults to the configured seed.",
    responses=error_responses(409, 422),
)
def generate(
    body: GenerateRequest,
    response: Response,
    db: DbDep,
    ctx: Annotated[JobContext, Depends(get_job_context)],
    runner: Annotated[JobRunner, Depends(get_job_runner)],
) -> JobAcceptedOut:
    presets = ctx.app_config.generator.presets
    if body.preset not in presets:
        raise InvalidRequestError(
            ["body", "preset"], f"Input should be one of: {', '.join(sorted(presets))}"
        )
    seed = ctx.app_config.generator.seed if body.seed is None else body.seed
    try:
        job_id = request_generation(db, ctx, runner, body.preset, seed, body.replace_existing)
    except JobConflictError as exc:
        raise ConflictError(str(exc)) from exc
    response.headers["Location"] = f"/api/v1/datasets/jobs/{job_id}"
    return JobAcceptedOut(job_id=job_id, status=JobStatus.QUEUED)


@router.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    summary="Get a dataset job",
    description="Status (QUEUED, RUNNING, SUCCEEDED, FAILED), UTC timestamps, the resulting "
    "`dataset_id` and a sanitised `error_message` (exception class names only). "
    "`current_stage` is the last pipeline stage reported by this server process; it is "
    "best-effort and absent after a restart.",
    responses=error_responses(404, 422),
)
def get_job(job_id: IdPath, db: DbDep, request: Request) -> JobOut:
    job = repo.get_job(db, job_id)
    if job is None:
        raise NotFoundError("Job", job_id)
    stage = request.app.state.job_stages.get(job_id)
    return JobOut.model_validate(job).model_copy(update={"current_stage": stage})


@router.get(
    "/manifests/{dataset_id}",
    response_model=dict[str, Any],
    summary="Get a dataset manifest",
    description="The manifest stored when the dataset was generated: configuration, row counts, "
    "file checksums and provenance. File paths are relative to the data directory; absolute "
    "filesystem paths are never returned.",
    responses=error_responses(404, 422),
)
def get_manifest(dataset_id: DatasetIdPath, db: DbDep, settings: SettingsDep) -> dict[str, Any]:
    row = repo.get_manifest(db, dataset_id)
    if row is None:
        raise NotFoundError("Dataset manifest", dataset_id)
    return relativise_paths(row.manifest, Path(settings.data_dir))

"""Dataset generation jobs (Task 21).

LIMITATIONS (read before relying on this): this is a PROCESS-LOCAL job runner for a local,
single-instance prototype. It is not a durable or distributed job system.
- The job row in ``dataset_jobs`` is the persisted state (QUEUED -> RUNNING -> SUCCEEDED/FAILED),
  but the work itself runs in a FastAPI background task of the serving process. If the process
  dies, the work is lost; at the next startup ``recover_interrupted_jobs`` marks every job left
  QUEUED or RUNNING as FAILED ("interrupted by restart"). There is no retry, queue, lease or
  heartbeat, and several workers/instances sharing one database are not supported.
- Submission (check for conflicts + insert the job row) is serialised by a process-wide lock
  (the Ruling R50 pattern used for score writes), so two concurrent requests in one process
  cannot both be accepted. The guarantee does not hold across processes.
- Timestamps are wall-clock UTC (the pipeline itself uses deterministic clocks for the data).
- ``current_stage`` of a running job is an in-memory, best-effort value kept in
  ``app.state.job_stages``; it is lost on restart and the entry is dropped as soon as the
  job reaches a terminal state, so the dict only ever holds running jobs (bounded).
- A terminal-state write (SUCCEEDED/FAILED) is retried once with a fresh session; if even
  that fails a CRITICAL line (job id and exception class only) is logged. A failed SUCCEEDED
  write falls through to a FAILED write, so a job is never stranded RUNNING (which would
  block all generation until restart) unless the database itself is unwritable.
- ``error_message`` is sanitised: only exception class names (and the failing pipeline stage)
  are stored, never ``str(exc)``, paths, tracebacks or the database URL. Full details go to the
  server log."""

import logging
import threading
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Protocol

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session, sessionmaker

from supplier_reliability.config import AppConfig, Settings
from supplier_reliability.repositories import jobs as repo
from supplier_reliability.services.pipeline import PipelineStageError, run_pipeline

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "interrupted by restart"
UNRECORDED_MESSAGE = "result could not be recorded"

_SUBMIT_LOCK = threading.Lock()


class JobConflictError(Exception):
    """A generation request conflicts with current state (the API maps it to HTTP 409)."""


class JobRunner(Protocol):
    def submit(self, job_id: str, fn: Callable[[], None]) -> None: ...


class BackgroundTaskRunner:
    """Runs the job after the response is sent, via FastAPI BackgroundTasks (process-local)."""

    def __init__(self, background: BackgroundTasks) -> None:
        self._background = background

    def submit(self, job_id: str, fn: Callable[[], None]) -> None:
        self._background.add_task(fn)


class InlineRunner:
    """Runs the job synchronously inside submit (tests)."""

    def submit(self, job_id: str, fn: Callable[[], None]) -> None:
        fn()


@dataclass(frozen=True)
class JobContext:
    session_factory: sessionmaker[Session]
    settings: Settings
    app_config: AppConfig
    clock: Callable[[], datetime]
    stages: MutableMapping[str, str]  # job_id -> last pipeline stage (best effort, in memory)


def sanitise_error(exc: BaseException) -> str:
    """Safe, bounded description of a failure: class names only."""
    if isinstance(exc, PipelineStageError):
        return f"stage '{exc.stage}' failed: {type(exc.cause).__name__}"
    return type(exc).__name__


def request_generation(
    session: Session,
    ctx: JobContext,
    runner: JobRunner,
    preset: str,
    seed: int,
    replace_existing: bool,
) -> str:
    """Accept a generation request: conflict checks and the job insert happen under one lock,
    then the job is handed to `runner`. Returns the new job id."""
    with _SUBMIT_LOCK:
        if repo.active_job_exists(session):
            raise JobConflictError("a dataset generation job is already queued or running")
        if repo.canonical_data_exists(session) and not replace_existing:
            raise JobConflictError(
                "a dataset is already loaded; set replace_existing to true to replace it"
            )
        job_id = repo.next_job_id(session)
        repo.insert_job(session, job_id, preset, seed, ctx.clock())
        session.commit()
    runner.submit(job_id, partial(run_generation_job, ctx, job_id, preset, seed, replace_existing))
    return job_id


def _record(ctx: JobContext, job_id: str, write: Callable[[Session], None]) -> bool:
    """Run a terminal-state write in its own session; one retry with a fresh session. Logs only
    the job id and exception class (CRITICAL when both attempts fail)."""
    for attempt in (1, 2):
        try:
            with ctx.session_factory() as s:
                write(s)
                s.commit()
            return True
        except Exception as exc:  # noqa: BLE001 - never let a bookkeeping failure escape
            level = logging.CRITICAL if attempt == 2 else logging.WARNING
            logger.log(
                level,
                "recording terminal state of job %s failed (attempt %d): %s",
                job_id,
                attempt,
                type(exc).__name__,
            )
    return False


def run_generation_job(
    ctx: JobContext, job_id: str, preset: str, seed: int, replace_existing: bool
) -> None:
    """The background function. Uses its own sessions, never the request's."""
    try:
        _execute(ctx, job_id, preset, seed, replace_existing)
    finally:
        ctx.stages.pop(job_id, None)  # keep the in-memory dict bounded to running jobs


def _execute(ctx: JobContext, job_id: str, preset: str, seed: int, replace_existing: bool) -> None:
    try:
        with ctx.session_factory() as s:
            repo.mark_running(s, job_id, ctx.clock())
            s.commit()
        result = run_pipeline(
            ctx.settings,
            ctx.app_config,
            preset,
            seed,
            replace_existing,
            on_progress=lambda stage: ctx.stages.__setitem__(job_id, stage),
        )
    except Exception as exc:  # noqa: BLE001 - any failure is recorded on the job, never raised
        # the original error's class is logged first; full details only in the traceback
        logger.error("dataset job %s failed: %s", job_id, type(exc).__name__, exc_info=exc)
        message = sanitise_error(exc)
        _record(ctx, job_id, lambda s: repo.mark_failed(s, job_id, ctx.clock(), message))
        return
    if not _record(
        ctx, job_id, lambda s: repo.mark_succeeded(s, job_id, ctx.clock(), result.dataset_id)
    ):
        _record(ctx, job_id, lambda s: repo.mark_failed(s, job_id, ctx.clock(), UNRECORDED_MESSAGE))


def recover_interrupted_jobs(
    session_factory: sessionmaker[Session], clock: Callable[[], datetime]
) -> int:
    """At startup: jobs left QUEUED or RUNNING by a previous process can never finish."""
    with session_factory() as s:
        count = repo.fail_unfinished(s, clock(), INTERRUPTED_MESSAGE)
        s.commit()
    if count:
        logger.warning("marked %d unfinished dataset job(s) as failed at startup", count)
    return count

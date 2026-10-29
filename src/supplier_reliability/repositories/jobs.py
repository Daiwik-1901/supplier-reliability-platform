"""Persistence for dataset jobs and manifests (Task 21). Functions flush but never commit: the
caller owns the transaction."""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import JobStatus
from supplier_reliability.models import DatasetJob, DatasetManifest, Supplier

JOB_ID_PREFIX = "JOB-"
_JOB_ID_MAX = 999_999
ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)


def next_job_id(session: Session) -> str:
    """`JOB-NNNNNN`: max existing + 1 (single writer: callers hold the submit lock)."""
    top = session.scalar(
        sa.select(sa.func.max(DatasetJob.job_id)).where(DatasetJob.job_id.like(f"{JOB_ID_PREFIX}%"))
    )
    number = 1 if top is None else int(top.removeprefix(JOB_ID_PREFIX)) + 1
    if number > _JOB_ID_MAX:
        raise ValueError("job id space JOB-NNNNNN exhausted")
    return f"{JOB_ID_PREFIX}{number:06d}"


def active_job_exists(session: Session) -> bool:
    stmt = sa.select(sa.literal(1)).where(DatasetJob.status.in_(ACTIVE)).limit(1)
    return session.execute(stmt).first() is not None


def canonical_data_exists(session: Session) -> bool:
    return session.execute(sa.select(Supplier.supplier_id).limit(1)).first() is not None


def insert_job(
    session: Session, job_id: str, preset: str, seed: int, requested_at: datetime
) -> DatasetJob:
    job = DatasetJob(
        job_id=job_id,
        status=JobStatus.QUEUED,
        preset=preset,
        seed=seed,
        requested_at=requested_at,
    )
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: str) -> DatasetJob | None:
    return session.get(DatasetJob, job_id)


def mark_running(session: Session, job_id: str, started_at: datetime) -> None:
    session.execute(
        sa.update(DatasetJob)
        .where(DatasetJob.job_id == job_id)
        .values(status=JobStatus.RUNNING, started_at=started_at)
    )


def mark_succeeded(session: Session, job_id: str, finished_at: datetime, dataset_id: str) -> None:
    session.execute(
        sa.update(DatasetJob)
        .where(DatasetJob.job_id == job_id)
        .values(status=JobStatus.SUCCEEDED, finished_at=finished_at, dataset_id=dataset_id)
    )


def mark_failed(session: Session, job_id: str, finished_at: datetime, message: str) -> None:
    session.execute(
        sa.update(DatasetJob)
        .where(DatasetJob.job_id == job_id)
        .values(status=JobStatus.FAILED, finished_at=finished_at, error_message=message)
    )


def fail_unfinished(session: Session, finished_at: datetime, message: str) -> int:
    """Mark every QUEUED or RUNNING job FAILED; returns how many there were."""
    result = session.execute(
        sa.update(DatasetJob)
        .where(DatasetJob.status.in_(ACTIVE))
        .values(status=JobStatus.FAILED, finished_at=finished_at, error_message=message)
    )
    return result.rowcount


def get_manifest(session: Session, dataset_id: str) -> DatasetManifest | None:
    return session.get(DatasetManifest, dataset_id)


def latest_manifest(session: Session) -> DatasetManifest | None:
    """The most recently created manifest (ties broken by dataset_id)."""
    stmt = (
        sa.select(DatasetManifest)
        .order_by(DatasetManifest.created_at.desc(), DatasetManifest.dataset_id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()

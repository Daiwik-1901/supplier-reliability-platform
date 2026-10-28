"""Persistence for validation runs and findings (Task 13). Never commits: the caller
(`run_validation`) owns the transaction."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.models import DataException, ValidationFinding, ValidationRun

RUN_ID_PREFIX = "VAL-"
_RUN_ID_MAX = 999_999


def next_run_id(session: Session) -> str:
    """`VAL-NNNNNN`: max existing + 1 (single writer, like score ids)."""
    top = session.scalar(
        sa.select(sa.func.max(ValidationRun.run_id)).where(
            ValidationRun.run_id.like(f"{RUN_ID_PREFIX}%")
        )
    )
    number = 1 if top is None else int(top.removeprefix(RUN_ID_PREFIX)) + 1
    if number > _RUN_ID_MAX:
        raise ValueError("validation run id space VAL-NNNNNN exhausted")
    return f"{RUN_ID_PREFIX}{number:06d}"


def insert_run(
    session: Session,
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    scope: str,
    finding_counts: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    """Insert the run, then its findings in bulk, and flush."""
    session.execute(
        sa.insert(ValidationRun),
        [
            {
                "run_id": run_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "scope": scope,
                "finding_counts": finding_counts,
            }
        ],
    )
    if findings:
        session.execute(sa.insert(ValidationFinding), [{"run_id": run_id, **f} for f in findings])
    session.flush()


def quarantined_record_ids(session: Session) -> set[str]:
    """source_record_ids of every data exception (records the detectors quarantined)."""
    stmt = sa.select(DataException.source_record_id).where(
        DataException.source_record_id.is_not(None)
    )
    return set(session.scalars(stmt))

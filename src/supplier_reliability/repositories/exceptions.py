"""Persistence and read access for data exceptions (Task 15). Never commits: the writer
(`detect_raw_batch`) owns the transaction. Every function runs a fixed number of statements."""

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import Disposition, IssueType, Severity
from supplier_reliability.models import DataException

EXCEPTION_ID_PREFIX = "EXC-"
_EXCEPTION_ID_MAX = 999_999


@dataclass(frozen=True)
class ExceptionFilters:
    """All set filters are ANDed."""

    severity: Severity | None = None
    issue_type: IssueType | None = None
    supplier_id: str | None = None
    disposition: Disposition | None = None


def _conditions(f: ExceptionFilters) -> list[sa.ColumnElement[bool]]:
    de = DataException
    out: list[sa.ColumnElement[bool]] = []
    if f.severity is not None:
        out.append(de.severity == f.severity)
    if f.issue_type is not None:
        out.append(de.issue_type == f.issue_type)
    if f.supplier_id is not None:
        out.append(de.supplier_id == f.supplier_id)
    if f.disposition is not None:
        out.append(de.disposition == f.disposition)
    return out


def list_exceptions(
    session: Session, filters: ExceptionFilters, limit: int, offset: int
) -> tuple[list[DataException], int]:
    """(one page of exceptions ordered by exception_id, total matching)."""
    if limit < 1 or offset < 0:
        raise ValueError("limit must be >= 1 and offset >= 0")
    conditions = _conditions(filters)
    total = session.scalar(sa.select(sa.func.count()).select_from(DataException).where(*conditions))
    stmt = (
        sa.select(DataException)
        .where(*conditions)
        .order_by(DataException.exception_id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt)), int(total or 0)


def get_exception(session: Session, exception_id: str) -> DataException | None:
    return session.get(DataException, exception_id)


def detected_keys(session: Session, batch_id: str) -> set[tuple[str, IssueType]]:
    """(raw_record_id, issue_type) of every exception already recorded for the batch."""
    stmt = sa.select(DataException.raw_record_id, DataException.issue_type).where(
        DataException.batch_id == batch_id
    )
    return {(raw_id, IssueType(issue)) for raw_id, issue in session.execute(stmt)}


def next_exception_ids(session: Session, n: int) -> list[str]:
    """`EXC-NNNNNN` ids: max existing + 1 onwards (single writer, like score ids)."""
    top = session.scalar(
        sa.select(sa.func.max(DataException.exception_id)).where(
            DataException.exception_id.like(f"{EXCEPTION_ID_PREFIX}%")
        )
    )
    first = 1 if top is None else int(top.removeprefix(EXCEPTION_ID_PREFIX)) + 1
    if first + n - 1 > _EXCEPTION_ID_MAX:
        raise ValueError("exception id space EXC-NNNNNN exhausted")
    return [f"{EXCEPTION_ID_PREFIX}{i:06d}" for i in range(first, first + n)]


def insert_exceptions(session: Session, rows: list[dict[str, Any]]) -> None:
    """Bulk-insert data_exceptions rows and flush."""
    if rows:
        session.execute(sa.insert(DataException), rows)
    session.flush()

"""Read access to persisted supplier scores (Task 12). Every function runs a fixed number of
statements (no per-row lazy loads; relationships are lazy="raise")."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import CalculationStatus, ReliabilityCategory
from supplier_reliability.models import ScoreEvidence, SupplierScore

# sort keys accepted by list_scores ("-" prefix = descending); anything else is rejected
SORTABLE_COLUMNS = {
    "score_id": SupplierScore.score_id,
    "supplier_id": SupplierScore.supplier_id,
    "evaluation_start": SupplierScore.evaluation_start,
    "evaluation_end": SupplierScore.evaluation_end,
    "data_cutoff_timestamp": SupplierScore.data_cutoff_timestamp,
    "calculated_at": SupplierScore.calculated_at,
    "overall_score": SupplierScore.overall_score,
    "version_number": SupplierScore.version_number,
}


@dataclass(frozen=True)
class ScoreFilters:
    """All set filters are ANDed. `evaluation_start` keeps scores whose window starts at or after
    it, `evaluation_end` those whose window ends at or before it. `current_only` (default) hides
    superseded versions."""

    supplier_id: str | None = None
    reliability_category: ReliabilityCategory | None = None
    calculation_status: CalculationStatus | None = None
    evaluation_start: datetime | None = None
    evaluation_end: datetime | None = None
    current_only: bool = True


def _conditions(f: ScoreFilters) -> list[sa.ColumnElement[bool]]:
    s = SupplierScore
    out: list[sa.ColumnElement[bool]] = []
    if f.supplier_id is not None:
        out.append(s.supplier_id == f.supplier_id)
    if f.reliability_category is not None:
        out.append(s.reliability_category == f.reliability_category)
    if f.calculation_status is not None:
        out.append(s.calculation_status == f.calculation_status)
    if f.evaluation_start is not None:
        out.append(s.evaluation_start >= f.evaluation_start)
    if f.evaluation_end is not None:
        out.append(s.evaluation_end <= f.evaluation_end)
    if f.current_only:
        out.append(s.is_current.is_(True))
    return out


def get_score(session: Session, score_id: str) -> SupplierScore | None:
    return session.get(SupplierScore, score_id)


def list_scores(
    session: Session,
    filters: ScoreFilters,
    limit: int,
    offset: int,
    sort: str = "score_id",
) -> tuple[list[SupplierScore], int]:
    """(one page of scores, total matching). `sort` is a whitelisted column name, optionally
    prefixed with "-" for descending; ties are broken by score_id ascending."""
    name = sort.removeprefix("-")
    if name not in SORTABLE_COLUMNS:
        raise ValueError(f"unsupported sort {sort!r}; allowed: {sorted(SORTABLE_COLUMNS)}")
    if limit < 1 or offset < 0:
        raise ValueError("limit must be >= 1 and offset >= 0")
    conditions = _conditions(filters)
    total = session.scalar(sa.select(sa.func.count()).select_from(SupplierScore).where(*conditions))
    column = SORTABLE_COLUMNS[name]
    order = [column.desc() if sort.startswith("-") else column.asc()]
    if name != "score_id":
        order.append(SupplierScore.score_id.asc())
    stmt = sa.select(SupplierScore).where(*conditions).order_by(*order).limit(limit).offset(offset)
    return list(session.scalars(stmt)), int(total or 0)


def current_scores_for_supplier(session: Session, supplier_id: str) -> Sequence[SupplierScore]:
    """Current versions for the supplier, oldest window first (restatements after the score
    they restate: ordered by window end, then cutoff)."""
    s = SupplierScore
    stmt = (
        sa.select(s)
        .where(s.supplier_id == supplier_id, s.is_current.is_(True))
        .order_by(s.evaluation_end, s.data_cutoff_timestamp, s.score_id)
    )
    return list(session.scalars(stmt))


def latest_current_score(session: Session, supplier_id: str) -> SupplierScore | None:
    """The current score with the latest window end (latest cutoff on ties), or None."""
    s = SupplierScore
    stmt = (
        sa.select(s)
        .where(s.supplier_id == supplier_id, s.is_current.is_(True))
        .order_by(s.evaluation_end.desc(), s.data_cutoff_timestamp.desc(), s.score_id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def lineage_counts(session: Session, score_id: str) -> dict[str, dict[str, int]]:
    """{component: {"included": n, "excluded": n}} over the score's lineage rows (one query)."""
    e = ScoreEvidence
    rows = session.execute(
        sa.select(e.component, e.included, sa.func.count())
        .where(e.score_id == score_id)
        .group_by(e.component, e.included)
    ).all()
    out: dict[str, dict[str, int]] = {}
    for component, included, n in rows:
        out.setdefault(component.value, {"included": 0, "excluded": 0})[
            "included" if included else "excluded"
        ] = n
    return out

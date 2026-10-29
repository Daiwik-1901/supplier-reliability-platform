"""Supplier read queries. Set-based: the list joins each supplier's latest current score in the
same statement (window function), details use selectinload; no per-row lazy loads.

"Latest current score" = the is_current, site_id IS NULL row with the greatest
data_cutoff_timestamp (ties: later evaluation_end, then score_id). The list, the detail and the
explanation all use it so the API tells one story."""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased, selectinload

from supplier_reliability.domain.enums import (
    Region,
    ReliabilityCategory,
    SupplierCategory,
    SupplierStatus,
)
from supplier_reliability.models import Supplier, SupplierScore

SUPPLIER_SORT_FIELDS = ("supplier_id", "supplier_name", "overall_score")

_S = SupplierScore
_LATEST_ORDER = (_S.data_cutoff_timestamp.desc(), _S.evaluation_end.desc(), _S.score_id.desc())


@dataclass(frozen=True)
class SupplierFilters:
    region: Region | None = None
    category: SupplierCategory | None = None
    status: SupplierStatus | None = None
    reliability_category: ReliabilityCategory | None = None
    q: str | None = None


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_suppliers(
    session: Session,
    filters: SupplierFilters,
    sort_field: str,
    descending: bool,
    limit: int,
    offset: int,
) -> tuple[list[tuple[Supplier, SupplierScore | None]], int]:
    """(page of (supplier, latest current score or None), total matching): two statements."""
    ranked = (
        sa.select(
            _S.score_id,
            _S.supplier_id,
            sa.func.row_number()
            .over(partition_by=_S.supplier_id, order_by=_LATEST_ORDER)
            .label("rn"),
        )
        .where(_S.is_current.is_(True), _S.site_id.is_(None))
        .subquery("ranked_scores")
    )
    latest = aliased(SupplierScore, name="latest_score")

    conditions: list[sa.ColumnElement[bool]] = []
    if filters.region is not None:
        conditions.append(Supplier.region == filters.region)
    if filters.category is not None:
        conditions.append(Supplier.supplier_category == filters.category)
    if filters.status is not None:
        conditions.append(Supplier.supplier_status == filters.status)
    if filters.reliability_category is not None:
        conditions.append(latest.reliability_category == filters.reliability_category)
    if filters.q:
        pattern = f"%{_escape_like(filters.q)}%"
        conditions.append(
            sa.or_(
                Supplier.supplier_name.ilike(pattern, escape="\\"),
                Supplier.supplier_id.ilike(pattern, escape="\\"),
            )
        )

    def joined(stmt: sa.Select) -> sa.Select:
        on_rank = sa.and_(ranked.c.supplier_id == Supplier.supplier_id, ranked.c.rn == 1)
        return (
            stmt.select_from(Supplier)
            .outerjoin(ranked, on_rank)
            .outerjoin(latest, latest.score_id == ranked.c.score_id)
            .where(*conditions)
        )

    total = session.scalar(joined(sa.select(sa.func.count(Supplier.supplier_id))))
    columns = {
        "supplier_id": Supplier.supplier_id,
        "supplier_name": Supplier.supplier_name,
        "overall_score": latest.overall_score,
    }
    column = columns[sort_field]
    primary = (column.desc() if descending else column.asc()).nulls_last()
    order = [primary] if sort_field == "supplier_id" else [primary, Supplier.supplier_id.asc()]
    stmt = joined(sa.select(Supplier, latest)).order_by(*order).limit(limit).offset(offset)
    return [(row[0], row[1]) for row in session.execute(stmt)], int(total or 0)


def get_supplier(session: Session, supplier_id: str) -> Supplier | None:
    """Supplier with sites and contracts eagerly loaded (3 statements, however many rows)."""
    stmt = (
        sa.select(Supplier)
        .where(Supplier.supplier_id == supplier_id)
        .options(selectinload(Supplier.sites), selectinload(Supplier.contracts))
    )
    return session.scalars(stmt).first()


def supplier_exists(session: Session, supplier_id: str) -> bool:
    stmt = sa.select(sa.literal(1)).where(sa.exists().where(Supplier.supplier_id == supplier_id))
    return session.scalar(stmt) is not None


def latest_current_score(session: Session, supplier_id: str) -> SupplierScore | None:
    stmt = (
        sa.select(_S)
        .where(_S.supplier_id == supplier_id, _S.is_current.is_(True), _S.site_id.is_(None))
        .order_by(*_LATEST_ORDER)
        .limit(1)
    )
    return session.scalars(stmt).first()


def current_scores(session: Session, supplier_id: str) -> Sequence[SupplierScore]:
    """Current score rows, one per window (restatements after the score they restate)."""
    stmt = (
        sa.select(_S)
        .where(_S.supplier_id == supplier_id, _S.is_current.is_(True), _S.site_id.is_(None))
        .order_by(_S.evaluation_end, _S.data_cutoff_timestamp, _S.score_id)
    )
    return list(session.scalars(stmt))


def score_history(
    session: Session, supplier_id: str, limit: int, offset: int
) -> tuple[list[SupplierScore], int]:
    """Every stored version incl. superseded ones, by window end then version number."""
    where = (_S.supplier_id == supplier_id, _S.site_id.is_(None))
    total = session.scalar(sa.select(sa.func.count()).select_from(_S).where(*where))
    stmt = (
        sa.select(_S)
        .where(*where)
        .order_by(_S.evaluation_end, _S.version_number, _S.data_cutoff_timestamp, _S.score_id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt)), int(total or 0)

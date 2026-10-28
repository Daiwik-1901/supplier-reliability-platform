"""Shared report plumbing: payload shape, the six scorecard windows read from persisted scores."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.models import Supplier, SupplierScore
from supplier_reliability.services.reporting.metrics import iso

__all__ = ["ScoreWindow", "current_rows", "latest_window", "report", "score_windows"]


@dataclass(frozen=True)
class ScoreWindow:
    start: datetime
    end: datetime
    cutoff: datetime

    @property
    def label(self) -> str:
        return (
            f"trailing window {self.start:%Y-%m-%d} to {self.end:%Y-%m-%d} "
            f"(cutoff {iso(self.cutoff)})"
        )

    def as_dict(self) -> dict[str, str]:
        return {"start": iso(self.start), "end": iso(self.end), "cutoff": iso(self.cutoff)}


def report(
    name: str,
    title: str,
    metrics: dict[str, Any],
    tables: dict[str, list[dict[str, Any]]] | None = None,
    notes: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "report": name,
        "title": title,
        "metrics": metrics,
        "tables": tables or {},
        "notes": notes or [],
    }
    payload.update(extra)
    return payload


_CURRENT = (SupplierScore.is_current.is_(True), SupplierScore.site_id.is_(None))


def score_windows(session: Session) -> list[ScoreWindow]:
    """Distinct windows of the CURRENT supplier-level scores, oldest cutoff first."""
    s = SupplierScore
    stmt = (
        sa.select(s.evaluation_start, s.evaluation_end, s.data_cutoff_timestamp)
        .where(*_CURRENT)
        .distinct()
        .order_by(s.data_cutoff_timestamp, s.evaluation_start)
    )
    return [ScoreWindow(*row) for row in session.execute(stmt)]


def latest_window(session: Session) -> ScoreWindow:
    windows = score_windows(session)
    if not windows:
        raise LookupError("no current supplier scores; run scoring before reporting")
    return windows[-1]


def current_rows(
    session: Session,
    *columns: Any,
    window: ScoreWindow | None = None,
    join_supplier: bool = False,
) -> list[sa.Row]:
    """Selected columns of current supplier-level scores (one row per supplier and window),
    ordered by cutoff then supplier_id; optionally restricted to one window."""
    s = SupplierScore
    stmt = sa.select(*columns).select_from(s).where(*_CURRENT)
    if join_supplier:
        stmt = stmt.join(Supplier, Supplier.supplier_id == s.supplier_id)
    if window is not None:
        stmt = stmt.where(
            s.evaluation_start == window.start,
            s.evaluation_end == window.end,
            s.data_cutoff_timestamp == window.cutoff,
        )
    return list(session.execute(stmt.order_by(s.data_cutoff_timestamp, s.supplier_id)))

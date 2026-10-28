"""Shared helpers of the anomaly injector: deterministic sampling and payload formatting."""

from collections.abc import Callable, Sequence
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.models import Shipment
from supplier_reliability.services.export import column_converter
from supplier_reliability.services.generator.config import GeneratorCapacityError

# datetime -> "YYYY-MM-DDTHH:MM:SS.ffffffZ", the export's timestamp format
format_ts = column_converter(Shipment.__table__.c.created_at)


def row_payload(table: sa.Table, row: Any) -> dict[str, Any]:
    """A canonical row (mapping) as a JSON payload: column order, export formatting."""
    out: dict[str, Any] = {}
    for column in table.columns:
        value = row[column.name]
        out[column.name] = None if value is None else column_converter(column)(value)
    return out


def table_rows(session: Session, table: sa.Table, *where: Any) -> list[Any]:
    """Row mappings of `table`, ordered by primary key."""
    pk = list(table.primary_key.columns)
    return list(session.execute(sa.select(table).where(*where).order_by(*pk)).mappings())


class Picker:
    """Deterministic sampling without replacement across all calls (one per entity type), so
    the canonical bases of positives and negatives never overlap."""

    def __init__(self, rng: np.random.Generator, ids: Sequence[str], what: str) -> None:
        self.order = [ids[i] for i in rng.permutation(len(ids))]
        self.used: set[str] = set()
        self.what = what

    def take(self, k: int, accept: Callable[[str], bool] = lambda _: True) -> list[str]:
        out: list[str] = []
        for record_id in self.order:
            if len(out) == k:
                break
            if record_id not in self.used and accept(record_id):
                out.append(record_id)
        if len(out) < k:
            raise GeneratorCapacityError(
                f"anomaly injection needs {k} {self.what}, only {len(out)} available"
            )
        self.used.update(out)
        return out

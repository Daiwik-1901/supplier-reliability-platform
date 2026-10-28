"""Independent recomputation of supplier metrics and scores from the exported CSVs (Add-D).

A deliberately separate implementation of D5 eligibility, the six D6 components and the D7
overall/category, written with pandas over `export_tables` output. It imports nothing from the
project and has no database access (enforced by tests/unit/test_anomaly_eval.py): only the
scoring parameters, passed in as a plain dict (`ScoringConfig.model_dump(mode="json")` or a
stored config snapshot), are shared with the engine. Decisions are not recomputed (they depend
on data exceptions).

What is and is not independent (stated plainly; docs/ copies this list):
- INDEPENDENT of the engine: the data access and the arithmetic. Everything is read from the
  exported CSVs (no SQL, no ORM, no engine query); joins and allocation paths (inspection ->
  shipment_line -> shipment -> PO -> supplier, requirement -> contract -> supplier) are rebuilt
  here; the disruption dedup key is recomputed from (supplier, site, type, observed date) instead
  of reading the stored dedup_key; cost uses the stored variance_amount where the engine uses
  invoiced - expected; fractional days, sums and roundings are recomputed in Decimal. So a wrong
  query, join, stored derived column or arithmetic slip in the engine shows up as a mismatch.
- TRANSCRIBED from the engine's documented interpretation (evidence.py), and therefore NOT
  cross-checked by reconciliation (a wrong reading of the rule would be wrong in both):
  - capacity: the expansion of a declaration to the window months it overlaps, and the
    per-(supplier, month) scope cells (site-level rows of any verification status override
    supplier-wide rows);
  - compliance: the certification state mapping (incl. R19 "not yet valid -> UNKNOWN") and the
    supersession scan (current = latest by (recorded_at, id) among rows recorded <= C that no
    row recorded <= C supersedes);
  - disruption: site_active as of date(C) (effective dates; open-ended -> site_status ACTIVE);
  - delivery: the correction loop (first ARRIVED by (recorded, id); latest valid
    event_timestamp CORRECTION recorded <= C wins; unparseable/naive ones skipped);
  - compliance requirements count only on a contract effective at date(C) and not DRAFT.
  These are checked against hand-calculated expectations (Task 9/10/12b tests), not here.

Arithmetic is Decimal from the CSV text (never float), except the disruption recency decay, which
uses `Decimal(str(math.exp(...)))` exactly as D6 prescribes. Component scores are clamped and
rounded half up to 2 dp once, at the end. Where results could legitimately differ from the
engine's in the last digit, see `reconciliation.TOLERANT_FIELDS`.

Columns of the returned frame (one row per supplier in suppliers.csv, sorted by id; None =
missing, counts 0 when there is no evidence): see `COLUMNS`.
"""

from pathlib import Path
from typing import Any

import pandas as pd

from ._tables import TABLES, Params, Tables, Window, WindowLike, is_null, load_tables
from .capacity import capacity
from .compliance import compliance
from .delivery import delivery
from .disruption import disruption
from .overall import COMPONENTS, overall
from .quality_cost import cost, quality

__all__ = [
    "COLUMNS",
    "COMPONENTS",
    "INDEPENDENT_TABLES",
    "Tables",
    "WindowLike",
    "compute_metrics",
    "independent_supplier_metrics",
    "is_null",
    "load_tables",
]

INDEPENDENT_TABLES = TABLES
_DEFAULTS: dict[str, Any] = {
    "delivery_due": 0,
    "delivery_on_time": 0,
    "on_time_rate": None,
    "mean_late_days": None,
    "delivery_score": None,
    "quality_inspections": 0,
    "inspected_units": None,
    "defect_rate": None,
    "quality_score": None,
    "cost_invoices": 0,
    "wavp": None,
    "cost_score": None,
    "capacity_months": 0,
    "capacity_util": None,
    "capacity_score": None,
    "compliance_required": 0,
    "compliance_known": 0,
    "mandatory_compliance_failure": False,
    "compliance_score": None,
    "disruption_signals": 0,
    "disruption_kept": 0,
    "disruption_penalty": None,
    "disruption_score": None,
}
COLUMNS = [
    "supplier_id",
    *_DEFAULTS,
    "overall_score",
    "evidence_coverage",
    "reliability_category",
]
_PARTS = (delivery, quality, cost, capacity, compliance, disruption)


def compute_metrics(tables: Tables, window: WindowLike, scoring_params: dict) -> pd.DataFrame:
    """The recomputation over already loaded tables (load once, reuse across windows)."""
    w, p = Window.of(window), Params.of(scoring_params)
    parts = [part(tables, w, p) for part in _PARTS]
    rows = []
    for supplier_id in tables.supplier_ids:
        row: dict[str, Any] = {"supplier_id": supplier_id, **_DEFAULTS}
        for part in parts:
            row.update(part.get(supplier_id, {}))
        row.update(overall(row, p))
        rows.append(row)
    return pd.DataFrame(rows, columns=COLUMNS).astype(object)


def independent_supplier_metrics(
    export_dir: Path, window: WindowLike, scoring_params: dict
) -> pd.DataFrame:
    """Raw metrics, component scores, overall and category per supplier for `window`
    (start/end/cutoff), recomputed from the CSVs in `export_dir` (see the module docstring)."""
    return compute_metrics(load_tables(Path(export_dir)), window, scoring_params)

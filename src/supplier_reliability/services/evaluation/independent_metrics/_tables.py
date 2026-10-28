"""Exported CSVs -> typed pandas frames, the window bounds and the scoring parameters.

Deliberately self-contained (Add-D): stdlib + pandas only, nothing from the project. CSV cells
are read as text; money / quantities become `Decimal` (never float), timestamps tz-aware UTC
`pd.Timestamp` (the export writes `...ffffffZ`, so nanosecond values are exact), DATE columns
`datetime.date`, booleans `true`/`false`.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

TABLES: tuple[str, ...] = (
    "suppliers",
    "sites",
    "contracts",
    "compliance_requirements",
    "supplier_certifications",
    "capacity_declarations",
    "purchase_orders",
    "po_lines",
    "shipments",
    "shipment_lines",
    "delivery_events",
    "inspections",
    "invoices",
    "risk_signals",
)
NS_PER_DAY = Decimal(86_400 * 10**9)
ZERO, ONE, HUNDRED = Decimal(0), Decimal(1), Decimal(100)
_CENT = Decimal("0.01")


def clamp_round(x: Decimal) -> Decimal:
    """Clamp to [0, 100], then round half up to 2 dp (D6: only the final score is rounded)."""
    return min(HUNDRED, max(ZERO, x)).quantize(_CENT, rounding=ROUND_HALF_UP)


def is_null(v: Any) -> bool:
    return v is None or (isinstance(v, float) and v != v) or v is pd.NaT


class WindowLike(Protocol):
    """Anything with aware `start`, `end`, `cutoff` datetimes (e.g. an EvaluationWindow)."""

    start: datetime
    end: datetime
    cutoff: datetime


@dataclass(frozen=True)
class Window:
    start: pd.Timestamp
    end: pd.Timestamp
    cutoff: pd.Timestamp

    @classmethod
    def of(cls, w: WindowLike) -> "Window":
        stamps = [pd.Timestamp(getattr(w, k)) for k in ("start", "end", "cutoff")]
        if any(s.tzinfo is None for s in stamps):
            raise ValueError("window bounds must be timezone-aware")
        start, end, cutoff = (s.tz_convert("UTC") for s in stamps)
        return cls(start, end, cutoff)

    @property
    def top(self) -> pd.Timestamp:
        """min(E, C): the last instant an eligible record may have occurred (D5)."""
        return min(self.end, self.cutoff)

    @property
    def first_day(self) -> date:
        return self.start.date()

    @property
    def last_day(self) -> date:
        return self.top.date()

    @property
    def cutoff_day(self) -> date:
        return self.cutoff.date()


def days(delta: pd.Timedelta) -> Decimal:
    """A duration in fractional days, exactly (integer nanoseconds / ns per day)."""
    return Decimal(int(delta.value)) / NS_PER_DAY


def d(x: Any) -> Decimal:
    """A config number as Decimal via str (0.1 stays 0.1)."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass(frozen=True)
class Params:
    """The ScoringConfig values the recomputation needs (from a plain dict)."""

    weights: dict[str, Decimal]
    thresholds: dict[str, tuple[Decimal, Decimal]]
    grace: pd.Timedelta
    min_obs: dict[str, int]
    coverage_threshold: Decimal
    penalty_per_day: Decimal
    penalty_cap: Decimal
    defect_rate_zero: Decimal
    variance_zero: Decimal
    underbilling_weight: Decimal
    breakpoints: list[tuple[Decimal, Decimal]]
    compliance_points: dict[str, Decimal]
    severity_weights: dict[str, Decimal]
    recency_days: Decimal
    lookback: pd.Timedelta
    resolved_multiplier: Decimal
    inactive_site_multiplier: Decimal
    dedup_window_days: int

    @classmethod
    def of(cls, p: dict[str, Any]) -> "Params":
        return cls(
            weights={k: d(v) for k, v in p["weights"].items()},
            thresholds={
                k: (d(v["lower"]), d(v["upper"])) for k, v in p["category_thresholds"].items()
            },
            grace=pd.Timedelta(hours=int(p["grace_hours"])),
            min_obs={k: int(v) for k, v in p["min_observations"].items()},
            coverage_threshold=d(p["coverage_threshold"]),
            penalty_per_day=d(p["delivery"]["delay_penalty_per_day"]),
            penalty_cap=d(p["delivery"]["delay_penalty_cap"]),
            defect_rate_zero=d(p["defect_rate_zero_score"]),
            variance_zero=d(p["variance_zero_score"]),
            underbilling_weight=d(p["underbilling_weight"]),
            breakpoints=sorted(
                (d(b["utilization"]), d(b["score"])) for b in p["capacity_breakpoints"]
            ),
            compliance_points={k: d(v) for k, v in p["compliance_points"].items()},
            severity_weights={k: d(v) for k, v in p["severity_weights"].items()},
            recency_days=d(p["recency_days"]),
            lookback=pd.Timedelta(days=int(p["lookback_days"])),
            resolved_multiplier=d(p["resolved_multiplier"]),
            inactive_site_multiplier=d(p["inactive_site_multiplier"]),
            dedup_window_days=int(p["dedup_window_days"]),
        )


# --- typed loading ----------------------------------------------------------------------------

_TIMESTAMPS = {
    "sites": ["created_at"],
    "contracts": ["created_at"],
    "compliance_requirements": ["created_at"],
    "supplier_certifications": ["recorded_at"],
    "capacity_declarations": ["recorded_at"],
    "purchase_orders": ["created_at"],
    "po_lines": ["created_at"],
    "shipments": ["expected_delivery_timestamp", "created_at"],
    "delivery_events": ["event_timestamp", "recorded_timestamp"],
    "inspections": ["inspection_timestamp", "recorded_at"],
    "invoices": ["recorded_at"],
    "risk_signals": ["observed_timestamp", "recorded_timestamp", "valid_to"],
}
_DATES = {
    "sites": ["effective_from", "effective_to"],
    "contracts": ["effective_from", "effective_to"],
    "compliance_requirements": ["effective_from", "effective_to"],
    "supplier_certifications": ["valid_from", "valid_to"],
    "capacity_declarations": ["period_start", "period_end"],
    "purchase_orders": ["order_date"],
    "invoices": ["invoice_date"],
}
_DECIMALS = {
    "capacity_declarations": ["available_capacity", "committed_capacity"],
    "po_lines": ["ordered_quantity"],
    "inspections": ["inspected_quantity", "defect_quantity"],
    "invoices": ["expected_amount", "invoiced_amount", "variance_amount"],
}
_BOOLEANS = {"compliance_requirements": ["mandatory"]}


def _date(v: Any) -> date | None:
    return date.fromisoformat(v) if isinstance(v, str) else None


def _decimal(v: Any) -> Decimal | None:
    return Decimal(v) if isinstance(v, str) else None


def read_table(export_dir: Path, name: str) -> pd.DataFrame:
    """One exported `<name>.csv`, typed (see the module docstring); empty cells are null."""
    df = pd.read_csv(
        Path(export_dir) / f"{name}.csv", dtype=str, keep_default_na=False, na_values=[""]
    )
    for col in _TIMESTAMPS.get(name, []):
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    for col in _DATES.get(name, []):
        df[col] = df[col].map(_date).astype(object)
    for col in _DECIMALS.get(name, []):
        df[col] = df[col].map(_decimal).astype(object)
    for col in _BOOLEANS.get(name, []):
        df[col] = df[col] == "true"
    return df


@dataclass(frozen=True)
class Tables:
    frames: dict[str, pd.DataFrame]

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.frames[name]

    @property
    def supplier_ids(self) -> list[str]:
        return sorted(self["suppliers"]["supplier_id"])

    def po_supplier(self) -> pd.DataFrame:
        return self["purchase_orders"][["po_id", "supplier_id"]]


def load_tables(export_dir: Path) -> Tables:
    """Every table the recomputation reads, loaded once (reuse it across windows)."""
    return Tables({name: read_table(export_dir, name) for name in TABLES})

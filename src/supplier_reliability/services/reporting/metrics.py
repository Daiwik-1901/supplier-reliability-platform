"""The metric contract shared by every report (spec section 13).

Every number a report states is a metric dict
`{value, numerator, denominator, filters, window, missing_data_handling}`. A payload is
`{"report", "title", "metrics": {...groups of metrics...}, "tables": {name: [row, ...]}, "notes"}`:
`metrics` holds only metrics (nested dicts are groups), `tables` hold descriptive rows (ids,
labels and the persisted figures of a listed record). `metric_problems` walks a payload and
reports every deviation, so a report cannot silently omit a definition."""

from datetime import datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "METRIC_KEYS",
    "NO_DENOMINATOR",
    "count_metric",
    "counted",
    "iso",
    "metric",
    "metric_problems",
    "ratio",
]

METRIC_KEYS = ("value", "numerator", "denominator", "filters", "window", "missing_data_handling")
NO_DENOMINATOR = "no denominator"
_NUMBER = (int, float, Decimal)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def metric(
    value: Any,
    numerator: Any,
    denominator: Any,
    filters: str,
    window: str,
    missing_data_handling: str,
) -> dict[str, Any]:
    return {
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "filters": filters,
        "window": window,
        "missing_data_handling": missing_data_handling,
    }


def count_metric(value: int, filters: str, window: str, note: str = "") -> dict[str, Any]:
    """A pure count: no numerator or denominator."""
    handling = "count; no denominator" + (f"; {note}" if note else "")
    return metric(value, None, None, filters, window, handling)


def counted(count: int, total: int, filters: str, window: str, missing: str) -> dict[str, Any]:
    """A count that is part of a population: value = count, numerator = count, denominator =
    population size (so the share is numerator / denominator)."""
    return metric(count, count, total, filters, window, missing)


def ratio(
    numerator: Any, denominator: Any, filters: str, window: str, missing: str
) -> dict[str, Any]:
    """numerator / denominator as a float (6 dp); value is None when the denominator is 0."""
    value = None
    if denominator:
        value = round(float(Decimal(str(numerator)) / Decimal(str(denominator))), 6)
    return metric(
        value,
        numerator,
        denominator,
        filters,
        window,
        f"{missing}; value is null when the denominator is 0",
    )


def _is_metric_like(node: dict) -> bool:
    return any(k in node for k in METRIC_KEYS)


def _check(node: Any, path: str, out: list[str]) -> None:
    if not isinstance(node, dict):
        out.append(f"{path}: expected a metric or a group of metrics, got {type(node).__name__}")
        return
    if not _is_metric_like(node):
        for key, child in node.items():
            _check(child, f"{path}.{key}", out)
        return
    if set(node) != set(METRIC_KEYS):
        out.append(f"{path}: keys {sorted(node)} != {sorted(METRIC_KEYS)}")
        return
    for key in ("numerator", "denominator"):
        if node[key] is not None and not isinstance(node[key], _NUMBER):
            out.append(f"{path}.{key}: not a number")
    for key in ("filters", "window", "missing_data_handling"):
        if not isinstance(node[key], str) or not node[key].strip():
            out.append(f"{path}.{key}: must be a non-empty string")
    if (node["numerator"] is None or node["denominator"] is None) and NO_DENOMINATOR not in str(
        node["missing_data_handling"]
    ):
        out.append(f"{path}: null numerator/denominator must say 'no denominator' in handling")
    if (node["numerator"] is None) != (node["denominator"] is None):
        out.append(f"{path}: numerator and denominator must both be set or both be null")


def metric_problems(payload: dict[str, Any]) -> list[str]:
    """Every deviation from the metric contract in `payload['metrics']` (empty when valid)."""
    out: list[str] = []
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        return ["payload has no 'metrics'"]
    for key, child in metrics.items():
        _check(child, f"metrics.{key}", out)
    return out

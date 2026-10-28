"""Write a report payload as deterministic JSON and readable Markdown (no HTML)."""

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from supplier_reliability.services.reporting.metrics import METRIC_KEYS, iso

__all__ = ["render_markdown", "write_report"]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return iso(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _escape(text: str) -> str:
    """Escape for a Markdown table cell or heading: backslash first, then pipe; newlines
    become spaces."""
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return " ".join(text.splitlines()) if "\n" in text or "\r" in text else text


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict | list | tuple):
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=_default)
    else:
        text = str(value)
    return _escape(text)


def _flatten(node: dict[str, Any], path: tuple[str, ...] = ()):
    if any(k in node for k in METRIC_KEYS):
        yield ".".join(path), node
        return
    for key, child in node.items():
        yield from _flatten(child, (*path, str(key)))


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    head = [_escape(h) for h in headers]
    lines = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return lines


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# {payload.get('title') or payload.get('report', 'Report')}", ""]
    if payload.get("generated_at"):
        lines += [f"Generated at: {payload['generated_at']}", ""]
    if isinstance(payload.get("window"), dict):
        w = payload["window"]
        lines += [f"Window: {w.get('start')} to {w.get('end')} (cutoff {w.get('cutoff')})", ""]
    if payload.get("notes"):
        lines += [f"- {note}" for note in payload["notes"]] + [""]
    metrics = list(_flatten(payload.get("metrics", {})))
    if metrics:
        rows = [[_cell(name), *(_cell(m[k]) for k in METRIC_KEYS)] for name, m in metrics]
        lines += ["## Metrics", "", *_table(["metric", *METRIC_KEYS], rows), ""]
    for name, rows in payload.get("tables", {}).items():
        lines += [f"## {_escape(str(name))}", ""]
        if not rows:
            lines += ["_none_", ""]
            continue
        columns: list[str] = []
        for row in rows:
            columns += [k for k in row if k not in columns]
        lines += _table(columns, [[_cell(r.get(c)) for c in columns] for r in rows]) + [""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write_report(
    name: str,
    payload: dict[str, Any],
    reports_dir: Path,
    generated_at: datetime | None = None,
) -> tuple[Path, Path]:
    """Write `<name>.json` (sorted keys, indent 2, UTF-8, LF) and `<name>.md`. The payload is
    deterministic; `generated_at` is the only wall-clock value and is included only when given."""
    if not _NAME.match(name):
        raise ValueError(f"invalid report name {name!r}")
    body = dict(payload)
    if generated_at is not None:
        body["generated_at"] = iso(generated_at)
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out / f"{name}.json", out / f"{name}.md"
    text = json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False, default=_default)
    with json_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    with md_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_markdown(body))
    return json_path, md_path

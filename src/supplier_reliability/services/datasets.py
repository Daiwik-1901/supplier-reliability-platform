"""Dataset manifest helpers: keep filesystem paths out of API responses."""

import re
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_absolute(text: str) -> bool:
    return text.startswith(("/", "\\")) or bool(_WINDOWS_ABSOLUTE.match(text))


def _relative(text: str, data_dir: Path) -> str:
    """An absolute path made relative to `data_dir`; outside it, only the final name is kept."""
    for base in (data_dir, data_dir.resolve()):
        try:
            rel = PurePath(text).relative_to(base)
        except ValueError:
            continue
        if rel.parts:
            return rel.as_posix()
    return PureWindowsPath(text).name


def relativise_paths(value: Any, data_dir: Path) -> Any:
    """Copy of a JSON value in which every string that STARTS with an absolute path (POSIX
    ``/``, UNC/backslash, or a drive letter) is replaced by a data-dir-relative one.

    LIMIT: only path-leading string VALUES are rewritten. Paths embedded in longer text and
    dictionary keys are left untouched. The pipeline stores relative paths only, so this is
    defence in depth for odd rows, not a general scrubber."""
    if isinstance(value, dict):
        return {k: relativise_paths(v, data_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [relativise_paths(v, data_dir) for v in value]
    if isinstance(value, str) and _is_absolute(value):
        return _relative(value, data_dir)
    return value

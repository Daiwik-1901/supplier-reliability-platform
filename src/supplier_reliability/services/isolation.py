"""Ground-truth isolation check (spec section 8/10, D9): scoring, validation and detection code
must never see the injected ground truth.

`find_ground_truth_references(package_name)` parses (never imports) every ``.py`` file of a
package with ``ast`` and reports:
- an import of ``supplier_reliability.services.anomalies`` or
  ``supplier_reliability.services.evaluation`` (or a submodule), or of any module whose dotted
  name contains ``ground_truth``. Relative imports are resolved against the importing module's
  package first; ``from pkg import name`` is checked as ``pkg.name`` too;
- a string literal containing ``ground_truth`` or naming one of the forbidden modules (so a
  dynamic ``importlib.import_module("...")`` or a hard-coded ground-truth file path is caught).

Each finding is ``"<module>:<line>: <what>"``. An empty list means the package is isolated.
"""

import ast
import importlib.util
from pathlib import Path

__all__ = ["FORBIDDEN_MODULES", "find_ground_truth_references"]

FORBIDDEN_MODULES = (
    "supplier_reliability.services.anomalies",
    "supplier_reliability.services.evaluation",
)
_MARKER = "ground_truth"


def _forbidden_module(name: str) -> bool:
    if _MARKER in name:
        return True
    return any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES)


def _forbidden_text(text: str) -> bool:
    return _MARKER in text or any(m in text for m in FORBIDDEN_MODULES)


def _module_files(package_name: str) -> list[tuple[str, Path, bool]]:
    """(dotted module name, file, is_package_init) for every module of the package."""
    spec = importlib.util.find_spec(package_name)
    if spec is None:
        raise ModuleNotFoundError(f"no module named {package_name!r}")
    if not spec.submodule_search_locations:  # a plain module
        return [(package_name, Path(spec.origin or ""), False)]
    out = []
    for location in spec.submodule_search_locations:
        root = Path(location)
        for path in sorted(root.rglob("*.py")):
            parts = list(path.relative_to(root).with_suffix("").parts)
            is_init = parts[-1] == "__init__"
            if is_init:
                parts = parts[:-1]
            out.append((".".join([package_name, *parts]), path, is_init))
    return out


def _resolve(node: ast.ImportFrom, module: str, is_init: bool) -> str:
    if node.level == 0:
        return node.module or ""
    package = module.split(".") if is_init else module.split(".")[:-1]
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*base, *([node.module] if node.module else [])])


def _check(module: str, path: Path, is_init: bool) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node, module, is_init)
            names = [base, *(f"{base}.{a.name}" for a in node.names)]
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _forbidden_text(node.value)
        ):
            found.append(f"{module}:{node.lineno}: string literal {node.value!r}")
            continue
        bad = [n for n in names if _forbidden_module(n)]
        if bad:
            found.append(f"{module}:{node.lineno}: imports {bad[0]}")
    return found


def find_ground_truth_references(package_name: str) -> list[str]:
    """Every ground-truth reference in `package_name` (see the module docstring)."""
    found: list[str] = []
    for module, path, is_init in _module_files(package_name):
        found.extend(_check(module, path, is_init))
    return found

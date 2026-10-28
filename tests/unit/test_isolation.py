"""Task 14: the reusable ground-truth isolation checker (also used by Task 23b)."""

import textwrap
from pathlib import Path

import pytest

from supplier_reliability.services.isolation import find_ground_truth_references


def _package(root: Path, name: str, modules: dict[str, str]) -> None:
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for rel, source in modules.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent != pkg and not (path.parent / "__init__.py").exists():
            (path.parent / "__init__.py").write_text("", encoding="utf-8")
        path.write_text(textwrap.dedent(source), encoding="utf-8")


def test_clean_synthetic_package_has_no_findings(tmp_path, monkeypatch):
    _package(
        tmp_path,
        "srp_clean_pkg",
        {
            "a.py": """
                import json
                from supplier_reliability.services.scoring import engine
                NAME = "ground truth is spelled with a space here"
            """
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert find_ground_truth_references("srp_clean_pkg") == []


def test_offending_synthetic_package_is_reported(tmp_path, monkeypatch):
    _package(
        tmp_path,
        "srp_bad_pkg",
        {
            "imports.py": """
                import supplier_reliability.services.anomalies.injector
                from supplier_reliability.services.evaluation import anomaly_eval
                from supplier_reliability.services import anomalies
                import my_ground_truth_reader
            """,
            "sub/rel.py": """
                from ..ground_truth_store import cases
            """,
            "sub/literal.py": """
                PATH = "data/ground_truth/anomaly_ground_truth.csv"
                MOD = "supplier_reliability.services.evaluation"
            """,
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    found = find_ground_truth_references("srp_bad_pkg")
    joined = "\n".join(found)
    assert len(found) == 7, joined
    assert sum(f.startswith("srp_bad_pkg.imports:") for f in found) == 4
    assert sum(f.startswith("srp_bad_pkg.sub.literal:") for f in found) == 2
    # the relative import is resolved against its package before it is checked
    assert any(
        f.startswith("srp_bad_pkg.sub.rel:") and "srp_bad_pkg.ground_truth_store" in f
        for f in found
    )
    assert "supplier_reliability.services.anomalies.injector" in joined
    assert "ground_truth" in joined


def test_unknown_package_raises():
    with pytest.raises(ModuleNotFoundError):
        find_ground_truth_references("srp_no_such_package_xyz")

"""Task 13: the generated canonical baseline passes every validation rule at MEDIUM or above.

The starter dataset is generated, loaded and scored (scorecard windows, as the pipeline does)
once per module; the full-scale variant is `slow` and also bounds run_validation's runtime.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from supplier_reliability.database import (
    make_engine,
    make_session_factory,
    run_migrations,
    session_scope,
)
from supplier_reliability.domain.enums import Severity
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import load_dataset
from supplier_reliability.services.scoring.evidence import scorecard_windows
from supplier_reliability.services.scoring.service import ScoringService
from supplier_reliability.services.validation import (
    REGISTRY,
    SEVERITY_RANK,
    ValidationRunResult,
    run_validation,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _build(app_config, preset: str, path: Path) -> Iterator[Session]:
    url = f"sqlite:///{path}"
    run_migrations(url)
    engine = make_engine(url)
    gen = GeneratorConfig.from_app_config(app_config, preset)
    with session_scope(make_session_factory(engine)) as s:
        load_dataset(s, generate_dataset(gen))
    windows = scorecard_windows(
        gen.reference_start, gen.reference_end, app_config.scoring.window_months
    )
    with make_session_factory(engine)() as s:
        ScoringService(s, app_config.scoring, lambda: NOW).score_history(windows)
        yield s
    engine.dispose()


def _serious(result: ValidationRunResult) -> list:
    return [f for f in result.findings if SEVERITY_RANK[f.severity] >= SEVERITY_RANK["MEDIUM"]]


@pytest.fixture(scope="module")
def starter_session(app_config, tmp_path_factory) -> Iterator[Session]:
    yield from _build(app_config, "starter", tmp_path_factory.mktemp("dq-baseline") / "s.db")


@pytest.fixture(scope="module")
def starter_result(starter_session) -> ValidationRunResult:
    return run_validation(starter_session, clock=lambda: NOW)


def test_starter_baseline_has_no_medium_or_worse_findings(starter_result):
    assert _serious(starter_result) == []


def test_every_rule_examined_the_starter_baseline(starter_result):
    assert set(starter_result.checked_counts_by_rule) == set(REGISTRY)
    unchecked = [r for r, n in starter_result.checked_counts_by_rule.items() if n <= 0]
    assert unchecked == []


def test_starter_baseline_findings_are_informational_and_unexpected(starter_result):
    assert {f.severity for f in starter_result.findings} <= {Severity.NONE, Severity.LOW}
    assert not any(f.is_expected_anomaly for f in starter_result.findings)  # no quarantine yet
    assert starter_result.run_id == "VAL-000001"
    assert sum(starter_result.counts_by_severity.values()) == len(starter_result.findings)


@pytest.mark.slow
def test_full_baseline_is_clean_and_validates_within_a_minute(app_config, tmp_path):
    for session in _build(app_config, "full", tmp_path / "full.db"):
        started = time.perf_counter()
        result = run_validation(session, clock=lambda: NOW)
        elapsed = time.perf_counter() - started
        assert _serious(result) == []
        assert all(n > 0 for n in result.checked_counts_by_rule.values())
        assert elapsed < 60, f"run_validation took {elapsed:.1f} s"

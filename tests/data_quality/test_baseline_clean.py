"""Task 13: the generated canonical baseline passes every validation rule at MEDIUM or above.

The starter dataset is generated, loaded and scored (scorecard windows, as the pipeline does)
once per module; the full-scale variant is `slow` and also bounds run_validation's runtime.
"""

import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.database import (
    make_engine,
    make_session_factory,
    run_migrations,
    session_scope,
)
from supplier_reliability.domain.enums import POStatus, Severity
from supplier_reliability.models import Contract, POLine, PurchaseOrder
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
# DC-009 flagged-contract shares on the full preset (seed 20261022); measured values and margin
# are in the Task 13 report (fix round 2)
CONSTRAINED_MIN_SHARE = 0.60
STABLE_MAX_SHARE = 0.15


def _build(app_config, preset: str, path: Path) -> Iterator[tuple[Session, dict[str, str]]]:
    """(scored session, supplier_id -> profile). Profiles come from the in-memory generated
    dataset, never from ground-truth files, and are used by tests only."""
    url = f"sqlite:///{path}"
    run_migrations(url)
    engine = make_engine(url)
    gen = GeneratorConfig.from_app_config(app_config, preset)
    ds = generate_dataset(gen)
    with session_scope(make_session_factory(engine)) as s:
        load_dataset(s, ds)
    windows = scorecard_windows(
        gen.reference_start, gen.reference_end, app_config.scoring.window_months
    )
    with make_session_factory(engine)() as s:
        ScoringService(s, app_config.scoring, lambda: NOW).score_history(windows)
        yield s, dict(ds.supplier_profiles)
    engine.dispose()


def _serious(result: ValidationRunResult) -> list:
    return [f for f in result.findings if SEVERITY_RANK[f.severity] >= SEVERITY_RANK["MEDIUM"]]


@pytest.fixture(scope="module")
def starter_session(app_config, tmp_path_factory) -> Iterator[Session]:
    for session, _ in _build(app_config, "starter", tmp_path_factory.mktemp("dq-b") / "s.db"):
        yield session


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


@dataclass(frozen=True)
class FullRun:
    session: Session
    profiles: dict[str, str]
    result: ValidationRunResult
    elapsed: float


@pytest.fixture(scope="module")
def full_run(app_config, tmp_path_factory) -> Iterator[FullRun]:
    """The full preset generated, loaded, scored and validated once (slow tests only)."""
    path = tmp_path_factory.mktemp("dq-full") / "full.db"
    for session, profiles in _build(app_config, "full", path):
        started = time.perf_counter()
        result = run_validation(session, clock=lambda: NOW)
        yield FullRun(session, profiles, result, time.perf_counter() - started)


@pytest.mark.slow
def test_full_baseline_is_clean_and_validates_within_a_minute(full_run):
    assert _serious(full_run.result) == []
    assert all(n > 0 for n in full_run.result.checked_counts_by_rule.values())
    assert full_run.elapsed < 60, f"run_validation took {full_run.elapsed:.1f} s"


def dc009_share_by_profile(session: Session, profiles: dict[str, str], findings) -> dict:
    """profile -> (flagged, evaluated) over contracts DC-009 evaluates (non-null contracted
    capacity and at least one non-cancelled PO line)."""
    stmt = (
        sa.select(Contract.contract_id, Contract.supplier_id)
        .where(
            Contract.contracted_capacity.is_not(None),
            sa.exists().where(
                PurchaseOrder.contract_id == Contract.contract_id,
                PurchaseOrder.po_status != POStatus.CANCELLED,
                POLine.po_id == PurchaseOrder.po_id,
            ),
        )
        .distinct()
    )
    flagged = {f.entity_id for f in findings if f.rule_id == "DC-009"}
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for contract_id, supplier_id in session.execute(stmt):
        cell = out[profiles[supplier_id]]
        cell[0] += contract_id in flagged
        cell[1] += 1
    return {k: tuple(v) for k, v in sorted(out.items())}


@pytest.mark.slow
def test_dc009_separates_capacity_constrained_from_stable_suppliers(full_run):
    """Rulings R33/R34: the contractual ceiling has 2x headroom over profile free capacity and
    DC-009 needs >= 3 months over, so comfortable suppliers are rarely flagged and
    capacity_constrained ones usually are (sparse_history is measured, not bounded)."""
    shares = dc009_share_by_profile(full_run.session, full_run.profiles, full_run.result.findings)
    assert sum(e for _, e in shares.values()) == full_run.result.checked_counts_by_rule["DC-009"]
    constrained, stable = shares["capacity_constrained"], shares["stable_high"]
    assert constrained[0] / constrained[1] >= CONSTRAINED_MIN_SHARE, shares
    assert stable[0] / stable[1] <= STABLE_MAX_SHARE, shares

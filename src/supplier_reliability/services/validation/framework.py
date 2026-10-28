"""Reusable validation framework (Task 13, spec section 11).

A rule is a function `check(session) -> RuleCheck` registered with `@rule(...)` into REGISTRY.
It returns how many entities it examined (`checked`, used by acceptance gates) and the
`Violation`s it found; the framework turns violations into `Finding`s by adding the rule id,
the rule's severity and one detection timestamp per run (from the injected clock).

Rules are set-based: a fixed number of bulk SQL statements per rule, plus Python over their
results where exact Decimal arithmetic or datetime/date semantics need it. No rule issues a
query per row.

Conventions:
- `entity_type` is the table name of the entity the finding is about (e.g. "shipments");
  `entity_id` its primary key (composite keys joined with "|").
- Rules read columns, never ORM entities, so a stale identity map cannot hide a change.
- Money is compared on Decimal in Python. SQLite stores Numeric as float and SQL-side arithmetic
  on it is float arithmetic; SQLAlchemy returns the columns as Decimal quantized to the column
  scale, which is exact for 2-dp values.

`run_validation` runs the selected rules (all by default) and, with `persist=True`, stores one
ValidationRun plus its ValidationFindings in ONE transaction that it commits itself (the
ScoringService pattern): it refuses to start while the session holds pending ORM changes, and
rolls back on error. That guard sees ORM state only (session.new/dirty/deleted): Core writes
(session.execute(insert/update/delete)) leave no trace there. Callers must not hold uncommitted
Core writes when calling run_validation with persist=True, because its commit would commit them
too (and its rollback on error would discard them). With `persist=False` it neither writes nor
commits (run_id is None).
`is_expected_anomaly` is True iff the finding's entity id equals the source_record_id of a
data_exceptions row, i.e. a record the anomaly detectors already quarantined; it never comes
from ground truth.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import Severity
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.repositories import validation as repo

CATEGORIES = ("integrity", "domain", "chronology", "arithmetic", "semantic")
SEVERITY_RANK: dict[str, int] = {s.value: i for i, s in enumerate(Severity)}
_MAX_VALUE = 500
_MAX_MESSAGE = 1000


@dataclass(frozen=True)
class Violation:
    """What a rule found, before the framework stamps rule id, severity and time."""

    entity_type: str
    entity_id: str | None
    field: str | None
    observed_value: Any
    expected_condition: str
    message: str


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: Severity
    entity_type: str
    entity_id: str | None
    field: str | None
    observed_value: str | None
    expected_condition: str | None
    message: str
    detection_timestamp: datetime
    is_expected_anomaly: bool = False


@dataclass(frozen=True)
class RuleCheck:
    checked: int
    violations: list[Violation] = field(default_factory=list)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    severity: Severity
    description: str
    check: Callable[[Session], RuleCheck]


REGISTRY: dict[str, Rule] = {}


def rule(
    rule_id: str, *, category: str, severity: Severity, description: str
) -> Callable[[Callable[[Session], RuleCheck]], Callable[[Session], RuleCheck]]:
    """Register `check` under `rule_id` (ids are unique)."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown rule category {category!r}")

    def register(check: Callable[[Session], RuleCheck]) -> Callable[[Session], RuleCheck]:
        if rule_id in REGISTRY:
            raise ValueError(f"rule {rule_id} registered twice")
        REGISTRY[rule_id] = Rule(rule_id, category, severity, description, check)
        return check

    return register


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def to_finding(rule_id: str, v: Violation, detected_at: datetime) -> Finding:
    """The Finding for violation `v` of `rule_id` (severity from the registry)."""
    return Finding(
        rule_id=rule_id,
        severity=REGISTRY[rule_id].severity,
        entity_type=v.entity_type,
        entity_id=v.entity_id,
        field=v.field,
        observed_value=_text(v.observed_value, _MAX_VALUE),
        expected_condition=_text(v.expected_condition, _MAX_VALUE),
        message=_text(v.message, _MAX_MESSAGE) or "",
        detection_timestamp=detected_at,
    )


@dataclass(frozen=True)
class ValidationRunResult:
    run_id: str | None  # None when not persisted
    counts_by_rule: dict[str, int]
    counts_by_severity: dict[str, int]
    findings: list[Finding]
    checked_counts_by_rule: dict[str, int]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sort_key(f: Finding) -> tuple:
    return (f.rule_id, f.entity_type, f.entity_id or "", f.field or "", f.observed_value or "")


def run_validation(
    session: Session,
    rule_ids: list[str] | None = None,
    persist: bool = True,
    clock: Callable[[], datetime] = _utcnow,
) -> ValidationRunResult:
    """Run the selected rules (all when None); see the module docstring for transactions."""
    selected = sorted(REGISTRY) if rule_ids is None else sorted(set(rule_ids))
    unknown = [r for r in selected if r not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown validation rule ids: {unknown}")
    if persist and (session.new or session.dirty or session.deleted):
        raise RuntimeError(
            "session has pending changes; commit or roll them back before a persisted "
            "validation run (it commits its own transaction)"
        )
    started = ensure_utc(clock())
    try:
        expected = repo.quarantined_record_ids(session)
        findings: list[Finding] = []
        checked: dict[str, int] = {}
        for rule_id in selected:
            outcome = REGISTRY[rule_id].check(session)
            checked[rule_id] = outcome.checked
            findings.extend(to_finding(rule_id, v, started) for v in outcome.violations)
        findings = [
            replace(f, is_expected_anomaly=f.entity_id is not None and f.entity_id in expected)
            for f in findings
        ]
        findings.sort(key=_sort_key)
        by_rule = {r: 0 for r in selected} | dict(Counter(f.rule_id for f in findings))
        by_severity = {s.value: 0 for s in Severity} | dict(
            Counter(f.severity.value for f in findings)
        )
        run_id = None
        if persist:
            finished = ensure_utc(clock())
            run_id = repo.next_run_id(session)
            repo.insert_run(
                session,
                run_id=run_id,
                started_at=started,
                finished_at=finished,
                scope="all" if rule_ids is None else _scope(selected),
                finding_counts={
                    "by_rule": by_rule,
                    "by_severity": by_severity,
                    "checked_by_rule": checked,
                    "expected_anomalies": sum(f.is_expected_anomaly for f in findings),
                },
                findings=[_row(f) for f in findings],
            )
            session.commit()
    except BaseException:
        if persist:
            session.rollback()
        raise
    return ValidationRunResult(run_id, by_rule, by_severity, findings, checked)


def _scope(rule_ids: list[str]) -> str:
    text = ",".join(rule_ids)
    return text if len(text) <= 100 else f"{len(rule_ids)} rules"


def _row(f: Finding) -> dict[str, Any]:
    return {
        "rule_id": f.rule_id,
        "severity": f.severity,
        "entity_type": f.entity_type,
        "entity_id": f.entity_id,
        "field": f.field,
        "observed_value": f.observed_value,
        "expected_condition": f.expected_condition,
        "message": f.message,
        "detection_timestamp": f.detection_timestamp,
        "is_expected_anomaly": f.is_expected_anomaly,
    }

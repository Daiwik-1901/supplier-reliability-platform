"""Validation run and findings schemas (Task 20)."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from supplier_reliability.domain.enums import Severity
from supplier_reliability.schemas.common import OrmModel, UtcDatetime

MAX_RULE_IDS = 200
RuleId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$", max_length=32)]


class ValidationRunRequest(BaseModel):
    rule_ids: list[RuleId] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RULE_IDS,
        description="Rules to run (duplicates ignored); omit the field, or send no body, "
        "for every registered rule.",
    )

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"rule_ids": ["RI-001", "DC-001"]}, {}]}
    )


class DefectCounts(BaseModel):
    """Findings of severity MEDIUM, HIGH or CRITICAL (the R32 grouping used by the reports)."""

    total: int
    by_rule: dict[str, int]
    by_severity: dict[str, int]


class InformationalCounts(BaseModel):
    """Findings of severity LOW or NONE: informational, not defects."""

    total: int
    by_rule: dict[str, int]


class ValidationRunOut(BaseModel):
    run_id: str
    counts_by_rule: dict[str, int]
    counts_by_severity: dict[str, int]
    checked_counts_by_rule: dict[str, int]
    total_findings: int
    expected_anomalies: int
    defects: DefectCounts
    informational: InformationalCounts

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "run_id": "VAL-000002",
                    "counts_by_rule": {"RI-001": 0, "DC-009": 3},
                    "counts_by_severity": {
                        "NONE": 0,
                        "LOW": 3,
                        "MEDIUM": 0,
                        "HIGH": 0,
                        "CRITICAL": 0,
                    },
                    "checked_counts_by_rule": {"RI-001": 450, "DC-009": 50},
                    "total_findings": 3,
                    "expected_anomalies": 0,
                    "defects": {"total": 0, "by_rule": {}, "by_severity": {}},
                    "informational": {"total": 3, "by_rule": {"DC-009": 3}},
                }
            ]
        }
    )


class FindingOut(OrmModel):
    finding_id: int
    run_id: str
    rule_id: str
    severity: Severity
    entity_type: str
    entity_id: str | None
    field: str | None
    observed_value: str | None
    expected_condition: str | None
    message: str
    detection_timestamp: UtcDatetime
    is_expected_anomaly: bool

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "finding_id": 1,
                    "run_id": "VAL-000002",
                    "rule_id": "DC-009",
                    "severity": "LOW",
                    "entity_type": "contracts",
                    "entity_id": "CON-000001",
                    "field": "contracted_capacity",
                    "observed_value": "3 of 18 months",
                    "expected_condition": "demand within contract ceiling",
                    "message": "Demand exceeded the contracted ceiling in 3 months",
                    "detection_timestamp": "2026-10-29T12:00:00Z",
                    "is_expected_anomaly": False,
                }
            ]
        },
    )

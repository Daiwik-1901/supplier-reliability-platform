"""Score request and response schemas (Task 20)."""

from typing import Annotated, Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from supplier_reliability.domain.enums import CalculationStatus, Decision, ReliabilityCategory
from supplier_reliability.schemas.common import IdStr, OrmModel, UtcDatetime
from supplier_reliability.schemas.suppliers import Score

MAX_SUPPLIER_IDS = 1000

SupplierIds = Annotated[
    list[IdStr] | None,
    Field(
        min_length=1,
        max_length=MAX_SUPPLIER_IDS,
        description="Suppliers to score (duplicates are ignored); omit for every supplier.",
    ),
]
Reason = Annotated[str, Field(min_length=1, max_length=500, description="Why, kept as audit text.")]


class _Window(BaseModel):
    """Evaluation window [start, end] and data cutoff. All datetimes must carry a UTC offset."""

    evaluation_start: AwareDatetime
    evaluation_end: AwareDatetime
    data_cutoff: AwareDatetime | None = Field(
        default=None, description="Only data known by this instant counts. Defaults to the end."
    )
    supplier_ids: SupplierIds = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.evaluation_end < self.evaluation_start:
            raise ValueError("evaluation_end must not be before evaluation_start")
        if self.data_cutoff is not None and self.data_cutoff < self.evaluation_start:
            raise ValueError("data_cutoff must not be before evaluation_start")
        return self


_EXAMPLE = {
    "evaluation_start": "2025-04-01T00:00:00Z",
    "evaluation_end": "2025-06-30T23:59:59Z",
    "data_cutoff": "2025-06-30T23:59:59Z",
    "supplier_ids": ["SUP-000001", "SUP-000002"],
}


class CalculateRequest(_Window):
    scoring_version: str | None = Field(
        default=None,
        max_length=32,
        description="Must equal the configured scoring version when given, else 409.",
    )

    model_config = ConfigDict(
        json_schema_extra={"examples": [_EXAMPLE, {**_EXAMPLE, "scoring_version": "1.0.0"}]}
    )


class RecalculateRequest(CalculateRequest):
    reason: Reason

    model_config = ConfigDict(
        json_schema_extra={"examples": [{**_EXAMPLE, "reason": "late ARRIVED events loaded"}]}
    )


class RestateRequest(_Window):
    """Same window, later cutoff: a new evaluation linked to the earlier score it restates."""

    data_cutoff: AwareDatetime = Field(
        description="Must be later than the cutoff of an existing score for the window."
    )
    reason: Reason

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    **_EXAMPLE,
                    "data_cutoff": "2025-08-15T00:00:00Z",
                    "reason": "supplier corrections received after the original cutoff",
                }
            ]
        }
    )


class CalculationOutcomeOut(BaseModel):
    """Score ids by what happened. `superseded` maps each replaced score id to its new version."""

    created: list[str]
    existing: list[str]
    unchanged: list[str]
    superseded: dict[str, str]

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "created": ["SCORE-2026-000301"],
                    "existing": [],
                    "unchanged": [],
                    "superseded": {},
                }
            ]
        }
    )


class ScoreOut(OrmModel):
    """One stored score version (without its explanation)."""

    score_id: str
    supplier_id: str
    site_id: str | None
    evaluation_start: UtcDatetime
    evaluation_end: UtcDatetime
    data_cutoff_timestamp: UtcDatetime
    calculated_at: UtcDatetime
    delivery_score: Score
    quality_score: Score
    cost_score: Score
    capacity_score: Score
    compliance_score: Score
    disruption_score: Score
    overall_score: Score
    evidence_coverage: float
    is_provisional: bool
    reliability_category: ReliabilityCategory
    decision: Decision
    calculation_status: CalculationStatus
    effective_weights: dict[str, Any]
    scoring_version: str
    config_hash: str
    version_number: int
    is_current: bool
    superseded_by_score_id: str | None
    restates_score_id: str | None
    change_reason: str | None

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "score_id": "SCORE-2026-000300",
                    "supplier_id": "SUP-000001",
                    "site_id": None,
                    "evaluation_start": "2025-07-01T00:00:00Z",
                    "evaluation_end": "2026-06-30T23:59:59Z",
                    "data_cutoff_timestamp": "2026-06-30T23:59:59Z",
                    "calculated_at": "2026-07-01T23:59:59Z",
                    "delivery_score": 91.5,
                    "quality_score": 88.0,
                    "cost_score": 80.25,
                    "capacity_score": 90.0,
                    "compliance_score": 100.0,
                    "disruption_score": 95.0,
                    "overall_score": 89.4,
                    "evidence_coverage": 1.0,
                    "is_provisional": False,
                    "reliability_category": "HIGH",
                    "decision": "PREFERRED",
                    "calculation_status": "CALCULATED",
                    "effective_weights": {"delivery": 0.3, "quality": 0.25},
                    "scoring_version": "1.0.0",
                    "config_hash": "9f2c...e1",
                    "version_number": 1,
                    "is_current": True,
                    "superseded_by_score_id": None,
                    "restates_score_id": None,
                    "change_reason": None,
                }
            ]
        },
    )


class LineageCounts(BaseModel):
    included: int
    excluded: int


class ScoreDetail(ScoreOut):
    """A score with its stored explanation and per-component lineage counts (not the rows)."""

    explanation: dict[str, Any]
    lineage_summary: dict[str, LineageCounts]

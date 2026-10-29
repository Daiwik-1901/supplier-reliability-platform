"""Supplier response schemas (Pydantic, separate from the ORM models)."""

from datetime import date
from typing import Any

from pydantic import ConfigDict, Field

from supplier_reliability.domain.enums import (
    CalculationStatus,
    ContractStatus,
    Decision,
    Region,
    ReliabilityCategory,
    SiteStatus,
    SiteType,
    SupplierCategory,
    SupplierStatus,
)
from supplier_reliability.schemas.common import Money, OrmModel, UtcDatetime

# Scores are 0-100 with two decimals; they are not money, so they travel as JSON numbers.
Score = float | None


class ScoreBrief(OrmModel):
    """The supplier's latest current score, embedded in supplier payloads."""

    score_id: str
    overall_score: Score
    reliability_category: ReliabilityCategory
    decision: Decision
    is_provisional: bool
    evidence_coverage: float
    evaluation_start: UtcDatetime
    evaluation_end: UtcDatetime
    data_cutoff: UtcDatetime = Field(validation_alias="data_cutoff_timestamp")


class SupplierSummary(OrmModel):
    supplier_id: str
    supplier_name: str
    supplier_category: SupplierCategory
    country: str
    region: Region
    supplier_status: SupplierStatus
    onboarding_date: date
    created_at: UtcDatetime
    updated_at: UtcDatetime
    latest_score: ScoreBrief | None = None

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "supplier_id": "SUP-000001",
                    "supplier_name": "Northwind Components",
                    "supplier_category": "ELECTRONICS",
                    "country": "Germany",
                    "region": "EMEA",
                    "supplier_status": "ACTIVE",
                    "onboarding_date": "2024-03-01",
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-01T00:00:00Z",
                    "latest_score": {
                        "score_id": "SCORE-2026-000300",
                        "overall_score": 87.5,
                        "reliability_category": "HIGH",
                        "decision": "PREFERRED",
                        "is_provisional": False,
                        "evidence_coverage": 1.0,
                        "evaluation_start": "2025-07-01T00:00:00Z",
                        "evaluation_end": "2026-06-30T23:59:59Z",
                        "data_cutoff": "2026-06-30T23:59:59Z",
                    },
                }
            ]
        },
    )


class SiteOut(OrmModel):
    site_id: str
    supplier_id: str
    site_name: str
    site_type: SiteType
    country: str
    region: Region
    site_status: SiteStatus
    effective_from: date
    effective_to: date | None


class ContractOut(OrmModel):
    contract_id: str
    supplier_id: str
    site_id: str | None
    contract_status: ContractStatus
    effective_from: date
    effective_to: date | None
    currency: str
    payment_terms: str
    delivery_terms: str
    contracted_capacity: Money | None
    capacity_unit: str


class SupplierDetail(SupplierSummary):
    sites: list[SiteOut]
    contracts: list[ContractOut]


class WindowPerformance(OrmModel):
    """One scoring window's component scores (a current score row)."""

    score_id: str
    evaluation_start: UtcDatetime
    evaluation_end: UtcDatetime
    data_cutoff_timestamp: UtcDatetime
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
    missing_reasons: dict[str, str] = {}


class PerformanceOut(OrmModel):
    supplier_id: str
    windows: list[WindowPerformance]


class ScoreVersionOut(OrmModel):
    """One stored score version; superseded versions stay visible."""

    score_id: str
    evaluation_start: UtcDatetime
    evaluation_end: UtcDatetime
    data_cutoff_timestamp: UtcDatetime
    calculated_at: UtcDatetime
    overall_score: Score
    reliability_category: ReliabilityCategory
    decision: Decision
    calculation_status: CalculationStatus
    scoring_version: str
    config_hash: str
    version_number: int
    is_current: bool
    superseded_by_score_id: str | None
    restates_score_id: str | None
    change_reason: str | None


class ExplanationOut(OrmModel):
    supplier_id: str
    score_id: str
    evaluation_start: UtcDatetime
    evaluation_end: UtcDatetime
    data_cutoff: UtcDatetime
    overall_score: Score
    reliability_category: ReliabilityCategory
    decision: Decision
    components: dict[str, Any]
    explanation: dict[str, Any]

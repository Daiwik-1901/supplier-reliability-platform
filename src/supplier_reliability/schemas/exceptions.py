"""Data exception schemas (Task 20)."""

from typing import Any

from pydantic import ConfigDict

from supplier_reliability.domain.enums import DetectionStatus, Disposition, IssueType, Severity
from supplier_reliability.schemas.common import OrmModel, UtcDatetime


class ExceptionSummary(OrmModel):
    """List row: evidence and related ids are left to the detail endpoint."""

    exception_id: str
    batch_id: str
    raw_record_id: str
    source_entity_type: str
    source_record_id: str | None
    issue_type: IssueType
    rule_id: str
    severity: Severity
    detection_status: DetectionStatus
    detected_at: UtcDatetime
    disposition: Disposition | None
    supplier_id: str | None
    corrected_score_id: str | None

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "exception_id": "EXC-000001",
                    "batch_id": "BATCH-starter-20261022",
                    "raw_record_id": "RAW-000001",
                    "source_entity_type": "shipments",
                    "source_record_id": "SHP-2026-000001",
                    "issue_type": "SHIPMENT_MISSING_PO",
                    "rule_id": "AN-001",
                    "severity": "MEDIUM",
                    "detection_status": "DETECTED",
                    "detected_at": "2026-07-02T00:00:00Z",
                    "disposition": "QUARANTINED",
                    "supplier_id": "SUP-000001",
                    "corrected_score_id": None,
                }
            ]
        },
    )


class ExceptionOut(ExceptionSummary):
    """Full exception including the evidence the detector stored."""

    related_entity_ids: list[Any] | dict[str, Any]
    evidence: dict[str, Any]

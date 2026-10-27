"""Operational tables: raw-ingestion layer, data exceptions, validation runs, dataset jobs (D9)."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supplier_reliability.domain.enums import (
    DetectionStatus,
    Disposition,
    IssueType,
    JobStatus,
    Severity,
)
from supplier_reliability.models.base import Base, UTCDateTime, enum_type

if TYPE_CHECKING:
    from supplier_reliability.models.master import Supplier
    from supplier_reliability.models.scoring import SupplierScore


class RawIngestionRecord(Base):
    """Source records as received, possibly malformed; never read by scoring."""

    __tablename__ = "raw_ingestion_records"

    raw_record_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    batch_id: Mapped[str] = mapped_column(sa.String(64), index=True)
    source_entity_type: Mapped[str] = mapped_column(sa.String(40), index=True)
    source_record_id: Mapped[str | None] = mapped_column(sa.String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)

    exceptions: Mapped[list["DataException"]] = relationship(
        back_populates="raw_record", lazy="raise"
    )


class DataException(Base):
    __tablename__ = "data_exceptions"
    __table_args__ = (
        # Re-running detection on a batch must not duplicate findings.
        sa.UniqueConstraint("batch_id", "raw_record_id", "issue_type"),
    )

    exception_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    batch_id: Mapped[str] = mapped_column(sa.String(64))
    raw_record_id: Mapped[str] = mapped_column(
        sa.ForeignKey("raw_ingestion_records.raw_record_id"), index=True
    )
    source_entity_type: Mapped[str] = mapped_column(sa.String(40))
    source_record_id: Mapped[str | None] = mapped_column(sa.String(64))
    issue_type: Mapped[IssueType] = mapped_column(enum_type(IssueType), index=True)
    rule_id: Mapped[str] = mapped_column(sa.String(32))
    severity: Mapped[Severity] = mapped_column(enum_type(Severity), index=True)
    detection_status: Mapped[DetectionStatus] = mapped_column(enum_type(DetectionStatus))
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime)
    disposition: Mapped[Disposition | None] = mapped_column(enum_type(Disposition))
    related_entity_ids: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    evidence: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    supplier_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("suppliers.supplier_id"), index=True
    )
    corrected_score_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("supplier_scores.score_id"), index=True
    )

    raw_record: Mapped["RawIngestionRecord"] = relationship(
        back_populates="exceptions", lazy="raise"
    )
    supplier: Mapped["Supplier | None"] = relationship(lazy="raise")
    corrected_score: Mapped["SupplierScore | None"] = relationship(lazy="raise")


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    run_id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scope: Mapped[str] = mapped_column(sa.String(100))
    finding_counts: Mapped[dict[str, Any]] = mapped_column(sa.JSON)

    findings: Mapped[list["ValidationFinding"]] = relationship(back_populates="run", lazy="raise")


class ValidationFinding(Base):
    __tablename__ = "validation_findings"

    finding_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.ForeignKey("validation_runs.run_id"), index=True)
    rule_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    severity: Mapped[Severity] = mapped_column(enum_type(Severity))
    entity_type: Mapped[str] = mapped_column(sa.String(40))
    entity_id: Mapped[str | None] = mapped_column(sa.String(64))
    field: Mapped[str | None] = mapped_column(sa.String(64))
    observed_value: Mapped[str | None] = mapped_column(sa.String(500))
    expected_condition: Mapped[str | None] = mapped_column(sa.String(500))
    message: Mapped[str] = mapped_column(sa.String(1000))
    detection_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    is_expected_anomaly: Mapped[bool] = mapped_column(sa.Boolean)

    run: Mapped["ValidationRun"] = relationship(back_populates="findings", lazy="raise")


class DatasetJob(Base):
    __tablename__ = "dataset_jobs"

    job_id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    status: Mapped[JobStatus] = mapped_column(enum_type(JobStatus))
    preset: Mapped[str] = mapped_column(sa.String(32))
    seed: Mapped[int] = mapped_column(sa.BigInteger)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    dataset_id: Mapped[str | None] = mapped_column(sa.String(64))


class DatasetManifest(Base):
    __tablename__ = "dataset_manifests"

    dataset_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

"""Scores, score lineage (D8) and the config snapshot each score was computed with."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supplier_reliability.domain.enums import (
    CalculationStatus,
    ComponentName,
    Decision,
    ReliabilityCategory,
)
from supplier_reliability.models.base import Base, UTCDateTime, enum_type

if TYPE_CHECKING:
    from supplier_reliability.models.master import Site, Supplier

SCORE = sa.Numeric(5, 2)
_COMPONENT_COLUMNS = (
    "delivery_score",
    "quality_score",
    "cost_score",
    "capacity_score",
    "compliance_score",
    "disruption_score",
    "overall_score",
)


def _score_range_check() -> sa.CheckConstraint:
    clauses = [f"({c} IS NULL OR ({c} >= 0 AND {c} <= 100))" for c in _COMPONENT_COLUMNS]
    return sa.CheckConstraint(" AND ".join(clauses), name="score_range")


class SupplierScore(Base):
    """Immutable except for is_current / superseded_by_score_id (D8)."""

    __tablename__ = "supplier_scores"
    __table_args__ = (
        sa.UniqueConstraint("evaluation_key", "version_number"),
        sa.Index(
            "uq_supplier_scores_one_current_per_key",
            "evaluation_key",
            unique=True,
            sqlite_where=sa.text("is_current"),
            postgresql_where=sa.text("is_current"),
        ),
        sa.Index("ix_supplier_scores_supplier_id_evaluation_end", "supplier_id", "evaluation_end"),
        _score_range_check(),
        sa.CheckConstraint(
            "evidence_coverage >= 0 AND evidence_coverage <= 1", name="coverage_range"
        ),
        sa.CheckConstraint("version_number >= 1", name="version_positive"),
        sa.CheckConstraint("evaluation_end >= evaluation_start", name="window_order"),
        sa.CheckConstraint(
            "NOT (is_current AND superseded_by_score_id IS NOT NULL)",
            name="superseded_not_current",
        ),
    )

    score_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(sa.ForeignKey("suppliers.supplier_id"))
    site_id: Mapped[str | None] = mapped_column(sa.ForeignKey("sites.site_id"), index=True)
    evaluation_start: Mapped[datetime] = mapped_column(UTCDateTime)
    evaluation_end: Mapped[datetime] = mapped_column(UTCDateTime)
    data_cutoff_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    calculated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    delivery_score: Mapped[Decimal | None] = mapped_column(SCORE)
    quality_score: Mapped[Decimal | None] = mapped_column(SCORE)
    cost_score: Mapped[Decimal | None] = mapped_column(SCORE)
    capacity_score: Mapped[Decimal | None] = mapped_column(SCORE)
    compliance_score: Mapped[Decimal | None] = mapped_column(SCORE)
    disruption_score: Mapped[Decimal | None] = mapped_column(SCORE)
    overall_score: Mapped[Decimal | None] = mapped_column(SCORE)
    evidence_coverage: Mapped[Decimal] = mapped_column(sa.Numeric(5, 4))
    is_provisional: Mapped[bool] = mapped_column(sa.Boolean)
    reliability_category: Mapped[ReliabilityCategory] = mapped_column(
        enum_type(ReliabilityCategory), index=True
    )
    decision: Mapped[Decision] = mapped_column(enum_type(Decision))
    calculation_status: Mapped[CalculationStatus] = mapped_column(
        enum_type(CalculationStatus), index=True
    )
    effective_weights: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    explanation: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    scoring_version: Mapped[str] = mapped_column(sa.String(32))
    config_hash: Mapped[str] = mapped_column(sa.String(64))
    code_version: Mapped[str] = mapped_column(sa.String(32))
    evaluation_key: Mapped[str] = mapped_column(sa.String(64))
    version_number: Mapped[int] = mapped_column(sa.Integer)
    is_current: Mapped[bool] = mapped_column(sa.Boolean)
    superseded_by_score_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("supplier_scores.score_id")
    )
    change_reason: Mapped[str | None] = mapped_column(sa.String(500))
    evidence_fingerprint: Mapped[str] = mapped_column(sa.String(64))
    restates_score_id: Mapped[str | None] = mapped_column(sa.ForeignKey("supplier_scores.score_id"))

    supplier: Mapped["Supplier"] = relationship(back_populates="scores", lazy="raise")
    site: Mapped["Site | None"] = relationship(lazy="raise")
    evidence: Mapped[list["ScoreEvidence"]] = relationship(back_populates="score", lazy="raise")
    superseded_by: Mapped["SupplierScore | None"] = relationship(
        foreign_keys=[superseded_by_score_id], remote_side=[score_id], lazy="raise"
    )
    restates: Mapped["SupplierScore | None"] = relationship(
        foreign_keys=[restates_score_id], remote_side=[score_id], lazy="raise"
    )


class ScoreEvidence(Base):
    """D8 lineage: one row per record considered for a score component, included or not."""

    __tablename__ = "score_evidence"
    __table_args__ = (
        sa.Index(
            "ix_score_evidence_source_entity_type_source_record_id",
            "source_entity_type",
            "source_record_id",
        ),
    )

    score_id: Mapped[str] = mapped_column(
        sa.ForeignKey("supplier_scores.score_id"), primary_key=True
    )
    component: Mapped[ComponentName] = mapped_column(enum_type(ComponentName), primary_key=True)
    source_entity_type: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    source_record_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    included: Mapped[bool] = mapped_column(sa.Boolean)
    exclusion_reason: Mapped[str | None] = mapped_column(sa.String(200))
    contribution: Mapped[dict[str, Any]] = mapped_column(sa.JSON)

    score: Mapped["SupplierScore"] = relationship(back_populates="evidence", lazy="raise")


class ScoringConfigSnapshot(Base):
    """Every config a score used, stored once, so lineage never depends on the YAML file."""

    __tablename__ = "scoring_config_snapshots"

    config_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    scoring_version: Mapped[str] = mapped_column(sa.String(32))
    config: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

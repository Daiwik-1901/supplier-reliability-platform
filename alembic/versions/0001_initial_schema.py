"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-27 18:27:57.754142
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dataset_jobs",
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                name="jobstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("preset", sa.String(length=32), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("dataset_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_dataset_jobs")),
    )
    op.create_table(
        "dataset_manifests",
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("dataset_id", name=op.f("pk_dataset_manifests")),
    )
    op.create_table(
        "raw_ingestion_records",
        sa.Column("raw_record_id", sa.String(length=32), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("source_entity_type", sa.String(length=40), nullable=False),
        sa.Column("source_record_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("raw_record_id", name=op.f("pk_raw_ingestion_records")),
    )
    with op.batch_alter_table("raw_ingestion_records", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_raw_ingestion_records_batch_id"), ["batch_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_raw_ingestion_records_source_entity_type"),
            ["source_entity_type"],
            unique=False,
        )

    op.create_table(
        "scoring_config_snapshots",
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("scoring_version", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("config_hash", name=op.f("pk_scoring_config_snapshots")),
    )
    op.create_table(
        "suppliers",
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("supplier_name", sa.String(length=200), nullable=False),
        sa.Column(
            "supplier_category",
            sa.Enum(
                "RAW_MATERIALS",
                "ELECTRONICS",
                "PACKAGING",
                "CHEMICALS",
                "MACHINED_PARTS",
                "LOGISTICS_SERVICES",
                name="suppliercategory",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("country", sa.String(length=64), nullable=False),
        sa.Column(
            "region",
            sa.Enum(
                "NA",
                "LATAM",
                "EMEA",
                "APAC",
                name="region",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "supplier_status",
            sa.Enum(
                "ACTIVE",
                "INACTIVE",
                "SUSPENDED",
                name="supplierstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("onboarding_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("supplier_id", name=op.f("pk_suppliers")),
    )
    op.create_table(
        "validation_runs",
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.String(length=100), nullable=False),
        sa.Column("finding_counts", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_validation_runs")),
    )
    op.create_table(
        "sites",
        sa.Column("site_id", sa.String(length=40), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("site_name", sa.String(length=200), nullable=False),
        sa.Column(
            "site_type",
            sa.Enum(
                "FACTORY",
                "WAREHOUSE",
                "DISTRIBUTION_CENTER",
                name="sitetype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("country", sa.String(length=64), nullable=False),
        sa.Column(
            "region",
            sa.Enum(
                "NA",
                "LATAM",
                "EMEA",
                "APAC",
                name="region",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "site_status",
            sa.Enum(
                "ACTIVE", "INACTIVE", name="sitestatus", native_enum=False, create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_sites_effective_dates"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["suppliers.supplier_id"], name=op.f("fk_sites_supplier_id_suppliers")
        ),
        sa.PrimaryKeyConstraint("site_id", name=op.f("pk_sites")),
    )
    with op.batch_alter_table("sites", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sites_supplier_id"), ["supplier_id"], unique=False)

    op.create_table(
        "validation_findings",
        sa.Column("finding_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("rule_id", sa.String(length=32), nullable=False),
        sa.Column(
            "severity",
            sa.Enum(
                "NONE",
                "LOW",
                "MEDIUM",
                "HIGH",
                "CRITICAL",
                name="severity",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("field", sa.String(length=64), nullable=True),
        sa.Column("observed_value", sa.String(length=500), nullable=True),
        sa.Column("expected_condition", sa.String(length=500), nullable=True),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column("detection_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_expected_anomaly", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["validation_runs.run_id"],
            name=op.f("fk_validation_findings_run_id_validation_runs"),
        ),
        sa.PrimaryKeyConstraint("finding_id", name=op.f("pk_validation_findings")),
    )
    with op.batch_alter_table("validation_findings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_validation_findings_rule_id"), ["rule_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_validation_findings_run_id"), ["run_id"], unique=False)

    op.create_table(
        "capacity_declarations",
        sa.Column("capacity_declaration_id", sa.String(length=40), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("site_id", sa.String(length=40), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("unit", sa.String(length=32), server_default="units/month", nullable=False),
        sa.Column("available_capacity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("committed_capacity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "verification_status",
            sa.Enum(
                "VERIFIED",
                "UNVERIFIED",
                name="capacityverification",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "available_capacity >= 0", name=op.f("ck_capacity_declarations_available_nonneg")
        ),
        sa.CheckConstraint(
            "committed_capacity >= 0", name=op.f("ck_capacity_declarations_committed_nonneg")
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_capacity_declarations_period_dates")
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.site_id"], name=op.f("fk_capacity_declarations_site_id_sites")
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.supplier_id"],
            name=op.f("fk_capacity_declarations_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("capacity_declaration_id", name=op.f("pk_capacity_declarations")),
    )
    with op.batch_alter_table("capacity_declarations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_capacity_declarations_site_id"), ["site_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_capacity_declarations_supplier_id"), ["supplier_id"], unique=False
        )

    op.create_table(
        "contracts",
        sa.Column("contract_id", sa.String(length=32), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("site_id", sa.String(length=40), nullable=True),
        sa.Column(
            "contract_status",
            sa.Enum(
                "ACTIVE",
                "EXPIRED",
                "TERMINATED",
                "DRAFT",
                name="contractstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_terms", sa.String(length=64), nullable=False),
        sa.Column("delivery_terms", sa.String(length=64), nullable=False),
        sa.Column("contracted_capacity", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column(
            "capacity_unit", sa.String(length=32), server_default="units/month", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contracted_capacity IS NULL OR contracted_capacity >= 0",
            name=op.f("ck_contracts_contracted_capacity_nonneg"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_contracts_effective_dates"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.site_id"], name=op.f("fk_contracts_site_id_sites")
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.supplier_id"],
            name=op.f("fk_contracts_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("contract_id", name=op.f("pk_contracts")),
    )
    with op.batch_alter_table("contracts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_contracts_site_id"), ["site_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_contracts_supplier_id"), ["supplier_id"], unique=False)

    op.create_table(
        "supplier_certifications",
        sa.Column("certification_id", sa.String(length=40), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("site_id", sa.String(length=40), nullable=True),
        sa.Column(
            "certification_type",
            sa.Enum(
                "ISO9001",
                "ISO14001",
                "IATF16949",
                "REACH",
                "CONFLICT_MINERALS",
                name="certificationtype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("certificate_number", sa.String(length=64), nullable=False),
        sa.Column("issuing_authority", sa.String(length=200), nullable=False),
        sa.Column(
            "verification_status",
            sa.Enum(
                "VERIFIED",
                "PENDING_VERIFICATION",
                "REJECTED",
                name="verificationstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "compliance_status",
            sa.Enum(
                "COMPLIANT",
                "NONCOMPLIANT",
                name="certificationstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_reference", sa.String(length=500), nullable=False),
        sa.Column("supersedes_certification_id", sa.String(length=40), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "valid_to >= valid_from", name=op.f("ck_supplier_certifications_valid_dates")
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.site_id"], name=op.f("fk_supplier_certifications_site_id_sites")
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_certification_id"],
            ["supplier_certifications.certification_id"],
            name="fk_supplier_certs_supersedes",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.supplier_id"],
            name=op.f("fk_supplier_certifications_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("certification_id", name=op.f("pk_supplier_certifications")),
        sa.UniqueConstraint(
            "supplier_id",
            "certificate_number",
            "recorded_at",
            name="uq_supplier_certs_supplier_number_recorded",
        ),
    )
    with op.batch_alter_table("supplier_certifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_supplier_certifications_site_id"), ["site_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_supplier_certifications_supersedes_certification_id"),
            ["supersedes_certification_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_supplier_certifications_supplier_id"), ["supplier_id"], unique=False
        )

    op.create_table(
        "supplier_scores",
        sa.Column("score_id", sa.String(length=32), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("site_id", sa.String(length=40), nullable=True),
        sa.Column("evaluation_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivery_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("quality_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("cost_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("capacity_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("compliance_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("disruption_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("overall_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("evidence_coverage", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), nullable=False),
        sa.Column(
            "reliability_category",
            sa.Enum(
                "HIGH",
                "ACCEPTABLE",
                "AT_RISK",
                "CRITICAL",
                "INSUFFICIENT_EVIDENCE",
                name="reliabilitycategory",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "SUSPEND_PENDING_REVIEW",
                "HOLD_FOR_DATA_REVIEW",
                "COLLECT_MORE_DATA",
                "PREFERRED",
                "APPROVED",
                "MONITOR",
                "ESCALATE",
                name="decision",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "calculation_status",
            sa.Enum(
                "CALCULATED",
                "PROVISIONAL",
                "INSUFFICIENT_EVIDENCE",
                "CORRECTED",
                name="calculationstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("effective_weights", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.JSON(), nullable=False),
        sa.Column("scoring_version", sa.String(length=32), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("code_version", sa.String(length=32), nullable=False),
        sa.Column("evaluation_key", sa.String(length=64), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("superseded_by_score_id", sa.String(length=32), nullable=True),
        sa.Column("change_reason", sa.String(length=500), nullable=True),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("restates_score_id", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "(delivery_score IS NULL OR (delivery_score >= 0 AND delivery_score <= 100)) AND (quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 100)) AND (cost_score IS NULL OR (cost_score >= 0 AND cost_score <= 100)) AND (capacity_score IS NULL OR (capacity_score >= 0 AND capacity_score <= 100)) AND (compliance_score IS NULL OR (compliance_score >= 0 AND compliance_score <= 100)) AND (disruption_score IS NULL OR (disruption_score >= 0 AND disruption_score <= 100)) AND (overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100))",
            name=op.f("ck_supplier_scores_score_range"),
        ),
        sa.CheckConstraint(
            "NOT (is_current AND superseded_by_score_id IS NOT NULL)",
            name=op.f("ck_supplier_scores_superseded_not_current"),
        ),
        sa.CheckConstraint(
            "evaluation_end >= evaluation_start", name=op.f("ck_supplier_scores_window_order")
        ),
        sa.CheckConstraint(
            "evidence_coverage >= 0 AND evidence_coverage <= 1",
            name=op.f("ck_supplier_scores_coverage_range"),
        ),
        sa.CheckConstraint("version_number >= 1", name=op.f("ck_supplier_scores_version_positive")),
        sa.ForeignKeyConstraint(
            ["config_hash"],
            ["scoring_config_snapshots.config_hash"],
            name=op.f("fk_supplier_scores_config_hash_scoring_config_snapshots"),
        ),
        sa.ForeignKeyConstraint(
            ["restates_score_id"],
            ["supplier_scores.score_id"],
            name=op.f("fk_supplier_scores_restates_score_id_supplier_scores"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.site_id"], name=op.f("fk_supplier_scores_site_id_sites")
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_score_id"],
            ["supplier_scores.score_id"],
            name=op.f("fk_supplier_scores_superseded_by_score_id_supplier_scores"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.supplier_id"],
            name=op.f("fk_supplier_scores_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("score_id", name=op.f("pk_supplier_scores")),
        sa.UniqueConstraint(
            "evaluation_key",
            "version_number",
            name=op.f("uq_supplier_scores_evaluation_key_version_number"),
        ),
    )
    with op.batch_alter_table("supplier_scores", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_supplier_scores_calculation_status"),
            ["calculation_status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_supplier_scores_config_hash"), ["config_hash"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_supplier_scores_reliability_category"),
            ["reliability_category"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_supplier_scores_restates_score_id"), ["restates_score_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_supplier_scores_site_id"), ["site_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_supplier_scores_superseded_by_score_id"),
            ["superseded_by_score_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_supplier_scores_supplier_id_evaluation_end",
            ["supplier_id", "evaluation_end"],
            unique=False,
        )
        batch_op.create_index(
            "uq_supplier_scores_one_current_per_key",
            ["evaluation_key"],
            unique=True,
            sqlite_where=sa.text("is_current"),
            postgresql_where=sa.text("is_current"),
        )

    op.create_table(
        "compliance_requirements",
        sa.Column("requirement_id", sa.String(length=40), nullable=False),
        sa.Column("contract_id", sa.String(length=32), nullable=False),
        sa.Column(
            "certification_type",
            sa.Enum(
                "ISO9001",
                "ISO14001",
                "IATF16949",
                "REACH",
                "CONFLICT_MINERALS",
                name="certificationtype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("mandatory", sa.Boolean(), nullable=False),
        sa.Column(
            "scope",
            sa.Enum(
                "SUPPLIER",
                "SITE",
                name="requirementscope",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("site_id", sa.String(length=40), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope = 'SITE' AND site_id IS NOT NULL) OR (scope = 'SUPPLIER' AND site_id IS NULL)",
            name=op.f("ck_compliance_requirements_scope_site"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_compliance_requirements_effective_dates"),
        ),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["contracts.contract_id"],
            name=op.f("fk_compliance_requirements_contract_id_contracts"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.site_id"], name=op.f("fk_compliance_requirements_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("requirement_id", name=op.f("pk_compliance_requirements")),
    )
    with op.batch_alter_table("compliance_requirements", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_compliance_requirements_contract_id"), ["contract_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_compliance_requirements_site_id"), ["site_id"], unique=False
        )

    op.create_table(
        "contract_prices",
        sa.Column("contract_price_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("contract_id", sa.String(length=32), nullable=False),
        sa.Column("product_category", sa.String(length=64), nullable=False),
        sa.Column("unit_of_measure", sa.String(length=16), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["contracts.contract_id"],
            name=op.f("fk_contract_prices_contract_id_contracts"),
        ),
        sa.PrimaryKeyConstraint("contract_price_id", name=op.f("pk_contract_prices")),
        sa.UniqueConstraint(
            "contract_id",
            "product_category",
            name=op.f("uq_contract_prices_contract_id_product_category"),
        ),
    )
    op.create_table(
        "data_exceptions",
        sa.Column("exception_id", sa.String(length=32), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("raw_record_id", sa.String(length=32), nullable=False),
        sa.Column("source_entity_type", sa.String(length=40), nullable=False),
        sa.Column("source_record_id", sa.String(length=64), nullable=True),
        sa.Column(
            "issue_type",
            sa.Enum(
                "SHIPMENT_MISSING_PO",
                "DELIVERY_BEFORE_DISPATCH",
                "SCORE_FUTURE_LEAKAGE",
                "RISK_SIGNAL_MISSING_PROVENANCE",
                "SITE_ATTRIBUTION_CONFLICT",
                name="issuetype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(length=32), nullable=False),
        sa.Column(
            "severity",
            sa.Enum(
                "NONE",
                "LOW",
                "MEDIUM",
                "HIGH",
                "CRITICAL",
                name="severity",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "detection_status",
            sa.Enum(
                "DETECTED",
                "CONFIRMED",
                "DISMISSED",
                name="detectionstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "disposition",
            sa.Enum(
                "QUARANTINED",
                "QUARANTINED_PENDING_PROVENANCE",
                "CORRECTED",
                "REJECTED",
                name="disposition",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("related_entity_ids", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=True),
        sa.Column("corrected_score_id", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["corrected_score_id"],
            ["supplier_scores.score_id"],
            name=op.f("fk_data_exceptions_corrected_score_id_supplier_scores"),
        ),
        sa.ForeignKeyConstraint(
            ["raw_record_id"],
            ["raw_ingestion_records.raw_record_id"],
            name=op.f("fk_data_exceptions_raw_record_id_raw_ingestion_records"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.supplier_id"],
            name=op.f("fk_data_exceptions_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("exception_id", name=op.f("pk_data_exceptions")),
        sa.UniqueConstraint(
            "batch_id",
            "raw_record_id",
            "issue_type",
            name=op.f("uq_data_exceptions_batch_id_raw_record_id_issue_type"),
        ),
    )
    with op.batch_alter_table("data_exceptions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_data_exceptions_corrected_score_id"),
            ["corrected_score_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_data_exceptions_issue_type"), ["issue_type"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_data_exceptions_raw_record_id"), ["raw_record_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_data_exceptions_severity"), ["severity"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_data_exceptions_supplier_id"), ["supplier_id"], unique=False
        )

    op.create_table(
        "purchase_orders",
        sa.Column("po_id", sa.String(length=32), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("contract_id", sa.String(length=32), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("required_delivery_date", sa.Date(), nullable=False),
        sa.Column(
            "po_status",
            sa.Enum(
                "OPEN",
                "PARTIALLY_RECEIVED",
                "RECEIVED",
                "CANCELLED",
                "CLOSED",
                name="postatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["contracts.contract_id"],
            name=op.f("fk_purchase_orders_contract_id_contracts"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.supplier_id"],
            name=op.f("fk_purchase_orders_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("po_id", name=op.f("pk_purchase_orders")),
    )
    with op.batch_alter_table("purchase_orders", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_purchase_orders_contract_id"), ["contract_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_purchase_orders_order_date"), ["order_date"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_purchase_orders_supplier_id"), ["supplier_id"], unique=False
        )

    op.create_table(
        "score_evidence",
        sa.Column("score_id", sa.String(length=32), nullable=False),
        sa.Column(
            "component",
            sa.Enum(
                "delivery",
                "quality",
                "cost",
                "capacity",
                "compliance",
                "disruption",
                name="componentname",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("source_entity_type", sa.String(length=40), nullable=False),
        sa.Column("source_record_id", sa.String(length=64), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=200), nullable=True),
        sa.Column("contribution", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["score_id"],
            ["supplier_scores.score_id"],
            name=op.f("fk_score_evidence_score_id_supplier_scores"),
        ),
        sa.PrimaryKeyConstraint(
            "score_id",
            "component",
            "source_entity_type",
            "source_record_id",
            name=op.f("pk_score_evidence"),
        ),
    )
    with op.batch_alter_table("score_evidence", schema=None) as batch_op:
        batch_op.create_index(
            "ix_score_evidence_source_entity_type_source_record_id",
            ["source_entity_type", "source_record_id"],
            unique=False,
        )

    op.create_table(
        "po_lines",
        sa.Column("po_line_id", sa.String(length=32), nullable=False),
        sa.Column("po_id", sa.String(length=32), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("product_category", sa.String(length=64), nullable=False),
        sa.Column("ordered_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("unit_of_measure", sa.String(length=16), nullable=False),
        sa.Column("agreed_unit_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("line_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("agreed_unit_price >= 0", name=op.f("ck_po_lines_unit_price_nonneg")),
        sa.CheckConstraint("line_number >= 1", name=op.f("ck_po_lines_line_number_positive")),
        sa.CheckConstraint(
            "ordered_quantity > 0", name=op.f("ck_po_lines_ordered_quantity_positive")
        ),
        sa.ForeignKeyConstraint(
            ["po_id"], ["purchase_orders.po_id"], name=op.f("fk_po_lines_po_id_purchase_orders")
        ),
        sa.PrimaryKeyConstraint("po_line_id", name=op.f("pk_po_lines")),
        sa.UniqueConstraint("po_id", "line_number", name=op.f("uq_po_lines_po_id_line_number")),
    )
    op.create_table(
        "shipments",
        sa.Column("shipment_id", sa.String(length=32), nullable=False),
        sa.Column("po_id", sa.String(length=32), nullable=False),
        sa.Column("originating_site_id", sa.String(length=40), nullable=False),
        sa.Column("destination_warehouse_id", sa.String(length=40), nullable=False),
        sa.Column("carrier_id", sa.String(length=40), nullable=True),
        sa.Column("dispatch_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_delivery_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_delivery_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "shipment_status",
            sa.Enum(
                "BOOKED",
                "IN_TRANSIT",
                "DELIVERED",
                "PARTIALLY_RECEIVED",
                "CANCELLED",
                name="shipmentstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("shipped_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("received_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "received_quantity <= shipped_quantity", name=op.f("ck_shipments_received_le_shipped")
        ),
        sa.CheckConstraint("received_quantity >= 0", name=op.f("ck_shipments_received_nonneg")),
        sa.CheckConstraint("shipped_quantity >= 0", name=op.f("ck_shipments_shipped_nonneg")),
        sa.ForeignKeyConstraint(
            ["originating_site_id"],
            ["sites.site_id"],
            name=op.f("fk_shipments_originating_site_id_sites"),
        ),
        sa.ForeignKeyConstraint(
            ["po_id"], ["purchase_orders.po_id"], name=op.f("fk_shipments_po_id_purchase_orders")
        ),
        sa.PrimaryKeyConstraint("shipment_id", name=op.f("pk_shipments")),
    )
    with op.batch_alter_table("shipments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_shipments_expected_delivery_timestamp"),
            ["expected_delivery_timestamp"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_shipments_originating_site_id"), ["originating_site_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_shipments_po_id"), ["po_id"], unique=False)

    op.create_table(
        "delivery_events",
        sa.Column("delivery_event_id", sa.String(length=32), nullable=False),
        sa.Column("shipment_id", sa.String(length=32), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "BOOKED",
                "DISPATCHED",
                "IN_TRANSIT",
                "DELAYED",
                "ARRIVED",
                "RECEIVED",
                "CORRECTION",
                name="deliveryeventtype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location_id", sa.String(length=40), nullable=True),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("event_metadata", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "recorded_timestamp >= event_timestamp",
            name=op.f("ck_delivery_events_recorded_after_event"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.shipment_id"],
            name=op.f("fk_delivery_events_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint("delivery_event_id", name=op.f("pk_delivery_events")),
    )
    with op.batch_alter_table("delivery_events", schema=None) as batch_op:
        batch_op.create_index(
            "ix_delivery_events_shipment_id_event_timestamp",
            ["shipment_id", "event_timestamp"],
            unique=False,
        )

    op.create_table(
        "invoices",
        sa.Column("invoice_id", sa.String(length=32), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("contract_id", sa.String(length=32), nullable=False),
        sa.Column("po_id", sa.String(length=32), nullable=False),
        sa.Column("shipment_id", sa.String(length=32), nullable=False),
        sa.Column("supplier_invoice_number", sa.String(length=64), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column(
            "invoice_status",
            sa.Enum(
                "RECEIVED",
                "APPROVED",
                "DISPUTED",
                "PAID",
                name="invoicestatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("invoiced_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("expected_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("variance_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("variance_percentage", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["contracts.contract_id"],
            name=op.f("fk_invoices_contract_id_contracts"),
        ),
        sa.ForeignKeyConstraint(
            ["po_id"], ["purchase_orders.po_id"], name=op.f("fk_invoices_po_id_purchase_orders")
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.shipment_id"],
            name=op.f("fk_invoices_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.supplier_id"],
            name=op.f("fk_invoices_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("invoice_id", name=op.f("pk_invoices")),
        sa.UniqueConstraint(
            "supplier_id",
            "supplier_invoice_number",
            name=op.f("uq_invoices_supplier_id_supplier_invoice_number"),
        ),
    )
    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_invoices_contract_id"), ["contract_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_invoices_invoice_date"), ["invoice_date"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_invoices_po_id"), ["po_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_invoices_shipment_id"), ["shipment_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_invoices_supplier_id"), ["supplier_id"], unique=False)

    op.create_table(
        "risk_signals",
        sa.Column("risk_signal_id", sa.String(length=32), nullable=False),
        sa.Column("supplier_id", sa.String(length=32), nullable=False),
        sa.Column("site_id", sa.String(length=40), nullable=True),
        sa.Column("related_shipment_id", sa.String(length=32), nullable=True),
        sa.Column("related_contract_id", sa.String(length=32), nullable=True),
        sa.Column(
            "signal_type",
            sa.Enum(
                "LOGISTICS_DISRUPTION",
                "FACTORY_INCIDENT",
                "FINANCIAL_DISTRESS",
                "CERTIFICATION_LAPSE",
                "CARRIER_DELAY",
                "MONITORING_ATTESTATION",
                name="risksignaltype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Enum(
                "NONE",
                "LOW",
                "MEDIUM",
                "HIGH",
                "CRITICAL",
                name="severity",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("signal_source", sa.String(length=100), nullable=False),
        sa.Column("observed_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolution_status",
            sa.Enum(
                "OPEN",
                "MITIGATING",
                "RESOLVED",
                name="resolutionstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("evidence_reference", sa.String(length=500), nullable=False),
        sa.Column("dedup_key", sa.String(length=128), nullable=False),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name=op.f("ck_risk_signals_validity_dates"),
        ),
        sa.ForeignKeyConstraint(
            ["related_contract_id"],
            ["contracts.contract_id"],
            name=op.f("fk_risk_signals_related_contract_id_contracts"),
        ),
        sa.ForeignKeyConstraint(
            ["related_shipment_id"],
            ["shipments.shipment_id"],
            name=op.f("fk_risk_signals_related_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.site_id"], name=op.f("fk_risk_signals_site_id_sites")
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.supplier_id"],
            name=op.f("fk_risk_signals_supplier_id_suppliers"),
        ),
        sa.PrimaryKeyConstraint("risk_signal_id", name=op.f("pk_risk_signals")),
    )
    with op.batch_alter_table("risk_signals", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_risk_signals_observed_timestamp"), ["observed_timestamp"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_risk_signals_related_contract_id"), ["related_contract_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_risk_signals_related_shipment_id"), ["related_shipment_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_risk_signals_site_id"), ["site_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_risk_signals_supplier_id"), ["supplier_id"], unique=False
        )

    op.create_table(
        "shipment_lines",
        sa.Column("shipment_line_id", sa.String(length=32), nullable=False),
        sa.Column("shipment_id", sa.String(length=32), nullable=False),
        sa.Column("po_line_id", sa.String(length=32), nullable=False),
        sa.Column("unit_of_measure", sa.String(length=16), nullable=False),
        sa.Column("allocated_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("shipped_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("received_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "received_quantity <= shipped_quantity AND shipped_quantity <= allocated_quantity",
            name=op.f("ck_shipment_lines_quantity_order"),
        ),
        sa.CheckConstraint(
            "received_quantity >= 0 AND shipped_quantity >= 0 AND allocated_quantity >= 0",
            name=op.f("ck_shipment_lines_quantities_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["po_line_id"],
            ["po_lines.po_line_id"],
            name=op.f("fk_shipment_lines_po_line_id_po_lines"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.shipment_id"],
            name=op.f("fk_shipment_lines_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint("shipment_line_id", name=op.f("pk_shipment_lines")),
        sa.UniqueConstraint(
            "shipment_id", "po_line_id", name=op.f("uq_shipment_lines_shipment_id_po_line_id")
        ),
    )
    with op.batch_alter_table("shipment_lines", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_shipment_lines_po_line_id"), ["po_line_id"], unique=False
        )

    op.create_table(
        "inspections",
        sa.Column("inspection_id", sa.String(length=32), nullable=False),
        sa.Column("shipment_line_id", sa.String(length=32), nullable=False),
        sa.Column(
            "inspection_kind",
            sa.Enum(
                "INITIAL",
                "REINSPECTION",
                name="inspectionkind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "inspection_type",
            sa.Enum(
                "FULL", "SAMPLE", name="inspectiontype", native_enum=False, create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column("inspection_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inspected_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("accepted_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("rejected_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("defect_quantity", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "inspection_status",
            sa.Enum(
                "PASSED",
                "FAILED",
                "CONDITIONAL",
                name="inspectionstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "defect_category",
            sa.Enum(
                "NONE",
                "DIMENSIONAL",
                "COSMETIC",
                "FUNCTIONAL",
                "PACKAGING",
                "DOCUMENTATION",
                name="defectcategory",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("inspection_source", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "inspected_quantity >= 0 AND accepted_quantity >= 0 AND rejected_quantity >= 0 AND defect_quantity >= 0",
            name=op.f("ck_inspections_quantities_nonneg"),
        ),
        sa.CheckConstraint(
            "rejected_quantity <= defect_quantity AND defect_quantity <= inspected_quantity",
            name=op.f("ck_inspections_defect_bounds"),
        ),
        sa.CheckConstraint(
            "round(accepted_quantity + rejected_quantity, 2) = round(inspected_quantity, 2)",
            name=op.f("ck_inspections_reconciliation"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_line_id"],
            ["shipment_lines.shipment_line_id"],
            name=op.f("fk_inspections_shipment_line_id_shipment_lines"),
        ),
        sa.PrimaryKeyConstraint("inspection_id", name=op.f("pk_inspections")),
    )
    with op.batch_alter_table("inspections", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_inspections_inspection_timestamp"),
            ["inspection_timestamp"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_inspections_shipment_line_id"), ["shipment_line_id"], unique=False
        )
        batch_op.create_index(
            "uq_inspections_one_initial_per_shipment_line",
            ["shipment_line_id"],
            unique=True,
            sqlite_where=sa.text("inspection_kind = 'INITIAL'"),
            postgresql_where=sa.text("inspection_kind = 'INITIAL'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("inspections", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_inspections_one_initial_per_shipment_line",
            sqlite_where=sa.text("inspection_kind = 'INITIAL'"),
            postgresql_where=sa.text("inspection_kind = 'INITIAL'"),
        )
        batch_op.drop_index(batch_op.f("ix_inspections_shipment_line_id"))
        batch_op.drop_index(batch_op.f("ix_inspections_inspection_timestamp"))

    op.drop_table("inspections")
    with op.batch_alter_table("shipment_lines", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_shipment_lines_po_line_id"))

    op.drop_table("shipment_lines")
    with op.batch_alter_table("risk_signals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_risk_signals_supplier_id"))
        batch_op.drop_index(batch_op.f("ix_risk_signals_site_id"))
        batch_op.drop_index(batch_op.f("ix_risk_signals_related_shipment_id"))
        batch_op.drop_index(batch_op.f("ix_risk_signals_related_contract_id"))
        batch_op.drop_index(batch_op.f("ix_risk_signals_observed_timestamp"))

    op.drop_table("risk_signals")
    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invoices_supplier_id"))
        batch_op.drop_index(batch_op.f("ix_invoices_shipment_id"))
        batch_op.drop_index(batch_op.f("ix_invoices_po_id"))
        batch_op.drop_index(batch_op.f("ix_invoices_invoice_date"))
        batch_op.drop_index(batch_op.f("ix_invoices_contract_id"))

    op.drop_table("invoices")
    with op.batch_alter_table("delivery_events", schema=None) as batch_op:
        batch_op.drop_index("ix_delivery_events_shipment_id_event_timestamp")

    op.drop_table("delivery_events")
    with op.batch_alter_table("shipments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_shipments_po_id"))
        batch_op.drop_index(batch_op.f("ix_shipments_originating_site_id"))
        batch_op.drop_index(batch_op.f("ix_shipments_expected_delivery_timestamp"))

    op.drop_table("shipments")
    op.drop_table("po_lines")
    with op.batch_alter_table("score_evidence", schema=None) as batch_op:
        batch_op.drop_index("ix_score_evidence_source_entity_type_source_record_id")

    op.drop_table("score_evidence")
    with op.batch_alter_table("purchase_orders", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_purchase_orders_supplier_id"))
        batch_op.drop_index(batch_op.f("ix_purchase_orders_order_date"))
        batch_op.drop_index(batch_op.f("ix_purchase_orders_contract_id"))

    op.drop_table("purchase_orders")
    with op.batch_alter_table("data_exceptions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_data_exceptions_supplier_id"))
        batch_op.drop_index(batch_op.f("ix_data_exceptions_severity"))
        batch_op.drop_index(batch_op.f("ix_data_exceptions_raw_record_id"))
        batch_op.drop_index(batch_op.f("ix_data_exceptions_issue_type"))
        batch_op.drop_index(batch_op.f("ix_data_exceptions_corrected_score_id"))

    op.drop_table("data_exceptions")
    op.drop_table("contract_prices")
    with op.batch_alter_table("compliance_requirements", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_compliance_requirements_site_id"))
        batch_op.drop_index(batch_op.f("ix_compliance_requirements_contract_id"))

    op.drop_table("compliance_requirements")
    with op.batch_alter_table("supplier_scores", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_supplier_scores_one_current_per_key",
            sqlite_where=sa.text("is_current"),
            postgresql_where=sa.text("is_current"),
        )
        batch_op.drop_index("ix_supplier_scores_supplier_id_evaluation_end")
        batch_op.drop_index(batch_op.f("ix_supplier_scores_superseded_by_score_id"))
        batch_op.drop_index(batch_op.f("ix_supplier_scores_site_id"))
        batch_op.drop_index(batch_op.f("ix_supplier_scores_restates_score_id"))
        batch_op.drop_index(batch_op.f("ix_supplier_scores_reliability_category"))
        batch_op.drop_index(batch_op.f("ix_supplier_scores_config_hash"))
        batch_op.drop_index(batch_op.f("ix_supplier_scores_calculation_status"))

    op.drop_table("supplier_scores")
    with op.batch_alter_table("supplier_certifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_supplier_certifications_supplier_id"))
        batch_op.drop_index(batch_op.f("ix_supplier_certifications_supersedes_certification_id"))
        batch_op.drop_index(batch_op.f("ix_supplier_certifications_site_id"))

    op.drop_table("supplier_certifications")
    with op.batch_alter_table("contracts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_contracts_supplier_id"))
        batch_op.drop_index(batch_op.f("ix_contracts_site_id"))

    op.drop_table("contracts")
    with op.batch_alter_table("capacity_declarations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_capacity_declarations_supplier_id"))
        batch_op.drop_index(batch_op.f("ix_capacity_declarations_site_id"))

    op.drop_table("capacity_declarations")
    with op.batch_alter_table("validation_findings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_validation_findings_run_id"))
        batch_op.drop_index(batch_op.f("ix_validation_findings_rule_id"))

    op.drop_table("validation_findings")
    with op.batch_alter_table("sites", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sites_supplier_id"))

    op.drop_table("sites")
    op.drop_table("validation_runs")
    op.drop_table("suppliers")
    op.drop_table("scoring_config_snapshots")
    with op.batch_alter_table("raw_ingestion_records", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_raw_ingestion_records_source_entity_type"))
        batch_op.drop_index(batch_op.f("ix_raw_ingestion_records_batch_id"))

    op.drop_table("raw_ingestion_records")
    op.drop_table("dataset_manifests")
    op.drop_table("dataset_jobs")

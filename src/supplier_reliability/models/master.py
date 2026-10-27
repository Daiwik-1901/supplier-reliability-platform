"""Master data: suppliers, sites, contracts, prices, compliance and capacity (D11, D12)."""

from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from supplier_reliability.domain.enums import (
    CapacityVerification,
    CertificationStatus,
    CertificationType,
    ContractStatus,
    Region,
    RequirementScope,
    SiteStatus,
    SiteType,
    SupplierCategory,
    SupplierStatus,
    VerificationStatus,
)
from supplier_reliability.models.base import Base, UTCDateTime, enum_type

MONEY = sa.Numeric(18, 2)
CAPACITY = sa.Numeric(14, 2)


class Supplier(Base):
    __tablename__ = "suppliers"

    supplier_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    supplier_name: Mapped[str] = mapped_column(sa.String(200))
    supplier_category: Mapped[SupplierCategory] = mapped_column(enum_type(SupplierCategory))
    country: Mapped[str] = mapped_column(sa.String(64))
    region: Mapped[Region] = mapped_column(enum_type(Region))
    supplier_status: Mapped[SupplierStatus] = mapped_column(enum_type(SupplierStatus))
    onboarding_date: Mapped[date] = mapped_column(sa.Date)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="effective_dates"
        ),
    )

    site_id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(sa.ForeignKey("suppliers.supplier_id"), index=True)
    site_name: Mapped[str] = mapped_column(sa.String(200))
    site_type: Mapped[SiteType] = mapped_column(enum_type(SiteType))
    country: Mapped[str] = mapped_column(sa.String(64))
    region: Mapped[Region] = mapped_column(enum_type(Region))
    site_status: Mapped[SiteStatus] = mapped_column(enum_type(SiteStatus))
    effective_from: Mapped[date] = mapped_column(sa.Date)
    effective_to: Mapped[date | None] = mapped_column(sa.Date)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="effective_dates"
        ),
        sa.CheckConstraint(
            "contracted_capacity IS NULL OR contracted_capacity >= 0",
            name="contracted_capacity_nonneg",
        ),
    )

    contract_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(sa.ForeignKey("suppliers.supplier_id"), index=True)
    site_id: Mapped[str | None] = mapped_column(sa.ForeignKey("sites.site_id"), index=True)
    contract_status: Mapped[ContractStatus] = mapped_column(enum_type(ContractStatus))
    effective_from: Mapped[date] = mapped_column(sa.Date)
    effective_to: Mapped[date | None] = mapped_column(sa.Date)
    currency: Mapped[str] = mapped_column(sa.String(3))
    payment_terms: Mapped[str] = mapped_column(sa.String(64))
    delivery_terms: Mapped[str] = mapped_column(sa.String(64))
    contracted_capacity: Mapped[Decimal | None] = mapped_column(CAPACITY)
    capacity_unit: Mapped[str] = mapped_column(
        sa.String(32), default="units/month", server_default="units/month"
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ContractPrice(Base):
    __tablename__ = "contract_prices"
    __table_args__ = (sa.UniqueConstraint("contract_id", "product_category"),)

    contract_price_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[str] = mapped_column(sa.ForeignKey("contracts.contract_id"))
    product_category: Mapped[str] = mapped_column(sa.String(64))
    unit_of_measure: Mapped[str] = mapped_column(sa.String(16))
    unit_price: Mapped[Decimal] = mapped_column(MONEY)


class ComplianceRequirement(Base):
    __tablename__ = "compliance_requirements"
    __table_args__ = (
        sa.CheckConstraint(
            "(scope = 'SITE' AND site_id IS NOT NULL) OR (scope = 'SUPPLIER' AND site_id IS NULL)",
            name="scope_site",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="effective_dates"
        ),
    )

    requirement_id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    contract_id: Mapped[str] = mapped_column(sa.ForeignKey("contracts.contract_id"), index=True)
    certification_type: Mapped[CertificationType] = mapped_column(enum_type(CertificationType))
    mandatory: Mapped[bool] = mapped_column(sa.Boolean)
    scope: Mapped[RequirementScope] = mapped_column(enum_type(RequirementScope))
    site_id: Mapped[str | None] = mapped_column(sa.ForeignKey("sites.site_id"))
    effective_from: Mapped[date] = mapped_column(sa.Date)
    effective_to: Mapped[date | None] = mapped_column(sa.Date)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SupplierCertification(Base):
    """Append-only history: changes insert a new row that supersedes the old one (D11)."""

    __tablename__ = "supplier_certifications"
    __table_args__ = (
        sa.UniqueConstraint("supplier_id", "certificate_number", "recorded_at"),
        sa.CheckConstraint("valid_to >= valid_from", name="valid_dates"),
    )

    certification_id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(sa.ForeignKey("suppliers.supplier_id"), index=True)
    site_id: Mapped[str | None] = mapped_column(sa.ForeignKey("sites.site_id"))
    certification_type: Mapped[CertificationType] = mapped_column(enum_type(CertificationType))
    certificate_number: Mapped[str] = mapped_column(sa.String(64))
    issuing_authority: Mapped[str] = mapped_column(sa.String(200))
    verification_status: Mapped[VerificationStatus] = mapped_column(enum_type(VerificationStatus))
    compliance_status: Mapped[CertificationStatus] = mapped_column(
        enum_type(CertificationStatus, name="certificationstatus")
    )
    valid_from: Mapped[date] = mapped_column(sa.Date)
    valid_to: Mapped[date] = mapped_column(sa.Date)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    evidence_reference: Mapped[str] = mapped_column(sa.String(500))
    supersedes_certification_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("supplier_certifications.certification_id")
    )
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CapacityDeclaration(Base):
    __tablename__ = "capacity_declarations"
    __table_args__ = (
        sa.CheckConstraint("committed_capacity >= 0", name="committed_nonneg"),
        sa.CheckConstraint("available_capacity >= 0", name="available_nonneg"),
        sa.CheckConstraint("period_end >= period_start", name="period_dates"),
    )

    capacity_declaration_id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(sa.ForeignKey("suppliers.supplier_id"), index=True)
    site_id: Mapped[str | None] = mapped_column(sa.ForeignKey("sites.site_id"))
    period_start: Mapped[date] = mapped_column(sa.Date)
    period_end: Mapped[date] = mapped_column(sa.Date)
    unit: Mapped[str] = mapped_column(
        sa.String(32), default="units/month", server_default="units/month"
    )
    available_capacity: Mapped[Decimal] = mapped_column(CAPACITY)
    committed_capacity: Mapped[Decimal] = mapped_column(CAPACITY)
    verification_status: Mapped[CapacityVerification] = mapped_column(
        enum_type(CapacityVerification)
    )
    source: Mapped[str] = mapped_column(sa.String(100))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, StatementError

from supplier_reliability.database import make_engine, make_session_factory, session_scope
from supplier_reliability.models.base import Base
from supplier_reliability.models.master import (
    CapacityDeclaration,
    ComplianceRequirement,
    Contract,
    ContractPrice,
    Site,
    Supplier,
    SupplierCertification,
)

T0 = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/m.db")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def supplier(sid="SUP-000001", **kw) -> Supplier:
    base = dict(
        supplier_id=sid,
        supplier_name="Synthetic Supplier",
        supplier_category="ELECTRONICS",
        country="US",
        region="NA",
        supplier_status="ACTIVE",
        onboarding_date=date(2024, 1, 1),
        created_at=T0,
        updated_at=T0,
    )
    base.update(kw)
    return Supplier(**base)


def contract(cid="CTR-000001", sid="SUP-000001", **kw) -> Contract:
    base = dict(
        contract_id=cid,
        supplier_id=sid,
        contract_status="ACTIVE",
        effective_from=date(2025, 1, 1),
        effective_to=date(2025, 12, 31),
        currency="USD",
        payment_terms="NET30",
        delivery_terms="FOB",
        created_at=T0,
    )
    base.update(kw)
    return Contract(**base)


def cert(cid="CERT-1", **kw) -> SupplierCertification:
    base = dict(
        certification_id=cid,
        supplier_id="SUP-000001",
        certification_type="ISO9001",
        certificate_number="N-1",
        issuing_authority="Synthetic Certification Body 03",
        verification_status="VERIFIED",
        compliance_status="COMPLIANT",
        valid_from=date(2025, 1, 1),
        valid_to=date(2026, 1, 1),
        evidence_reference="synthetic://cert/1",
        recorded_at=T0,
    )
    base.update(kw)
    return SupplierCertification(**base)


def capacity(**kw) -> CapacityDeclaration:
    base = dict(
        capacity_declaration_id="CAP-1",
        supplier_id="SUP-000001",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        unit="units/month",
        available_capacity=Decimal("1000.00"),
        committed_capacity=Decimal("400.00"),
        verification_status="VERIFIED",
        source="SYNTHETIC",
        recorded_at=T0,
    )
    base.update(kw)
    return CapacityDeclaration(**base)


def requirement(**kw) -> ComplianceRequirement:
    base = dict(
        requirement_id="REQ-1",
        contract_id="CTR-000001",
        certification_type="ISO9001",
        mandatory=True,
        scope="SUPPLIER",
        effective_from=date(2025, 1, 1),
        created_at=T0,
    )
    base.update(kw)
    return ComplianceRequirement(**base)


def commit_fails(factory, *objs, match=None):
    with pytest.raises(IntegrityError, match=match), session_scope(factory) as s:
        add_in_order(s, *objs)


def add_in_order(session, *objs):
    """No ORM relationships are declared, so the unit of work will not order FK parents first."""
    for obj in objs:
        session.add(obj)
        session.flush()


def seed_supplier(factory):
    with session_scope(factory) as s:
        s.add(supplier())


def test_fk_enforced_for_site_without_supplier(factory):
    site = Site(
        site_id="SITE-SUP-000009-01",
        supplier_id="SUP-000009",
        site_name="x",
        site_type="FACTORY",
        country="US",
        region="NA",
        site_status="ACTIVE",
        effective_from=date(2025, 1, 1),
        created_at=T0,
    )
    commit_fails(factory, site, match="FOREIGN KEY")


def test_valid_graph_persists(factory):
    with session_scope(factory) as s:
        add_in_order(s, supplier(), contract(contracted_capacity=Decimal("1000.50")))
        s.add(
            ContractPrice(
                contract_id="CTR-000001",
                product_category="ELECTRONICS",
                unit_of_measure="EA",
                unit_price=Decimal("12.34"),
            )
        )
    with session_scope(factory) as s:
        c = s.get(Contract, "CTR-000001")
        assert c.capacity_unit == "units/month"
        assert c.site_id is None
        assert c.contracted_capacity == Decimal("1000.50")
        assert s.scalar(sa.select(ContractPrice.unit_price)) == Decimal("12.34")


def test_contract_price_unique_per_category(factory):
    def price():
        return ContractPrice(
            contract_id="CTR-000001",
            product_category="ELECTRONICS",
            unit_of_measure="EA",
            unit_price=Decimal("1.00"),
        )

    with session_scope(factory) as s:
        add_in_order(s, supplier(), contract())
        s.add(price())
    commit_fails(factory, price(), match="UNIQUE")


def test_contract_effective_dates_check(factory):
    seed_supplier(factory)
    commit_fails(
        factory,
        contract(effective_from=date(2025, 6, 1), effective_to=date(2025, 1, 1)),
        match="effective_dates",
    )


def test_contract_open_ended_effective_to_allowed(factory):
    with session_scope(factory) as s:
        add_in_order(s, supplier(), contract(effective_to=None))


def test_timestamps_roundtrip_as_utc_aware(factory):
    offset = timezone(timedelta(hours=5, minutes=30))
    with session_scope(factory) as s:
        s.add(supplier(created_at=datetime(2025, 3, 1, 12, 0, tzinfo=offset)))
    with session_scope(factory) as s:
        got = s.get(Supplier, "SUP-000001").created_at
    assert got.tzinfo is not None
    assert got.utcoffset() == timedelta(0)
    assert got == datetime(2025, 3, 1, 6, 30, tzinfo=UTC)


def test_naive_datetime_rejected(factory):
    with pytest.raises((StatementError, ValueError)), session_scope(factory) as s:
        s.add(supplier(created_at=datetime(2025, 1, 1)))


def test_invalid_enum_rejected(factory):
    # ORM layer refuses unknown members ...
    with pytest.raises(StatementError), session_scope(factory) as s:
        s.add(supplier(supplier_status="BOGUS"))
    # ... and the database CHECK refuses them even when the ORM is bypassed.
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.execute(
            sa.text(
                "INSERT INTO suppliers VALUES ('SUP-000002','n','ELECTRONICS','US','NA',"
                "'BOGUS','2024-01-01','2025-01-01 00:00:00','2025-01-01 00:00:00')"
            )
        )


def test_requirement_with_unknown_contract_rejected(factory):
    commit_fails(factory, requirement(contract_id="CTR-NOPE"), match="FOREIGN KEY")


def test_requirement_effective_dates_check(factory):
    with session_scope(factory) as s:
        add_in_order(s, supplier(), contract())
    commit_fails(
        factory,
        requirement(effective_from=date(2025, 6, 1), effective_to=date(2025, 1, 1)),
        match="effective_dates",
    )


def test_capacity_declaration_persists(factory):
    with session_scope(factory) as s:
        add_in_order(s, supplier(), capacity())
    with session_scope(factory) as s:
        got = s.get(CapacityDeclaration, "CAP-1")
        assert got.available_capacity - got.committed_capacity == Decimal("600.00")


def test_negative_committed_capacity_rejected(factory):
    seed_supplier(factory)
    commit_fails(factory, capacity(committed_capacity=Decimal("-1")), match="committed_nonneg")


def test_negative_available_capacity_rejected(factory):
    seed_supplier(factory)
    commit_fails(factory, capacity(available_capacity=Decimal("-1")), match="available_nonneg")


def test_capacity_period_check(factory):
    seed_supplier(factory)
    commit_fails(factory, capacity(period_end=date(2024, 12, 31)), match="period_dates")


def test_certification_supersedes_must_exist(factory):
    seed_supplier(factory)
    commit_fails(factory, cert(supersedes_certification_id="CERT-MISSING"), match="FOREIGN KEY")


def test_certification_supersedes_chain_and_uniqueness(factory):
    with session_scope(factory) as s:
        add_in_order(
            s,
            supplier(),
            cert("CERT-1"),
            cert("CERT-2", certificate_number="N-2", supersedes_certification_id="CERT-1"),
        )
    commit_fails(factory, cert("CERT-3", certificate_number="N-1"), match="UNIQUE")


def test_certification_validity_check(factory):
    seed_supplier(factory)
    commit_fails(
        factory, cert(valid_from=date(2026, 1, 1), valid_to=date(2025, 1, 1)), match="valid_dates"
    )


def test_supplier_id_foreign_keys_are_indexed():
    for table in ("sites", "contracts", "supplier_certifications", "capacity_declarations"):
        t = Base.metadata.tables[table]
        assert any([c.name for c in ix.columns][:1] == ["supplier_id"] for ix in t.indexes), table


def test_constraint_names_follow_convention():
    t = Base.metadata.tables["contracts"]
    names = {c.name for c in t.constraints}
    assert "pk_contracts" in names
    assert "fk_contracts_supplier_id_suppliers" in names
    assert "ck_contracts_effective_dates" in names


def test_session_scope_rolls_back_on_error(factory):
    with pytest.raises(RuntimeError), session_scope(factory) as s:
        s.add(supplier())
        s.flush()
        raise RuntimeError
    with session_scope(factory) as s:
        assert s.get(Supplier, "SUP-000001") is None

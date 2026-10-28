"""Domain-constraint rules DC-001..DC-010 (spec section 11; D3, D12).

Severity: HIGH when a violation corrupts what is built on the data (missing/unknown values,
negative quantities, mixed currencies or units, impossible capacity); MEDIUM for precision and
contract-validity problems that leave the record usable; LOW for DC-009, which is informational.

DC-008 interpretation (documented): capacity is declared in the single capacity unit
"units/month" (D12), and the capacity score counts every PO line's ordered quantity as units of
that rate, whatever the line's physical unit (EA, KG, ...). So DC-008 checks that (a) every
allocation uses its PO line's unit of measure and (b) every capacity declaration and every
contract's capacity_unit use "units/month". A PO line's physical unit is not compared with the
capacity rate unit (they are different dimensions by design).
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import CapacityVerification, POStatus, Severity
from supplier_reliability.models import (
    CapacityDeclaration,
    Contract,
    ContractPrice,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    Shipment,
    ShipmentLineAllocation,
)
from supplier_reliability.services.validation.framework import RuleCheck, Violation, rule
from supplier_reliability.services.validation.rules_integrity import (
    CANONICAL_MODELS,
    count,
    entity_key,
)

CAPACITY_UNIT = "units/month"
_HIGH = {"category": "domain", "severity": Severity.HIGH}
_MEDIUM = {"category": "domain", "severity": Severity.MEDIUM}

# (model, column, strictly positive?) quantities and amounts that must not be negative.
# invoices.invoiced_amount is DC-007; invoices.variance_amount may be negative (underbilling).
NONNEGATIVE: tuple[tuple[Any, str, bool], ...] = (
    (Contract, "contracted_capacity", False),
    (ContractPrice, "unit_price", False),
    (CapacityDeclaration, "available_capacity", False),
    (CapacityDeclaration, "committed_capacity", False),
    (POLine, "ordered_quantity", True),
    (POLine, "agreed_unit_price", False),
    (POLine, "line_amount", False),
    (Shipment, "shipped_quantity", False),
    (Shipment, "received_quantity", False),
    (ShipmentLineAllocation, "allocated_quantity", False),
    (ShipmentLineAllocation, "shipped_quantity", False),
    (ShipmentLineAllocation, "received_quantity", False),
    (Inspection, "inspected_quantity", False),
    (Inspection, "accepted_quantity", False),
    (Inspection, "rejected_quantity", False),
    (Inspection, "defect_quantity", False),
    (Invoice, "expected_amount", False),
)
MONEY: tuple[tuple[Any, str], ...] = (
    (ContractPrice, "unit_price"),
    (POLine, "agreed_unit_price"),
    (POLine, "line_amount"),
    (Invoice, "invoiced_amount"),
    (Invoice, "expected_amount"),
    (Invoice, "variance_amount"),
)


def _pk(model: Any) -> list[sa.Column]:
    return list(model.__table__.primary_key.columns)


def _rows_of(session: Session, models: set[Any]) -> int:
    return sum(count(session, m) for m in sorted(models, key=lambda m: m.__tablename__))


@rule(
    "DC-001",
    **_HIGH,
    description=(
        "Required fields: every NOT NULL column of every canonical table is non-null and every "
        "required text column is non-blank (e.g. risk-signal provenance). HIGH: a missing "
        "required value makes the record unusable."
    ),
)
def dc_001(session: Session) -> RuleCheck:
    violations: list[Violation] = []
    checked = 0
    for model in CANONICAL_MODELS:
        table = model.__table__
        pk = _pk(model)
        required = [c for c in table.columns if not c.nullable]
        checked += count(session, model)

        def bad(c: sa.Column) -> sa.ColumnElement[bool]:
            if isinstance(c.type, sa.String) and not isinstance(c.type, sa.Enum):
                return sa.or_(c.is_(None), sa.func.trim(c) == "")
            return c.is_(None)

        tallies = session.execute(
            sa.select(*[sa.func.sum(sa.case((bad(c), 1), else_=0)) for c in required])
        ).one()
        for c, n in zip(required, tallies, strict=True):
            if not n:
                continue
            stmt = sa.select(*pk, sa.type_coerce(c, sa.String).label("value")).where(bad(c))
            for r in session.execute(stmt.order_by(*pk)):
                violations.append(
                    Violation(
                        table.name,
                        entity_key(r, [k.name for k in pk]),
                        c.name,
                        r.value,
                        "required: non-null and non-blank",
                        f"{table.name}.{c.name} is missing",
                    )
                )
    return RuleCheck(checked, violations)


@rule(
    "DC-002",
    **_HIGH,
    description=(
        "Enum values: every enum column holds one of its enum's values (defence in depth; the "
        "schema has CHECK constraints). Read as plain text so an invalid value is reported, "
        "not raised. HIGH."
    ),
)
def dc_002(session: Session) -> RuleCheck:
    violations: list[Violation] = []
    tables: set[Any] = set()
    for model in CANONICAL_MODELS:
        table = model.__table__
        pk = _pk(model)
        for c in table.columns:
            if not isinstance(c.type, sa.Enum):
                continue
            tables.add(model)
            text = sa.type_coerce(c, sa.String)
            stmt = (
                sa.select(*pk, text.label("value"))
                .where(c.is_not(None), text.not_in(list(c.type.enums)))
                .order_by(*pk)
            )
            for r in session.execute(stmt):
                violations.append(
                    Violation(
                        table.name,
                        entity_key(r, [k.name for k in pk]),
                        c.name,
                        r.value,
                        f"one of {sorted(c.type.enums)}",
                        f"{table.name}.{c.name} = {r.value!r} is not a valid value",
                    )
                )
    return RuleCheck(_rows_of(session, tables), violations)


@rule(
    "DC-003",
    **_HIGH,
    description=(
        "Nonnegative quantities and amounts (ordered quantity strictly positive); variance may "
        "be negative and invoiced amount is DC-007. HIGH."
    ),
)
def dc_003(session: Session) -> RuleCheck:
    violations: list[Violation] = []
    for model, name, strict in NONNEGATIVE:
        table, pk = model.__table__, _pk(model)
        c = table.c[name]
        cond = c <= 0 if strict else c < 0
        stmt = sa.select(*pk, c.label("value")).where(cond).order_by(*pk)
        for r in session.execute(stmt):
            violations.append(
                Violation(
                    table.name,
                    entity_key(r, [k.name for k in pk]),
                    name,
                    r.value,
                    "> 0" if strict else ">= 0",
                    f"{table.name}.{name} = {r.value} is negative" + (" or zero" if strict else ""),
                )
            )
    return RuleCheck(_rows_of(session, {m for m, _, _ in NONNEGATIVE}), violations)


@rule(
    "DC-004",
    **_MEDIUM,
    description=(
        "Money has at most 2 decimal places. Read as float in SQL (|100 x v - round(100 x v)| > "
        "1e-6) because the ORM type would quantize the value on read; only reachable where "
        "Numeric is not enforced (SQLite storage, raw imports). MEDIUM."
    ),
)
def dc_004(session: Session) -> RuleCheck:
    violations: list[Violation] = []
    for model, name in MONEY:
        table, pk = model.__table__, _pk(model)
        raw = sa.type_coerce(table.c[name], sa.Float)
        cents = raw * 100
        stmt = (
            sa.select(*pk, raw.label("value"))
            .where(sa.func.abs(cents - sa.func.round(cents)) > 1e-6)
            .order_by(*pk)
        )
        for r in session.execute(stmt):
            violations.append(
                Violation(
                    table.name,
                    entity_key(r, [k.name for k in pk]),
                    name,
                    repr(r.value),
                    "at most 2 decimal places",
                    f"{table.name}.{name} = {r.value!r} has more than 2 decimal places",
                )
            )
    return RuleCheck(_rows_of(session, {m for m, _ in MONEY}), violations)


@rule(
    "DC-005",
    **_MEDIUM,
    description=(
        "Contract validity: a PO's order_date lies within its contract's effective period "
        "[effective_from, effective_to] (dates). MEDIUM."
    ),
)
def dc_005(session: Session) -> RuleCheck:
    po, c = PurchaseOrder, Contract
    stmt = (
        sa.select(po.po_id, po.order_date, c.contract_id, c.effective_from, c.effective_to)
        .join(c, c.contract_id == po.contract_id)
        .where(
            sa.or_(
                po.order_date < c.effective_from,
                sa.and_(c.effective_to.is_not(None), po.order_date > c.effective_to),
            )
        )
        .order_by(po.po_id)
    )
    violations = [
        Violation(
            "purchase_orders",
            r.po_id,
            "order_date",
            r.order_date.isoformat(),
            f"within contract {r.contract_id} [{r.effective_from}, {r.effective_to or 'open'}]",
            f"PO {r.po_id} ordered on {r.order_date} outside its contract's effective period",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, po), violations)


@rule(
    "DC-006",
    **_HIGH,
    description=(
        "Currency: a PO's and an invoice's currency equal their contract's currency (one "
        "currency per contract, no FX, D3). HIGH: mixed currencies corrupt cost evidence."
    ),
)
def dc_006(session: Session) -> RuleCheck:
    violations: list[Violation] = []
    for model, pk, table in (
        (PurchaseOrder, PurchaseOrder.po_id, "purchase_orders"),
        (Invoice, Invoice.invoice_id, "invoices"),
    ):
        stmt = (
            sa.select(
                pk.label("id"), model.currency, Contract.contract_id, Contract.currency.label("cc")
            )
            .join(Contract, Contract.contract_id == model.contract_id)
            .where(model.currency != Contract.currency)
            .order_by(pk)
        )
        for r in session.execute(stmt):
            violations.append(
                Violation(
                    table,
                    r.id,
                    "currency",
                    r.currency,
                    f"contract {r.contract_id} currency {r.cc}",
                    f"{table} {r.id} is in {r.currency} but its contract is in {r.cc}",
                )
            )
    return RuleCheck(count(session, PurchaseOrder) + count(session, Invoice), violations)


@rule(
    "DC-007",
    **_HIGH,
    description=(
        "Invoiced amount >= 0 (credits are not modelled, D3). HIGH: a negative invoice "
        "corrupts the cost component."
    ),
)
def dc_007(session: Session) -> RuleCheck:
    stmt = (
        sa.select(Invoice.invoice_id, Invoice.invoiced_amount)
        .where(Invoice.invoiced_amount < 0)
        .order_by(Invoice.invoice_id)
    )
    violations = [
        Violation(
            "invoices",
            r.invoice_id,
            "invoiced_amount",
            r.invoiced_amount,
            ">= 0",
            f"invoice {r.invoice_id} has a negative invoiced amount {r.invoiced_amount}",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, Invoice), violations)


@rule(
    "DC-008",
    **_HIGH,
    description=(
        "Units of measure (D1, D12): an allocation's unit equals its PO line's, and capacity "
        "declarations and contract capacity use the capacity unit 'units/month' (see module "
        "docstring). HIGH: mismatched units silently corrupt quantity sums."
    ),
)
def dc_008(session: Session) -> RuleCheck:
    a = ShipmentLineAllocation
    violations = [
        Violation(
            "shipment_lines",
            r.shipment_line_id,
            "unit_of_measure",
            r.unit_of_measure,
            f"PO line {r.po_line_id} unit {r.line_uom}",
            f"allocation {r.shipment_line_id} is in {r.unit_of_measure} but its PO line is in "
            f"{r.line_uom}",
        )
        for r in session.execute(
            sa.select(
                a.shipment_line_id,
                a.unit_of_measure,
                a.po_line_id,
                POLine.unit_of_measure.label("line_uom"),
            )
            .join(POLine, POLine.po_line_id == a.po_line_id)
            .where(a.unit_of_measure != POLine.unit_of_measure)
            .order_by(a.shipment_line_id)
        )
    ]
    for table, pk, unit in (
        (
            "capacity_declarations",
            CapacityDeclaration.capacity_declaration_id,
            CapacityDeclaration.unit,
        ),
        ("contracts", Contract.contract_id, Contract.capacity_unit),
    ):
        stmt = sa.select(pk.label("id"), unit.label("unit")).where(unit != CAPACITY_UNIT)
        for r in session.execute(stmt.order_by(pk)):
            violations.append(
                Violation(
                    table,
                    r.id,
                    unit.name,
                    r.unit,
                    f"capacity unit {CAPACITY_UNIT!r}",
                    f"{table} {r.id} declares capacity in {r.unit!r}, not {CAPACITY_UNIT!r}",
                )
            )
    checked = count(session, a) + count(session, CapacityDeclaration) + count(session, Contract)
    return RuleCheck(checked, violations)


@rule(
    "DC-009",
    category="domain",
    severity=Severity.LOW,
    description=(
        "Informational (D12): a contract-month whose demand (sum of ordered quantity of "
        "non-cancelled POs ordered that month) exceeds the contract's contracted_capacity. The "
        "capacity score uses verified capacity instead, so this is LOW."
    ),
)
def dc_009(session: Session) -> RuleCheck:
    stmt = (
        sa.select(
            Contract.contract_id,
            Contract.contracted_capacity,
            PurchaseOrder.order_date,
            POLine.ordered_quantity,
        )
        .join(PurchaseOrder, PurchaseOrder.contract_id == Contract.contract_id)
        .join(POLine, POLine.po_id == PurchaseOrder.po_id)
        .where(
            Contract.contracted_capacity.is_not(None),
            PurchaseOrder.po_status != POStatus.CANCELLED,
        )
    )
    demand: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    capacity: dict[str, Decimal] = {}
    for r in session.execute(stmt):
        demand[(r.contract_id, r.order_date.replace(day=1))] += r.ordered_quantity
        capacity[r.contract_id] = r.contracted_capacity
    violations = [
        Violation(
            "contracts",
            contract_id,
            "contracted_capacity",
            f"{month:%Y-%m}: demand {total} > {capacity[contract_id]}",
            "monthly demand <= contracted_capacity",
            f"contract {contract_id} demand in {month:%Y-%m} ({total}) exceeds its contracted "
            f"capacity {capacity[contract_id]}",
        )
        for (contract_id, month), total in sorted(demand.items())
        if total > capacity[contract_id]
    ]
    return RuleCheck(len(demand), violations)


@rule(
    "DC-010",
    **_HIGH,
    description=(
        "A VERIFIED capacity declaration never commits more than is available (committed <= "
        "available). HIGH: the capacity score rejects such evidence."
    ),
)
def dc_010(session: Session) -> RuleCheck:
    cd = CapacityDeclaration
    verified = cd.verification_status == CapacityVerification.VERIFIED
    stmt = (
        sa.select(cd.capacity_declaration_id, cd.available_capacity, cd.committed_capacity)
        .where(verified, cd.committed_capacity > cd.available_capacity)
        .order_by(cd.capacity_declaration_id)
    )
    violations = [
        Violation(
            "capacity_declarations",
            r.capacity_declaration_id,
            "committed_capacity",
            f"committed {r.committed_capacity} > available {r.available_capacity}",
            "committed_capacity <= available_capacity",
            f"verified capacity declaration {r.capacity_declaration_id} commits more than it "
            "has available",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, cd, verified), violations)

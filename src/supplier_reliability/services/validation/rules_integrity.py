"""Referential-integrity rules RI-001..RI-009 (spec section 11; D1, D3, D11, D12).

Every violation here is HIGH: the record graph itself is inconsistent, so any aggregate built on
it is suspect. Several rules duplicate a database constraint on purpose (defence in depth: the
same checks guard exports, other backends and future schema drift); they still examine and
count every row.
"""

from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import POStatus, RequirementScope, Severity
from supplier_reliability.models import (
    Base,
    CapacityDeclaration,
    ComplianceRequirement,
    Contract,
    ContractPrice,
    DeliveryEvent,
    Inspection,
    Invoice,
    POLine,
    PurchaseOrder,
    RiskSignal,
    ScoreEvidence,
    ScoringConfigSnapshot,
    Shipment,
    ShipmentLineAllocation,
    Site,
    Supplier,
    SupplierCertification,
    SupplierScore,
)
from supplier_reliability.services.validation.framework import RuleCheck, Violation, rule

# Canonical tables (master data, transactions, scores and their lineage), parents first.
CANONICAL_MODELS: tuple[type[Base], ...] = (
    Supplier,
    Site,
    Contract,
    ContractPrice,
    ComplianceRequirement,
    SupplierCertification,
    CapacityDeclaration,
    PurchaseOrder,
    POLine,
    Shipment,
    ShipmentLineAllocation,
    DeliveryEvent,
    Inspection,
    Invoice,
    RiskSignal,
    ScoringConfigSnapshot,
    SupplierScore,
    ScoreEvidence,
)
INTEGRITY = {"category": "integrity", "severity": Severity.HIGH}


def count(session: Session, model: Any, *where: Any) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(model).where(*where)) or 0)


def entity_key(row: Any, names: list[str]) -> str:
    return "|".join(str(getattr(row, n)) for n in names)


# --- RI-001 -------------------------------------------------------------------------------------

# Export-level natural keys (D3, D11, PO line numbering); each must identify one row.
_NATURAL_KEYS: tuple[tuple[Any, tuple[str, ...]], ...] = (
    (Invoice, ("supplier_id", "supplier_invoice_number")),
    (POLine, ("po_id", "line_number")),
    (SupplierCertification, ("supplier_id", "certificate_number", "recorded_at")),
)


def _duplicates(session: Session, model: Any, key: tuple[str, ...]) -> list[Violation]:
    table = model.__table__
    pk = [c.name for c in table.primary_key.columns]
    cols = [table.c[k] for k in key]
    groups = sa.select(*cols).group_by(*cols).having(sa.func.count() > 1).subquery("duplicate_keys")
    stmt = (
        sa.select(*[table.c[n] for n in pk], *cols)
        .join(groups, sa.and_(*[table.c[k] == groups.c[k] for k in key]))
        .order_by(*cols, *[table.c[n] for n in pk])
    )
    out, seen = [], set()
    for row in session.execute(stmt):
        group = tuple(getattr(row, k) for k in key)
        if group in seen:  # the first row of a key is the original; the others duplicate it
            out.append(
                Violation(
                    table.name,
                    entity_key(row, pk),
                    ",".join(key),
                    "|".join(str(v) for v in group),
                    f"({', '.join(key)}) identifies exactly one {table.name} row",
                    f"{table.name} key ({', '.join(key)}) = {group} appears more than once",
                )
            )
        seen.add(group)
    return out


@rule(
    "RI-001",
    **INTEGRITY,
    description=(
        "Unique keys: export-level natural keys (supplier_invoice_number per supplier, "
        "(po_id, line_number), certificate (supplier, number, recorded_at)) occur once, and "
        "every canonical table's primary key is unique (COUNT vs COUNT DISTINCT). HIGH: a "
        "duplicate key multiplies joins and exports."
    ),
)
def ri_001(session: Session) -> RuleCheck:
    violations: list[Violation] = []
    checked = 0
    for model, key in _NATURAL_KEYS:
        checked += count(session, model)
        violations += _duplicates(session, model, key)
    for model in CANONICAL_MODELS:
        table = model.__table__
        pk = list(table.primary_key.columns)
        rows = count(session, model)
        distinct = session.scalar(
            sa.select(sa.func.count()).select_from(sa.select(*pk).distinct().subquery())
        )
        checked += rows
        if rows != distinct:
            names = ",".join(c.name for c in pk)
            violations.append(
                Violation(
                    table.name,
                    None,
                    names,
                    f"{rows} rows, {distinct} distinct keys",
                    f"primary key ({names}) unique",
                    f"{table.name} has {rows - int(distinct or 0)} duplicate primary keys",
                )
            )
    return RuleCheck(checked, violations)


# --- RI-002 -------------------------------------------------------------------------------------


@rule(
    "RI-002",
    **INTEGRITY,
    description=(
        "Orphan foreign keys: every non-null FK value of every table references an existing "
        "row (defence in depth; FKs are enforced by the database). HIGH."
    ),
)
def ri_002(session: Session) -> RuleCheck:
    violations: list[Violation] = []
    checked = 0
    for table in Base.metadata.sorted_tables:
        pk = [c.name for c in table.primary_key.columns]
        for fk in sorted(table.foreign_keys, key=lambda f: f.parent.name):
            child = fk.parent
            parent = sa.alias(fk.column.table)
            target = parent.c[fk.column.name]
            joined = table.outerjoin(parent, target == child)
            stmt = sa.select(sa.func.count(child), sa.func.count(target)).select_from(joined)
            values, matched = session.execute(stmt).one()
            checked += values
            if values == matched:
                continue
            orphans = (
                sa.select(*[table.c[n] for n in pk], child.label("fk_value"))
                .select_from(joined)
                .where(child.is_not(None), target.is_(None))
                .order_by(*[table.c[n] for n in pk])
            )
            for row in session.execute(orphans):
                violations.append(
                    Violation(
                        table.name,
                        entity_key(row, pk),
                        child.name,
                        row.fk_value,
                        f"references an existing {fk.column.table.name}.{fk.column.name}",
                        f"{table.name}.{child.name} = {row.fk_value!r} has no "
                        f"{fk.column.table.name} row",
                    )
                )
    return RuleCheck(checked, violations)


# --- RI-003 .. RI-009 ---------------------------------------------------------------------------


@rule(
    "RI-003",
    **INTEGRITY,
    description=(
        "A shipment's dispatch (originating) site belongs to the PO's supplier (site "
        "attribution, D9-E). HIGH."
    ),
)
def ri_003(session: Session) -> RuleCheck:
    stmt = (
        sa.select(
            Shipment.shipment_id,
            Shipment.originating_site_id,
            Site.supplier_id.label("site_supplier"),
            PurchaseOrder.supplier_id.label("po_supplier"),
        )
        .join(PurchaseOrder, PurchaseOrder.po_id == Shipment.po_id)
        .join(Site, Site.site_id == Shipment.originating_site_id)
        .where(Site.supplier_id != PurchaseOrder.supplier_id)
        .order_by(Shipment.shipment_id)
    )
    violations = [
        Violation(
            "shipments",
            r.shipment_id,
            "originating_site_id",
            f"{r.originating_site_id} (supplier {r.site_supplier})",
            f"site of the PO supplier {r.po_supplier}",
            f"shipment {r.shipment_id} dispatched from {r.originating_site_id} of "
            f"{r.site_supplier}, but its PO belongs to {r.po_supplier}",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, Shipment), violations)


def _effective(day: date, start: date, end: date | None) -> bool:
    return start <= day and (end is None or day <= end)


@rule(
    "RI-004",
    **INTEGRITY,
    description=(
        "The dispatch site is effective on the dispatch date (UTC calendar date within "
        "[effective_from, effective_to]). Compared in Python on dates: a DATETIME vs DATE "
        "comparison in SQL would misjudge the last effective day. HIGH."
    ),
)
def ri_004(session: Session) -> RuleCheck:
    stmt = (
        sa.select(
            Shipment.shipment_id,
            Shipment.dispatch_timestamp,
            Site.site_id,
            Site.effective_from,
            Site.effective_to,
        )
        .join(Site, Site.site_id == Shipment.originating_site_id)
        .order_by(Shipment.shipment_id)
    )
    checked, violations = 0, []
    for r in session.execute(stmt):
        checked += 1
        day = r.dispatch_timestamp.date()
        if not _effective(day, r.effective_from, r.effective_to):
            violations.append(
                Violation(
                    "shipments",
                    r.shipment_id,
                    "dispatch_timestamp",
                    day.isoformat(),
                    f"within site {r.site_id} effective period "
                    f"[{r.effective_from}, {r.effective_to or 'open'}]",
                    f"shipment {r.shipment_id} dispatched on {day} from site {r.site_id}, "
                    "which was not effective that day",
                )
            )
    return RuleCheck(checked, violations)


@rule(
    "RI-005",
    **INTEGRITY,
    description="A purchase order's contract belongs to the PO's supplier. HIGH.",
)
def ri_005(session: Session) -> RuleCheck:
    stmt = (
        sa.select(
            PurchaseOrder.po_id,
            PurchaseOrder.supplier_id,
            Contract.contract_id,
            Contract.supplier_id.label("contract_supplier"),
        )
        .join(Contract, Contract.contract_id == PurchaseOrder.contract_id)
        .where(Contract.supplier_id != PurchaseOrder.supplier_id)
        .order_by(PurchaseOrder.po_id)
    )
    violations = [
        Violation(
            "purchase_orders",
            r.po_id,
            "contract_id",
            f"{r.contract_id} (supplier {r.contract_supplier})",
            f"a contract of supplier {r.supplier_id}",
            f"PO {r.po_id} of {r.supplier_id} uses contract {r.contract_id} of "
            f"{r.contract_supplier}",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, PurchaseOrder), violations)


@rule(
    "RI-006",
    **INTEGRITY,
    description=(
        "Invoice consistency (D3): invoice supplier and contract equal its PO's, and its "
        "shipment belongs to that PO. HIGH."
    ),
)
def ri_006(session: Session) -> RuleCheck:
    stmt = (
        sa.select(
            Invoice.invoice_id,
            Invoice.supplier_id,
            Invoice.contract_id,
            Invoice.po_id,
            PurchaseOrder.supplier_id.label("po_supplier"),
            PurchaseOrder.contract_id.label("po_contract"),
            Shipment.po_id.label("shipment_po"),
        )
        .join(PurchaseOrder, PurchaseOrder.po_id == Invoice.po_id)
        .join(Shipment, Shipment.shipment_id == Invoice.shipment_id)
        .where(
            sa.or_(
                Invoice.supplier_id != PurchaseOrder.supplier_id,
                Invoice.contract_id != PurchaseOrder.contract_id,
                Shipment.po_id != Invoice.po_id,
            )
        )
        .order_by(Invoice.invoice_id)
    )
    violations = []
    for r in session.execute(stmt):
        for field, observed, expected in (
            ("supplier_id", r.supplier_id, r.po_supplier),
            ("contract_id", r.contract_id, r.po_contract),
            ("po_id", r.po_id, r.shipment_po),
        ):
            if observed != expected:
                violations.append(
                    Violation(
                        "invoices",
                        r.invoice_id,
                        field,
                        observed,
                        f"{field} = {expected} (from its PO / shipment)",
                        f"invoice {r.invoice_id} {field} {observed} disagrees with {expected}",
                    )
                )
    return RuleCheck(count(session, Invoice), violations)


@rule(
    "RI-007",
    **INTEGRITY,
    description=(
        "Cardinality: every non-cancelled purchase order has at least one shipment (the "
        "generator dispatches every non-cancelled PO). HIGH."
    ),
)
def ri_007(session: Session) -> RuleCheck:
    has_shipment = sa.exists().where(Shipment.po_id == PurchaseOrder.po_id)
    active = PurchaseOrder.po_status != POStatus.CANCELLED
    stmt = (
        sa.select(PurchaseOrder.po_id, PurchaseOrder.po_status)
        .where(active, ~has_shipment)
        .order_by(PurchaseOrder.po_id)
    )
    violations = [
        Violation(
            "purchase_orders",
            r.po_id,
            None,
            "0 shipments",
            ">= 1 shipment for a non-cancelled PO",
            f"PO {r.po_id} ({r.po_status}) has no shipment",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, PurchaseOrder, active), violations)


def _site_owner_violations(
    session: Session, table: str, pk: Any, owner: Any, site_col: Any, *joins: tuple, where=()
) -> tuple[int, list[Violation]]:
    base = sa.select(pk.label("id"), owner.label("owner"), site_col.label("site_id"))
    for target, on in joins:
        base = base.join(target, on)
    base = base.where(site_col.is_not(None), *where)
    rows = base.subquery()
    checked = int(session.scalar(sa.select(sa.func.count()).select_from(rows)) or 0)
    stmt = (
        sa.select(rows.c.id, rows.c.owner, rows.c.site_id, Site.supplier_id)
        .join(Site, Site.site_id == rows.c.site_id)
        .where(Site.supplier_id != rows.c.owner)
        .order_by(rows.c.id)
    )
    return checked, [
        Violation(
            table,
            r.id,
            "site_id",
            f"{r.site_id} (supplier {r.supplier_id})",
            f"a site of supplier {r.owner}",
            f"{table} {r.id} of supplier {r.owner} references site {r.site_id} of {r.supplier_id}",
        )
        for r in session.execute(stmt)
    ]


@rule(
    "RI-008",
    **INTEGRITY,
    description=(
        "Site attribution of site-scoped records: a SITE-scoped compliance requirement (via "
        "its contract), a site certification, a site capacity declaration, a site contract and "
        "a site risk signal reference a site of their own supplier (D11, D12). HIGH."
    ),
)
def ri_008(session: Session) -> RuleCheck:
    req = ComplianceRequirement
    parts = [
        _site_owner_violations(
            session,
            "compliance_requirements",
            req.requirement_id,
            Contract.supplier_id,
            req.site_id,
            (Contract, Contract.contract_id == req.contract_id),
            where=(req.scope == RequirementScope.SITE,),
        ),
        _site_owner_violations(
            session,
            "supplier_certifications",
            SupplierCertification.certification_id,
            SupplierCertification.supplier_id,
            SupplierCertification.site_id,
        ),
        _site_owner_violations(
            session,
            "capacity_declarations",
            CapacityDeclaration.capacity_declaration_id,
            CapacityDeclaration.supplier_id,
            CapacityDeclaration.site_id,
        ),
        _site_owner_violations(
            session, "contracts", Contract.contract_id, Contract.supplier_id, Contract.site_id
        ),
        _site_owner_violations(
            session,
            "risk_signals",
            RiskSignal.risk_signal_id,
            RiskSignal.supplier_id,
            RiskSignal.site_id,
        ),
    ]
    return RuleCheck(sum(c for c, _ in parts), [v for _, vs in parts for v in vs])


@rule(
    "RI-009",
    **INTEGRITY,
    description=(
        "An allocation (shipment_line) allocates a PO line of the shipment's own PO (D1). HIGH."
    ),
)
def ri_009(session: Session) -> RuleCheck:
    a = ShipmentLineAllocation
    stmt = (
        sa.select(a.shipment_line_id, a.po_line_id, POLine.po_id, Shipment.po_id.label("ship_po"))
        .join(Shipment, Shipment.shipment_id == a.shipment_id)
        .join(POLine, POLine.po_line_id == a.po_line_id)
        .where(POLine.po_id != Shipment.po_id)
        .order_by(a.shipment_line_id)
    )
    violations = [
        Violation(
            "shipment_lines",
            r.shipment_line_id,
            "po_line_id",
            f"{r.po_line_id} (PO {r.po_id})",
            f"a line of the shipment's PO {r.ship_po}",
            f"allocation {r.shipment_line_id} allocates {r.po_line_id} of PO {r.po_id}, but "
            f"its shipment belongs to PO {r.ship_po}",
        )
        for r in session.execute(stmt)
    ]
    return RuleCheck(count(session, a), violations)

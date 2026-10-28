"""Duplicate and semantic rules SE-001..SE-005 (spec section 11; D1, D3, D4, D6).

Severity: SE-004 join multiplication is HIGH (it inflates aggregates); duplicate events and
invoice numbers are MEDIUM (scoring keeps one arrival per shipment, but the data is suspect);
SE-003 repeated risk signals and SE-005 missing performance evidence are LOW (informational:
scoring deduplicates signals, and missing evidence is reported as missing, never as zero).
In every duplicate group the first row (by its id) is the original and each later row is
reported.
"""

from collections import defaultdict
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import ComponentName, DeliveryEventType, Severity
from supplier_reliability.models import (
    DeliveryEvent,
    Invoice,
    PurchaseOrder,
    RiskSignal,
    ScoreEvidence,
    SupplierScore,
)
from supplier_reliability.services.validation.framework import RuleCheck, Violation, rule
from supplier_reliability.services.validation.rules_arithmetic import shipment_values
from supplier_reliability.services.validation.rules_integrity import count

PERFORMANCE_COMPONENTS = (ComponentName.delivery, ComponentName.quality, ComponentName.cost)


@rule(
    "SE-001",
    category="semantic",
    severity=Severity.MEDIUM,
    description=(
        "Duplicate delivery events: two non-CORRECTION events of one shipment with the same "
        "type and event_timestamp. MEDIUM."
    ),
)
def se_001(session: Session) -> RuleCheck:
    ev = DeliveryEvent
    real = ev.event_type != DeliveryEventType.CORRECTION
    key = (ev.shipment_id, ev.event_type, ev.event_timestamp)
    groups = sa.select(*key).where(real).group_by(*key).having(sa.func.count() > 1).subquery()
    stmt = (
        sa.select(ev.delivery_event_id, *key)
        .join(
            groups,
            sa.and_(
                ev.shipment_id == groups.c.shipment_id,
                ev.event_type == groups.c.event_type,
                ev.event_timestamp == groups.c.event_timestamp,
            ),
        )
        .where(real)
        .order_by(*key, ev.delivery_event_id)
    )
    violations, seen = [], set()
    for r in session.execute(stmt):
        group = (r.shipment_id, r.event_type, r.event_timestamp)
        if group in seen:
            violations.append(
                Violation(
                    "delivery_events",
                    r.delivery_event_id,
                    "event_type",
                    f"{r.event_type.value} at {r.event_timestamp.isoformat()}",
                    "one event per shipment, type and timestamp",
                    f"event {r.delivery_event_id} duplicates another {r.event_type.value} event "
                    f"of shipment {r.shipment_id}",
                )
            )
        seen.add(group)
    return RuleCheck(count(session, ev, real), violations)


def normalised_invoice_number(number: str) -> str:
    """Case- and whitespace-insensitive form of a supplier invoice number."""
    return "".join(number.split()).upper()


@rule(
    "SE-002",
    category="semantic",
    severity=Severity.MEDIUM,
    description=(
        "Duplicate supplier invoice references: two invoices of one supplier whose "
        "supplier_invoice_number is equal ignoring case and whitespace (the exact-key duplicate "
        "is also RI-001; the database's unique constraint is exact-match only). MEDIUM."
    ),
)
def se_002(session: Session) -> RuleCheck:
    stmt = sa.select(
        Invoice.invoice_id, Invoice.supplier_id, Invoice.supplier_invoice_number
    ).order_by(Invoice.invoice_id)
    first: dict[tuple[str, str], str] = {}
    checked, violations = 0, []
    for r in session.execute(stmt):
        checked += 1
        key = (r.supplier_id, normalised_invoice_number(r.supplier_invoice_number))
        if key in first:
            violations.append(
                Violation(
                    "invoices",
                    r.invoice_id,
                    "supplier_invoice_number",
                    r.supplier_invoice_number,
                    "unique per supplier (case/whitespace-insensitive)",
                    f"invoice {r.invoice_id} repeats supplier invoice number of {first[key]}",
                )
            )
        else:
            first[key] = r.invoice_id
    return RuleCheck(checked, violations)


@rule(
    "SE-003",
    category="semantic",
    severity=Severity.LOW,
    description=(
        "Repeated risk signals: several signals share a dedup_key (supplier, site, type, "
        "observed week). Informational (LOW): expected, the disruption score keeps the highest "
        "severity per key (D6)."
    ),
)
def se_003(session: Session) -> RuleCheck:
    rs = RiskSignal
    repeated = sa.select(rs.dedup_key).group_by(rs.dedup_key).having(sa.func.count() > 1).subquery()
    stmt = (
        sa.select(rs.risk_signal_id, rs.dedup_key)
        .join(repeated, repeated.c.dedup_key == rs.dedup_key)
        .order_by(rs.dedup_key, rs.risk_signal_id)
    )
    violations, first = [], {}
    for r in session.execute(stmt):
        if r.dedup_key in first:
            violations.append(
                Violation(
                    "risk_signals",
                    r.risk_signal_id,
                    "dedup_key",
                    r.dedup_key,
                    "one signal per dedup_key (handled by scoring deduplication)",
                    f"risk signal {r.risk_signal_id} repeats {first[r.dedup_key]}",
                )
            )
        else:
            first[r.dedup_key] = r.risk_signal_id
    return RuleCheck(count(session, rs), violations)


@rule(
    "SE-004",
    category="semantic",
    severity=Severity.HIGH,
    description=(
        "Join multiplication: per PO, the sum of its invoices' expected_amount equals the sum, "
        "over the distinct invoiced shipments of the PO, of to_money(received x agreed price) "
        "computed independently at the allocation grain (D1, D3). A shipment invoiced twice or "
        "an invoice attached to the wrong PO shows up here. Decimal in Python. HIGH."
    ),
)
def se_004(session: Session) -> RuleCheck:
    values = shipment_values(session)
    invoiced: dict[str, Decimal] = defaultdict(Decimal)
    shipments: dict[str, set[str]] = defaultdict(set)
    stmt = sa.select(Invoice.po_id, Invoice.shipment_id, Invoice.expected_amount)
    for r in session.execute(stmt):
        invoiced[r.po_id] += r.expected_amount
        shipments[r.po_id].add(r.shipment_id)
    pos = sa.select(PurchaseOrder.po_id).where(PurchaseOrder.po_id.in_(sa.select(Invoice.po_id)))
    checked, violations = 0, []
    for (po_id,) in session.execute(pos.order_by(PurchaseOrder.po_id)):
        checked += 1
        independent = sum((values.get(s, Decimal(0)) for s in shipments[po_id]), Decimal(0))
        if invoiced[po_id] != independent:
            violations.append(
                Violation(
                    "purchase_orders",
                    po_id,
                    "invoices.expected_amount",
                    invoiced[po_id],
                    f"= sum over invoiced shipments of received x price = {independent}",
                    f"PO {po_id} invoices total {invoiced[po_id]} but its invoiced shipments are "
                    f"worth {independent}",
                )
            )
    return RuleCheck(checked, violations)


@rule(
    "SE-005",
    category="semantic",
    severity=Severity.LOW,
    description=(
        "Missing performance evidence: a supplier whose current score(s) in the latest scored "
        "window (greatest evaluation_end) include no delivery, quality or cost evidence. LOW "
        "(informational): the score already reports the components as missing."
    ),
)
def se_005(session: Session) -> RuleCheck:
    s, se = SupplierScore, ScoreEvidence
    latest = session.scalar(sa.select(sa.func.max(s.evaluation_end)).where(s.is_current))
    if latest is None:
        return RuleCheck(0)
    with_evidence = sa.exists().where(
        se.score_id == s.score_id,
        se.included.is_(True),
        se.component.in_(PERFORMANCE_COMPONENTS),
    )
    stmt = sa.select(s.supplier_id, s.score_id, with_evidence.label("has_evidence")).where(
        s.is_current.is_(True), s.evaluation_end == latest
    )
    scores: dict[str, list[str]] = defaultdict(list)
    evidenced: set[str] = set()
    for r in session.execute(stmt):
        scores[r.supplier_id].append(r.score_id)
        if r.has_evidence:
            evidenced.add(r.supplier_id)
    violations = [
        Violation(
            "suppliers",
            supplier_id,
            None,
            ", ".join(sorted(scores[supplier_id])),
            "delivery, quality or cost evidence in the latest window",
            f"supplier {supplier_id} has no performance evidence in the window ending "
            f"{latest.isoformat()}",
        )
        for supplier_id in sorted(scores)
        if supplier_id not in evidenced
    ]
    return RuleCheck(len(scores), violations)

"""Arithmetic rules AR-001..AR-007 (spec section 11; D1, D2, D3, D7, D8).

Each rule reads its rows in one or two bulk statements and recomputes in Python on Decimal
(SQLite arithmetic on Numeric is float arithmetic; SQLAlchemy returns the stored values as
Decimals at the column scale, which is exact for the 2-dp and 6-dp columns used here). All are
HIGH, except AR-006 (overall score reconciliation), which is CRITICAL: a published score that
does not follow from its own components is wrong at the point of decision.
"""

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability.domain.enums import ComponentName, InspectionType, Severity
from supplier_reliability.domain.numeric import round_score, to_money
from supplier_reliability.models import (
    Inspection,
    Invoice,
    POLine,
    ScoreEvidence,
    ScoringConfigSnapshot,
    Shipment,
    ShipmentLineAllocation,
    SupplierScore,
)
from supplier_reliability.services.validation.framework import RuleCheck, Violation, rule

_HIGH = {"category": "arithmetic", "severity": Severity.HIGH}
_PCT = Decimal("0.000001")
_FOUR_DP = Decimal("0.0001")
SCORE_TOLERANCE = Decimal("0.01")
COMPONENTS = [c.value for c in ComponentName]


@rule(
    "AR-001",
    **_HIGH,
    description=(
        "PO line totals: line_amount = to_money(ordered_quantity x agreed_unit_price) "
        "(ROUND_HALF_UP, Decimal). HIGH."
    ),
)
def ar_001(session: Session) -> RuleCheck:
    stmt = sa.select(
        POLine.po_line_id, POLine.ordered_quantity, POLine.agreed_unit_price, POLine.line_amount
    ).order_by(POLine.po_line_id)
    checked, violations = 0, []
    for r in session.execute(stmt):
        checked += 1
        expected = to_money(r.ordered_quantity * r.agreed_unit_price)
        if r.line_amount != expected:
            violations.append(
                Violation(
                    "po_lines",
                    r.po_line_id,
                    "line_amount",
                    r.line_amount,
                    f"= {r.ordered_quantity} x {r.agreed_unit_price} = {expected}",
                    f"PO line {r.po_line_id} amount {r.line_amount} != {expected}",
                )
            )
    return RuleCheck(checked, violations)


def _allocation_rows(session: Session) -> list[Any]:
    a = ShipmentLineAllocation
    return list(
        session.execute(
            sa.select(
                a.shipment_line_id,
                a.shipment_id,
                a.po_line_id,
                a.allocated_quantity,
                a.shipped_quantity,
                a.received_quantity,
            )
        )
    )


@rule(
    "AR-002",
    **_HIGH,
    description=(
        "Shipment totals (D1): shipment.shipped_quantity = sum of its allocations' shipped, and "
        "received_quantity = sum of their received. HIGH."
    ),
)
def ar_002(session: Session) -> RuleCheck:
    shipped: dict[str, Decimal] = defaultdict(Decimal)
    received: dict[str, Decimal] = defaultdict(Decimal)
    for a in _allocation_rows(session):
        shipped[a.shipment_id] += a.shipped_quantity
        received[a.shipment_id] += a.received_quantity
    stmt = sa.select(
        Shipment.shipment_id, Shipment.shipped_quantity, Shipment.received_quantity
    ).order_by(Shipment.shipment_id)
    checked, violations = 0, []
    for r in session.execute(stmt):
        checked += 1
        for field, stored, total in (
            ("shipped_quantity", r.shipped_quantity, shipped[r.shipment_id]),
            ("received_quantity", r.received_quantity, received[r.shipment_id]),
        ):
            if stored != total:
                violations.append(
                    Violation(
                        "shipments",
                        r.shipment_id,
                        field,
                        stored,
                        f"= sum over allocations = {total}",
                        f"shipment {r.shipment_id} {field} {stored} != allocations' {total}",
                    )
                )
    return RuleCheck(checked, violations)


@rule(
    "AR-003",
    **_HIGH,
    description=(
        "Allocation reconciliation (D1): per PO line, sum of allocated <= ordered_quantity; per "
        "allocation received <= shipped <= allocated; per shipment received <= shipped. HIGH."
    ),
)
def ar_003(session: Session) -> RuleCheck:
    rows = _allocation_rows(session)
    violations: list[Violation] = []
    allocated: dict[str, Decimal] = defaultdict(Decimal)
    for a in sorted(rows, key=lambda a: a.shipment_line_id):
        allocated[a.po_line_id] += a.allocated_quantity
        if not a.received_quantity <= a.shipped_quantity <= a.allocated_quantity:
            violations.append(
                Violation(
                    "shipment_lines",
                    a.shipment_line_id,
                    "shipped_quantity",
                    f"received {a.received_quantity}, shipped {a.shipped_quantity}, "
                    f"allocated {a.allocated_quantity}",
                    "received <= shipped <= allocated",
                    f"allocation {a.shipment_line_id} quantities are out of order",
                )
            )
    lines = sa.select(POLine.po_line_id, POLine.ordered_quantity).order_by(POLine.po_line_id)
    n_lines = 0
    for r in session.execute(lines):
        n_lines += 1
        total = allocated.get(r.po_line_id, Decimal(0))
        if total > r.ordered_quantity:
            violations.append(
                Violation(
                    "po_lines",
                    r.po_line_id,
                    "ordered_quantity",
                    f"allocated {total}",
                    f"sum of allocated <= ordered {r.ordered_quantity}",
                    f"PO line {r.po_line_id} is allocated {total} of {r.ordered_quantity}",
                )
            )
    shipments = sa.select(
        Shipment.shipment_id, Shipment.shipped_quantity, Shipment.received_quantity
    ).where(Shipment.received_quantity > Shipment.shipped_quantity)
    for r in session.execute(shipments.order_by(Shipment.shipment_id)):
        violations.append(
            Violation(
                "shipments",
                r.shipment_id,
                "received_quantity",
                r.received_quantity,
                f"<= shipped {r.shipped_quantity}",
                f"shipment {r.shipment_id} received more than it shipped",
            )
        )
    return RuleCheck(len(rows) + n_lines, violations)


@rule(
    "AR-004",
    **_HIGH,
    description=(
        "Inspection reconciliation (D2): accepted + rejected = inspected; rejected <= defect <= "
        "inspected; inspected <= the allocation's received_quantity; FULL means inspected = "
        "received and SAMPLE inspected < received. HIGH."
    ),
)
def ar_004(session: Session) -> RuleCheck:
    i, a = Inspection, ShipmentLineAllocation
    stmt = (
        sa.select(
            i.inspection_id,
            i.inspection_type,
            i.inspected_quantity,
            i.accepted_quantity,
            i.rejected_quantity,
            i.defect_quantity,
            a.received_quantity,
        )
        .join(a, a.shipment_line_id == i.shipment_line_id)
        .order_by(i.inspection_id)
    )
    checked, violations = 0, []
    for r in session.execute(stmt):
        checked += 1
        problems = []
        if r.accepted_quantity + r.rejected_quantity != r.inspected_quantity:
            problems.append(("inspected_quantity", "accepted + rejected = inspected"))
        if not r.rejected_quantity <= r.defect_quantity <= r.inspected_quantity:
            problems.append(("defect_quantity", "rejected <= defect <= inspected"))
        if r.inspected_quantity > r.received_quantity:
            problems.append(("inspected_quantity", "inspected <= allocation received"))
        if r.inspection_type == InspectionType.FULL and r.inspected_quantity != r.received_quantity:
            problems.append(("inspection_type", "FULL: inspected = received"))
        if (
            r.inspection_type == InspectionType.SAMPLE
            and r.inspected_quantity >= r.received_quantity
        ):
            problems.append(("inspection_type", "SAMPLE: inspected < received"))
        observed = (
            f"inspected {r.inspected_quantity}, accepted {r.accepted_quantity}, rejected "
            f"{r.rejected_quantity}, defect {r.defect_quantity}, received {r.received_quantity}"
        )
        for field, condition in problems:
            violations.append(
                Violation(
                    "inspections",
                    r.inspection_id,
                    field,
                    observed,
                    condition,
                    f"inspection {r.inspection_id} violates {condition}",
                )
            )
    return RuleCheck(checked, violations)


def shipment_values(session: Session) -> dict[str, Decimal]:
    """shipment_id -> to_money(sum of allocation received x agreed unit price) (D3)."""
    a = ShipmentLineAllocation
    stmt = sa.select(a.shipment_id, a.received_quantity, POLine.agreed_unit_price).join(
        POLine, POLine.po_line_id == a.po_line_id
    )
    raw: dict[str, Decimal] = defaultdict(Decimal)
    for r in session.execute(stmt):
        raw[r.shipment_id] += r.received_quantity * r.agreed_unit_price
    return {k: to_money(v) for k, v in raw.items()}


@rule(
    "AR-005",
    **_HIGH,
    description=(
        "Invoice arithmetic (D3): expected_amount = to_money(sum over the shipment's "
        "allocations of received x agreed price); variance_amount = invoiced - expected; "
        "variance_percentage = variance / expected quantized to 6 dp (HALF_UP), NULL iff "
        "expected = 0. HIGH."
    ),
)
def ar_005(session: Session) -> RuleCheck:
    values = shipment_values(session)
    inv = Invoice
    stmt = sa.select(
        inv.invoice_id,
        inv.shipment_id,
        inv.invoiced_amount,
        inv.expected_amount,
        inv.variance_amount,
        inv.variance_percentage,
    ).order_by(inv.invoice_id)
    checked, violations = 0, []
    for r in session.execute(stmt):
        checked += 1
        expected = values.get(r.shipment_id, Decimal("0.00"))
        variance = r.invoiced_amount - r.expected_amount
        pct = (
            None
            if r.expected_amount == 0
            else (variance / r.expected_amount).quantize(_PCT, rounding=ROUND_HALF_UP)
        )
        for field, stored, want in (
            ("expected_amount", r.expected_amount, expected),
            ("variance_amount", r.variance_amount, variance),
            ("variance_percentage", r.variance_percentage, pct),
        ):
            if stored != want:
                violations.append(
                    Violation(
                        "invoices",
                        r.invoice_id,
                        field,
                        stored,
                        f"= {want}",
                        f"invoice {r.invoice_id} {field} {stored} != recomputed {want}",
                    )
                )
    return RuleCheck(checked, violations)


def _config_weights(session: Session) -> dict[str, dict[str, Decimal]]:
    out = {}
    for h, config in session.execute(
        sa.select(ScoringConfigSnapshot.config_hash, ScoringConfigSnapshot.config)
    ):
        out[h] = {k: Decimal(str(v)) for k, v in config["weights"].items()}
    return out


def recompute_overall(
    components: dict[str, Decimal], weights: dict[str, Decimal]
) -> Decimal | None:
    """D7: round2(sum w_k c_k / sum w_k) over the available components."""
    if not components:
        return None
    total = sum((weights[k] for k in components), Decimal(0))
    return round_score(sum((weights[k] * c for k, c in components.items()), Decimal(0)) / total)


@rule(
    "AR-006",
    category="arithmetic",
    severity=Severity.CRITICAL,
    description=(
        "Overall score reconciliation (D7): overall_score = round_half_up(sum w_k c_k / sum w_k, "
        "2) over the non-null stored components (+/- 0.01), with the weights of the score's "
        "config snapshot (exact; the 4-dp effective_weights JSON alone can drift by ~0.01); the "
        "effective_weights keys are exactly the non-null components and each value equals "
        "w_k / sum w quantized to 4 dp. Without a snapshot the effective weights are used. "
        "CRITICAL."
    ),
)
def ar_006(session: Session) -> RuleCheck:
    s = SupplierScore
    snapshots = _config_weights(session)
    stmt = sa.select(
        s.score_id,
        s.overall_score,
        s.effective_weights,
        s.config_hash,
        *[getattr(s, f"{c}_score").label(c) for c in COMPONENTS],
    ).order_by(s.score_id)
    checked, violations = 0, []
    for r in session.execute(stmt):
        checked += 1
        comps = {c: getattr(r, c) for c in COMPONENTS if getattr(r, c) is not None}
        stored_ew = {k: Decimal(str(v)) for k, v in (r.effective_weights or {}).items()}
        if set(stored_ew) != set(comps):
            violations.append(
                Violation(
                    "supplier_scores",
                    r.score_id,
                    "effective_weights",
                    sorted(stored_ew),
                    f"keys = available components {sorted(comps)}",
                    f"score {r.score_id} effective weights do not match its components",
                )
            )
            continue
        weights = snapshots.get(r.config_hash, stored_ew)
        if r.config_hash in snapshots and comps:
            total = sum((weights[k] for k in comps), Decimal(0))
            drift = {
                k: v
                for k, v in stored_ew.items()
                if v != (weights[k] / total).quantize(_FOUR_DP, rounding=ROUND_HALF_UP)
            }
            if drift:
                violations.append(
                    Violation(
                        "supplier_scores",
                        r.score_id,
                        "effective_weights",
                        {k: str(v) for k, v in sorted(drift.items())},
                        "w_k / sum of available weights, 4 dp",
                        f"score {r.score_id} effective weights disagree with its config",
                    )
                )
        recomputed = recompute_overall(comps, weights)
        overall = r.overall_score
        bad = (overall is None) != (recomputed is None) or (
            overall is not None and abs(overall - recomputed) > SCORE_TOLERANCE
        )
        if bad:
            violations.append(
                Violation(
                    "supplier_scores",
                    r.score_id,
                    "overall_score",
                    overall,
                    f"= {recomputed} (+/- {SCORE_TOLERANCE})",
                    f"score {r.score_id} overall {overall} does not reconcile with its "
                    f"components ({recomputed})",
                )
            )
    return RuleCheck(checked, violations)


@rule(
    "AR-007",
    **_HIGH,
    description=(
        "Lineage reconciliation (D8): per score and component, the INCLUDED score_evidence rows "
        "equal the explanation's observation count (delivery: shipment rows only, events are "
        "lineage too; capacity counts months, so it is compared with its listed "
        "source_record_ids + truncated_count). Explanation values are extracted in SQL (JSON "
        "paths) so whole explanations are never loaded. HIGH."
    ),
)
def ar_007(session: Session) -> RuleCheck:
    se, s = ScoreEvidence, SupplierScore
    counts: dict[tuple[str, str], int] = defaultdict(int)
    shipments: dict[str, int] = defaultdict(int)
    stmt = (
        sa.select(se.score_id, se.component, se.source_entity_type, sa.func.count())
        .where(se.included.is_(True))
        .group_by(se.score_id, se.component, se.source_entity_type)
    )
    for score_id, component, entity, n in session.execute(stmt):
        counts[(score_id, ComponentName(component).value)] += n
        if component == ComponentName.delivery and entity == "shipment":
            shipments[score_id] += n

    def path(*keys: str) -> Any:
        return s.explanation[("components", *keys)]

    columns = [s.score_id]
    for c in COMPONENTS:
        columns.append(path(c, "observations").as_integer().label(f"{c}_obs"))
    columns += [
        path("capacity", "source_record_ids").label("capacity_ids"),
        path("capacity", "truncated_count").as_integer().label("capacity_truncated"),
    ]
    checked, violations = 0, []
    for r in session.execute(sa.select(*columns).order_by(s.score_id)):
        for c in COMPONENTS:
            checked += 1
            if c == "capacity":
                expected = len(r.capacity_ids or []) + (r.capacity_truncated or 0)
                actual = counts[(r.score_id, c)]
                what = "listed source records"
            elif c == "delivery":
                expected, actual = getattr(r, f"{c}_obs"), shipments[r.score_id]
                what = "observations (shipments)"
            else:
                expected, actual = getattr(r, f"{c}_obs"), counts[(r.score_id, c)]
                what = "observations"
            if actual != expected:
                violations.append(
                    Violation(
                        "supplier_scores",
                        r.score_id,
                        c,
                        f"{actual} included lineage rows",
                        f"= explanation {what} {expected}",
                        f"score {r.score_id} {c}: {actual} included evidence rows but the "
                        f"explanation reports {expected}",
                    )
                )
    return RuleCheck(checked, violations)

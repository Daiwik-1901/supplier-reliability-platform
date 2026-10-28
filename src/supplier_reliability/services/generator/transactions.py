"""Deterministic purchase orders, PO lines, shipments, shipment-line allocations (D1) and
delivery events (D4, via ``transactions_events``).

Streams: "orders" (PO counts, dates, cancellations), "po_lines" (line counts, products,
quantities), "shipments" (shipment counts, line coverage, timelines, sites), "allocations"
(quantity splits, short-shipping, losses), "events" (recording lags), "corrections".

Decisions (documented assumptions):
- Line quantities follow the shared demand model in ``constants.py`` (1 + Binomial(4, 0.4)
  lines per PO, mean 2.6; lognormal quantity with mean MEAN_QTY_PER_LINE), so realised demand
  matches the capacity sizing done by master data. Quantities are whole units.
- A PO uses the supplier's contract in force at ``order_date``; its currency is the contract's,
  and each line's ``agreed_unit_price`` is that contract's price for the line's product.
- Every allocation carries >= 1 unit: a line is split over at most its quantity in shipments,
  and a PO has at most its total ordered units in shipments (``cap_shipment_counts`` keeps the
  shipment total exact). Short-shipping always leaves shipped >= 1.
- Every non-cancelled PO line is fully allocated (sum allocated = ordered). Shortfalls come
  from short-shipping (~3% of allocations) and rare in-transit losses, which leaves ~4% of
  fulfilled PO lines short.
- Shipment status: IN_TRANSIT (no ARRIVED by reference_end), DELIVERED (received = shipped) or
  PARTIALLY_RECEIVED (loss). BOOKED/CANCELLED are not generated: every shipment is dispatched
  inside the period. PO status: CANCELLED (no shipments, ~2%), OPEN (nothing arrived yet),
  RECEIVED (everything arrived and every line received in full), otherwise
  PARTIALLY_RECEIVED. CLOSED is not generated.
- Dispatch site: an ACTIVE site of the supplier that is effective on the dispatch date.
- Corrected arrivals: see ``transactions_events`` (shipment row holds the corrected value).
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np

from supplier_reliability.domain.enums import (
    DeliveryEventType,
    POStatus,
    ShipmentStatus,
    SiteStatus,
    SupplierCategory,
)
from supplier_reliability.domain.numeric import to_money
from supplier_reliability.services.generator.config import GeneratorCapacityError, GeneratorConfig
from supplier_reliability.services.generator.constants import (
    MEAN_LINES_PER_PO,
    MEAN_QTY_PER_LINE,
    PRODUCT_CATEGORIES,
    QTY_LOGNORMAL_SIGMA,
)
from supplier_reliability.services.generator.dataset import GeneratedDataset
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.profiles import allocate_exact, exact_subset
from supplier_reliability.services.generator.rng import RandomSource
from supplier_reliability.services.generator.transactions_events import (
    apply_corrections,
    draw_timeline,
    emit_events,
)

__all__ = ["generate_orders_and_shipments"]

_LAST_ORDER_GAP_DAYS = 10
_CANCEL_FRACTION = 0.02
_SHORT_SHIP_FRACTION = 0.03
_LOSS_P = 0.005
_EXTRA_SPLIT_P = 0.25
_MAX_SHIPMENTS_PER_PO = 4
_MAX_LINES_PER_PO = 5
_LINE_P = (MEAN_LINES_PER_PO - 1) / (_MAX_LINES_PER_PO - 1)  # 1 + Binomial(4, p): mean 2.6
_QTY_MU = math.log(float(MEAN_QTY_PER_LINE)) - QTY_LOGNORMAL_SIGMA**2 / 2
# Order-to-required lead time in days, by supplier category (all within 14-45).
_LEAD_DAYS: dict[SupplierCategory, tuple[int, int]] = {
    SupplierCategory.RAW_MATERIALS: (21, 45),
    SupplierCategory.ELECTRONICS: (28, 45),
    SupplierCategory.PACKAGING: (14, 28),
    SupplierCategory.CHEMICALS: (21, 40),
    SupplierCategory.MACHINED_PARTS: (21, 45),
    SupplierCategory.LOGISTICS_SERVICES: (14, 21),
}
_WAREHOUSES = tuple(f"WH-SYN-{i:02d}" for i in range(1, 7))
_CARRIERS = tuple(f"CARRIER-SYN-{i:02d}" for i in range(1, 13))


@dataclass
class _ShipmentPlan:
    po: dict[str, Any]
    index: int
    n_shipments: int
    allocations: list[dict[str, Any]] = field(default_factory=list)


def _utc(d: date, minutes: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(minutes=minutes)


def generate_orders_and_shipments(
    cfg: GeneratorConfig, rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset
) -> None:
    lines_by_po = _gen_orders(cfg, rs, ids, ds)
    plans = _plan_shipments(cfg, rs, ds, lines_by_po)
    _gen_shipments(cfg, rs, ids, ds, plans)
    _derive_po_status(ds, lines_by_po)


def _contract_at(contracts: list[dict[str, Any]], d: date) -> dict[str, Any]:
    for c in contracts:
        if c["effective_from"] <= d and (c["effective_to"] is None or d <= c["effective_to"]):
            return c
    raise GeneratorCapacityError(f"no contract in force on {d} for {contracts[0]['supplier_id']}")


def _gen_orders(
    cfg: GeneratorConfig, rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset
) -> dict[str, list[dict[str, Any]]]:
    rng = rs.stream("orders")
    weights = np.array(
        [cfg.profiles[ds.supplier_profiles[s["supplier_id"]]].po_weight for s in ds.suppliers]
    )
    counts = allocate_exact(cfg.n_purchase_orders, weights, 0, rng)
    first = cfg.reference_start.date()
    span = (cfg.reference_end.date() - timedelta(days=_LAST_ORDER_GAP_DAYS) - first).days
    drafts: list[tuple[date, str, int, int, SupplierCategory]] = []
    for sup, n in zip(ds.suppliers, counts, strict=True):
        lo, hi = _LEAD_DAYS[sup["supplier_category"]]
        for _ in range(int(n)):
            order_date = first + timedelta(days=int(rng.integers(0, span + 1)))
            lead = int(rng.integers(lo, hi + 1))
            minute = int(rng.integers(8 * 60, 17 * 60))
            drafts.append((order_date, sup["supplier_id"], lead, minute, sup["supplier_category"]))
    drafts.sort(key=lambda d: (d[0], d[1]))  # stable: ties keep generation order
    cancelled = set(exact_subset(rng, len(drafts), round(_CANCEL_FRACTION * len(drafts))))

    contracts: dict[str, list[dict[str, Any]]] = {}
    for c in ds.contracts:
        contracts.setdefault(c["supplier_id"], []).append(c)
    prices = {(p["contract_id"], p["product_category"]): p for p in ds.contract_prices}
    line_rng = rs.stream("po_lines")
    lines_by_po: dict[str, list[dict[str, Any]]] = {}
    for i, (order_date, sid, lead, minute, category) in enumerate(drafts):
        contract = _contract_at(contracts[sid], order_date)
        created = _utc(order_date, minute)
        po_id = ids.next("po")
        ds.purchase_orders.append(
            {
                "po_id": po_id,
                "supplier_id": sid,
                "contract_id": contract["contract_id"],
                "order_date": order_date,
                "required_delivery_date": order_date + timedelta(days=lead),
                "po_status": POStatus.CANCELLED if i in cancelled else POStatus.OPEN,
                "currency": contract["currency"],
                "created_at": created,
            }
        )
        products = PRODUCT_CATEGORIES[category]
        n_lines = 1 + int(line_rng.binomial(_MAX_LINES_PER_PO - 1, _LINE_P))
        rows = []
        for number in range(1, n_lines + 1):
            product, uom, _ = products[int(line_rng.integers(len(products)))]
            qty = Decimal(max(1, round(float(line_rng.lognormal(_QTY_MU, QTY_LOGNORMAL_SIGMA)))))
            price = prices[(contract["contract_id"], product)]["unit_price"]
            row = {
                "po_line_id": ids.next("po_line"),
                "po_id": po_id,
                "line_number": number,
                "product_category": product,
                "ordered_quantity": qty,
                "unit_of_measure": uom,
                "agreed_unit_price": price,
                "line_amount": to_money(qty * price),
                "created_at": created,
            }
            rows.append(row)
        ds.po_lines.extend(rows)
        lines_by_po[po_id] = rows
    return lines_by_po


def _plan_shipments(
    cfg: GeneratorConfig,
    rs: RandomSource,
    ds: GeneratedDataset,
    lines_by_po: dict[str, list[dict[str, Any]]],
) -> list[_ShipmentPlan]:
    """Shipment counts per PO, which lines each shipment carries and the allocated split."""
    rng = rs.stream("shipments")
    alloc_rng = rs.stream("allocations")
    open_pos = [p for p in ds.purchase_orders if p["po_status"] != POStatus.CANCELLED]
    weights = np.array([len(lines_by_po[p["po_id"]]) for p in open_pos], dtype=float)
    counts = allocate_exact(cfg.n_shipments, weights, 1, rng, maximum=_MAX_SHIPMENTS_PER_PO)
    # A PO cannot have more shipments than units ordered (each allocation carries >= 1 unit).
    caps = np.array(
        [
            min(
                _MAX_SHIPMENTS_PER_PO,
                int(sum(x["ordered_quantity"] for x in lines_by_po[p["po_id"]])),
            )
            for p in open_pos
        ]
    )
    counts = cap_shipment_counts(counts, caps, rng)
    plans: list[_ShipmentPlan] = []
    for po, k in zip(open_pos, counts, strict=True):
        k = int(k)
        lines = lines_by_po[po["po_id"]]
        members = plan_line_memberships(rng, [int(x["ordered_quantity"]) for x in lines], k)
        po_plans = [_ShipmentPlan(po, j, k) for j in range(k)]
        for line, m in zip(lines, members, strict=True):
            qty = int(line["ordered_quantity"])
            split = allocate_exact(qty, alloc_rng.dirichlet(np.full(len(m), 2.0)), 1, alloc_rng)
            for j, q in zip(m, split, strict=True):
                po_plans[j].allocations.append(
                    {
                        "po_line_id": line["po_line_id"],
                        "unit_of_measure": line["unit_of_measure"],
                        "allocated_quantity": Decimal(int(q)),
                        "shipped_quantity": Decimal(int(q)),
                    }
                )
        for p in po_plans:  # stable line order within a shipment
            p.allocations.sort(key=lambda a: a["po_line_id"])
        plans.extend(po_plans)

    every = [a for p in plans for a in p.allocations]
    k_short = round(_SHORT_SHIP_FRACTION * len(every))
    # Only allocations of >= 2 units can be short-shipped: shipped stays >= 1 (no allocation is
    # fully short-shipped).
    shortable = [a for a in every if a["allocated_quantity"] >= 2]
    for pos in exact_subset(alloc_rng, len(shortable), min(k_short, len(shortable))):
        a = shortable[pos]
        allocated = int(a["allocated_quantity"])
        cut = max(1, int(allocated * float(alloc_rng.uniform(0.05, 0.30))))
        a["shipped_quantity"] = Decimal(allocated - min(cut, allocated - 1))
    return plans


def cap_shipment_counts(
    counts: np.ndarray, caps: np.ndarray, rng: np.random.Generator
) -> np.ndarray:
    """Clip per-PO shipment counts to ``caps`` and hand the excess, one at a time, to random POs
    with room, so the total stays exact. Draws nothing when no PO exceeds its cap."""
    out = np.minimum(counts, caps).astype(np.int64)
    excess = int(counts.sum() - out.sum())
    while excess > 0:
        room = np.flatnonzero(out < caps)
        if len(room) == 0:
            raise GeneratorCapacityError("not enough ordered units for the shipment count")
        out[room[int(rng.integers(len(room)))]] += 1
        excess -= 1
    return out


def plan_line_memberships(
    rng: np.random.Generator, quantities: list[int], k: int
) -> list[list[int]]:
    """Which of the PO's ``k`` shipments carry each line (sorted shipment indexes per line).

    Every line is on >= 1 and <= its quantity shipments (so each allocation gets >= 1 unit),
    every shipment carries >= 1 line, and ~25% of lines split over one more shipment.
    Requires k <= sum(quantities).
    """
    n = len(quantities)
    cap = [min(q, k) for q in quantities]
    members: list[list[int]] = [[] for _ in quantities]
    if n >= k:  # the first k lines (random order) each open their own shipment
        for pos, li in enumerate(rng.permutation(n)):
            members[int(li)].append(pos if pos < k else int(rng.integers(k)))
    else:  # one shipment per line, then fill the remaining shipments from lines with spare units
        order = [int(j) for j in rng.permutation(k)]
        for li in range(n):
            members[li].append(order[li])
        for j in order[n:]:
            spare = [li for li in range(n) if len(members[li]) < cap[li]]
            if not spare:
                raise GeneratorCapacityError("shipments exceed the PO's ordered units")
            members[spare[int(rng.integers(len(spare)))]].append(j)
    for li, m in enumerate(members):
        if len(m) < cap[li] and float(rng.random()) < _EXTRA_SPLIT_P:
            rest = [j for j in range(k) if j not in m]
            m.append(rest[int(rng.integers(len(rest)))])
        m.sort()
    return members


def _gen_shipments(
    cfg: GeneratorConfig,
    rs: RandomSource,
    ids: IdRegistry,
    ds: GeneratedDataset,
    plans: list[_ShipmentPlan],
) -> None:
    rng = rs.stream("shipments")
    alloc_rng = rs.stream("allocations")
    event_rng = rs.stream("events")
    ref_start, ref_end = cfg.reference_start, cfg.reference_end
    grace = timedelta(hours=cfg.grace_hours)
    sites: dict[str, list[dict[str, Any]]] = {}
    for s in ds.sites:
        if s["site_status"] == SiteStatus.ACTIVE:
            sites.setdefault(s["supplier_id"], []).append(s)
    delivered = []
    for plan in plans:
        po = plan.po
        profile = cfg.profiles[ds.supplier_profiles[po["supplier_id"]]]
        tl = draw_timeline(
            rng, po, plan.index, plan.n_shipments, profile, ref_start, ref_end, grace
        )
        d = tl.dispatch.date()
        eligible = [
            s
            for s in sites[po["supplier_id"]]
            if s["effective_from"] <= d and (s["effective_to"] is None or d <= s["effective_to"])
        ]
        if not eligible:
            raise GeneratorCapacityError(f"no effective site for {po['supplier_id']} on {d}")
        site = eligible[int(rng.integers(len(eligible)))]
        shipment = {
            "shipment_id": ids.next("shipment"),
            "po_id": po["po_id"],
            "originating_site_id": site["site_id"],
            "destination_warehouse_id": _WAREHOUSES[int(rng.integers(len(_WAREHOUSES)))],
            "carrier_id": _CARRIERS[int(rng.integers(len(_CARRIERS)))],
            "dispatch_timestamp": tl.dispatch,
            "expected_delivery_timestamp": tl.expected,
            "actual_delivery_timestamp": tl.arrival,
            "shipment_status": ShipmentStatus.IN_TRANSIT,
            "shipped_quantity": Decimal(0),
            "received_quantity": Decimal(0),
            "created_at": tl.booking,
        }
        events = emit_events(event_rng, ids, ds, shipment, tl, ref_end)
        received_at = events[DeliveryEventType.RECEIVED]["event_timestamp"] if tl.arrival else None
        for a in plan.allocations:
            shipped = a["shipped_quantity"]
            received = Decimal(0)
            if received_at is not None:
                received = shipped
                if shipped > 0 and float(alloc_rng.random()) < _LOSS_P:
                    lost = max(1, int(int(shipped) * float(alloc_rng.uniform(0.01, 0.05))))
                    received = shipped - lost
            row = {
                "shipment_line_id": ids.next("shipment_line"),
                "shipment_id": shipment["shipment_id"],
                "po_line_id": a["po_line_id"],
                "unit_of_measure": a["unit_of_measure"],
                "allocated_quantity": a["allocated_quantity"],
                "shipped_quantity": shipped,
                "received_quantity": received,
                "allocated_at": tl.booking,
                "shipped_at": tl.dispatch,
                "received_at": received_at,
                "created_at": tl.booking,
            }
            ds.shipment_lines.append(row)
            shipment["shipped_quantity"] += shipped
            shipment["received_quantity"] += received
        if tl.arrival is not None:
            full = shipment["received_quantity"] == shipment["shipped_quantity"]
            shipment["shipment_status"] = (
                ShipmentStatus.DELIVERED if full else ShipmentStatus.PARTIALLY_RECEIVED
            )
            delivered.append((shipment, events))
        ds.shipments.append(shipment)
    apply_corrections(rs.stream("corrections"), ids, ds, delivered, ref_end)


def _derive_po_status(ds: GeneratedDataset, lines_by_po: dict[str, list[dict[str, Any]]]) -> None:
    received: dict[str, Decimal] = {}
    for a in ds.shipment_lines:
        received[a["po_line_id"]] = (
            received.get(a["po_line_id"], Decimal(0)) + a["received_quantity"]
        )
    arrived: dict[str, list[bool]] = {}
    for s in ds.shipments:
        arrived.setdefault(s["po_id"], []).append(s["actual_delivery_timestamp"] is not None)
    for po in ds.purchase_orders:
        if po["po_status"] == POStatus.CANCELLED:
            continue
        flags = arrived[po["po_id"]]
        short = any(
            received.get(x["po_line_id"], Decimal(0)) < x["ordered_quantity"]
            for x in lines_by_po[po["po_id"]]
        )
        if not any(flags):
            po["po_status"] = POStatus.OPEN
        elif all(flags) and not short:
            po["po_status"] = POStatus.RECEIVED
        else:
            po["po_status"] = POStatus.PARTIALLY_RECEIVED

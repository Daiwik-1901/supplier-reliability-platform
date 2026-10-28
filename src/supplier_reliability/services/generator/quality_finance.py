"""Deterministic inspections (D2) and invoices (D3).

Streams: "inspections" (target choice, types, quantities, defects, timestamps), "invoices"
(uninvoiced tail, dates, variances).

Inspections (documented assumptions):
- Targets are allocations (shipment_lines) of DELIVERED / PARTIALLY_RECEIVED shipments with
  received_quantity >= 1. Exactly ``n_inspections`` rows: round(3%) REINSPECTIONs, the rest
  INITIAL (one INITIAL per line, an exact random subset of the targets).
- ~20% of INITIAL inspections of lines with received >= 2 are SAMPLE (1 <= inspected <
  received, 10-50% of received); every other inspection is FULL (inspected = received).
  Re-inspections are FULL.
- defect ~ Binomial(inspected, profile.defect_mean at the inspection's period fraction);
  rejected ~ Binomial(defect, 0.85) (minor defects can be accepted under concession);
  accepted = inspected - rejected. Status by defect rate (defect / inspected): FAILED
  > 5%, CONDITIONAL > 2%, otherwise PASSED. defect_category is NONE iff defect = 0.
- inspection_timestamp = ORIGINAL ARRIVED event timestamp + 0-3 days (whole minutes), capped
  at reference_end. The ARRIVED event, not ``Shipment.actual_delivery_timestamp`` (which holds
  a corrected value for ~1% of shipments, ruling R12). recorded_at = inspection + 0-4 h, or
  1-7 days for ~5%, capped at reference_end.
- Re-inspections go preferentially to lines whose INITIAL found defects, among lines whose
  INITIAL is at least 1 h before reference_end; timestamp = INITIAL + 1 h-7 days (capped), so it
  is strictly later.
- Inspection ids are assigned in (inspection_timestamp, shipment_line_id, kind) order.

Invoices (documented assumptions):
- One invoice per delivered shipment, except the round(3%) delivered shipments with the latest
  RECEIVED events, which are still un-invoiced at reference_end.
- expected = sum(received x agreed_unit_price) over the shipment's allocations, quantised.
  With probability ``invoice_variance_p`` an invoice is off by m = magnitude x U(0.5, 1.5),
  overbilling with p 0.8, underbilling otherwise: invoiced = to_money(expected x (1 +/- m)).
- invoice_date (a DATE) is the RECEIVED event's UTC date + 0-14 days, never after the
  reference_end date. "invoice_date >= RECEIVED" compares dates. recorded_at (= created_at) is
  a datetime >= max(invoice_date 00:00 UTC, RECEIVED event time), <= reference_end.
- Status: DISPUTED if |variance_percentage| > 5%; else PAID if the invoice is > 60 days old at
  reference_end, APPROVED if > 14 days, otherwise RECEIVED.
- supplier_invoice_number = "SYN-<supplier digits>-<per-supplier counter:05d>". Invoice ids are
  assigned in (invoice_date, shipment_id) order.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np

from supplier_reliability.domain.enums import (
    DefectCategory,
    DeliveryEventType,
    InspectionKind,
    InspectionStatus,
    InspectionType,
    InvoiceStatus,
    ShipmentStatus,
)
from supplier_reliability.domain.numeric import to_money
from supplier_reliability.services.generator.config import GeneratorCapacityError, GeneratorConfig
from supplier_reliability.services.generator.dataset import GeneratedDataset
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.profiles import exact_subset
from supplier_reliability.services.generator.rng import RandomSource

__all__ = ["generate_inspections_and_invoices"]

_REINSPECTION_FRACTION = 0.03
_SAMPLE_P = 0.20
_REJECT_P = 0.85
_FAILED_DEFECT_RATE = Decimal("0.05")
_CONDITIONAL_DEFECT_RATE = Decimal("0.02")
_LATE_RECORDING_P = 0.05
_UNINVOICED_FRACTION = 0.03
_OVERBILL_P = 0.8
_DISPUTE_THRESHOLD = Decimal("0.05")
_MAX_INVOICE_LAG_DAYS = 14
_PCT = Decimal("0.000001")
_DEFECT_CATEGORIES = (
    DefectCategory.DIMENSIONAL,
    DefectCategory.COSMETIC,
    DefectCategory.FUNCTIONAL,
    DefectCategory.PACKAGING,
    DefectCategory.DOCUMENTATION,
)
_DEFECT_CATEGORY_P = (0.30, 0.25, 0.20, 0.15, 0.10)
_DELIVERED = (ShipmentStatus.DELIVERED, ShipmentStatus.PARTIALLY_RECEIVED)
_KIND_ORDER = {InspectionKind.INITIAL: 0, InspectionKind.REINSPECTION: 1}


def _minutes(rng: np.random.Generator, lo: int, hi: int) -> timedelta:
    return timedelta(minutes=int(rng.integers(lo, hi + 1)))


def _fraction(cfg: GeneratorConfig, at: datetime) -> float:
    span = cfg.reference_end - cfg.reference_start
    return min(1.0, max(0.0, (at - cfg.reference_start) / span))


def _event_times(ds: GeneratedDataset, etype: DeliveryEventType) -> dict[str, datetime]:
    return {
        e["shipment_id"]: e["event_timestamp"]
        for e in ds.delivery_events
        if e["event_type"] == etype
    }


def generate_inspections_and_invoices(
    cfg: GeneratorConfig, rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset
) -> None:
    po_supplier = {p["po_id"]: p["supplier_id"] for p in ds.purchase_orders}
    delivered = [s for s in ds.shipments if s["shipment_status"] in _DELIVERED]
    supplier_of = {s["shipment_id"]: po_supplier[s["po_id"]] for s in ds.shipments}
    _gen_inspections(cfg, rs.stream("inspections"), ids, ds, delivered, supplier_of)
    _gen_invoices(cfg, rs.stream("invoices"), ids, ds, delivered)


# --- inspections ----------------------------------------------------------------------------


def _record_at(rng: np.random.Generator, at: datetime, ref_end: datetime) -> datetime:
    if float(rng.random()) < _LATE_RECORDING_P:
        lag = _minutes(rng, 1440, 7 * 1440)
    else:
        lag = _minutes(rng, 0, 4 * 60)
    return min(at + lag, ref_end)


def _inspect(
    cfg: GeneratorConfig,
    rng: np.random.Generator,
    line: dict[str, Any],
    profile_name: str,
    kind: InspectionKind,
    at: datetime,
) -> dict[str, Any]:
    received = int(line["received_quantity"])
    sample = kind == InspectionKind.INITIAL and received >= 2 and float(rng.random()) < _SAMPLE_P
    if sample:
        inspected = max(1, min(received - 1, round(received * float(rng.uniform(0.1, 0.5)))))
    else:
        inspected = received
    p = cfg.profiles[profile_name].at("defect_mean", _fraction(cfg, at))
    defect = int(rng.binomial(inspected, p))
    rejected = int(rng.binomial(defect, _REJECT_P)) if defect else 0
    if defect == 0:
        status, category = InspectionStatus.PASSED, DefectCategory.NONE
    else:
        rate = Decimal(defect) / Decimal(inspected)
        if rate > _FAILED_DEFECT_RATE:
            status = InspectionStatus.FAILED
        elif rate > _CONDITIONAL_DEFECT_RATE:
            status = InspectionStatus.CONDITIONAL
        else:
            status = InspectionStatus.PASSED
        category = _DEFECT_CATEGORIES[
            int(rng.choice(len(_DEFECT_CATEGORIES), p=_DEFECT_CATEGORY_P))
        ]
    return {
        "inspection_id": None,  # assigned after sorting
        "shipment_line_id": line["shipment_line_id"],
        "inspection_kind": kind,
        "inspection_type": InspectionType.SAMPLE if sample else InspectionType.FULL,
        "inspection_timestamp": at,
        "recorded_at": _record_at(rng, at, cfg.reference_end),
        "inspected_quantity": Decimal(inspected),
        "accepted_quantity": Decimal(inspected - rejected),
        "rejected_quantity": Decimal(rejected),
        "defect_quantity": Decimal(defect),
        "inspection_status": status,
        "defect_category": category,
        "inspection_source": "SYNTHETIC_QMS",
    }


def _gen_inspections(
    cfg: GeneratorConfig,
    rng: np.random.Generator,
    ids: IdRegistry,
    ds: GeneratedDataset,
    delivered: list[dict[str, Any]],
    supplier_of: dict[str, str],
) -> None:
    ref_end = cfg.reference_end
    arrived = _event_times(ds, DeliveryEventType.ARRIVED)  # original events (R12)
    delivered_ids = {s["shipment_id"] for s in delivered}
    targets = [
        a
        for a in ds.shipment_lines
        if a["shipment_id"] in delivered_ids and a["received_quantity"] >= 1
    ]
    n_re = round(_REINSPECTION_FRACTION * cfg.n_inspections)
    n_initial = cfg.n_inspections - n_re
    if n_initial > len(targets):
        raise GeneratorCapacityError(
            f"{n_initial} initial inspections requested but only {len(targets)} received lines"
        )
    initial: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pos in exact_subset(rng, len(targets), n_initial):
        line = targets[pos]
        sid = line["shipment_id"]
        at = min(arrived[sid] + _minutes(rng, 0, 3 * 1440), ref_end)
        row = _inspect(
            cfg, rng, line, ds.supplier_profiles[supplier_of[sid]], InspectionKind.INITIAL, at
        )
        initial.append((line, row))

    roomy = [
        (ln, r) for ln, r in initial if r["inspection_timestamp"] <= ref_end - timedelta(hours=1)
    ]
    defective = [x for x in roomy if x[1]["defect_quantity"] > 0]
    clean = [x for x in roomy if x[1]["defect_quantity"] == 0]
    if n_re > len(roomy):
        raise GeneratorCapacityError(f"{n_re} re-inspections requested, {len(roomy)} candidates")
    if n_re <= len(defective):
        chosen = [defective[i] for i in exact_subset(rng, len(defective), n_re)]
    else:
        rest = exact_subset(rng, len(clean), n_re - len(defective))
        chosen = defective + [clean[i] for i in rest]
    rows = [r for _, r in initial]
    for line, first in chosen:
        sid = line["shipment_id"]
        at = min(first["inspection_timestamp"] + _minutes(rng, 60, 7 * 1440), ref_end)
        rows.append(
            _inspect(
                cfg,
                rng,
                line,
                ds.supplier_profiles[supplier_of[sid]],
                InspectionKind.REINSPECTION,
                at,
            )
        )
    rows.sort(
        key=lambda r: (
            r["inspection_timestamp"],
            r["shipment_line_id"],
            _KIND_ORDER[r["inspection_kind"]],
        )
    )
    for r in rows:
        r["inspection_id"] = ids.next("inspection")
    ds.inspections.extend(rows)


# --- invoices -------------------------------------------------------------------------------


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _gen_invoices(
    cfg: GeneratorConfig,
    rng: np.random.Generator,
    ids: IdRegistry,
    ds: GeneratedDataset,
    delivered: list[dict[str, Any]],
) -> None:
    ref_end = cfg.reference_end
    received_at = _event_times(ds, DeliveryEventType.RECEIVED)
    pos = {p["po_id"]: p for p in ds.purchase_orders}
    price = {x["po_line_id"]: x["agreed_unit_price"] for x in ds.po_lines}
    expected: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for a in ds.shipment_lines:
        expected[a["shipment_id"]] += a["received_quantity"] * price[a["po_line_id"]]

    by_receipt = sorted(delivered, key=lambda s: (received_at[s["shipment_id"]], s["shipment_id"]))
    n_open = round(_UNINVOICED_FRACTION * len(delivered))
    to_invoice = by_receipt[: len(by_receipt) - n_open]
    to_invoice.sort(key=lambda s: s["shipment_id"])  # draw order independent of receipt ties

    rows: list[dict[str, Any]] = []
    for s in to_invoice:
        po = pos[s["po_id"]]
        profile = cfg.profiles[ds.supplier_profiles[po["supplier_id"]]]
        got = received_at[s["shipment_id"]]
        room = (ref_end.date() - got.date()).days
        invoice_date = got.date() + timedelta(
            days=int(rng.integers(0, min(room, _MAX_INVOICE_LAG_DAYS) + 1))
        )
        stamp = max(_midnight(invoice_date) + _minutes(rng, 8 * 60, 18 * 60), got)
        recorded = min(stamp + _minutes(rng, 0, 2 * 1440), ref_end)
        exp = to_money(expected[s["shipment_id"]])
        invoiced = exp
        if float(rng.random()) < profile.invoice_variance_p:
            m = profile.invoice_variance_magnitude * float(rng.uniform(0.5, 1.5))
            sign = 1 if float(rng.random()) < _OVERBILL_P else -1
            invoiced = to_money(exp * (1 + sign * Decimal(str(round(m, 6)))))
        variance = invoiced - exp
        pct = (variance / exp).quantize(_PCT, rounding=ROUND_HALF_UP) if exp != 0 else None
        age = (ref_end.date() - invoice_date).days
        if pct is not None and abs(pct) > _DISPUTE_THRESHOLD:
            status = InvoiceStatus.DISPUTED
        elif age > 60:
            status = InvoiceStatus.PAID
        elif age > 14:
            status = InvoiceStatus.APPROVED
        else:
            status = InvoiceStatus.RECEIVED
        rows.append(
            {
                "invoice_id": None,  # assigned after sorting
                "supplier_id": po["supplier_id"],
                "contract_id": po["contract_id"],
                "po_id": po["po_id"],
                "shipment_id": s["shipment_id"],
                "supplier_invoice_number": None,
                "invoice_date": invoice_date,
                "invoice_status": status,
                "currency": po["currency"],
                "invoiced_amount": invoiced,
                "expected_amount": exp,
                "variance_amount": variance,
                "variance_percentage": pct,
                "created_at": recorded,
                "recorded_at": recorded,
            }
        )
    rows.sort(key=lambda r: (r["invoice_date"], r["shipment_id"]))
    seq: dict[str, int] = defaultdict(int)
    for r in rows:
        r["invoice_id"] = ids.next("invoice")
        seq[r["supplier_id"]] += 1
        r["supplier_invoice_number"] = f"SYN-{r['supplier_id'][4:]}-{seq[r['supplier_id']]:05d}"
    ds.invoices.extend(rows)

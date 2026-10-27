"""Deterministic master data: suppliers, sites, contracts, prices, compliance, capacity.

One named random stream per concern, so adding draws to one concern never shifts another.
Supplier profiles live only in ``ds.supplier_profiles`` (ground truth), never in supplier rows.
Simplifications (documented assumptions): every supplier is ACTIVE; every supplier onboards
before the reference period starts, so each has a site and a contract in force on day one.
"""

import calendar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np

from supplier_reliability.config import ProfileParams
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
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.constants import (
    CURRENCY_FACTOR,
    MEAN_LINES_PER_PO,
    MEAN_QTY_PER_LINE,
    PRODUCT_CATEGORIES,
)
from supplier_reliability.services.generator.dataset import GeneratedDataset
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.profiles import assign_profiles
from supplier_reliability.services.generator.rng import RandomSource

__all__ = ["expected_monthly_demand", "generate_master_data"]

# (country, currency) by region.
_COUNTRIES: dict[Region, tuple[tuple[str, str], ...]] = {
    Region.NA: (("United States", "USD"), ("Canada", "USD"), ("Mexico", "USD")),
    Region.LATAM: (("Brazil", "USD"), ("Chile", "USD"), ("Colombia", "USD")),
    Region.EMEA: (
        ("Germany", "EUR"),
        ("France", "EUR"),
        ("Poland", "EUR"),
        ("United Kingdom", "GBP"),
    ),
    Region.APAC: (("India", "INR"), ("Vietnam", "USD"), ("Japan", "USD"), ("Malaysia", "USD")),
}
_REGION_P = (0.30, 0.15, 0.30, 0.25)  # in Region enum order: NA, LATAM, EMEA, APAC
_PAYMENT_TERMS = ("NET30", "NET45", "NET60", "NET90")
_DELIVERY_TERMS = ("FOB", "CIF", "DAP", "EXW")
_CERT_VALIDITY_DAYS = 1095

_RENEWAL_FRACTION = 0.15
_NULL_CAPACITY_FRACTION = 0.05
_INACTIVE_SITE_FRACTION = 0.10
_NO_CAPACITY_SUPPLIER_FRACTION = 0.05
_UNVERIFIED_FRACTION = 0.10
_NO_CERT_FRACTION = 0.08
_SITE_REQUIREMENT_FRACTION = 0.15
_HISTORY_SUPPLIER_P = 0.30
_SITE_COUNT_P = (0.5, 0.35, 0.15)


def _dec(x: float) -> Decimal:
    return Decimal(str(round(float(x), 2)))


def _utc(d: date, hours: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(hours=hours)


def _n_months(cfg: GeneratorConfig) -> int:
    s, e = cfg.reference_start, cfg.reference_end
    return (e.year - s.year) * 12 + e.month - s.month + 1


def expected_monthly_demand(
    profile: ProfileParams, n_purchase_orders_for_supplier_estimate: float, cfg: GeneratorConfig
) -> Decimal:
    """Expected units ordered per month for one supplier.

    ``n_purchase_orders_for_supplier_estimate`` = n_purchase_orders * po_weight / sum(po_weights).
    Assumes MEAN_LINES_PER_PO lines per PO and MEAN_QTY_PER_LINE units per line (constants.py,
    shared with the PO generator), spread evenly over the months of the reference period.
    ``profile`` is accepted so callers pass the supplier's own parameters; its PO weight is
    already folded into the PO estimate.
    """
    del profile
    units = (
        n_purchase_orders_for_supplier_estimate
        * MEAN_LINES_PER_PO
        * float(MEAN_QTY_PER_LINE)
        / _n_months(cfg)
    )
    return _dec(units)


def _exact_subset(rng: np.random.Generator, n: int, k: int) -> set[int]:
    return {int(i) for i in rng.permutation(n)[:k]}


def generate_master_data(
    cfg: GeneratorConfig, rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset
) -> None:
    ref_start = cfg.reference_start.date()
    ref_end = cfg.reference_end.date()

    profiles = assign_profiles(
        cfg.n_suppliers, {name: p.share for name, p in cfg.profiles.items()}, rs.stream("profiles")
    )
    total_w = sum(cfg.profiles[p].po_weight for p in profiles)

    _gen_suppliers(rs, ids, ds, profiles, ref_start)
    _gen_sites(rs, ids, ds, ref_start, ref_end)
    demand = {
        s["supplier_id"]: expected_monthly_demand(
            cfg.profiles[p], cfg.n_purchase_orders * cfg.profiles[p].po_weight / total_w, cfg
        )
        for s, p in zip(ds.suppliers, profiles, strict=True)
    }
    _gen_contracts(rs, ids, ds, demand, ref_start, ref_end)
    _gen_prices(rs, ids, ds)
    _gen_requirements(rs, ids, ds, ref_start)
    _gen_certifications(cfg, rs, ids, ds, ref_start)
    _gen_capacity(cfg, rs, ids, ds, demand, ref_start, ref_end)


def _gen_suppliers(
    rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset, profiles: list[str], ref_start: date
) -> None:
    rng = rs.stream("suppliers")
    fk = rs.faker("suppliers")
    regions = list(Region)
    categories = list(SupplierCategory)
    span = (ref_start - timedelta(days=92) - date(2019, 1, 1)).days
    for profile in profiles:
        sid = ids.next("supplier")
        region = regions[int(rng.choice(4, p=_REGION_P))]
        country, _ = _COUNTRIES[region][int(rng.integers(len(_COUNTRIES[region])))]
        onboarding = date(2019, 1, 1) + timedelta(days=int(rng.integers(0, span + 1)))
        created = _utc(onboarding, 8 + int(rng.integers(0, 8)))
        ds.supplier_profiles[sid] = profile
        ds.suppliers.append(
            {
                "supplier_id": sid,
                "supplier_name": fk.company(),
                "supplier_category": categories[int(rng.integers(len(categories)))],
                "country": country,
                "region": region,
                "supplier_status": SupplierStatus.ACTIVE,
                "onboarding_date": onboarding,
                "created_at": created,
                "updated_at": created + timedelta(days=int(rng.integers(0, 120))),
            }
        )


def _gen_sites(
    rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset, ref_start: date, ref_end: date
) -> None:
    rng = rs.stream("sites")
    site_types = list(SiteType)
    rows: list[dict[str, Any]] = []
    extras: list[int] = []  # indexes of non-first sites: only these may become INACTIVE
    for sup in ds.suppliers:
        sid = sup["supplier_id"]
        if ds.supplier_profiles[sid] == "sparse_history":
            n_sites = 1
        else:
            n_sites = int(rng.choice(3, p=_SITE_COUNT_P)) + 1
        for j in range(n_sites):
            stype = SiteType.FACTORY if j == 0 else site_types[int(rng.integers(len(site_types)))]
            if j > 0:
                extras.append(len(rows))
            rows.append(
                {
                    "site_id": ids.next("site", parent=sid),
                    "supplier_id": sid,
                    "site_name": f"{sup['supplier_name']} {stype.value.title()} {j + 1:02d}",
                    "site_type": stype,
                    "country": sup["country"],
                    "region": sup["region"],
                    "site_status": SiteStatus.ACTIVE,
                    "effective_from": sup["onboarding_date"],
                    "effective_to": None,
                    "created_at": _utc(sup["onboarding_date"], 9 + int(rng.integers(0, 48))),
                }
            )
    k_inactive = min(len(extras), round(_INACTIVE_SITE_FRACTION * len(rows)))
    for pos in _exact_subset(rng, len(extras), k_inactive):
        row = rows[extras[pos]]
        row["site_status"] = SiteStatus.INACTIVE
        row["effective_to"] = ref_start + timedelta(days=int(rng.integers(59, 481)))
        assert row["effective_to"] <= ref_end
    ds.sites.extend(rows)


def _full_active_sites(ds: GeneratedDataset, ref_start: date) -> dict[str, list[str]]:
    """Sites ACTIVE for the whole reference period, by supplier."""
    out: dict[str, list[str]] = {}
    for s in ds.sites:
        if (
            s["site_status"] == SiteStatus.ACTIVE
            and s["effective_to"] is None
            and s["effective_from"] <= ref_start
        ):
            out.setdefault(s["supplier_id"], []).append(s["site_id"])
    return out


def _gen_contracts(
    rs: RandomSource,
    ids: IdRegistry,
    ds: GeneratedDataset,
    demand: dict[str, Decimal],
    ref_start: date,
    ref_end: date,
) -> None:
    rng = rs.stream("contracts")
    full_active = _full_active_sites(ds, ref_start)
    n_sites: dict[str, int] = {}
    for s in ds.sites:
        n_sites[s["supplier_id"]] = n_sites.get(s["supplier_id"], 0) + 1
    renewed = _exact_subset(rng, len(ds.suppliers), round(_RENEWAL_FRACTION * len(ds.suppliers)))
    rows: list[dict[str, Any]] = []
    for i, sup in enumerate(ds.suppliers):
        sid = sup["supplier_id"]
        currency = dict(_COUNTRIES[sup["region"]])[sup["country"]]
        pay = _PAYMENT_TERMS[int(rng.integers(len(_PAYMENT_TERMS)))]
        delivery = _DELIVERY_TERMS[int(rng.integers(len(_DELIVERY_TERMS)))]
        first_from = sup["onboarding_date"] + timedelta(days=int(rng.integers(0, 31)))
        # Site-scoped contracts only for single-site, never-inactive, never-renewed suppliers,
        # so supplier-level coverage of the reference period stays gapless.
        site_id = None
        if i not in renewed and n_sites[sid] == 1 and sid in full_active and rng.random() < 0.10:
            site_id = full_active[sid][0]
        spans: list[tuple[date, date | None, ContractStatus]]
        if i in renewed:
            expiry = ref_start + timedelta(days=int(rng.integers(120, 421)))
            spans = [
                (first_from, expiry, ContractStatus.EXPIRED),
                (expiry + timedelta(days=1), None, ContractStatus.ACTIVE),
            ]
        else:
            end = None
            if rng.random() < 0.5:
                end = ref_end + timedelta(days=int(rng.integers(184, 550)))
            spans = [(first_from, end, ContractStatus.ACTIVE)]
        for eff_from, eff_to, status in spans:
            created = _utc(eff_from, 10) - timedelta(days=int(rng.integers(1, 30)))
            created = max(created, _utc(sup["onboarding_date"], 10))
            ceiling = max(50.0, float(demand[sid]) * float(rng.uniform(1.3, 2.2)))
            rows.append(
                {
                    "contract_id": ids.next("contract"),
                    "supplier_id": sid,
                    "site_id": site_id,
                    "contract_status": status,
                    "effective_from": eff_from,
                    "effective_to": eff_to,
                    "currency": currency,
                    "payment_terms": pay,
                    "delivery_terms": delivery,
                    "contracted_capacity": _dec(ceiling),
                    "capacity_unit": "units/month",
                    "created_at": created,
                }
            )
    k_null = max(1, round(_NULL_CAPACITY_FRACTION * len(rows)))
    for pos in _exact_subset(rng, len(rows), k_null):
        rows[pos]["contracted_capacity"] = None
    ds.contracts.extend(rows)


def _gen_prices(rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset) -> None:
    rng = rs.stream("prices")
    sup = {s["supplier_id"]: s for s in ds.suppliers}
    last: dict[tuple[str, str], float] = {}
    for c in ds.contracts:
        category = sup[c["supplier_id"]]["supplier_category"]
        factor = CURRENCY_FACTOR[c["currency"]]
        for product, uom, base in PRODUCT_CATEGORIES[category]:
            key = (c["supplier_id"], product)
            if key in last:  # renewal: price drifts up a little
                price = last[key] * (1 + float(rng.uniform(0.01, 0.06)))
            else:
                price = base * factor * float(rng.lognormal(0.0, 0.15))
            price = max(0.01, round(price, 2))
            last[key] = price
            price_id = ids.next("contract_price")  # CPR-nnnnnn; the column is an integer key
            ds.contract_prices.append(
                {
                    "contract_price_id": int(price_id.split("-")[1]),
                    "contract_id": c["contract_id"],
                    "product_category": product,
                    "unit_of_measure": uom,
                    "unit_price": _dec(price),
                }
            )


def _gen_requirements(
    rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset, ref_start: date
) -> None:
    rng = rs.stream("compliance")
    types = list(CertificationType)
    full_active = _full_active_sites(ds, ref_start)
    for c in ds.contracts:
        k = int(rng.integers(2, 5))
        chosen = [types[int(i)] for i in rng.permutation(len(types))[:k]]
        for j, ctype in enumerate(chosen):
            mandatory = True if j == 0 else bool(rng.random() < 0.7)
            site_scoped = bool(rng.random() < _SITE_REQUIREMENT_FRACTION)
            pool = [c["site_id"]] if c["site_id"] else full_active[c["supplier_id"]]
            site_id = pool[int(rng.integers(len(pool)))] if site_scoped else None
            ds.compliance_requirements.append(
                {
                    "requirement_id": ids.next("compliance_requirement"),
                    "contract_id": c["contract_id"],
                    "certification_type": ctype,
                    "mandatory": mandatory,
                    "scope": RequirementScope.SITE if site_scoped else RequirementScope.SUPPLIER,
                    "site_id": site_id,
                    "effective_from": c["effective_from"],
                    "effective_to": c["effective_to"],
                    "created_at": c["created_at"],
                }
            )


def _gen_certifications(
    cfg: GeneratorConfig, rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset, ref_start: date
) -> None:
    rng = rs.stream("certifications")
    contract_supplier = {c["contract_id"]: c["supplier_id"] for c in ds.contracts}
    keys: dict[str, list[tuple[CertificationType, str | None]]] = {}
    for r in ds.compliance_requirements:
        lst = keys.setdefault(contract_supplier[r["contract_id"]], [])
        k = (r["certification_type"], r["site_id"])
        if k not in lst:
            lst.append(k)
    seq: dict[str, int] = {}
    V, C = VerificationStatus, CertificationStatus

    def add(
        sid: str,
        site_id: str | None,
        ctype: CertificationType,
        number: str,
        verification: VerificationStatus,
        compliance: CertificationStatus,
        valid: tuple[date, date],
        recorded: datetime,
        supersedes: str | None,
    ) -> dict[str, Any]:
        cid = ids.next("certification")
        pending = verification == V.PENDING_VERIFICATION
        out = {
            "certification_id": cid,
            "supplier_id": sid,
            "site_id": site_id,
            "certification_type": ctype,
            "certificate_number": number,
            "issuing_authority": f"Synthetic Certification Body {int(rng.integers(1, 21)):02d}",
            "verification_status": verification,
            "compliance_status": compliance,
            "valid_from": valid[0],
            "valid_to": valid[1],
            "verified_at": None if pending else recorded,
            "evidence_reference": f"synthetic://cert/{cid}",
            "supersedes_certification_id": supersedes,
            "recorded_at": recorded,
        }
        ds.supplier_certifications.append(out)
        return out

    def new_number(sid: str, ctype: CertificationType) -> str:
        seq[sid] = seq.get(sid, 0) + 1
        return f"SYN-{ctype.value}-{sid[4:]}-{seq[sid]:03d}"

    def in_period() -> date:
        return ref_start + timedelta(days=int(rng.integers(30, 501)))

    for sup in ds.suppliers:
        sid = sup["supplier_id"]
        p_lapse = cfg.profiles[ds.supplier_profiles[sid]].at("cert_expiry_p", 0.5)
        want_history = bool(rng.random() < _HISTORY_SUPPLIER_P)
        normal_bases: list[dict[str, Any]] = []
        has_history = False
        for ctype, site_id in keys.get(sid, []):
            no_cert = rng.random() < _NO_CERT_FRACTION
            lapse = rng.random() < p_lapse
            mode = int(rng.choice(3, p=[0.4, 0.3, 0.3]))  # 0 expired, 1 revoked, 2 pending renewal
            if no_cert:
                continue
            number = new_number(sid, ctype)
            if lapse and mode in (0, 2):  # valid_to falls inside the period
                valid_to = ref_start + timedelta(days=int(rng.integers(59, 481)))
                valid_from = valid_to - timedelta(days=_CERT_VALIDITY_DAYS)
            else:
                valid_from = ref_start - timedelta(days=int(rng.integers(100, 366)))
                valid_to = valid_from + timedelta(days=_CERT_VALIDITY_DAYS)
            recorded = _utc(valid_from + timedelta(days=int(rng.integers(1, 6))), 9)
            base = add(
                sid, site_id, ctype, number, V.VERIFIED, C.COMPLIANT,
                (valid_from, valid_to), recorded, None,
            )  # fmt: skip
            if not lapse:
                normal_bases.append(base)
            elif mode == 1:  # later superseding row: revoked or rejected
                verification = V.VERIFIED if rng.random() < 0.7 else V.REJECTED
                add(
                    sid, site_id, ctype, number, verification, C.NONCOMPLIANT,
                    (valid_from, valid_to), _utc(in_period(), 9), base["certification_id"],
                )  # fmt: skip
                has_history = True
            elif mode == 2:  # renewal submitted before expiry, still pending verification
                new_from = valid_to + timedelta(days=1)
                add(
                    sid, site_id, ctype, new_number(sid, ctype), V.PENDING_VERIFICATION,
                    C.COMPLIANT, (new_from, new_from + timedelta(days=_CERT_VALIDITY_DAYS)),
                    _utc(valid_to - timedelta(days=int(rng.integers(5, 31))), 9),
                    base["certification_id"],
                )  # fmt: skip
                has_history = True
        if want_history and not has_history and normal_bases:
            base = normal_bases[int(rng.integers(len(normal_bases)))]
            when = max(in_period(), base["recorded_at"].date() + timedelta(days=1))
            add(
                sid, base["site_id"], base["certification_type"],
                new_number(sid, base["certification_type"]), V.VERIFIED, C.COMPLIANT,
                (when, when + timedelta(days=_CERT_VALIDITY_DAYS)), _utc(when, 9),
                base["certification_id"],
            )  # fmt: skip


def _gen_capacity(
    cfg: GeneratorConfig,
    rs: RandomSource,
    ids: IdRegistry,
    ds: GeneratedDataset,
    demand: dict[str, Decimal],
    ref_start: date,
    ref_end: date,
) -> None:
    """Monthly per-site declarations sized so expected demand / free capacity ~ profile ratio.

    The supplier's monthly free capacity is split across its sites active that month, so the
    scope rule (sum site-level rows) reproduces the target.
    """
    rng = rs.stream("capacity")
    months: list[date] = []
    m = ref_start.replace(day=1)
    while m <= ref_end:
        months.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    sites_by_supplier: dict[str, list[dict[str, Any]]] = {}
    for s in ds.sites:
        sites_by_supplier.setdefault(s["supplier_id"], []).append(s)
    none_set = _exact_subset(
        rng, len(ds.suppliers), round(_NO_CAPACITY_SUPPLIER_FRACTION * len(ds.suppliers))
    )
    for i, sup in enumerate(ds.suppliers):
        if i in none_set:
            continue
        sid = sup["supplier_id"]
        ratio = cfg.profiles[ds.supplier_profiles[sid]].demand_capacity_ratio
        base_free = float(demand[sid]) / ratio
        sites = sites_by_supplier[sid]
        weights = {s["site_id"]: float(rng.uniform(0.5, 1.5)) for s in sites}
        for month in months:
            last_day = calendar.monthrange(month.year, month.month)[1]
            month_end = date(month.year, month.month, last_day)
            active = [
                s
                for s in sites
                if s["effective_from"] <= month_end
                and (s["effective_to"] is None or s["effective_to"] >= month)
            ]
            month_free = base_free * float(rng.lognormal(0.0, 0.08))
            total_w = sum(weights[s["site_id"]] for s in active)
            for s in active:
                free = max(1.0, month_free * weights[s["site_id"]] / total_w)
                commit_frac = round(float(rng.uniform(0.25, 0.65)), 2)
                available = _dec(free / (1 - commit_frac))
                committed = (available * Decimal(str(commit_frac))).quantize(Decimal("0.01"))
                unverified = bool(rng.random() < _UNVERIFIED_FRACTION)
                ds.capacity_declarations.append(
                    {
                        "capacity_declaration_id": ids.next("capacity_declaration"),
                        "supplier_id": sid,
                        "site_id": s["site_id"],
                        "period_start": month,
                        "period_end": month_end,
                        "unit": "units/month",
                        "available_capacity": available,
                        "committed_capacity": committed,
                        "verification_status": (
                            CapacityVerification.UNVERIFIED
                            if unverified
                            else CapacityVerification.VERIFIED
                        ),
                        "source": (
                            "SYNTHETIC_SELF_DECLARED" if unverified else "SYNTHETIC_SUPPLIER_AUDIT"
                        ),
                        "recorded_at": _utc(month) - timedelta(days=7),
                    }
                )

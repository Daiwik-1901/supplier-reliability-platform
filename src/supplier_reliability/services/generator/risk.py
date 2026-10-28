"""Deterministic risk signals and the D6 disruption dedup key.

Streams: "risk_signals" (counts, types, severities, times, sites, resolution),
"risk_attestations" (which suppliers are monitored), "risk_duplicates" (duplicate copies).

Decisions (documented assumptions):
- Each supplier gets Poisson(profile.risk_signals_per_year x years) risk signals, years = the
  reference period length / 365.25 days. observed_timestamp is uniform (whole minutes) over the
  period. Severity mix LOW .40 / MEDIUM .35 / HIGH .18 / CRITICAL .07 (never NONE).
- ~60% of signals name a site of the supplier that is effective on the observed date.
  LOGISTICS_DISRUPTION / CARRIER_DELAY name the supplier's latest shipment dispatched before
  the observation (if any); FINANCIAL_DISTRESS / CERTIFICATION_LAPSE name the contract in
  force on the observed date.
- recorded = observed + 0-48 h, or 3-20 days for ~10% (late-arriving evidence), capped at
  reference_end. valid_from = observed.
- Resolution: p(RESOLVED) = clamp(age_days / 240, 0.05, 0.90) with age measured to
  reference_end, so older signals are more often resolved; otherwise MITIGATING (40%) or OPEN.
  A RESOLVED signal has valid_to = observed + 7-90 days, capped at reference_end; open and
  mitigating signals have valid_to NULL.
- MONITORING_ATTESTATION: round(70%) of suppliers (exact subset) get one attestation in every
  calendar quarter of the period (observed in the quarter's first 14 days, severity NONE,
  supplier-wide, RESOLVED with valid_to = quarter end: there is nothing to resolve, and the
  attestation covers that quarter).
- Duplicates: round(5%) of the risk signals (not attestations) get one copy from a different
  SYNTHETIC_ source: same supplier/site/type, observed 0-2 days later but inside the same D6
  week bucket and period, so the copy shares the original's dedup_key. ~30% of copies carry
  a re-drawn severity (sources disagree; scoring keeps the highest).
- Every signal_source starts with "SYNTHETIC_" and evidence_reference is
  "synthetic://risk/<risk_signal_id>". Ids are assigned in (observed_timestamp, supplier_id,
  signal_type, signal_source) order.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from supplier_reliability.domain.enums import ResolutionStatus, RiskSignalType, Severity
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import GeneratedDataset
from supplier_reliability.services.generator.ids import IdRegistry
from supplier_reliability.services.generator.profiles import exact_subset
from supplier_reliability.services.generator.rng import RandomSource

__all__ = ["generate_risk_signals", "risk_dedup_key"]

T = RiskSignalType
_TYPES = (
    T.LOGISTICS_DISRUPTION,
    T.FACTORY_INCIDENT,
    T.FINANCIAL_DISTRESS,
    T.CERTIFICATION_LAPSE,
    T.CARRIER_DELAY,
)
_TYPE_P = (0.25, 0.20, 0.15, 0.15, 0.25)
_SEVERITIES = (Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)
_SEVERITY_P = (0.40, 0.35, 0.18, 0.07)
_SOURCES = {
    T.LOGISTICS_DISRUPTION: ("SYNTHETIC_LOGISTICS_MONITOR", "SYNTHETIC_NEWS_FEED"),
    T.FACTORY_INCIDENT: ("SYNTHETIC_SITE_AUDIT", "SYNTHETIC_NEWS_FEED"),
    T.FINANCIAL_DISTRESS: ("SYNTHETIC_CREDIT_MONITOR", "SYNTHETIC_NEWS_FEED"),
    T.CERTIFICATION_LAPSE: ("SYNTHETIC_CERT_REGISTRY", "SYNTHETIC_SITE_AUDIT"),
    T.CARRIER_DELAY: ("SYNTHETIC_CARRIER_FEED", "SYNTHETIC_LOGISTICS_MONITOR"),
}
_ATTESTATION_SOURCE = "SYNTHETIC_MONITORING_SERVICE"
_SITE_P = 0.60
_LATE_RECORDING_P = 0.10
_ATTESTED_FRACTION = 0.70
_DUPLICATE_FRACTION = 0.05
_DUPLICATE_RESEVERITY_P = 0.30
_SHIPMENT_TYPES = (T.LOGISTICS_DISRUPTION, T.CARRIER_DELAY)
_CONTRACT_TYPES = (T.FINANCIAL_DISTRESS, T.CERTIFICATION_LAPSE)


def risk_dedup_key(
    supplier_id: str,
    site_id: str | None,
    signal_type: str,
    observed_timestamp: datetime,
) -> str:
    """D6 dedup key: ``supplier|site or *|type|UTC observed-date ordinal // 7``.

    Rejects naive timestamps (ValueError). Shared with disruption scoring.
    """
    week = ensure_utc(observed_timestamp).date().toordinal() // 7
    return f"{supplier_id}|{site_id or '*'}|{RiskSignalType(signal_type).value}|{week}"


def _minutes(rng: np.random.Generator, lo: int, hi: int) -> timedelta:
    return timedelta(minutes=int(rng.integers(lo, hi + 1)))


def _quarters(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """(first instant, last second) of every calendar quarter overlapping [start, end]."""
    out = []
    q = datetime(start.year, 3 * ((start.month - 1) // 3) + 1, 1, tzinfo=UTC)
    while q <= end:
        nxt = datetime(q.year + (q.month == 10), (q.month + 2) % 12 + 1, 1, tzinfo=UTC)
        out.append((q, min(nxt - timedelta(seconds=1), end)))
        q = nxt
    return out


def _effective(row: dict[str, Any], d: date) -> bool:
    return row["effective_from"] <= d and (row["effective_to"] is None or d <= row["effective_to"])


def generate_risk_signals(
    cfg: GeneratorConfig, rs: RandomSource, ids: IdRegistry, ds: GeneratedDataset
) -> None:
    rng = rs.stream("risk_signals")
    ref_start, ref_end = cfg.reference_start, cfg.reference_end
    span_min = int((ref_end - ref_start).total_seconds() // 60)
    years = (ref_end - ref_start) / timedelta(days=365.25)
    sites: dict[str, list[dict[str, Any]]] = {}
    for s in ds.sites:
        sites.setdefault(s["supplier_id"], []).append(s)
    contracts: dict[str, list[dict[str, Any]]] = {}
    for c in ds.contracts:
        contracts.setdefault(c["supplier_id"], []).append(c)
    po_supplier = {p["po_id"]: p["supplier_id"] for p in ds.purchase_orders}
    shipments: dict[str, list[dict[str, Any]]] = {}
    for sh in sorted(ds.shipments, key=lambda x: (x["dispatch_timestamp"], x["shipment_id"])):
        shipments.setdefault(po_supplier[sh["po_id"]], []).append(sh)

    risks: list[dict[str, Any]] = []
    for sup in ds.suppliers:
        sid = sup["supplier_id"]
        rate = cfg.profiles[ds.supplier_profiles[sid]].risk_signals_per_year
        for _ in range(int(rng.poisson(rate * years))):
            stype = _TYPES[int(rng.choice(len(_TYPES), p=_TYPE_P))]
            observed = ref_start + timedelta(minutes=int(rng.integers(0, span_min + 1)))
            d = observed.date()
            site_id = None
            if float(rng.random()) < _SITE_P:
                pool = [s for s in sites[sid] if _effective(s, d)]
                if pool:
                    site_id = pool[int(rng.integers(len(pool)))]["site_id"]
            shipment_id = contract_id = None
            if stype in _SHIPMENT_TYPES:
                before = [x for x in shipments.get(sid, []) if x["dispatch_timestamp"] <= observed]
                shipment_id = before[-1]["shipment_id"] if before else None
            elif stype in _CONTRACT_TYPES:
                active = [c for c in contracts[sid] if _effective(c, d)]
                contract_id = active[0]["contract_id"] if active else None
            sources = _SOURCES[stype]
            risks.append(
                _signal(
                    rng,
                    cfg,
                    supplier_id=sid,
                    site_id=site_id,
                    shipment_id=shipment_id,
                    contract_id=contract_id,
                    stype=stype,
                    severity=_SEVERITIES[int(rng.choice(len(_SEVERITIES), p=_SEVERITY_P))],
                    source=sources[int(rng.integers(len(sources)))],
                    observed=observed,
                )
            )
    rows = risks + _duplicates(rs.stream("risk_duplicates"), cfg, risks)
    rows += _attestations(rs.stream("risk_attestations"), cfg, ds)
    rows.sort(
        key=lambda r: (
            r["observed_timestamp"],
            r["supplier_id"],
            r["signal_type"],
            r["signal_source"],
        )
    )
    for r in rows:
        r["risk_signal_id"] = ids.next("risk")
        r["evidence_reference"] = f"synthetic://risk/{r['risk_signal_id']}"
    ds.risk_signals.extend(rows)


def _signal(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    *,
    supplier_id: str,
    site_id: str | None,
    shipment_id: str | None,
    contract_id: str | None,
    stype: RiskSignalType,
    severity: Severity,
    source: str,
    observed: datetime,
) -> dict[str, Any]:
    ref_end = cfg.reference_end
    if float(rng.random()) < _LATE_RECORDING_P:
        lag = _minutes(rng, 3 * 1440, 20 * 1440)
    else:
        lag = _minutes(rng, 0, 48 * 60)
    age_days = (ref_end - observed) / timedelta(days=1)
    p_resolved = min(0.90, max(0.05, age_days / 240))
    valid_to = None
    if float(rng.random()) < p_resolved:
        status = ResolutionStatus.RESOLVED
        valid_to = min(observed + _minutes(rng, 7 * 1440, 90 * 1440), ref_end)
    elif float(rng.random()) < 0.4:
        status = ResolutionStatus.MITIGATING
    else:
        status = ResolutionStatus.OPEN
    return {
        "risk_signal_id": None,  # assigned after sorting
        "supplier_id": supplier_id,
        "site_id": site_id,
        "related_shipment_id": shipment_id,
        "related_contract_id": contract_id,
        "signal_type": stype,
        "severity": severity,
        "signal_source": source,
        "observed_timestamp": observed,
        "recorded_timestamp": min(observed + lag, ref_end),
        "valid_from": observed,
        "valid_to": valid_to,
        "resolution_status": status,
        "evidence_reference": None,  # assigned with the id
        "dedup_key": risk_dedup_key(supplier_id, site_id, stype, observed),
    }


def _duplicates(
    rng: np.random.Generator, cfg: GeneratorConfig, risks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = []
    for pos in exact_subset(rng, len(risks), round(_DUPLICATE_FRACTION * len(risks))):
        orig = risks[pos]
        observed = orig["observed_timestamp"]
        week = observed.date().toordinal() // 7
        bucket_end = datetime.combine(
            date.fromordinal((week + 1) * 7), datetime.min.time(), UTC
        ) - timedelta(minutes=1)
        limit = min(observed + timedelta(days=2), bucket_end, cfg.reference_end)
        room = max(0, int((limit - observed).total_seconds() // 60))
        dup_at = observed + _minutes(rng, 0, room)
        others = [s for s in _all_sources(orig["signal_type"]) if s != orig["signal_source"]]
        severity = orig["severity"]
        if float(rng.random()) < _DUPLICATE_RESEVERITY_P:
            severity = _SEVERITIES[int(rng.choice(len(_SEVERITIES), p=_SEVERITY_P))]
        out.append(
            _signal(
                rng,
                cfg,
                supplier_id=orig["supplier_id"],
                site_id=orig["site_id"],
                shipment_id=orig["related_shipment_id"],
                contract_id=orig["related_contract_id"],
                stype=orig["signal_type"],
                severity=severity,
                source=others[int(rng.integers(len(others)))],
                observed=dup_at,
            )
        )
    return out


def _all_sources(stype: RiskSignalType) -> list[str]:
    """Every synthetic risk source, the type's own first (sorted for determinism)."""
    own = list(_SOURCES[stype])
    rest = sorted({s for v in _SOURCES.values() for s in v} - set(own))
    return own + rest


def _attestations(
    rng: np.random.Generator, cfg: GeneratorConfig, ds: GeneratedDataset
) -> list[dict[str, Any]]:
    out = []
    quarters = _quarters(cfg.reference_start, cfg.reference_end)
    n = len(ds.suppliers)
    for pos in exact_subset(rng, n, round(_ATTESTED_FRACTION * n)):
        sid = ds.suppliers[pos]["supplier_id"]
        for q_start, q_end in quarters:
            observed = q_start + _minutes(rng, 0, 14 * 1440)
            recorded = min(observed + _minutes(rng, 0, 24 * 60), cfg.reference_end)
            out.append(
                {
                    "risk_signal_id": None,
                    "supplier_id": sid,
                    "site_id": None,
                    "related_shipment_id": None,
                    "related_contract_id": None,
                    "signal_type": T.MONITORING_ATTESTATION,
                    "severity": Severity.NONE,
                    "signal_source": _ATTESTATION_SOURCE,
                    "observed_timestamp": observed,
                    "recorded_timestamp": recorded,
                    "valid_from": observed,
                    "valid_to": q_end,
                    "resolution_status": ResolutionStatus.RESOLVED,
                    "evidence_reference": None,
                    "dedup_key": risk_dedup_key(sid, None, T.MONITORING_ATTESTATION, observed),
                }
            )
    return out

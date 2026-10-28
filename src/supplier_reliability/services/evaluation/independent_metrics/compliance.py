"""Compliance, recomputed from compliance_requirements/contracts/supplier_certifications (D11).

Day d = date(C).
- Requirements: effective at d (effective_from <= d <= effective_to, open end allowed), created
  <= C, on a contract that is not DRAFT and is itself effective at d; supplier = the contract's.
- Current certification as of C per (supplier, type, site or none): among rows recorded <= C that
  no other row recorded <= C supersedes, the latest by (recorded_at, certification_id).
- Matching (R16): a SITE requirement only by that site's certification, a SUPPLIER requirement
  only by a supplier-wide one (site_id empty).
- State: PENDING_VERIFICATION -> PENDING_VERIFICATION; REJECTED -> NONCOMPLIANT; VERIFIED +
  NONCOMPLIANT -> NONCOMPLIANT; VERIFIED + COMPLIANT: valid_to < d -> EXPIRED, valid_from <= d ->
  COMPLIANT, not yet valid -> UNKNOWN (R19); no certification -> UNKNOWN.
- score = mean configured points over known (non-UNKNOWN) states; missing when none is known.
  mandatory_compliance_failure = a mandatory requirement EXPIRED or NONCOMPLIANT.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from ._tables import ZERO, Params, Tables, Window, clamp_round

UNKNOWN = "UNKNOWN"
_FAILURE = {"EXPIRED", "NONCOMPLIANT"}


def _effective(start: date, end: date | None, day: date) -> bool:
    return start <= day and (end is None or day <= end)


def _state(cert: Any, day: date) -> str:
    if cert is None:
        return UNKNOWN
    if cert.verification_status == "PENDING_VERIFICATION":
        return "PENDING_VERIFICATION"
    if cert.verification_status == "REJECTED" or cert.compliance_status == "NONCOMPLIANT":
        return "NONCOMPLIANT"
    if cert.valid_to < day:
        return "EXPIRED"
    if cert.valid_from <= day:
        return "COMPLIANT"
    return UNKNOWN


def _current_certifications(t: Tables, w: Window) -> dict[tuple[str, str, str | None], Any]:
    certs = t["supplier_certifications"]
    certs = certs[certs["recorded_at"] <= w.cutoff]
    superseded = set(certs["supersedes_certification_id"].dropna())
    current: dict[tuple[str, str, str | None], Any] = {}
    live = certs[~certs["certification_id"].isin(superseded)]
    for r in live.sort_values(["recorded_at", "certification_id"]).itertuples(index=False):
        site = r.site_id if isinstance(r.site_id, str) else None
        current[(r.supplier_id, r.certification_type, site)] = r  # the latest wins
    return current


def compliance(t: Tables, w: Window, p: Params) -> dict[str, dict[str, Any]]:
    day = w.cutoff_day
    contracts = t["contracts"][
        ["contract_id", "supplier_id", "contract_status", "effective_from", "effective_to"]
    ].rename(columns={"effective_from": "contract_from", "effective_to": "contract_to"})
    reqs = t["compliance_requirements"].merge(contracts, on="contract_id")
    current = _current_certifications(t, w)
    states: dict[str, list[tuple[bool, str]]] = defaultdict(list)
    for r in reqs.sort_values("requirement_id").itertuples(index=False):
        if not (
            _effective(r.effective_from, r.effective_to, day)
            and r.created_at <= w.cutoff
            and r.contract_status != "DRAFT"
            and _effective(r.contract_from, r.contract_to, day)
        ):
            continue
        site = r.site_id if r.scope == "SITE" else None
        cert = current.get((r.supplier_id, r.certification_type, site))
        states[r.supplier_id].append((bool(r.mandatory), _state(cert, day)))
    out: dict[str, dict[str, Any]] = {}
    for supplier, items in states.items():
        known = [s for _, s in items if s != UNKNOWN]
        score = None
        if known:
            points: Decimal = sum((p.compliance_points[s] for s in known), ZERO)
            score = clamp_round(points / len(known))
        out[supplier] = {
            "compliance_required": len(items),
            "compliance_known": len(known),
            "mandatory_compliance_failure": any(m and s in _FAILURE for m, s in items),
            "compliance_score": score,
        }
    return out

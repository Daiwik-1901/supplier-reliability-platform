"""The six explainable component scores (Task 10, D6). Pure functions, no DB access.

Each `score_*(ev, cfg)` maps an EvidenceBundle to a ComponentResult. Every parameter comes from
ScoringConfig. Arithmetic is Decimal end to end (config floats enter via `Decimal(str(x))`); the
single exception is the disruption recency decay, which needs exp(): per signal we compute
`Decimal(str(math.exp(-age_days / recency_days)))` with age_days = (C - observed) in fractional
days. Only the final score is rounded (`round_score`, after clamping to [0, 100]); numerator and
denominator stay unrounded. `coverage` is quantized to 4 dp.

A missing component has `score=None` and a non-empty `missing_reason` (missing is not zero).
Evidence that violates a hard invariant raises InvalidEvidenceError naming the record; in the
normal pipeline validation and DB CHECKs stop such records before they get here.

`metrics` values are JSON-friendly: str (Decimals), int, None, plus two structured values (the
capacity per-month utilisation map and the disruption `deduplicated_signal_ids` list).
`excluded_record_ids` holds component-level exclusions as (record id, reason); the engine turns
them into score_evidence rows with included=False.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from supplier_reliability.config import ScoringConfig
from supplier_reliability.domain.enums import (
    ComplianceState,
    ComponentName,
    RiskSignalType,
    Severity,
)
from supplier_reliability.domain.numeric import round_score
from supplier_reliability.services.scoring.evidence import EvidenceBundle, RiskObs

ZERO, ONE, HUNDRED = Decimal(0), Decimal(1), Decimal(100)
_SECONDS_PER_DAY = Decimal(86400)
_COVERAGE_QUANTUM = Decimal("0.0001")
# A validity invariant, deliberately not a ScoringConfig parameter: an arrival more than a year
# before the due date is a data error (wrong year / swapped fields), not a scoring judgement, so
# changing it must never change scores and must not require a scoring_version bump.
_MAX_EARLY_ARRIVAL = timedelta(days=365)
INFINITE_UTILISATION = "inf"
# Every reason a component puts in `excluded_record_ids` (the engine maps each to an entity type).
COMPONENT_EXCLUSION_REASONS = frozenset(
    {
        "zero_expected",
        "capacity_unverified",
        "zero_quantity_demand",
        "no_verified_capacity_for_month",
        "no_demand_in_month",
        "state_unknown",
        "deduplicated",
    }
)


class InvalidEvidenceError(ValueError):
    """Evidence violating a hard invariant; the message names the offending record id."""


@dataclass(frozen=True)
class ComponentResult:
    name: ComponentName
    score: Decimal | None
    numerator: Decimal | None
    denominator: Decimal | None
    observations: int
    coverage: Decimal
    metrics: dict[str, Any]
    missing_reason: str | None
    source_record_ids: list[str]
    flags: dict[str, bool]
    excluded_record_ids: list[tuple[str, str]] = field(default_factory=list)


# --- helpers ----------------------------------------------------------------------------------


def _d(x: float | int | str | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _coverage(part: int | Decimal, whole: int | Decimal) -> Decimal:
    if whole == 0:
        return ZERO.quantize(_COVERAGE_QUANTUM)
    return (Decimal(part) / Decimal(whole)).quantize(_COVERAGE_QUANTUM, rounding=ROUND_HALF_UP)


def _clamp_round(x: Decimal) -> Decimal:
    return round_score(min(HUNDRED, max(ZERO, x)))


def _days(delta: timedelta) -> Decimal:
    seconds = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000
    return seconds / _SECONDS_PER_DAY


def _s(x: Decimal) -> str:
    return str(x)


def _missing(
    name: ComponentName,
    reason: str,
    *,
    observations: int = 0,
    coverage: Decimal = ZERO,
    metrics: dict[str, Any] | None = None,
    excluded: list[tuple[str, str]] | None = None,
    flags: dict[str, bool] | None = None,
    sources: list[str] | None = None,
) -> ComponentResult:
    """A missing result. `sources` are the records the component evaluated (R24): included
    lineage even though too few of them exist to produce a score."""
    return ComponentResult(
        name=name,
        score=None,
        numerator=None,
        denominator=None,
        observations=observations,
        coverage=coverage.quantize(_COVERAGE_QUANTUM),
        metrics=metrics or {},
        missing_reason=reason,
        source_record_ids=sorted(sources or []),
        flags=flags or {},
        excluded_record_ids=sorted(excluded or []),
    )


# --- delivery ---------------------------------------------------------------------------------


def score_delivery(ev: EvidenceBundle, cfg: ScoringConfig) -> ComponentResult:
    """D6 delivery: 100 * on_time_rate - min(cap, per_day * mean_late_days), clamped."""
    name = ComponentName.delivery
    grace = timedelta(hours=cfg.grace_hours)
    cutoff = ev.window.cutoff
    n = len(ev.deliveries)

    on_time, known = 0, 0
    late_delays: list[Decimal] = []
    for d in ev.deliveries:
        if d.arrived_known is not None:
            if d.arrived_known < d.expected - _MAX_EARLY_ARRIVAL:
                raise InvalidEvidenceError(
                    f"shipment {d.shipment_id}: arrival {d.arrived_known.isoformat()} is more "
                    f"than 365 days before expected {d.expected.isoformat()}"
                )
            known += 1
            if d.arrived_known <= d.expected + grace:
                on_time += 1
                continue
            late_delays.append(_days(d.arrived_known - d.expected))
        else:  # overdue: no known arrival by the cutoff counts as late
            late_delays.append(_days(cutoff - d.expected))

    coverage = _coverage(known, n)
    minimum = cfg.min_observations["delivery"]
    if n < minimum:
        return _missing(
            name,
            f"insufficient deliveries: {n} < {minimum}",
            observations=n,
            coverage=coverage,
            metrics={"eligible_count": n, "known_outcomes": known, "coverage": _s(coverage)},
            sources=[d.shipment_id for d in ev.deliveries],
        )

    count = Decimal(n)
    otr = Decimal(on_time) / count
    total_late = sum(late_delays, ZERO)
    mean_late = total_late / len(late_delays) if late_delays else ZERO
    penalty = min(
        _d(cfg.delivery.delay_penalty_cap), _d(cfg.delivery.delay_penalty_per_day) * mean_late
    )
    score = HUNDRED * otr - penalty
    return ComponentResult(
        name=name,
        score=_clamp_round(score),
        numerator=Decimal(on_time),
        denominator=count,
        observations=n,
        coverage=coverage,
        metrics={
            "on_time_rate": _s(otr),
            "avg_delay_days": _s(total_late / count),
            "mean_late_days": _s(mean_late),
            "late_frequency": _s(Decimal(len(late_delays)) / count),
            "delay_penalty": _s(penalty),
            "eligible_count": n,
            "known_outcomes": known,
            "coverage": _s(coverage),
        },
        missing_reason=None,
        source_record_ids=sorted(d.shipment_id for d in ev.deliveries),
        flags={},
    )


# --- quality ----------------------------------------------------------------------------------


def score_quality(ev: EvidenceBundle, cfg: ScoringConfig) -> ComponentResult:
    """D6 quality: 100 * max(0, 1 - defect_rate / defect_rate_zero_score)."""
    name = ComponentName.quality
    for i in ev.inspections:
        if i.defect > i.inspected:
            raise InvalidEvidenceError(
                f"inspection {i.inspection_id}: defect {i.defect} exceeds inspected {i.inspected}"
            )
    n = len(ev.inspections)
    minimum = cfg.min_observations["quality"]
    if n < minimum:
        return _missing(
            name,
            f"insufficient inspections: {n} < {minimum}",
            observations=n,
            sources=[i.inspection_id for i in ev.inspections],
        )
    inspected = sum((i.inspected for i in ev.inspections), ZERO)
    if inspected == 0:
        return _missing(
            name,
            "no inspected units",
            observations=n,
            sources=[i.inspection_id for i in ev.inspections],
        )
    defects = sum((i.defect for i in ev.inspections), ZERO)
    rate = defects / inspected
    score = HUNDRED * max(ZERO, ONE - rate / _d(cfg.defect_rate_zero_score))
    return ComponentResult(
        name=name,
        score=_clamp_round(score),
        numerator=defects,
        denominator=inspected,
        observations=n,
        coverage=_coverage(1, 1),
        metrics={
            "defect_rate": _s(rate),
            "inspected_units": _s(inspected),
            "defect_units": _s(defects),
        },
        missing_reason=None,
        source_record_ids=sorted(i.inspection_id for i in ev.inspections),
        flags={},
    )


# --- cost -------------------------------------------------------------------------------------


def score_cost(ev: EvidenceBundle, cfg: ScoringConfig) -> ComponentResult:
    """D6 cost: wavp = sum(w_i*|var_i|) / sum(expected_i); w = 1, or underbilling_weight."""
    name = ComponentName.cost
    for inv in ev.invoices:
        if inv.expected < 0:
            raise InvalidEvidenceError(
                f"invoice {inv.invoice_id}: negative expected {inv.expected}"
            )
    usable = [i for i in ev.invoices if i.expected != 0]
    excluded = [(i.invoice_id, "zero_expected") for i in ev.invoices if i.expected == 0]
    coverage = _coverage(len(usable), len(ev.invoices))
    base_metrics = {"excluded_zero_expected": len(excluded)}
    minimum = cfg.min_observations["cost"]
    if len(usable) < minimum:
        return _missing(
            name,
            f"insufficient invoices: {len(usable)} < {minimum}",
            observations=len(usable),
            coverage=coverage,
            metrics=base_metrics,
            excluded=excluded,
            sources=[i.invoice_id for i in usable],
        )
    under = _d(cfg.underbilling_weight)
    numerator = sum(
        (
            abs(i.invoiced - i.expected) * (under if i.invoiced < i.expected else ONE)
            for i in usable
        ),
        ZERO,
    )
    denominator = sum((i.expected for i in usable), ZERO)
    wavp = numerator / denominator
    score = HUNDRED * max(ZERO, ONE - wavp / _d(cfg.variance_zero_score))
    return ComponentResult(
        name=name,
        score=_clamp_round(score),
        numerator=numerator,
        denominator=denominator,
        observations=len(usable),
        coverage=coverage,
        metrics={"wavp": _s(wavp), **base_metrics},
        missing_reason=None,
        source_record_ids=sorted(i.invoice_id for i in usable),
        flags={},
        excluded_record_ids=sorted(excluded),
    )


# --- capacity ---------------------------------------------------------------------------------


def _utilisation_score(util: Decimal, cfg: ScoringConfig) -> Decimal:
    """Piecewise-linear map through cfg.capacity_breakpoints; 0 beyond the last breakpoint."""
    points = sorted((_d(b.utilization), _d(b.score)) for b in cfg.capacity_breakpoints)
    if util <= points[0][0]:
        return points[0][1]
    for (u0, s0), (u1, s1) in zip(points, points[1:], strict=False):
        if util <= u1:
            return s0 + (s1 - s0) * (util - u0) / (u1 - u0)
    return ZERO


def score_capacity(ev: EvidenceBundle, cfg: ScoringConfig) -> ComponentResult:
    """D6 capacity: worst month's required / verified-free utilisation through the breakpoints."""
    name = ComponentName.capacity
    for c in ev.capacities:
        if c.verified and c.committed > c.available:
            raise InvalidEvidenceError(
                f"capacity declaration {c.capacity_declaration_id}: committed {c.committed} "
                f"exceeds available {c.available}"
            )
    for d in ev.demand:
        if d.quantity < 0:
            raise InvalidEvidenceError(f"PO line {d.po_line_id}: negative demand {d.quantity}")
    unverified = sorted(
        {
            (c.capacity_declaration_id, "capacity_unverified")
            for c in ev.capacities
            if not c.verified
        }
        | {(d.po_line_id, "zero_quantity_demand") for d in ev.demand if d.quantity == 0}
    )
    evaluated_lines = sorted({d.po_line_id for d in ev.demand if d.quantity > 0})
    if not ev.capacities:
        return _missing(name, "capacity unknown", excluded=unverified, sources=evaluated_lines)
    verified = [c for c in ev.capacities if c.verified]
    if not verified:
        return _missing(name, "capacity unverified", excluded=unverified, sources=evaluated_lines)
    verified_ids = {c.capacity_declaration_id for c in verified}

    required: dict[date, Decimal] = defaultdict(lambda: ZERO)
    lines: dict[date, list[str]] = defaultdict(list)
    for d in ev.demand:
        if d.quantity > 0:
            required[d.month] += d.quantity
            lines[d.month].append(d.po_line_id)
    if not required:
        unused = [(cid, "no_demand_in_month") for cid in verified_ids]
        return _missing(name, "no demand in window", excluded=unverified + unused)

    free: dict[date, Decimal] = defaultdict(lambda: ZERO)
    declarations: dict[date, set[str]] = defaultdict(set)
    for c in verified:
        free[c.month] += c.available - c.committed
        declarations[c.month].add(c.capacity_declaration_id)

    months = sorted(m for m in required if m in free)
    skipped = sorted(m for m in required if m not in free)
    used_declarations = {cid for m in months for cid in declarations[m]}
    excluded = (
        list(unverified)
        + [(line, "no_verified_capacity_for_month") for m in skipped for line in lines[m]]
        + [(cid, "no_demand_in_month") for cid in verified_ids - used_declarations]
    )
    coverage = _coverage(len(months), len(required))
    if not months:
        return _missing(
            name,
            "no verified capacity covering demand months",
            coverage=coverage,
            excluded=excluded,
        )

    # finite utilisation per month; months with free <= 0 (and required > 0) are exhausted
    finite: dict[date, Decimal] = {m: required[m] / free[m] for m in months if free[m] > 0}
    exhausted = [m for m in months if m not in finite]
    if exhausted:
        worst = exhausted[0]
        score, util_text = ZERO, INFINITE_UTILISATION
    else:
        worst = max(months, key=lambda m: (finite[m], -m.toordinal()))  # earliest on ties
        score, util_text = _utilisation_score(finite[worst], cfg), _s(finite[worst])
    monthly = {
        m.isoformat(): _s(finite[m]) if m in finite else INFINITE_UTILISATION for m in months
    }
    sources = {line for m in months for line in lines[m]} | used_declarations
    return ComponentResult(
        name=name,
        score=_clamp_round(score),
        numerator=required[worst],
        denominator=free[worst],
        observations=len(months),
        coverage=coverage,
        metrics={
            "util": util_text,
            "util_month": worst.isoformat(),
            "monthly_utilisation": monthly,
        },
        missing_reason=None,
        source_record_ids=sorted(sources),
        flags={"capacity_exhausted": bool(exhausted)},
        excluded_record_ids=sorted(excluded),
    )


# --- compliance -------------------------------------------------------------------------------

_MANDATORY_FAILURE = (ComplianceState.EXPIRED, ComplianceState.NONCOMPLIANT)


def score_compliance(ev: EvidenceBundle, cfg: ScoringConfig) -> ComponentResult:
    """D6 compliance: mean points of known requirement states; mandatory failure flag."""
    name = ComponentName.compliance
    for c in ev.compliance:
        if c.valid_from is not None and c.valid_to is not None and c.valid_to < c.valid_from:
            raise InvalidEvidenceError(
                f"requirement {c.requirement_id}: certification valid_to {c.valid_to} is before "
                f"valid_from {c.valid_from}"
            )
    required = len(ev.compliance)
    failure = any(c.mandatory and c.state in _MANDATORY_FAILURE for c in ev.compliance)
    flags = {"mandatory_compliance_failure": failure}
    if required == 0:
        return _missing(name, "no compliance requirements", flags=flags)
    states: dict[str, int] = defaultdict(int)
    for c in ev.compliance:
        states[c.state.value] += 1
    known = [c for c in ev.compliance if c.state != ComplianceState.UNKNOWN]
    coverage = _coverage(len(known), required)
    metrics = {f"state_{k}": v for k, v in sorted(states.items())} | {
        "required_count": required,
        "known_count": len(known),
    }
    if not known:
        return _missing(
            name,
            "all compliance states unknown",
            coverage=coverage,
            metrics=metrics,
            flags=flags,
            excluded=[(c.requirement_id, "state_unknown") for c in ev.compliance],
        )
    points = sum((_d(cfg.compliance_points[c.state.value]) for c in known), ZERO)
    return ComponentResult(
        name=name,
        score=_clamp_round(points / len(known)),
        numerator=points,
        denominator=Decimal(len(known)),
        observations=len(known),
        coverage=coverage,
        metrics=metrics,
        missing_reason=None,
        source_record_ids=sorted(c.requirement_id for c in known),
        flags=flags,
        excluded_record_ids=sorted(
            (c.requirement_id, "state_unknown")
            for c in ev.compliance
            if c.state == ComplianceState.UNKNOWN
        ),
    )


# --- disruption -------------------------------------------------------------------------------


def score_disruption(ev: EvidenceBundle, cfg: ScoringConfig) -> ComponentResult:
    """D6 disruption: 100 - sum(weight * decay * resolved * site) over deduplicated signals."""
    name = ComponentName.disruption
    for r in ev.risks:
        if r.severity == Severity.NONE and r.signal_type != RiskSignalType.MONITORING_ATTESTATION:
            raise InvalidEvidenceError(
                f"risk signal {r.risk_signal_id}: severity NONE on a "
                f"{r.signal_type.value} signal (only MONITORING_ATTESTATION may be NONE)"
            )
    if not ev.risks:
        return _missing(name, "no risk monitoring evidence")

    weights = {k: _d(v) for k, v in cfg.severity_weights.items()}
    groups: dict[str, list[RiskObs]] = defaultdict(list)
    for r in ev.risks:
        groups[r.dedup_key].append(r)
    kept, dropped = [], []
    for members in groups.values():
        ordered = sorted(members, key=lambda r: (-weights[r.severity.value], r.risk_signal_id))
        kept.append(ordered[0])
        dropped.extend(ordered[1:])

    recency, cutoff = _d(cfg.recency_days), ev.window.cutoff
    penalty = ZERO
    for r in kept:
        if r.signal_type == RiskSignalType.MONITORING_ATTESTATION:
            continue  # proof of monitoring only: no penalty by construction
        age_days = max(ZERO, _days(cutoff - r.observed))
        # exp() has no Decimal form: evaluate in float, then convert via str (documented above)
        decay = Decimal(str(math.exp(-float(age_days / recency))))
        factor = _d(cfg.resolved_multiplier) if r.resolved_at_cutoff else ONE
        site = ONE if r.site_active else _d(cfg.inactive_site_multiplier)
        penalty += weights[r.severity.value] * decay * factor * site
    dedup_ids = sorted(r.risk_signal_id for r in dropped)
    return ComponentResult(
        name=name,
        score=_clamp_round(HUNDRED - penalty),
        numerator=penalty,
        denominator=None,
        observations=len(kept),
        coverage=_coverage(1, 1),
        metrics={
            "penalty": _s(penalty),
            "signal_count": len(ev.risks),
            "deduplicated_signal_ids": dedup_ids,
        },
        missing_reason=None,
        source_record_ids=sorted(r.risk_signal_id for r in kept),
        flags={},
        excluded_record_ids=[(i, "deduplicated") for i in dedup_ids],
    )

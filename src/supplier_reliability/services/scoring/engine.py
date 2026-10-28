"""Score one supplier for one window from an EvidenceBundle (Task 11). Pure; no DB access.

`score_supplier(bundle, cfg, config_hash)` runs the six components, rounds nothing further
(component scores are already 2 dp), aggregates (D7) and builds the explanation and the evidence
fingerprint. `config_hash` is an explicit argument (the caller owns it: `AppConfig.config_hash()`),
so this module does not need the whole AppConfig and the fingerprint states which config it used.

Explanation: JSON-serialisable (Decimals as strings, datetimes ISO-8601 with a trailing Z).
`source_record_ids` per component and `excluded_records` per component are capped at 200 entries;
the cut-off amounts are reported as `truncated_count` / `excluded_records_truncated_count`. The
fingerprint always covers every included observation.

Evidence fingerprint (R23): sha256 hex of canonical JSON (sorted keys, separators (",", ":")) of
{"scoring_version", "config_hash", "observations"}. Observations are exactly the lineage set
(R24): the items in the components' `source_record_ids` (every record a component evaluated,
whether or not it reached its minimum observation count; deliberately unused records are listed
in `excluded_record_ids` with a reason instead). Each is a dict {component, record_id, <values
the component used>} with Decimals/dates as strings, sorted by (component, record_id, canonical
JSON). Excluded records are not part of it, so ignored evidence can never change a score's
identity.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from supplier_reliability.config import ScoringConfig
from supplier_reliability.domain.enums import ComponentName
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.services.scoring.aggregate import OverallResult, aggregate
from supplier_reliability.services.scoring.components import (
    ComponentResult,
    score_capacity,
    score_compliance,
    score_cost,
    score_delivery,
    score_disruption,
    score_quality,
)
from supplier_reliability.services.scoring.evidence import EvaluationWindow, EvidenceBundle

LIST_CAP = 200
_FOUR_DP = Decimal("0.0001")
FORMULA = (
    "overall = round2(Σ w_k · c_k / Σ w_k) over available components, with each c_k "
    "rounded to 2 dp first; effective_weight_k = w_k / Σ w_available; "
    "evidence_coverage = Σ w_available"
)
_SCORERS = (
    (ComponentName.delivery, score_delivery),
    (ComponentName.quality, score_quality),
    (ComponentName.cost, score_cost),
    (ComponentName.capacity, score_capacity),
    (ComponentName.compliance, score_compliance),
    (ComponentName.disruption, score_disruption),
)
# entity type of component-level exclusions (`ComponentResult.excluded_record_ids`), by reason;
# must cover components.COMPONENT_EXCLUSION_REASONS (tested)
_ENTITY_BY_REASON = {
    "zero_expected": "invoice",
    "capacity_unverified": "capacity_declaration",
    "zero_quantity_demand": "po_line",
    "no_verified_capacity_for_month": "po_line",
    "no_demand_in_month": "capacity_declaration",
    "state_unknown": "compliance_requirement",
    "deduplicated": "risk_signal",
}


def _entity_for_reason(reason: str) -> str:
    try:
        return _ENTITY_BY_REASON[reason]
    except KeyError:
        raise ValueError(f"unmapped component exclusion reason: {reason!r}") from None


@dataclass(frozen=True)
class ScoreResult:
    supplier_id: str
    window: EvaluationWindow
    components: dict[str, ComponentResult]
    overall: OverallResult
    explanation: dict[str, Any]
    evidence_fingerprint: str


def score_supplier(bundle: EvidenceBundle, cfg: ScoringConfig, config_hash: str) -> ScoreResult:
    results = {name.value: scorer(bundle, cfg) for name, scorer in _SCORERS}
    overall = aggregate(list(results.values()), cfg, bundle.open_critical_exception_ids)
    return ScoreResult(
        supplier_id=bundle.supplier_id,
        window=bundle.window,
        components=results,
        overall=overall,
        explanation=_explanation(bundle, cfg, results, overall),
        evidence_fingerprint=_fingerprint(bundle, cfg, config_hash, results),
    )


# --- serialisation helpers -------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def _plain(value: Any) -> Any:
    """JSON-ready form: Decimals as strings, datetimes ISO Z, dates ISO, enums by value."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    raise TypeError(f"not serialisable: {type(value).__name__}")


def _capped(items: list[Any]) -> tuple[list[Any], int]:
    return items[:LIST_CAP], max(0, len(items) - LIST_CAP)


# --- explanation -----------------------------------------------------------------------------


def _explanation(
    bundle: EvidenceBundle,
    cfg: ScoringConfig,
    results: dict[str, ComponentResult],
    overall: OverallResult,
) -> dict[str, Any]:
    weights = {str(k): Decimal(str(v)) for k, v in cfg.weights.items()}
    components: dict[str, Any] = {}
    missing: dict[str, str] = {}
    for name, r in results.items():
        ids, truncated = _capped(r.source_record_ids)
        eff = overall.effective_weights.get(name)
        components[name] = {
            "score": _plain(r.score),
            "weight": _plain(weights[name]),
            "effective_weight": None if eff is None else _plain(eff),
            "numerator": _plain(r.numerator),
            "denominator": _plain(r.denominator),
            "observations": r.observations,
            "coverage": _plain(r.coverage),
            "metrics": _plain(r.metrics),
            "missing_reason": r.missing_reason,
            "source_record_ids": ids,
            "truncated_count": truncated,
        }
        if r.score is None:
            missing[name] = r.missing_reason or "missing"

    note = None
    if missing and overall.effective_weights:
        used = ", ".join(f"{n} {_plain(w)}" for n, w in overall.effective_weights.items())
        note = (
            f"missing components ({', '.join(missing)}) are excluded and the available weights "
            f"are renormalised over {_plain(overall.evidence_coverage)} "
            f"(effective weights: {used})"
        )

    excluded, excluded_truncated = _excluded(bundle, results)
    window = bundle.window
    return {
        "overall_score": _plain(overall.overall),
        "components": components,
        "evidence_coverage": _plain(overall.evidence_coverage.quantize(_FOUR_DP)),
        "missing_data_reasons": dict(missing),
        "risk_signals": _risk_signals(bundle, results["disruption"]),
        "detected_exceptions": list(bundle.open_critical_exception_ids),
        "scoring_version": cfg.scoring_version,
        "evaluation_period": {"start": _iso(window.start), "end": _iso(window.end)},
        "data_cutoff": _iso(window.cutoff),
        "decision": overall.decision.value,
        "decision_rationale": list(overall.decision_rationale),
        "is_provisional": overall.is_provisional,
        "formula": FORMULA,
        "applied_overrides": list(overall.applied_overrides),
        "missing_data_decisions": {"missing_components": dict(missing), "renormalisation": note},
        "exceptions": list(bundle.open_critical_exception_ids),
        "excluded_records": excluded,
        "excluded_records_truncated_count": excluded_truncated,
    }


def _risk_signals(bundle: EvidenceBundle, disruption: ComponentResult) -> list[dict[str, Any]]:
    kept = set(disruption.source_record_ids)
    return [
        _plain(
            {
                "risk_signal_id": r.risk_signal_id,
                "site_id": r.site_id,
                "signal_type": r.signal_type,
                "severity": r.severity,
                "observed": r.observed,
                "resolution": r.resolution,
                "resolved_at_cutoff": r.resolved_at_cutoff,
            }
        )
        for r in bundle.risks
        if r.risk_signal_id in kept
    ]


def _excluded(
    bundle: EvidenceBundle, results: dict[str, ComponentResult]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    rows: dict[str, set[tuple[str, str, str, str]]] = {n.value: set() for n in ComponentName}
    for e in bundle.excluded:
        month = "" if e.month is None else e.month.isoformat()
        rows[e.component.value].add((e.source_entity_type, e.source_record_id, e.reason, month))
    for name, r in results.items():
        for record_id, reason in r.excluded_record_ids:
            entity = _entity_for_reason(reason)
            rows[name].add((entity, record_id, reason, ""))
    out: dict[str, list[dict[str, Any]]] = {}
    truncated: dict[str, int] = {}
    for name, items in rows.items():
        listed, truncated[name] = _capped(sorted(items))
        out[name] = [
            {"source_entity_type": t, "source_record_id": i, "reason": why}
            | ({"month": month} if month else {})
            for t, i, why, month in listed
        ]
    return out, truncated


# --- fingerprint -----------------------------------------------------------------------------


def _observations(bundle: EvidenceBundle, results: dict[str, ComponentResult]) -> list[dict]:
    """Exactly the lineage set (R24): one item per record in a component's `source_record_ids`,
    with the values the component used. Nothing is added beyond the components' included ids."""
    obs: list[dict[str, Any]] = []

    def add(component: ComponentName, record_id: str, **values: Any) -> None:
        obs.append(_plain({"component": component.value, "record_id": record_id, **values}))

    def included(name: ComponentName) -> set[str]:
        return set(results[name.value].source_record_ids)

    ids = included(ComponentName.delivery)
    for d in bundle.deliveries:
        if d.shipment_id in ids:
            add(ComponentName.delivery, d.shipment_id, expected=d.expected, arrived=d.arrived_known)
    ids = included(ComponentName.quality)
    for i in bundle.inspections:
        if i.inspection_id in ids:
            add(
                ComponentName.quality,
                i.inspection_id,
                shipment_line_id=i.shipment_line_id,
                inspected=i.inspected,
                defect=i.defect,
            )
    ids = included(ComponentName.cost)
    for inv in bundle.invoices:
        if inv.invoice_id in ids:
            add(ComponentName.cost, inv.invoice_id, expected=inv.expected, invoiced=inv.invoiced)
    ids = included(ComponentName.capacity)
    for line in bundle.demand:
        if line.po_line_id in ids:
            add(
                ComponentName.capacity,
                line.po_line_id,
                kind="demand",
                month=line.month,
                quantity=line.quantity,
                unit_of_measure=line.unit_of_measure,
            )
    for c in bundle.capacities:
        if c.capacity_declaration_id in ids:
            add(
                ComponentName.capacity,
                c.capacity_declaration_id,
                kind="declaration",
                site_id=c.site_id,
                month=c.month,
                available=c.available,
                committed=c.committed,
                verified=c.verified,
            )
    ids = included(ComponentName.compliance)
    for r in bundle.compliance:
        if r.requirement_id in ids:
            add(
                ComponentName.compliance,
                r.requirement_id,
                certification_id=r.certification_id,
                state=r.state,
                mandatory=r.mandatory,
                valid_from=r.valid_from,
                valid_to=r.valid_to,
            )
    ids = included(ComponentName.disruption)
    for s in bundle.risks:
        if s.risk_signal_id in ids:
            add(
                ComponentName.disruption,
                s.risk_signal_id,
                signal_type=s.signal_type,
                severity=s.severity,
                observed=s.observed,
                site_active=s.site_active,
                resolved_at_cutoff=s.resolved_at_cutoff,
            )
    return sorted(obs, key=lambda o: (o["component"], o["record_id"], _canonical(o)))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _fingerprint(
    bundle: EvidenceBundle,
    cfg: ScoringConfig,
    config_hash: str,
    results: dict[str, ComponentResult],
) -> str:
    payload = {
        "scoring_version": cfg.scoring_version,
        "config_hash": config_hash,
        "observations": _observations(bundle, results),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()

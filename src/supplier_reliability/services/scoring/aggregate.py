"""Overall score, reliability category and decision (Task 11, D7). Pure, no DB access.

- `overall = sum(w_k * c_k) / sum(w_k)` over the AVAILABLE components, using the already rounded
  component scores, rounded once with `round_score`. The division uses the exact (unquantized)
  weight ratios; `effective_weights` (w_k / sum of available weights) is quantized to 4 dp for
  storage and display only (so 0.3 / 0.9 shows as 0.3333).
- `evidence_coverage = sum of available weights`. Coverage below `coverage_threshold`, or a missing
  delivery component, gives INSUFFICIENT_EVIDENCE (the overall is still computed when any component
  exists). `0 < coverage < 1` with coverage >= threshold is provisional.
- Decision precedence (first match wins): mandatory compliance failure, open CRITICAL exception,
  INSUFFICIENT_EVIDENCE, then the category map. `applied_overrides` names an override that fired
  and `decision_rationale` says which rule fired and why.
- "Deteriorating" is derived later from persisted history (Task 17); nothing here depends on it.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from supplier_reliability.config import CategoryThreshold, ScoringConfig
from supplier_reliability.domain.enums import (
    ComponentName,
    Decision,
    ReliabilityCategory,
)
from supplier_reliability.domain.numeric import round_score
from supplier_reliability.services.scoring.components import ComponentResult

_FOUR_DP = Decimal("0.0001")
OVERRIDE_COMPLIANCE = "mandatory_compliance_failure"
OVERRIDE_EXCEPTION = "open_critical_exception"

_CATEGORY_DECISION = {
    ReliabilityCategory.HIGH: Decision.PREFERRED,
    ReliabilityCategory.ACCEPTABLE: Decision.APPROVED,
    ReliabilityCategory.AT_RISK: Decision.MONITOR,
    ReliabilityCategory.CRITICAL: Decision.ESCALATE,
}


@dataclass(frozen=True)
class OverallResult:
    overall: Decimal | None
    effective_weights: dict[str, Decimal]
    evidence_coverage: Decimal
    is_provisional: bool
    category: ReliabilityCategory
    decision: Decision
    decision_rationale: list[str]
    applied_overrides: list[str] = field(default_factory=list)


def categorize(score: Decimal, thresholds: dict[str, CategoryThreshold]) -> ReliabilityCategory:
    """Lower bound inclusive, upper bound exclusive, except the top band which includes its upper
    bound (100). Bounds come from the configuration."""
    top = max(t.upper for t in thresholds.values())
    for name, band in thresholds.items():
        lower, upper = Decimal(str(band.lower)), Decimal(str(band.upper))
        if score >= lower and (score < upper or (upper == Decimal(str(top)) and score <= upper)):
            return ReliabilityCategory(name)
    raise ValueError(f"score {score} outside the configured category thresholds")


def aggregate(
    components: list[ComponentResult],
    cfg: ScoringConfig,
    open_critical_exceptions: list[str],
) -> OverallResult:
    weight = {ComponentName(k): Decimal(str(v)) for k, v in cfg.weights.items()}
    available = [c for c in components if c.score is not None]
    total = sum((weight[c.name] for c in available), Decimal(0))
    coverage = total.quantize(_FOUR_DP, rounding=ROUND_HALF_UP)
    threshold = Decimal(str(cfg.coverage_threshold))

    overall: Decimal | None = None
    effective: dict[str, Decimal] = {}
    if available:
        overall = round_score(
            sum((weight[c.name] * c.score for c in available), Decimal(0)) / total
        )
        effective = {
            c.name.value: (weight[c.name] / total).quantize(_FOUR_DP, rounding=ROUND_HALF_UP)
            for c in available
        }

    delivery_missing = not any(c.name == ComponentName.delivery for c in available)
    if coverage < threshold or delivery_missing:
        category = ReliabilityCategory.INSUFFICIENT_EVIDENCE
    else:
        assert overall is not None  # coverage >= threshold > 0 implies a component exists
        category = categorize(overall, cfg.category_thresholds)
    provisional = threshold <= total < 1

    rationale: list[str] = []
    overrides: list[str] = []
    if category == ReliabilityCategory.INSUFFICIENT_EVIDENCE:
        reasons = []
        if delivery_missing:
            reasons.append("delivery component is missing")
        if coverage < threshold:
            reasons.append(f"evidence coverage {coverage} is below threshold {threshold}")
        rationale.append("category INSUFFICIENT_EVIDENCE: " + "; ".join(reasons))
    else:
        rationale.append(f"category {category.value}: overall score {overall}")

    if any(c.flags.get("mandatory_compliance_failure") for c in components):
        decision = Decision.SUSPEND_PENDING_REVIEW
        overrides.append(OVERRIDE_COMPLIANCE)
        rationale.append(
            "rule 1 fired: a mandatory compliance requirement is expired or noncompliant "
            "-> SUSPEND_PENDING_REVIEW"
        )
    elif open_critical_exceptions:
        decision = Decision.HOLD_FOR_DATA_REVIEW
        overrides.append(OVERRIDE_EXCEPTION)
        rationale.append(
            "rule 2 fired: open CRITICAL data exception(s) "
            f"{', '.join(sorted(open_critical_exceptions))} -> HOLD_FOR_DATA_REVIEW"
        )
    elif category == ReliabilityCategory.INSUFFICIENT_EVIDENCE:
        decision = Decision.COLLECT_MORE_DATA
        rationale.append("rule 3 fired: insufficient evidence -> COLLECT_MORE_DATA")
    else:
        decision = _CATEGORY_DECISION[category]
        rationale.append(f"rule 4 fired: category {category.value} -> {decision.value}")

    return OverallResult(
        overall=overall,
        effective_weights=effective,
        evidence_coverage=coverage,
        is_provisional=provisional,
        category=category,
        decision=decision,
        decision_rationale=rationale,
        applied_overrides=overrides,
    )

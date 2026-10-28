"""Overall score, coverage and category per D7, from the (already 2-dp) component scores.

overall = round2(sum w_k c_k / sum w_k) over available components; coverage = sum w_k(available)
(4 dp). Coverage below the threshold or a missing delivery component -> INSUFFICIENT_EVIDENCE
(the overall is still reported). Otherwise the category whose [lower, upper) band holds the
overall; the top band includes its upper bound.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ._tables import ZERO, Params

COMPONENTS = ("delivery", "quality", "cost", "capacity", "compliance", "disruption")
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
_FOUR_DP, _TWO_DP = Decimal("0.0001"), Decimal("0.01")


def category(score: Decimal, p: Params) -> str:
    top = max(upper for _, upper in p.thresholds.values())
    for name, (lower, upper) in p.thresholds.items():
        if score >= lower and (score < upper or (upper == top and score <= upper)):
            return name
    raise ValueError(f"score {score} outside the category thresholds")


def overall(row: dict[str, Any], p: Params) -> dict[str, Any]:
    available = [(c, row[f"{c}_score"]) for c in COMPONENTS if row[f"{c}_score"] is not None]
    total = sum((p.weights[c] for c, _ in available), ZERO)
    score = None
    if available:
        weighted = sum((p.weights[c] * s for c, s in available), ZERO)
        score = (weighted / total).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    coverage = total.quantize(_FOUR_DP, rounding=ROUND_HALF_UP)
    if coverage < p.coverage_threshold or row["delivery_score"] is None:
        cat = INSUFFICIENT
    else:
        assert score is not None
        cat = category(score, p)
    return {"overall_score": score, "evidence_coverage": coverage, "reliability_category": cat}

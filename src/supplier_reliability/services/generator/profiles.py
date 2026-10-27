"""Profile assignment and exact integer allocation."""

import numpy as np

from supplier_reliability.services.generator.config import GeneratorCapacityError


def assign_profiles(n: int, shares: dict[str, float], rng: np.random.Generator) -> list[str]:
    """Exactly round(share*n) per profile; ``average`` absorbs the rounding remainder.

    ``round`` is Python's banker's rounding (round half to even): 3.5 -> 4, 2.5 -> 2.
    Profile names are sorted first so YAML ordering never changes the assignment.
    """
    if "average" not in shares:
        raise ValueError("shares must include 'average'")
    counts = {name: round(share * n) for name, share in shares.items()}
    others = sum(c for name, c in counts.items() if name != "average")
    counts["average"] = n - others
    if counts["average"] < 0:
        raise GeneratorCapacityError("profile counts exceed n even with average at zero")
    labels = [name for name, c in sorted(counts.items()) for _ in range(c)]
    order = rng.permutation(len(labels))
    return [labels[i] for i in order]


def allocate_exact(
    total: int,
    weights: np.ndarray,
    minimum: int,
    rng: np.random.Generator,
    maximum: int | None = None,
) -> np.ndarray:
    """Integer allocation summing to ``total`` with minimum <= x_i <= maximum.

    Gives every slot the minimum, then spreads the rest by multinomial draws proportional to
    ``weights``; slots at the maximum are closed and their weight redistributed in the next
    round. Deterministic given rng.
    """
    w = np.asarray(weights, dtype=float)
    k = len(w)
    if minimum < 0:
        raise ValueError("minimum must be nonnegative")
    if not np.isfinite(w).all():
        raise ValueError("weights must be finite")
    if k == 0 or (w < 0).any() or w.sum() <= 0:
        raise GeneratorCapacityError("weights must be nonnegative with a positive sum")
    if maximum is not None and maximum < minimum:
        raise GeneratorCapacityError("maximum below minimum")
    if total < minimum * k:
        raise GeneratorCapacityError(f"total {total} cannot satisfy minimum {minimum} x {k}")
    if maximum is not None and total > maximum * k:
        raise GeneratorCapacityError(f"total {total} exceeds maximum {maximum} x {k}")
    alloc = np.full(k, minimum, dtype=np.int64)
    remaining = total - minimum * k
    while remaining > 0:
        open_mask = np.ones(k, dtype=bool) if maximum is None else alloc < maximum
        ow = np.where(open_mask, w, 0.0)
        if ow.sum() <= 0:  # only zero-weight slots left with room: share them evenly
            ow = open_mask.astype(float)
        draw = rng.multinomial(remaining, ow / ow.sum())
        if maximum is not None:
            draw = np.minimum(draw, maximum - alloc)
        alloc += draw
        remaining -= int(draw.sum())
    return alloc

"""Allocate one existing reasoning deadline without starving ranked alternatives."""
from dataclasses import dataclass, asdict
import math


@dataclass(frozen=True)
class AttemptBudget:
    seconds: float
    remaining_seconds: float
    minimum_window_seconds: float
    viable_candidates: int
    reserved_seconds: float
    latency_seconds: float | None
    latency_source: str

    def to_dict(self):
        return asdict(self)


def allocate_attempt(remaining, ranked, health, *, background, attempts_left):
    """Keep ranking authoritative; reserve windows, then weight surplus by latency.

    Floors and caps are task-lane scheduling policy, not provider-specific limits.
    Unknown latency gets a neutral scheduling weight, never a synthetic observation.
    Recompute on every attempt so an early failure leaves its unused time available.
    """
    minimum, cap = (10., 45.) if background else (2., 12.)
    if not math.isfinite(remaining) or remaining < minimum or attempts_left <= 0 or not ranked:
        return None
    # Count only windows we can meaningfully schedule, in the existing rank order.
    count = min(len(ranked), attempts_left, max(1, int(remaining // minimum)))
    routes = [route for route, _ in ranked[:count]]
    weights, observations = [], []
    for route in routes:
        observed = health.get(route.provider.name, route.provider.model).average_latency
        prior = route.profile.expected_latency
        latency, source = (observed, "observed") if observed is not None else (
            (prior, "profile_prior") if prior is not None else (None, "unknown"))
        # Unknown does not mean fast. Give it twice the floor as a neutral weight.
        weight = 2 * minimum if latency is None else 1.5 * latency
        weights.append(max(minimum, min(cap, weight)))
        observations.append((latency, source))
    reserve = (count - 1) * minimum
    # A small generic bookkeeping margin protects the last window from event I/O.
    margin = min(.05 * (count - 1), max(0., remaining - count * minimum))
    surplus = max(0., remaining - count * minimum - margin)
    seconds = min(cap, remaining - reserve - margin,
                  minimum + surplus * weights[0] / sum(weights))
    return AttemptBudget(seconds, remaining, minimum, count, reserve + margin, *observations[0])

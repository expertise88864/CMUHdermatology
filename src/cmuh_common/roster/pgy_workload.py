"""Equalize PGY duties before availability-weighted total work."""
from math import gcd
from functools import reduce
import heapq

from .session_leave import on_leave


def square_floor(total, scale, targets):
    """Exact integer lower bound ignoring roster constraints (convex allocation)."""
    values = list(targets.values())
    counts = [0] * len(values)
    heap = [(scale * scale - 2 * scale * t, i) for i, t in enumerate(values)]
    heapq.heapify(heap)
    for _ in range(total):
        cost, i = heapq.heappop(heap)
        counts[i] += 1
        heapq.heappush(heap, (cost + 2 * scale * scale, i))
    return sum((scale * n - t) ** 2 for n, t in zip(counts, values, strict=True))


def availability_weights(inp, people):
    counts = {p: sum(not on_leave(inp, "pgy", p, d, s)
                     for d, sessions in inp.grid.items()
                     if d.strftime("%Y-%m") == inp.ym and d.weekday() < 5
                     and d not in inp.holidays
                     for s in sessions)
              for p in people}
    divisor = reduce(gcd, counts.values()) or 1
    return {p: n // divisor for p, n in counts.items()}


def workload_goals(model, people, totals, original, weights, offsets):
    """Return lexicographic objectives and their incumbent values.

    Required coverage fixes the total number of seats. Photo and treatment
    counts are shared equally among PGYs, apart from manual photo offsets.
    Availability affects only the final total-work goal. Wednesday photo is
    already included in photo and necessary work, so it is never counted twice.
    """
    if not any(weights.values()):
        weights = dict.fromkeys(people, 1)
    terms, scores, floors = {}, {}, {}
    request_terms, request_scores = {}, {}
    for kind in ("necessary", "photo", "tx", "wed", "all", "follow"):
        shares = weights if kind == "all" else dict.fromkeys(people, 1)
        scale = sum(shares.values())
        adjusted = kind in ("necessary", "photo", "all")
        shifts = {p: offsets.get(p, 0) if adjusted else 0 for p in people}
        total = sum(original[p, kind] for p in people)
        pool = total - sum(shifts.values())
        targets = {p: pool * shares[p] + scale * shifts[p] for p in people}
        floors[kind] = square_floor(total, scale, targets)
        requested_targets = {p: scale * (round(total * shares[p] / scale) + shifts[p])
                             for p in people if shifts[p]}
        bound = max([abs(t) for t in (*targets.values(), *requested_targets.values())] + [0]) + scale * total + 1
        terms[kind], scores[kind] = [], 0
        request_terms[kind], request_scores[kind] = [], 0
        for p in people:
            diff = model.new_int_var(-bound, bound, f"workload/{kind}/{p}")
            model.add(diff == scale * sum(totals[p, kind]) - targets[p])
            square = model.new_int_var(0, bound * bound, f"workload_square/{kind}/{p}")
            model.add_multiplication_equality(square, [diff, diff])
            terms[kind].append(square)
            old_diff = scale * original[p, kind] - targets[p]
            scores[kind] += old_diff ** 2
            if shifts[p]:
                requested = requested_targets[p]
                gap = model.new_int_var(0, bound, f"request/{kind}/{p}")
                model.add_abs_equality(gap, scale * sum(totals[p, kind]) - requested)
                request_terms[kind].append(gap)
                request_scores[kind] += abs(scale * original[p, kind] - requested)
    # Fix individual photo and treatment targets first. Follow assignments
    # then absorb unavoidable differences while balancing the total number of
    # worked sessions according to each person's actual availability.
    groups = [(["photo"], True), (["photo"], False),
              (["tx"], False), (["wed"], False),
              (["necessary"], True), (["necessary"], False),
              (["all"], True), (["all"], False), (["follow"], False)]
    objectives, incumbent, bounds = [], [], []
    for kinds, request in groups:
        source, values = (request_terms, request_scores) if request else (terms, scores)
        objective = [v for kind in kinds for v in source[kind]]
        if objective:
            objectives.append(objective)
            incumbent.append(sum(values[kind] for kind in kinds))
            bounds.append(0 if request else sum(floors[kind] for kind in kinds))
    return objectives, incumbent, bounds

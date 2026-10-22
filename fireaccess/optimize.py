"""Location-allocation: Maximal Covering Location Problem (ILP) and small-p exact p-median.

MCLP (Church & ReVelle 1974), with existing stations fixed open:
    max  sum_i w_i y_i
    s.t. y_i <= sum_{j in N_i} x_j     for demand i not already covered
         sum_j x_j = p,  x_j in {0,1},  0 <= y_i <= 1
where N_i is the set of candidate sites that reach demand i within the standard S.

p-median (Hakimi 1964) with existing stations fixed: choose p new sites minimising the
population-weighted mean drive time to the nearest station. For p <= 2 we enumerate every
site (or pair of sites), which is exact and fast, and use the same enumeration to verify the
MCLP solver.
"""
from __future__ import annotations

import itertools

import numpy as np


def mclp(cover: np.ndarray, w: np.ndarray, p: int, already: np.ndarray | None = None,
         time_limit: float = 300) -> dict:
    """Solve MCLP. `cover` is bool (demand x candidate); `already` marks demand covered today.

    Returns dict(sites=[candidate positions], gain=weight newly covered, status, solver).
    """
    import pulp

    already = np.zeros(len(w), bool) if already is None else already
    rows = np.flatnonzero(~already & cover.any(axis=1) & (w > 0))
    sub, ws = cover[rows], w[rows].astype(float)
    cols = np.flatnonzero(sub.any(axis=0))
    if len(cols) == 0:
        return {"sites": [], "gain": 0.0, "status": "nothing to cover", "solver": None}
    p_eff = min(p, len(cols))
    sub = sub[:, cols]
    prob = pulp.LpProblem("MCLP", pulp.LpMaximize)
    x = [_var(prob, f"x{j}", cat="Binary") for j in range(len(cols))]
    y = [_var(prob, f"y{i}", lowBound=0, upBound=1) for i in range(len(rows))]
    prob += pulp.lpSum(float(ws[i]) * y[i] for i in range(len(rows)))
    for i in range(len(rows)):
        prob += y[i] <= pulp.lpSum(x[j] for j in np.flatnonzero(sub[i]))
    prob += pulp.lpSum(x) == p_eff
    solver, name = _solver(time_limit)
    st = prob.solve(solver)
    chosen = [int(cols[j]) for j in range(len(cols)) if (x[j].value() or 0) > 0.5]
    gain = float(w[rows][cover[rows][:, chosen].any(axis=1)].sum()) if chosen else 0.0
    return {"sites": sorted(chosen), "gain": gain, "status": _status_name(st), "solver": name}


def _var(prob, name, **kw):
    """PuLP >= 4 creates variables on the problem; older PuLP uses LpVariable(...)."""
    import pulp

    if hasattr(prob, "add_variable"):
        return prob.add_variable(name, **kw)
    return pulp.LpVariable(name, **kw)


def _status_name(st) -> str:
    import pulp

    if hasattr(st, "status"):  # PuLP >= 4 returns an LpSolveStats object
        return st.status.name
    return pulp.LpStatus[st]


def _solver(time_limit):
    import pulp

    if "HiGHS" in pulp.listSolvers(onlyAvailable=True):
        return pulp.HiGHS(msg=False, timeLimit=time_limit), "HiGHS (highspy) via PuLP"
    return pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit), "CBC via PuLP"


def enumerate_best(T: np.ndarray, w: np.ndarray, current: np.ndarray, p: int, objective: str,
                   standard_s: float | None = None) -> dict:
    """Exact best set of p (1 or 2) new sites by exhaustive search.

    T: drive times (demand x candidate, s); current: time to nearest existing station (s).
    objective "coverage": maximise weight with min time <= standard_s.
    objective "median": minimise weighted mean of min time.
    """
    if p not in (1, 2):
        raise ValueError("enumeration is implemented for p = 1 or 2")
    w = w.astype(np.float64)
    if objective == "coverage":
        S = standard_s
        base = current <= S
        reach = T <= S
        gain1 = (w[:, None] * (reach & ~base[:, None])).sum(axis=0)
        if p == 1:
            j = int(np.argmax(gain1))
            return {"sites": [j], "value": float(gain1[j])}
        # pair gain = g_a + g_b - (weight newly reached by both a and b)
        R = (reach & ~base[:, None]).astype(np.float64)
        both = (R * w[:, None]).T @ R
        pair = gain1[:, None] + gain1[None, :] - both
        pair[np.tril_indices_from(pair)] = -np.inf
        a, b = np.unravel_index(int(np.argmax(pair)), pair.shape)
        return {"sites": [int(a), int(b)], "value": float(pair[a, b])}
    if objective == "median":
        W = w.sum()
        cur = current.astype(np.float32)
        m1 = (w[:, None] * np.minimum(cur[:, None], T)).sum(axis=0) / W
        if p == 1:
            j = int(np.argmin(m1))
            return {"sites": [j], "value": float(m1[j])}
        best = (np.inf, None)
        for a in range(T.shape[1] - 1):
            base = np.minimum(cur, T[:, a])
            v = (w[:, None] * np.minimum(base[:, None], T[:, a + 1:])).sum(axis=0) / W
            k = int(np.argmin(v))
            if v[k] < best[0]:
                best = (float(v[k]), [a, a + 1 + k])
        return {"sites": best[1], "value": best[0]}
    raise ValueError(objective)


def brute_force_mclp(cover: np.ndarray, w: np.ndarray, p: int, already: np.ndarray) -> float:
    """Reference optimum for tiny instances (used in tests)."""
    best = 0.0
    for combo in itertools.combinations(range(cover.shape[1]), p):
        c = ~already & cover[:, list(combo)].any(axis=1)
        best = max(best, float(w[c].sum()))
    return best

"""Shortest drive times, coverage counts and service-area (isochrone) polygons."""
from __future__ import annotations

import numpy as np
from scipy.sparse.csgraph import dijkstra


def nearest_facility(csr, sources: np.ndarray, limit: float = np.inf, offsets: np.ndarray | None = None):
    """Multi-source Dijkstra on a directed graph: time from the nearest source to every node.

    `offsets` (s, one per source) is a head start added before leaving the source, e.g. the
    station's driveway to the road. Returns (time_s, source_position) with source_position
    indexing `sources` (-1 where nothing is reachable within `limit`).
    """
    sources = np.asarray(sources)
    if offsets is not None and np.any(offsets):
        t = dijkstra(csr, directed=True, indices=sources, limit=limit) + np.asarray(offsets)[:, None]
        t[t > limit] = np.inf
        which = np.argmin(t, axis=0)
        best = t[which, np.arange(t.shape[1])]
        return best, np.where(np.isfinite(best), which, -1)
    t, _, src = dijkstra(csr, directed=True, indices=sources, min_only=True,
                         return_predecessors=True, limit=limit)
    lookup = {int(s): i for i, s in enumerate(sources)}
    which = np.array([lookup.get(int(s), -1) for s in src])
    return t, which


def facility_times(csr, sources: np.ndarray, targets: np.ndarray, extra: np.ndarray | None = None,
                   limit: float = np.inf, chunk: int = 100) -> np.ndarray:
    """Matrix (targets x sources) of drive time from each source to each target node (s).

    `extra` (per target) is added, e.g. off-network access time from the snapped node.
    """
    out = np.empty((len(targets), len(sources)), dtype=np.float32)
    for a in range(0, len(sources), chunk):
        t = dijkstra(csr, directed=True, indices=sources[a:a + chunk], limit=limit)
        out[:, a:a + chunk] = t[:, targets].T
    if extra is not None:
        out += extra[:, None].astype(np.float32)
    return out


def covered_share(times_s: np.ndarray, pop: np.ndarray, minutes) -> dict:
    """Population and share with time <= each threshold (minutes)."""
    tot = float(pop.sum())
    res = {}
    for m in minutes:
        p = float(pop[times_s <= m * 60].sum())
        res[m] = {"population": int(round(p)), "share": round(p / tot, 4) if tot else 0.0}
    return res


def coverage_curve(times_s: np.ndarray, pop: np.ndarray, max_min: float = 20, step: float = 0.25):
    """(minutes, cumulative share of population reached) for a coverage curve."""
    mins = np.arange(0, max_min + step / 2, step)
    order = np.argsort(times_s)
    t, w = times_s[order] / 60, pop[order].astype(float)
    cum = np.cumsum(w) / w.sum()
    idx = np.searchsorted(t, mins, side="right")
    share = np.where(idx > 0, cum[np.maximum(idx - 1, 0)], 0.0)
    return mins, share


def weighted_percentile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    o = np.argsort(values)
    v, w = values[o], weights[o].astype(float)
    c = np.cumsum(w) / w.sum()
    return float(v[np.searchsorted(c, q / 100)])


def isochrones(edges_utm, node_time: dict, thresholds_min, buffer_m: float = 200.0,
               simplify_m: float = 40.0):
    """Service-area polygons from the edges reachable within each threshold.

    `edges_utm` is an OSMnx edge GeoDataFrame (index u, v, key; geometry drawn u -> v) in metres;
    `node_time` maps node id -> drive time (s) from the nearest facility. An edge is kept in full
    if its far end is reached in time, or cut at the fraction that can be driven before the limit
    (like a trimmed Network Analyst service area). The kept lines are buffered and dissolved.
    Polygons are for display; population counts come from network times of blocks.
    """
    import shapely
    from shapely.ops import substring

    u = edges_utm.index.get_level_values(0)
    tu = np.array([node_time.get(n, np.inf) for n in u])
    tt = edges_utm["tt"].to_numpy()
    geoms = edges_utm.geometry.to_numpy()
    out = {}
    for m in sorted(thresholds_min):
        T = m * 60.0
        lines = []
        for g, a, d in zip(geoms, tu, tt):
            if a >= T:
                continue
            frac = 1.0 if a + d <= T else (T - a) / d
            lines.append(g if frac >= 1 else substring(g, 0, frac, normalized=True))
        poly = shapely.union_all(shapely.buffer(np.array(lines), buffer_m, quad_segs=4))
        out[m] = poly.simplify(simplify_m)
    return out


def grid_candidates(xy: np.ndarray, eligible: np.ndarray, cell_m: float, inside: np.ndarray) -> np.ndarray:
    """One candidate node per grid cell: the eligible node closest to the cell centre.

    `xy` are projected node coordinates (n x 2). Returns positions into `xy`.
    """
    ok = np.flatnonzero(eligible & inside)
    cx = np.floor(xy[ok, 0] / cell_m)
    cy = np.floor(xy[ok, 1] / cell_m)
    centre = np.column_stack([(cx + 0.5) * cell_m, (cy + 0.5) * cell_m])
    d = np.hypot(*(xy[ok] - centre).T)
    best: dict[tuple, tuple[float, int]] = {}
    for key, dist, i in zip(zip(cx, cy), d, ok):
        if dist < best.get(key, (np.inf, -1))[0]:
            best[key] = (dist, i)
    return np.array(sorted(i for _, i in best.values()))

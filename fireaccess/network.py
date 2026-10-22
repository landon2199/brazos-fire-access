"""Road network: download (OSMnx), speeds, strong connectivity, sparse travel-time graph.

Travel time on an edge = length / (imputed free-flow speed x speed factor). No traffic,
no turn or intersection penalties, no signal delay.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import scipy.sparse as sp

# OSM highway values kept from the "drive" network that are poor sites for a station
# (grade-separated or ramps); used only to filter candidate sites.
_NO_SITE_HWY = {"motorway", "motorway_link", "trunk_link", "primary_link"}


def download_graph(polygon, cache_dir: Path, name: str):
    """OSM drive network inside `polygon` (EPSG:4326), cached as GraphML."""
    import osmnx as ox

    cache_dir.mkdir(parents=True, exist_ok=True)
    ox.settings.cache_folder = str(cache_dir / "osmnx_http")
    ox.settings.use_cache = True
    path = cache_dir / f"{name}_drive.graphml"
    if path.exists():
        return ox.load_graphml(path)
    G = with_retry(lambda: ox.graph_from_polygon(polygon, network_type="drive", simplify=True, retain_all=True,
                                                 truncate_by_edge=True))
    ox.save_graphml(G, path)
    return G


OVERPASS = ["https://overpass-api.de/api", "https://overpass.private.coffee/api",
            "https://overpass.kumi.systems/api"]


def with_retry(fn, tries: int = 6, wait: float = 20.0):
    """Call an Overpass-backed OSMnx function, rotating public endpoints with backoff when busy."""
    import time

    import osmnx as ox

    for k in range(tries):
        ox.settings.overpass_url = OVERPASS[k % len(OVERPASS)]
        try:
            return fn()
        except Exception as e:  # busy / gateway errors from public Overpass servers
            if k == tries - 1:
                raise
            print(f"   Overpass busy ({type(e).__name__}); retrying via {OVERPASS[(k + 1) % len(OVERPASS)]}")
            time.sleep(wait * (1 + k // len(OVERPASS)))


def prepare(G, fallback_kph: float = 48.0):
    """Impute speeds, keep the largest strongly connected component.

    Returns (G_scc, info) where info records what was dropped.
    """
    import osmnx as ox

    G = ox.routing.add_edge_speeds(G, fallback=fallback_kph)
    n0, e0 = G.number_of_nodes(), G.number_of_edges()
    len0 = sum(d["length"] for _, _, d in G.edges(data=True))
    Gs = ox.truncate.largest_component(G, strongly=True)
    len1 = sum(d["length"] for _, _, d in Gs.edges(data=True))
    # per-highway-type speeds actually used (first tag if a list)
    by_type: dict[str, list[float]] = {}
    tagged = 0
    for _, _, d in Gs.edges(data=True):
        h = d.get("highway")
        h = h[0] if isinstance(h, list) else h
        by_type.setdefault(h, []).append(d["speed_kph"])
        tagged += d.get("maxspeed") is not None
    info = {
        "nodes_downloaded": n0, "edges_downloaded": e0,
        "nodes_kept": Gs.number_of_nodes(), "edges_kept": Gs.number_of_edges(),
        "nodes_dropped": n0 - Gs.number_of_nodes(), "edges_dropped": e0 - Gs.number_of_edges(),
        "road_km_downloaded": round(len0 / 1000, 1), "road_km_kept": round(len1 / 1000, 1),
        "share_edges_with_maxspeed_tag": round(tagged / max(Gs.number_of_edges(), 1), 3),
        "fallback_kph": fallback_kph,
        "mean_speed_kph_by_type": {k: round(float(np.mean(v)), 1) for k, v in
                                   sorted(by_type.items(), key=lambda kv: -len(kv[1])) if k},
    }
    return Gs, info


def to_csr(G, speed_factor: float = 1.0):
    """Directed sparse matrix of edge travel times (seconds) + node id order.

    Parallel edges keep the fastest one.
    """
    nodes = np.array(list(G.nodes), dtype=np.int64)
    index = {n: i for i, n in enumerate(nodes)}
    best: dict[tuple[int, int], float] = {}
    for u, v, d in G.edges(data=True):
        t = d["length"] / (d["speed_kph"] * speed_factor / 3.6)
        k = (index[u], index[v])
        if k[0] != k[1] and t < best.get(k, np.inf):
            best[k] = t
    rows = np.fromiter((k[0] for k in best), dtype=np.int64, count=len(best))
    cols = np.fromiter((k[1] for k in best), dtype=np.int64, count=len(best))
    vals = np.fromiter(best.values(), dtype=np.float64, count=len(best))
    vals = np.maximum(vals, 1e-3)  # csgraph treats explicit 0 as "no edge"
    n = len(nodes)
    return sp.csr_matrix((vals, (rows, cols)), shape=(n, n)), nodes


def site_eligible(G, nodes: np.ndarray) -> np.ndarray:
    """True for nodes touching at least one surface-street edge (not only ramps/freeway)."""
    ok = np.zeros(len(nodes), dtype=bool)
    pos = {n: i for i, n in enumerate(nodes)}
    for u, v, d in G.edges(data=True):
        h = d.get("highway")
        hs = set(h) if isinstance(h, list) else {h}
        if not hs <= _NO_SITE_HWY:
            ok[pos[u]] = ok[pos[v]] = True
    return ok


def split_edges_at_points(G, lon, lat, utm: str, end_tol_m: float = 1.0):
    """Attach points to the road network at the nearest point on the nearest EDGE.

    For each point the nearest edge (u, v) is found, a new node is inserted at the projected
    point, and the edge is split into u -> new -> v with lengths (and so travel times) in
    proportion; the opposite-direction edge v -> u of a two-way road is split the same way.
    If the projected point is within `end_tol_m` of an end node, that node is used instead.
    Modifies G in place. Returns (node ids, perpendicular point-to-road distance in m).
    """
    import geopandas as gpd
    import osmnx as ox
    from pyproj import Transformer
    from shapely import STRtree
    import shapely
    from shapely.ops import substring

    edges = ox.graph_to_gdfs(G, nodes=False, fill_edge_geometry=True).to_crs(utm)
    keys = list(edges.index)
    geoms = list(edges.geometry)
    to_wgs = Transformer.from_crs(utm, 4326, always_xy=True).transform
    pts = gpd.GeoSeries(gpd.points_from_xy(lon, lat), crs=4326).to_crs(utm)
    next_id = max(G.nodes) + 1
    out_nodes, perp = [], []

    def _replace(i, a, b, k, n, g_a, g_b, frac):
        """Replace edge keys[i] = (a, b, k) by a -> n and n -> b."""
        d = dict(G.edges[a, b, k])
        G.remove_edge(a, b, k)
        for (s, t), g, part in (((a, n), g_a, frac), ((n, b), g_b, 1 - frac)):
            dd = dict(d, length=d["length"] * part, geometry=shapely.transform(g, lambda c: np.column_stack(to_wgs(c[:, 0], c[:, 1]))))
            kk = G.add_edge(s, t, **dd)
            if (s, t) == (a, n):
                keys[i], geoms[i] = (s, t, kk), g
            else:
                keys.append((s, t, kk))
                geoms.append(g)

    for p in pts:
        tree = STRtree(geoms)
        i = int(tree.nearest(p))
        u, v, k = keys[i]
        g = geoms[i]
        perp.append(float(g.distance(p)))
        L, s = g.length, g.project(p)
        if s <= end_tol_m:
            out_nodes.append(u)
            continue
        if L - s <= end_tol_m:
            out_nodes.append(v)
            continue
        q = g.interpolate(s)
        x, y = to_wgs(q.x, q.y)
        n = next_id
        next_id += 1
        G.add_node(n, x=x, y=y, street_count=2)
        length = G.edges[u, v, k]["length"]
        # reverse edge of a two-way road: same length, opposite direction
        rev = None
        if G.has_edge(v, u):
            for k2, d2 in G[v][u].items():
                if abs(d2["length"] - length) <= max(1.0, 0.01 * length):
                    rev = k2
                    break
        _replace(i, u, v, k, n, substring(g, 0, s), substring(g, s, L), s / L)
        if rev is not None:
            j = keys.index((v, u, rev))
            gr = geoms[j]
            sr = gr.length - s
            _replace(j, v, u, rev, n, substring(gr, 0, sr), substring(gr, sr, gr.length), sr / gr.length)
        out_nodes.append(n)
    return np.array(out_nodes, dtype=np.int64), np.array(perp)

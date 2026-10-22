"""Fast offline tests on tiny synthetic inputs (no downloads)."""
import fireaccess  # noqa: F401  (runtime paths on Windows)

import numpy as np
import pytest
import scipy.sparse as sp
from shapely.geometry import LineString, Point

from fireaccess import coverage, optimize


def line_graph(n=6, t=60.0, one_way=None):
    """Path 0-1-...-(n-1), two-way edges of `t` seconds; optional one-way (u, v) override."""
    r, c, v = [], [], []
    for i in range(n - 1):
        for a, b in ((i, i + 1), (i + 1, i)):
            if one_way and (b, a) == one_way:
                continue
            r.append(a); c.append(b); v.append(t)
    return sp.csr_matrix((v, (r, c)), shape=(n, n))


def test_multi_source_nearest():
    g = line_graph(6)
    t, which = coverage.nearest_facility(g, np.array([0, 5]))
    assert np.allclose(t, [0, 60, 120, 120, 60, 0])
    assert list(which[[0, 1, 4, 5]]) == [0, 0, 1, 1]


def test_direction_matters():
    # edge 2->1 removed: from source 5, node 1 must be reached... only via 2->1 (gone), so unreachable
    g = line_graph(6, one_way=(1, 2))  # keep 1->2, drop 2->1
    t, which = coverage.nearest_facility(g, np.array([5]))
    assert np.isinf(t[0]) and np.isinf(t[1]) and t[2] == 180
    assert which[0] == -1


def test_facility_times_matrix_with_access():
    g = line_graph(4)
    T = coverage.facility_times(g, np.array([0, 3]), np.array([1, 2]), extra=np.array([10.0, 0.0]))
    assert T.shape == (2, 2)
    assert np.allclose(T, [[70, 130], [120, 60]])


def test_covered_share_and_curve():
    times = np.array([100, 200, 300, 600], float)  # seconds
    pop = np.array([10, 20, 30, 40])
    cov = coverage.covered_share(times, pop, [4, 8])
    assert cov[4]["population"] == 30 and cov[4]["share"] == 0.3
    assert cov[8]["population"] == 60
    m, s = coverage.coverage_curve(times, pop, max_min=10, step=1)
    assert s[0] == 0 and s[2] == pytest.approx(0.1) and s[-1] == pytest.approx(1.0)
    assert np.all(np.diff(s) >= 0)


def test_isochrone_trims_partial_edge():
    import geopandas as gpd
    import pandas as pd

    # two 1 km edges in a row from node 1 (t=0); each takes 100 s
    idx = pd.MultiIndex.from_tuples([(1, 2, 0), (2, 3, 0)], names=["u", "v", "key"])
    e = gpd.GeoDataFrame({"tt": [100.0, 100.0]}, index=idx, crs=32614,
                         geometry=[LineString([(0, 0), (1000, 0)]), LineString([(1000, 0), (2000, 0)])])
    iso = coverage.isochrones(e, {1: 0.0, 2: 100.0, 3: 200.0}, [2.5], buffer_m=10, simplify_m=0)[2.5]
    # 150 s reaches 1.5 km along the line
    assert iso.contains(Point(1400, 0)) and not iso.contains(Point(1600, 0))


def test_mclp_toy_known_optimum():
    # 5 demand points, 4 candidates; demand 0 already covered
    cover = np.array([[1, 0, 0, 0],
                      [1, 1, 0, 0],
                      [0, 1, 0, 1],
                      [0, 0, 1, 1],
                      [0, 0, 1, 0]], bool)
    w = np.array([100, 5, 7, 4, 6], float)
    already = np.array([True, False, False, False, False])
    r1 = optimize.mclp(cover, w, 1, already)
    assert r1["gain"] == 12 and r1["sites"] == [1]           # covers 5 + 7
    r2 = optimize.mclp(cover, w, 2, already)
    assert r2["gain"] == optimize.brute_force_mclp(cover, w, 2, already) == 22
    assert r2["status"] == "Optimal"


def test_enumeration_matches_ilp_and_pmedian():
    rng = np.random.default_rng(0)
    T = rng.uniform(60, 900, size=(40, 12)).astype(np.float32)
    w = rng.integers(1, 50, 40).astype(float)
    cur = rng.uniform(200, 1200, 40)
    for p in (1, 2):
        e = optimize.enumerate_best(T, w, cur, p, "coverage", 480)
        r = optimize.mclp(T <= 480, w, p, already=cur <= 480)
        assert e["value"] == pytest.approx(r["gain"])
    # p-median by enumeration equals brute force
    e = optimize.enumerate_best(T, w, cur, 2, "median")
    best = min(((w * np.minimum(cur, T[:, [a, b]].min(axis=1))).sum() / w.sum(), (a, b))
               for a in range(12) for b in range(a + 1, 12))
    assert e["value"] == pytest.approx(best[0], rel=1e-5) and tuple(e["sites"]) == best[1]


def test_grid_candidates_one_per_cell():
    xy = np.array([[10, 10], [50, 50], [90, 90], [150, 50], [160, 60]], float)
    ok = np.ones(5, bool)
    c = coverage.grid_candidates(xy, ok, 100, ok)
    assert sorted(c.tolist()) == [1, 3]  # nodes closest to centres (50,50) and (150,50)


def test_station_supplement_merge(tmp_path):
    """Supplement rows are added unless excluded, unplaced, outside the area or already mapped."""
    import geopandas as gpd
    from shapely.geometry import box

    from fireaccess import demand

    area = box(-96.5, 30.5, -96.2, 30.8)
    osm = gpd.GeoDataFrame({"name": ["A"], "id": ["1"]}, geometry=[Point(-96.35, 30.65)], crs=4326)
    csv = tmp_path / "sup.csv"
    csv.write_text("name,lat,lon,include\n"
                   "new,30.70,-96.30,1\n"          # added
                   "dup,30.6505,-96.3505,1\n"      # ~70 m from A: already mapped
                   "outside,31.50,-96.30,1\n"      # outside the area
                   "excluded,30.60,-96.40,0\n"     # include = 0
                   "unplaced,,,1\n")               # no coordinates
    out, info = demand.add_supplement(osm, csv, area, "EPSG:32614")
    assert list(out["name"]) == ["A", "new"]
    assert list(out["source"]) == ["OpenStreetMap", "supplement"]
    assert info["supplement_added"] == 1 and len(info["supplement_skipped"]) == 4
    same, _ = demand.add_supplement(osm, None, area, "EPSG:32614")
    assert len(same) == 1


def test_offsets_head_start():
    g = line_graph(5)
    t, which = coverage.nearest_facility(g, np.array([0, 4]), offsets=np.array([0.0, 100.0]))
    # node 2 is 120 s from node 0 and 120 + 100 s from node 4 -> served by source 0
    assert np.allclose(t, [0, 60, 120, 160, 100]) and which[2] == 0 and which[4] == 1


def test_split_edge_two_way():
    import networkx as nx
    from shapely.geometry import LineString as LS

    from fireaccess import network

    G = nx.MultiDiGraph(crs="EPSG:4326")
    G.add_node(1, x=-96.0, y=30.0)
    G.add_node(2, x=-96.0104, y=30.0)  # ~1 km west
    for a, b in ((1, 2), (2, 1)):
        G.add_edge(a, b, length=1000.0, speed_kph=36.0, highway="residential",
                   geometry=LS([(G.nodes[a]["x"], G.nodes[a]["y"]), (G.nodes[b]["x"], G.nodes[b]["y"])]))
    nodes, gap = network.split_edges_at_points(G, np.array([-96.0026]), np.array([30.0005]), "EPSG:32614")
    n = int(nodes[0])
    assert n not in (1, 2) and G.number_of_edges() == 4
    assert G.edges[1, n, 0]["length"] == pytest.approx(250, abs=5)
    assert G.edges[n, 2, 0]["length"] == pytest.approx(750, abs=5)
    assert G.edges[2, n, 0]["length"] == pytest.approx(750, abs=5)
    assert G.edges[n, 1, 0]["length"] == pytest.approx(250, abs=5)
    assert gap[0] == pytest.approx(55, abs=5)  # 0.0005 deg latitude
    csr, order = network.to_csr(G)
    t, _ = coverage.nearest_facility(csr, np.array([list(order).index(n)]))
    assert sorted(np.round(t)) == [0, 25, 75]  # 36 km/h = 10 m/s

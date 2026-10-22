"""How fast can a fire engine reach you, and where would one more station help most?

Pipeline: OSM drive network + OSM fire stations + 2020 Census block populations ->
multi-source Dijkstra drive times -> 4/8/12-minute service areas -> Maximal Covering
Location Problem and p-median for 1-2 new stations -> speed sensitivity -> figures, map.

    python run.py                          # Brazos County, TX (FIPS 48041)
    python run.py --standards 4 8 --new 2  # coverage standards (min) and new stations
    python run.py --speed-factor 0.9       # everyone drives 10% slower than the imputed speeds
"""
from __future__ import annotations

import fireaccess  # noqa: F401  (sets Windows/conda runtime paths first)

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from fireaccess import coverage, demand, maps, network, optimize

ROOT = Path(__file__).resolve().parent
UTM = "EPSG:32614"
TYPO = {"Colloge": "College", "Deapartment": "Department"}  # display-only fixes of OSM name typos
# display-only labels for OSM stations matched (within ~200 m) to a department-reported address;
# see "Fire stations" in README.md
OSM_LABELS = {"909084779": "Brazos County Precinct 4 VFD Station 1 (unnamed in OSM)",
              "1241674515": "Brushy Station (Brazos County Precinct 4 VFD Station 3)"}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default="48", help="state FIPS (48 = Texas)")
    p.add_argument("--county", default="041", help="county FIPS (041 = Brazos)")
    p.add_argument("--standards", nargs="+", type=float, default=[4, 8], help="coverage standards for MCLP (min)")
    p.add_argument("--thresholds", nargs="+", type=float, default=[4, 8, 12], help="service-area breaks (min)")
    p.add_argument("--new", type=int, default=2, help="solve MCLP for 1..N new stations")
    p.add_argument("--speed-factor", type=float, default=1.0, help="multiplier on imputed road speeds")
    p.add_argument("--sensitivity", type=float, default=0.2, help="± share for the speed sensitivity run")
    p.add_argument("--fallback-kph", type=float, default=48.0,
                   help="speed for highway types with no maxspeed data at all (48 km/h = 30 mph)")
    p.add_argument("--access-kph", type=float, default=20.0,
                   help="speed for the straight-line hop from a block's internal point to its road node")
    p.add_argument("--station-access-kph", type=float, default=30.0,
                   help="speed for the gap between a station point and the road edge it joins")
    p.add_argument("--cell-km", type=float, default=1.6, help="candidate-site grid cell size (km)")
    p.add_argument("--graph-buffer-km", type=float, default=2.0, help="road network buffer beyond the county")
    p.add_argument("--no-supplement", action="store_true",
                   help="use OSM stations only (skip data_sources/stations_supplement.csv)")
    p.add_argument("--no-basemap", action="store_true", help="skip contextily basemap tiles")
    return p.parse_args()


def clean(name: str) -> str:
    for a, b in TYPO.items():
        name = name.replace(a, b)
    return name


MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary"}


def describe_site(edges, x: float, y: float) -> str:
    """General location: distance to the nearest named major road (no street addresses)."""
    pt = gpd.GeoSeries(gpd.points_from_xy([x], [y]), crs=4326).to_crs(UTM).iloc[0]
    maj = edges[edges["hwy"].isin(MAJOR) & edges["label"].notna()]
    d = maj.distance(pt)
    i = d.idxmin()
    km = d[i] / 1000
    return f"{maj.at[i, 'label']}" + ("" if km < 0.15 else f" (~{km:.1f} km away)")


def _road_label(name, ref):
    for v in (ref, name):
        if isinstance(v, list):
            v = v[0]
        if isinstance(v, str) and v:
            return v
    return None


def main():
    a = parse_args()
    t0 = time.time()
    raw, cache = ROOT / "data" / "raw", ROOT / "data" / "cache"
    figs, res, docs = ROOT / "figures", ROOT / "results", ROOT / "docs"
    for d in (figs, res, docs):
        d.mkdir(exist_ok=True)
    tag = a.state + a.county

    print("1/8 census blocks")
    blocks = demand.load_blocks(a.state, a.county, raw, cache)
    county_poly = demand.county_polygon(blocks)
    county = gpd.GeoDataFrame(geometry=[county_poly], crs=4326)
    county_utm = county.to_crs(UTM).geometry.iloc[0]

    print("2/8 road network")
    buf = county.to_crs(UTM).buffer(a.graph_buffer_km * 1000).to_crs(4326).iloc[0]
    G_raw = network.download_graph(buf, cache, tag)
    G, net_info = network.prepare(G_raw, a.fallback_kph)
    print("3/8 stations (snapped onto road edges)")
    stations, st_info = demand.load_stations(county_poly, cache, UTM)
    sup_csv = ROOT / "data_sources" / "stations_supplement.csv"
    stations, sup_info = demand.add_supplement(stations, None if a.no_supplement else sup_csv, county_poly, UTM)
    st_info["osm_stations"] = st_info.pop("stations")
    st_info.update(sup_info)
    print(f"   {st_info['osm_stations']} OSM stations + {sup_info['supplement_added']} from the supplement")
    stations["label"] = stations["name"].map(clean).replace("(unnamed)", "Unnamed OSM fire station")
    osm_id = stations["id"].astype("string") if "id" in stations else pd.Series(pd.NA, index=stations.index)
    stations["label"] = osm_id.map(OSM_LABELS).fillna(stations["label"])
    # attach each station to the nearest road EDGE (split it there), not just the nearest node
    st_node, st_snap = network.split_edges_at_points(G, stations.geometry.x.values, stations.geometry.y.values,
                                                     UTM)
    stations["snap_m"] = st_snap.round(0)
    st_off = st_snap / (a.station_access_kph / 3.6)  # station to road, timed
    csr, nodes = network.to_csr(G, a.speed_factor)
    st_idx = demand.to_index(st_node, nodes)
    import osmnx as ox
    edges = ox.graph_to_gdfs(G, nodes=False)
    edges["label"] = [_road_label(n, r) for n, r in zip(edges["name"], edges.get("ref", [None] * len(edges)))]
    edges = edges[["length", "speed_kph", "highway", "label", "geometry"]].to_crs(UTM)
    edges["tt"] = edges["length"] / (edges["speed_kph"] * a.speed_factor / 3.6)
    edges["hwy"] = edges["highway"].map(lambda h: h[0] if isinstance(h, list) else h)
    nx_, ny_ = (np.array([G.nodes[n][k] for n in nodes]) for k in ("x", "y"))
    nxy = gpd.GeoSeries(gpd.points_from_xy(nx_, ny_), crs=4326).to_crs(UTM)
    node_xy = np.column_stack([nxy.x, nxy.y])
    print(f"   {net_info['nodes_kept']:,} nodes / {net_info['edges_kept']:,} edges kept "
          f"({net_info['nodes_dropped']} nodes outside the largest strongly connected component)")

    popb = blocks[blocks["pop"] > 0].reset_index(drop=True)
    b_node, b_snap = demand.snap(G, popb["lon"].values, popb["lat"].values, UTM)
    b_idx = demand.to_index(b_node, nodes)
    access_s = b_snap / (a.access_kph / 3.6)
    w = popb["pop"].to_numpy(float)
    total_pop = int(w.sum())

    print("4/8 drive times from the nearest station")

    def station_times(c):
        nt, which = coverage.nearest_facility(c, st_idx, offsets=st_off)
        return nt, which, nt[b_idx] + access_s

    node_t, which, bt = station_times(csr)
    popb["t_min"] = bt / 60
    popb["first_due"] = stations["label"].to_numpy()[which[b_idx]]
    cov = coverage.covered_share(bt, w, a.thresholds)
    curve_now = coverage.coverage_curve(bt, w)
    # validation: drive time vs straight line to the station that serves each block
    bxy = gpd.GeoSeries(gpd.points_from_xy(popb["lon"], popb["lat"]), crs=4326).to_crs(UTM)
    sxy = stations.to_crs(UTM).geometry
    srv = which[b_idx]
    crow = np.hypot(bxy.x.values - sxy.x.values[srv], bxy.y.values - sxy.y.values[srv])
    far = crow > 2000
    eff_kph = crow[far] / 1000 / (bt[far] / 3600)
    max_speed = max(d["speed_kph"] for _, _, d in G.edges(data=True)) * a.speed_factor
    rng = np.random.default_rng(41)
    sample = rng.choice(np.flatnonzero(far), size=min(8, far.sum()), replace=False)
    validation = {
        "blocks_over_2km_from_station": int(far.sum()),
        "straight_line_speed_kph_median": round(float(np.median(eff_kph)), 1),
        "straight_line_speed_kph_p5_p95": [round(float(np.percentile(eff_kph, q)), 1) for q in (5, 95)],
        "max_edge_speed_kph": round(float(max_speed), 1),
        "blocks_faster_than_max_edge_speed": int((eff_kph > max_speed + 1e-6).sum()),
        "sample": [{"straight_line_km": round(float(crow[i] / 1000), 2), "drive_min": round(float(bt[i] / 60), 1),
                    "straight_line_kph": round(float(crow[i] / 1000 / (bt[i] / 3600)), 1)} for i in sample],
        "block_snap_m_median": round(float(np.median(b_snap)), 0),
        "block_snap_m_p95": round(float(np.percentile(b_snap, 95)), 0),
        "station_snap_m_max": round(float(st_snap.max()), 0),
    }
    print(f"   within 4/8/12 min: " + ", ".join(f"{cov[m]['share']:.1%}" for m in a.thresholds))

    print("5/8 candidate sites and drive-time matrix")
    inside = np.asarray(gpd.GeoSeries(gpd.points_from_xy(*node_xy.T), crs=UTM).within(county_utm))
    elig = network.site_eligible(G, nodes)
    cand = coverage.grid_candidates(node_xy, elig, a.cell_km * 1000, inside)
    cand = cand[~np.isin(cand, st_idx)]
    T = coverage.facility_times(csr, cand, b_idx, extra=access_s)
    print(f"   {len(cand)} candidates x {len(b_idx):,} populated blocks")
    cand_g = gpd.GeoDataFrame(geometry=gpd.points_from_xy(nx_[cand], ny_[cand]), crs=4326)

    print("6/8 location-allocation (MCLP + p-median)")

    def solve_mclp(Tm, bt_now, S, p):
        r = optimize.mclp(Tm <= S * 60, w, p, already=bt_now <= S * 60)
        return r

    mclp_res, plans, chosen = {}, {}, {}
    for S in a.standards:
        key = f"{S:g}"
        mclp_res[key] = {"covered_today": cov.get(S, coverage.covered_share(bt, w, [S])[S])}
        for p in range(1, a.new + 1):
            r = solve_mclp(T, bt, S, p)
            chosen[(S, p)] = r["sites"]
            entry = {"new_sites": [], "population_gained": int(round(r["gain"])),
                     "share_gained": round(r["gain"] / total_pop, 4), "solver": r["solver"], "status": r["status"]}
            newt = np.minimum(bt, T[:, r["sites"]].min(axis=1)) if r["sites"] else bt
            entry["covered_after"] = coverage.covered_share(newt, w, [S])[S]
            for j in r["sites"]:
                entry["new_sites"].append({"lon": round(float(nx_[cand[j]]), 5), "lat": round(float(ny_[cand[j]]), 5),
                                           "near": describe_site(edges, nx_[cand[j]], ny_[cand[j]]),
                                           "alone_gain": int(round(float(w[(T[:, j] <= S * 60) & (bt > S * 60)].sum())))})
            if p <= 2:  # exact check by enumeration
                e = optimize.enumerate_best(T, w, bt, p, "coverage", S * 60)
                entry["enumeration_check"] = {"best_value": int(round(e["value"])),
                                              "matches_ilp": abs(e["value"] - r["gain"]) < 0.5}
            mclp_res[key][f"add_{p}"] = entry
        s1 = mclp_res[key]["add_1"]["new_sites"]
        s2 = mclp_res[key].get("add_2", mclp_res[key]["add_1"])["new_sites"]
        mk = lambda s: gpd.GeoDataFrame(s, geometry=gpd.points_from_xy([x["lon"] for x in s], [x["lat"] for x in s]),
                                        crs=4326)
        plans[S] = {"sites1": mk(s1), "sites2": mk(s2), "gain1": mclp_res[key]["add_1"]["population_gained"],
                    "gain2": mclp_res[key].get("add_2", mclp_res[key]["add_1"])["population_gained"]}
        g1 = (T[:, chosen[(S, 1)]].min(axis=1) <= S * 60) & (bt > S * 60)
        g2 = (T[:, chosen[(S, min(2, a.new))]].min(axis=1) <= S * 60) & (bt > S * 60)
        popb[f"newcov_{S:g}"] = np.where(g1, 1, np.where(g2, 2, 0))
        print(f"   {S:g} min: today {mclp_res[key]['covered_today']['share']:.1%}; +1 site "
              f"+{plans[S]['gain1']:,}; +2 sites +{plans[S]['gain2']:,}")

    pmed = {"mean_drive_min_today": round(float((w * bt).sum() / w.sum() / 60), 2)}
    for p in (1, 2):
        e = optimize.enumerate_best(T, w, bt, p, "median")
        pmed[f"add_{p}"] = {"mean_drive_min": round(e["value"] / 60, 2),
                            "new_sites": [{"lon": round(float(nx_[cand[j]]), 5), "lat": round(float(ny_[cand[j]]), 5),
                                           "near": describe_site(edges, nx_[cand[j]], ny_[cand[j]])} for j in e["sites"]]}
    print(f"   p-median: mean {pmed['mean_drive_min_today']} -> {pmed['add_1']['mean_drive_min']} (+1) -> "
          f"{pmed['add_2']['mean_drive_min']} (+2) min")

    print("7/8 speed sensitivity")
    sens, band_curves = {}, []
    base_sites = {f"{S:g}": [(s["lon"], s["lat"]) for s in mclp_res[f"{S:g}"][f"add_{a.new}"]["new_sites"]]
                  for S in a.standards}
    for f in (1 - a.sensitivity, 1 + a.sensitivity):
        c2, _ = network.to_csr(G, a.speed_factor * f)
        _, _, bt2 = station_times(c2)
        band_curves.append(coverage.coverage_curve(bt2, w)[1])
        T2 = coverage.facility_times(c2, cand, b_idx, extra=access_s)
        entry = {"coverage_today": {f"{m:g}": coverage.covered_share(bt2, w, [m])[m] for m in a.thresholds}}
        for S in a.standards:
            r = solve_mclp(T2, bt2, S, a.new)
            pts = [(float(nx_[cand[j]]), float(ny_[cand[j]])) for j in r["sites"]]
            # distance from each re-optimised site to the closest base-case site
            bs = gpd.GeoSeries(gpd.points_from_xy(*zip(*base_sites[f"{S:g}"])), crs=4326).to_crs(UTM)
            ps = gpd.GeoSeries(gpd.points_from_xy(*zip(*pts)), crs=4326).to_crs(UTM)
            shift = [round(float(bs.distance(p).min() / 1000), 2) for p in ps]
            entry[f"mclp_{S:g}min_add_{a.new}"] = {"population_gained": int(round(r["gain"])),
                                                   "site_shift_km": shift}
        sens[f"speed_x{f:.2f}"] = entry
    band = (curve_now[0], np.minimum(*band_curves), np.maximum(*band_curves))

    print("8/8 service areas, figures, map")
    nt_map = dict(zip(nodes.tolist(), node_t.tolist()))
    iso = coverage.isochrones(edges, nt_map, a.thresholds)
    # after: existing + best pair for the largest standard
    Sx = max(a.standards)
    add_idx = [cand[np.argmin(np.hypot(nx_[cand] - s["lon"], ny_[cand] - s["lat"]))]
               for s in mclp_res[f"{Sx:g}"][f"add_{a.new}"]["new_sites"]]
    node_t_after, _ = coverage.nearest_facility(csr, np.concatenate([st_idx, add_idx]),
                                             offsets=np.concatenate([st_off, np.zeros(len(add_idx))]))
    iso_after = coverage.isochrones(edges, dict(zip(nodes.tolist(), node_t_after.tolist())), [Sx])
    bt_after = node_t_after[b_idx] + access_s
    curve_after = coverage.coverage_curve(bt_after, w)
    s1 = mclp_res[f"{Sx:g}"]["add_1"]["new_sites"]
    s1_idx = [cand[np.argmin(np.hypot(nx_[cand] - s["lon"], ny_[cand] - s["lat"]))] for s in s1]
    nt1, _ = coverage.nearest_facility(csr, np.concatenate([st_idx, s1_idx]),
                                    offsets=np.concatenate([st_off, np.zeros(len(s1_idx))]))
    curve_one = coverage.coverage_curve(nt1[b_idx] + access_s, w)
    tu = edges.index.get_level_values(0).map(nt_map).to_numpy()
    tv = edges.index.get_level_values(1).map(nt_map).to_numpy()
    edge_t = np.minimum(tu, tv) + edges["tt"].to_numpy() / 2

    bmap = not a.no_basemap
    blocks_plot = blocks.merge(popb[["geoid", "t_min"] + [f"newcov_{S:g}" for S in a.standards]], on="geoid",
                               how="left")
    for S in a.standards:
        blocks_plot[f"newcov_{S:g}"] = blocks_plot[f"newcov_{S:g}"].fillna(0)
    maps.drive_time_map(blocks_plot, stations, county, figs / "drive_time_map.png", bmap)
    maps.service_area_map(iso, stations, county, cov, figs / "service_areas.png", bmap)
    plans_k = {int(S) if S == int(S) else S: v for S, v in plans.items()}
    blocks_plot2 = blocks_plot.rename(columns={f"newcov_{S:g}": f"newcov_{int(S) if S == int(S) else S}"
                                               for S in a.standards})
    iso_k = {int(k) if k == int(k) else k: v for k, v in iso.items()}
    maps.mclp_map(blocks_plot2, stations, county, cand_g, iso_k, plans_k, figs / "mclp_sites.png", bmap)
    maps.coverage_curve({"Today (existing stations)": curve_now,
                         f"+1 site ({Sx:g}-min MCLP)": curve_one,
                         f"+{a.new} sites ({Sx:g}-min MCLP)": curve_after}, band, a.thresholds,
                        figs / "coverage_curve.png")
    cov_k = {int(k) if k == int(k) else k: v for k, v in cov.items()}
    g2 = mclp_res[f"{Sx:g}"][f"add_{a.new}"]
    hero_txt = (f"{a.new} well-placed new stations would bring\n{g2['population_gained']:,} more residents "
                f"within {Sx:g} min\n({cov[Sx]['share']:.0%} → {g2['covered_after']['share']:.0%})")
    maps.hero(edges, edge_t, stations, plans[Sx]["sites2"], county, cov_k, hero_txt, figs / "hero.png")
    iso_after_k = {int(Sx) if Sx == int(Sx) else Sx: iso_after[Sx]}
    maps.folium_map(iso_k, stations, plans_k, county, cov_k, docs / "map.html", iso_after_k)

    # small GeoJSON outputs
    st_out = stations[["label", "name", "source", "id", "snap_m", "geometry"]].rename(columns={"id": "osm_id"})
    st_out["population_first_due"] = st_out["label"].map(popb.groupby("first_due")["pop"].sum()).fillna(0).astype(int)
    st_out.to_file(res / "stations.geojson", driver="GeoJSON", COORDINATE_PRECISION=5)
    rows = []
    for S in a.standards:
        for p in range(1, a.new + 1):
            for s in mclp_res[f"{S:g}"][f"add_{p}"]["new_sites"]:
                rows.append({"standard_min": S, "n_new": p, **s})
    gpd.GeoDataFrame(rows, geometry=gpd.points_from_xy([r["lon"] for r in rows], [r["lat"] for r in rows]),
                     crs=4326).to_file(res / "proposed_sites.geojson", driver="GeoJSON", COORDINATE_PRECISION=5)
    sa = gpd.GeoDataFrame({"minutes": list(iso), "scenario": "today",
                           "population": [cov[m]["population"] for m in iso]}, geometry=list(iso.values()), crs=UTM)
    sa2 = gpd.GeoDataFrame({"minutes": [Sx], "scenario": f"with {a.new} new sites ({Sx:g}-min plan)",
                            "population": [g2["covered_after"]["population"]]}, geometry=[iso_after[Sx]], crs=UTM)
    sa_all = pd.concat([sa, sa2])
    sa_all["geometry"] = sa_all.geometry.simplify(80)
    sa_all.to_crs(4326).to_file(res / "service_areas.geojson", driver="GeoJSON", COORDINATE_PRECISION=5)

    first_due = (popb.groupby("first_due")["pop"].sum().sort_values(ascending=False).astype(int)).to_dict()
    metrics = {
        "area": {"state_fips": a.state, "county_fips": a.county, "name": "Brazos County, Texas"},
        "population_2020": total_pop,
        "blocks_total": int(len(blocks)), "blocks_populated": int(len(popb)),
        "network": net_info,
        "assumptions": {"speed_factor": a.speed_factor, "fallback_kph": a.fallback_kph,
                        "access_kph": a.access_kph, "station_access_kph": a.station_access_kph,
                        "station_snapping": "nearest road edge, split at the projected point (both directions)", "turn_penalties": False, "traffic": False,
                        "measure": "travel time only (no call processing, turnout or set-up)",
                        "candidate_grid_km": a.cell_km, "graph_buffer_km": a.graph_buffer_km},
        "stations": {**st_info, "names": stations["label"].tolist()},
        "coverage_today": {f"{m:g}": v for m, v in cov.items()},
        "drive_time_min": {"pop_weighted_mean": pmed["mean_drive_min_today"],
                           "pop_weighted_median": round(coverage.weighted_percentile(bt, w, 50) / 60, 2),
                           "pop_weighted_p90": round(coverage.weighted_percentile(bt, w, 90) / 60, 2),
                           "max": round(float(bt.max() / 60), 1)},
        "population_by_first_due_station": first_due,
        "validation": validation,
        "candidates": int(len(cand)),
        "mclp": mclp_res,
        "p_median": pmed,
        "sensitivity": sens,
        "runtime_s": None,
    }
    metrics["runtime_s"] = round(time.time() - t0, 1)
    (res / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"done in {metrics['runtime_s']} s")


if __name__ == "__main__":
    main()

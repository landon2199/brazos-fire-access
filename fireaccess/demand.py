"""Demand and supply points: 2020 Census blocks and OSM fire stations."""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

BLOCKS_URL = "https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/tl_2020_{state}_tabblock20.zip"


def download(url: str, dest: Path) -> Path:
    """Stream a file to disk once (skips if present)."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def load_blocks(state: str, county: str, raw_dir: Path, cache_dir: Path) -> gpd.GeoDataFrame:
    """2020 tabulation blocks for one county with POP20, internal points and polygons (EPSG:4326)."""
    cache = cache_dir / f"blocks_{state}{county}.gpkg"
    if cache.exists():
        return gpd.read_file(cache)
    z = download(BLOCKS_URL.format(state=state), raw_dir / f"tl_2020_{state}_tabblock20.zip")
    g = gpd.read_file(f"zip://{z}", where=f"COUNTYFP20 = '{county}'",
                      columns=["GEOID20", "COUNTYFP20", "ALAND20", "AWATER20", "INTPTLAT20",
                               "INTPTLON20", "HOUSING20", "POP20", "UR20"])
    g = g.to_crs(4326)
    g["lat"] = g["INTPTLAT20"].astype(float)
    g["lon"] = g["INTPTLON20"].astype(float)
    g = g.rename(columns={"GEOID20": "geoid", "POP20": "pop", "HOUSING20": "housing", "UR20": "ur"})
    g = g[["geoid", "pop", "housing", "ur", "ALAND20", "AWATER20", "lat", "lon", "geometry"]]
    cache_dir.mkdir(parents=True, exist_ok=True)
    g.to_file(cache, driver="GPKG")
    return g


def county_polygon(blocks: gpd.GeoDataFrame):
    """County outline as the union of its blocks (EPSG:4326)."""
    return blocks.geometry.union_all().buffer(0)


def load_stations(polygon, cache_dir: Path, utm: str, dedupe_m: float = 150.0) -> tuple[gpd.GeoDataFrame, dict]:
    """OSM amenity=fire_station features inside the polygon, as one point per station.

    Building polygons become their centroid; a node and a polygon tagged for the same station
    (closer than `dedupe_m`) are merged, keeping the one with a name.
    """
    import osmnx as ox

    cache = cache_dir / "osm_fire_stations.gpkg"
    if cache.exists():
        raw = gpd.read_file(cache)
    else:
        ox.settings.cache_folder = str(cache_dir / "osmnx_http")
        from fireaccess.network import with_retry
        f = with_retry(lambda: ox.features.features_from_polygon(polygon, tags={"amenity": "fire_station"}))
        f = f.reset_index()
        keep = [c for c in ["element", "id", "name", "operator", "ref", "addr:city", "geometry"] if c in f]
        raw = f[keep].copy()
        for c in raw.columns:
            if c != "geometry":
                raw[c] = raw[c].astype("string")
        raw.to_file(cache, driver="GPKG")
    pts = raw.to_crs(utm).copy()
    pts["geometry"] = pts.geometry.centroid
    pts["has_name"] = pts["name"].notna() if "name" in pts else False
    pts = pts.sort_values("has_name", ascending=False).reset_index(drop=True)
    keep_idx, dropped = [], []
    for i, p in enumerate(pts.geometry):
        dup = [j for j in keep_idx if pts.geometry[j].distance(p) < dedupe_m]
        (dropped if dup else keep_idx).append(i)
    out = pts.loc[keep_idx].reset_index(drop=True)
    out["name"] = out["name"].fillna("(unnamed)") if "name" in out else "(unnamed)"
    out = out[out.within(gpd.GeoSeries([polygon], crs=4326).to_crs(utm).iloc[0])].reset_index(drop=True)
    info = {"osm_features": int(len(raw)), "merged_duplicates": len(dropped), "stations": int(len(out))}
    return out.to_crs(4326), info


def add_supplement(stations: gpd.GeoDataFrame, csv: Path | None, polygon, utm: str,
                   min_sep_m: float = 300.0) -> tuple[gpd.GeoDataFrame, dict]:
    """Append stations from a documented CSV (official rosters) that OpenStreetMap is missing.

    Rows with include != 1 or no coordinates are skipped; a row within `min_sep_m` of an
    existing station is treated as already mapped and skipped. Rows outside the county are skipped.
    """
    use = csv is not None and csv.exists()
    info = {"supplement_file": f"{csv.parent.name}/{csv.name}" if use else None,
            "supplement_added": 0, "supplement_skipped": []}
    stations = stations.copy()
    stations["source"] = "OpenStreetMap"
    if not use:
        return stations, info
    sup = pd.read_csv(csv)
    poly_utm = gpd.GeoSeries([polygon], crs=4326).to_crs(utm).iloc[0]
    have = stations.to_crs(utm).geometry
    add = []
    for _, r in sup.iterrows():
        if int(r.get("include", 1)) != 1 or pd.isna(r["lat"]) or pd.isna(r["lon"]):
            info["supplement_skipped"].append({"name": r["name"], "reason": "excluded in file (see note)"})
            continue
        pt = gpd.GeoSeries(gpd.points_from_xy([r["lon"]], [r["lat"]]), crs=4326).to_crs(utm).iloc[0]
        if not pt.within(poly_utm):
            info["supplement_skipped"].append({"name": r["name"], "reason": "outside county"})
            continue
        if len(have) and have.distance(pt).min() < min_sep_m:
            info["supplement_skipped"].append({"name": r["name"], "reason": f"within {min_sep_m:g} m of a mapped station"})
            continue
        add.append({"name": r["name"], "source": "supplement", "lon": r["lon"], "lat": r["lat"]})
    if add:
        a = gpd.GeoDataFrame(add, geometry=gpd.points_from_xy([x["lon"] for x in add], [x["lat"] for x in add]),
                             crs=4326).drop(columns=["lon", "lat"])
        stations = gpd.GeoDataFrame(pd.concat([stations, a], ignore_index=True), crs=4326)
    info["supplement_added"] = len(add)
    info["stations_total"] = int(len(stations))
    return stations, info


def snap(G, lon: np.ndarray, lat: np.ndarray, utm: str):
    """Nearest graph node for each point and the straight-line snap distance (m)."""
    import osmnx as ox

    Gp = ox.projection.project_graph(G, to_crs=utm)
    xy = gpd.GeoSeries(gpd.points_from_xy(lon, lat), crs=4326).to_crs(utm)
    nn, d = ox.distance.nearest_nodes(Gp, xy.x.values, xy.y.values, return_dist=True)
    return np.asarray(nn, dtype=np.int64), np.asarray(d, dtype=float)


def to_index(node_ids: np.ndarray, nodes: np.ndarray) -> np.ndarray:
    """Positions of OSM node ids inside the csr node order."""
    pos = pd.Series(np.arange(len(nodes)), index=nodes)
    return pos.loc[node_ids].to_numpy()

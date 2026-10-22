"""Static figures (matplotlib + contextily) and the interactive folium map."""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import BoundaryNorm, ListedColormap  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

INK, MUTED, GRID = "#1f2328", "#5b6470", "#d0d4d9"
STATION, NEW1, NEW2 = "#1f2328", "#0b7a75", "#7a3fb3"
# single-hue sequential steps (light -> dark) for drive-time bins
TIME_BINS = [0, 4, 8, 12, 16, 60]
TIME_COLORS = ["#fde4c8", "#f9b27a", "#ee7a3a", "#c94a1c", "#7f2a0c"]
TIME_LABELS = ["0–4 min", "4–8 min", "8–12 min", "12–16 min", "> 16 min"]
ISO_COLORS = {4: "#c94a1c", 8: "#ee7a3a", 12: "#f9b27a"}
ESRI_ATTR = "Basemap: Powered by Esri · Esri, HERE, Garmin, © OpenStreetMap contributors"
DATA_ATTR = ("Roads & stations © OpenStreetMap contributors (ODbL), plus stations from department lists · "
             "Population: 2020 Census blocks")

plt.rcParams.update({"font.family": "DejaVu Sans", "axes.edgecolor": GRID, "text.color": INK})


def _basemap(ax, use):
    if not use:
        return
    try:
        import contextily as cx
        cx.add_basemap(ax, crs="EPSG:3857", source=cx.providers.Esri.WorldGrayCanvas, attribution_size=6,
                       zorder=0, attribution=ESRI_ATTR + " · " + DATA_ATTR)
    except Exception as e:  # tiles unreachable: draw without
        print(f"  basemap skipped: {e}")


def _frame(ax, county, title=None, sub=None):
    county.boundary.plot(ax=ax, color=INK, linewidth=0.8, zorder=5)
    ax.set_axis_off()
    if title:
        ax.set_title(title, loc="left", fontsize=13, fontweight="bold", color=INK, pad=18)
    if sub:
        ax.text(0, 1.012, sub, transform=ax.transAxes, fontsize=9, color=MUTED, va="bottom")


def _stations(ax, st, size=34, label="Fire station"):
    st.plot(ax=ax, marker="s", color="white", edgecolor=STATION, markersize=size, linewidth=1.3,
            zorder=8, label=label)


def drive_time_map(blocks, stations, county, out, basemap=True):
    b = blocks.to_crs(3857)
    cmap, norm = ListedColormap(TIME_COLORS), BoundaryNorm(TIME_BINS, len(TIME_COLORS))
    fig, ax = plt.subplots(figsize=(9, 9))
    b[b["pop"] == 0].plot(ax=ax, color="#eceef0", linewidth=0, zorder=1)
    pop = b[b["pop"] > 0]
    pop.plot(ax=ax, column="t_min", cmap=cmap, norm=norm, linewidth=0.05, edgecolor="white", zorder=2)
    _stations(ax, stations.to_crs(3857))
    _frame(ax, county.to_crs(3857), "Drive time from the nearest fire station",
           "2020 census blocks with residents, Brazos County, TX · free-flow OSM speeds, travel time only")
    _basemap(ax, basemap)
    h = [Patch(color=c, label=lab) for c, lab in zip(TIME_COLORS, TIME_LABELS)]
    h += [Patch(color="#eceef0", label="No residents"),
          Line2D([], [], marker="s", ls="", mfc="white", mec=STATION, ms=7, label="Fire station")]
    ax.legend(handles=h, loc="upper right", fontsize=9, frameon=True, framealpha=0.9, edgecolor=GRID)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)


def service_area_map(iso: dict, stations, county, cov: dict, out, basemap=True):
    fig, ax = plt.subplots(figsize=(9, 9))
    for m in sorted(iso, reverse=True):
        gpd.GeoSeries([iso[m]], crs=32614).to_crs(3857).plot(ax=ax, color=ISO_COLORS[m], alpha=0.75,
                                                              linewidth=0, zorder=2 + 12 - m)
    _stations(ax, stations.to_crs(3857))
    _frame(ax, county.to_crs(3857), "4-, 8- and 12-minute service areas",
           "Areas reachable from the nearest station by road; population counted from block drive times")
    _basemap(ax, basemap)
    h = [Patch(color=ISO_COLORS[m], alpha=0.75,
               label=f"≤ {m} min  ·  {cov[m]['share']:.0%} of residents") for m in sorted(iso)]
    h.append(Line2D([], [], marker="s", ls="", mfc="white", mec=STATION, ms=7, label="Fire station"))
    ax.legend(handles=h, loc="upper right", fontsize=9, frameon=True, framealpha=0.9, edgecolor=GRID)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)


def mclp_map(blocks, stations, county, cand, iso, plans: dict, out, basemap=True):
    """One panel per coverage standard: today's area, candidates, chosen sites, newly covered blocks."""
    stds = sorted(plans)
    fig, axes = plt.subplots(1, len(stds), figsize=(8 * len(stds), 8.6))
    axes = np.atleast_1d(axes)
    b = blocks.to_crs(3857)
    for ax, S in zip(axes, stds):
        plan = plans[S]
        gpd.GeoSeries([iso[S]], crs=32614).to_crs(3857).plot(ax=ax, color="#c9ccd1", alpha=0.6, linewidth=0,
                                                             zorder=1)
        cand.to_crs(3857).plot(ax=ax, color=MUTED, markersize=2, alpha=0.6, zorder=3)
        new = b[b[f"newcov_{S}"] > 0]
        for k, col in ((2, NEW2), (1, NEW1)):
            sel = new[new[f"newcov_{S}"] == k]
            if len(sel):
                sel.plot(ax=ax, color=col, alpha=0.45, linewidth=0, zorder=4)
        _stations(ax, stations.to_crs(3857), size=26)
        s1, s2 = plan["sites1"].to_crs(3857), plan["sites2"].to_crs(3857)
        s2.plot(ax=ax, marker="D", color=NEW2, edgecolor="white", markersize=90, linewidth=1.2, zorder=9)
        s1.plot(ax=ax, marker="*", color=NEW1, edgecolor="white", markersize=320, linewidth=1.0, zorder=10)
        nested = all(s2.distance(p).min() < 1 for p in s1.geometry)
        _frame(ax, county.to_crs(3857), f"{S}-minute standard",
               f"+1 station: +{plan['gain1']:,} residents covered · +2 stations: +{plan['gain2']:,}"
               + ("  (best pair = star + diamond)" if nested else ""))
        _basemap(ax, basemap)
        h = [Patch(color="#c9ccd1", label=f"Covered today (≤ {S} min)"),
             Line2D([], [], marker="o", ls="", color=MUTED, ms=3, label="Candidate site"),
             Line2D([], [], marker="*", ls="", mfc=NEW1, mec="white", ms=15, label="Best single new site"),
             Line2D([], [], marker="D", ls="", mfc=NEW2, mec="white", ms=8, label="Best pair of new sites"),
             Patch(color=NEW1, alpha=0.45, label="Blocks gained (+1 site)"),
             Patch(color=NEW2, alpha=0.45, label="Blocks gained (+2 sites only)"),
             Line2D([], [], marker="s", ls="", mfc="white", mec=STATION, ms=6, label="Existing station")]
        ax.legend(handles=h, loc="upper right", fontsize=8.5, frameon=True, framealpha=0.9, edgecolor=GRID)
    fig.suptitle("Where would one or two more stations add the most coverage? (Maximal Covering Location Problem)",
                 x=0.02, ha="left", fontsize=14, fontweight="bold", color=INK)
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)


def coverage_curve(curves: dict, band: tuple, standards, out):
    """curves: label -> (minutes, share); band: (minutes, low, high) for the speed sensitivity."""
    fig, ax = plt.subplots(figsize=(9, 5.2))
    mins, lo, hi = band
    ax.fill_between(mins, lo * 100, hi * 100, color="#9aa3ad", alpha=0.3, linewidth=0,
                    label="Today, speeds ±20%")
    colors = [INK, NEW1, NEW2]
    for (lab, (m, s)), c in zip(curves.items(), colors):
        ax.plot(m, s * 100, color=c, lw=2, label=lab)
    for S in standards:
        ax.axvline(S, color=GRID, lw=1, zorder=0)
        y = np.interp(S, *list(curves.values())[0]) * 100
        ax.annotate(f"{y:.0f}%", (S, y), xytext=(4, -14), textcoords="offset points", fontsize=9, color=INK)
    ax.set_xlim(0, mins.max())
    ax.set_ylim(0, 100)
    ax.set_xlabel("Drive time from nearest station (minutes)", color=INK)
    ax.set_ylabel("Residents reached (%)", color=INK)
    ax.set_xticks(np.arange(0, mins.max() + 1, 2))
    ax.grid(axis="y", color=GRID, lw=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title("Share of Brazos County residents within a given drive time", loc="left",
                 fontsize=13, fontweight="bold")
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    fig.text(0.01, 0.012, "2020 Census block populations; OSM roads, free-flow speeds; travel time only (no "
             "turnout, dispatch or traffic).", fontsize=7.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out, dpi=150)
    plt.close(fig)


def hero(edges_utm, edge_t, stations, sites, county, cov, extra_txt, out):
    """16:9 thumbnail: road network coloured by drive time on a dark background."""
    bg = "#0f1419"
    fig = plt.figure(figsize=(16, 9), dpi=100, facecolor=bg)
    ax = fig.add_axes([0.36, 0.02, 0.62, 0.96], facecolor=bg)
    e = edges_utm.assign(t=np.minimum(edge_t / 60, 20)).sort_values("t", ascending=False)
    cmap = plt.get_cmap("inferno_r")
    lw = np.where(e["hwy"].isin(["motorway", "trunk", "primary", "secondary"]), 1.4, 0.55)
    e.plot(ax=ax, column="t", cmap=cmap, vmin=0, vmax=20, linewidth=lw, zorder=2)
    county.to_crs(32614).boundary.plot(ax=ax, color="#5b6470", linewidth=0.8, zorder=1)
    stations.to_crs(32614).plot(ax=ax, marker="s", color="white", edgecolor=bg, markersize=60, zorder=5)
    sites.to_crs(32614).plot(ax=ax, marker="*", color="#34d1bf", edgecolor=bg, markersize=520, linewidth=1,
                             zorder=6)
    ax.set_axis_off()
    ax.set_aspect("equal")
    t = fig.text
    t(0.035, 0.86, "How fast can a fire engine\nreach you?", color="white", fontsize=34, fontweight="bold",
      va="top", linespacing=1.1)
    t(0.035, 0.68, "Brazos County, Texas · drive time from the nearest station", color="#c9d1d9", fontsize=15)
    y = 0.58
    for m in sorted(cov):
        t(0.035, y, f"{cov[m]['share']:.0%}", color="white", fontsize=30, fontweight="bold")
        t(0.13, y + 0.012, f"of residents within {m} min", color="#c9d1d9", fontsize=15)
        y -= 0.085
    t(0.035, y - 0.01, extra_txt, color="#34d1bf", fontsize=14, va="top", linespacing=1.35)
    # colour bar
    cax = fig.add_axes([0.035, 0.12, 0.25, 0.022])
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, 20))
    cb = fig.colorbar(sm, cax=cax, orientation="horizontal")
    cb.set_ticks([0, 4, 8, 12, 16, 20])
    cb.set_ticklabels(["0", "4", "8", "12", "16", "20+ min"])
    cb.ax.tick_params(colors="#c9d1d9", labelsize=11)
    cb.outline.set_visible(False)
    t(0.035, 0.155, "Road drive time (free-flow)", color="#c9d1d9", fontsize=11)
    t(0.035, 0.035, "□ fire station   ★ proposed site (MCLP)\n© OpenStreetMap contributors · 2020 Census",
      color="#8b949e", fontsize=10, linespacing=1.4)
    fig.savefig(out, dpi=100, facecolor=bg)
    plt.close(fig)


def folium_map(iso: dict, stations, plans: dict, county, cov: dict, out: Path, iso_after: dict | None = None):
    import folium

    c = county.to_crs(4326).geometry.iloc[0].centroid
    m = folium.Map(location=[c.y, c.x], zoom_start=10, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        attr="Powered by <a href='https://www.esri.com'>Esri</a> | Esri, HERE, Garmin, &copy; "
             "<a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors | "
             "Roads &amp; stations &copy; OpenStreetMap contributors (ODbL), plus stations from department lists | Population: U.S. Census Bureau 2020",
        name="Esri World Light Gray Canvas", max_zoom=16,
    ).add_to(m)
    folium.GeoJson(county.to_crs(4326).boundary.__geo_interface__, name="County boundary",
                   style_function=lambda f: {"color": "#1f2328", "weight": 1.5, "fill": False}).add_to(m)
    for mm in sorted(iso, reverse=True):
        g = gpd.GeoSeries([iso[mm]], crs=32614).to_crs(4326)
        fg = folium.FeatureGroup(name=f"Today: ≤ {mm} min ({cov[mm]['share']:.0%} of residents)",
                                 show=mm in (4, 8))
        folium.GeoJson(_round(g.__geo_interface__), style_function=lambda f, col=ISO_COLORS[mm]: {
            "fillColor": col, "color": col, "weight": 0.5, "fillOpacity": 0.35}).add_to(fg)
        fg.add_to(m)
    if iso_after:
        for mm, geom in iso_after.items():
            g = gpd.GeoSeries([geom], crs=32614).to_crs(4326)
            fg = folium.FeatureGroup(name=f"With 2 new sites ({mm}-min plan): ≤ {mm} min", show=False)
            folium.GeoJson(_round(g.__geo_interface__), style_function=lambda f: {
                "fillColor": NEW2, "color": NEW2, "weight": 0.8, "fillOpacity": 0.2}).add_to(fg)
            fg.add_to(m)
    fg = folium.FeatureGroup(name="Fire stations")
    for _, r in stations.to_crs(4326).iterrows():
        folium.CircleMarker([r.geometry.y, r.geometry.x], radius=6, color="#1f2328", weight=2, fill=True,
                            fill_color="white", fill_opacity=1,
                            tooltip=r["label"] + ("" if r.get("source", "OpenStreetMap") == "OpenStreetMap"
                                                  else " (added from official list)")).add_to(fg)
    fg.add_to(m)
    for S, plan in plans.items():
        fg = folium.FeatureGroup(name=f"Proposed sites, {S}-min standard")
        for k, gdf, col in ((2, plan["sites2"], NEW2), (1, plan["sites1"], NEW1)):  # star drawn on top
            for _, r in gdf.to_crs(4326).iterrows():
                tip = (f"{S}-min standard, best {'single site' if k == 1 else 'pair'}: "
                       f"+{plan['gain%d' % k]:,} residents covered<br>near {r['near']}")
                glyph, size = ("&#9733;", 30) if k == 1 else ("&#9670;", 22)
                folium.Marker([r.geometry.y, r.geometry.x], tooltip=tip, icon=folium.DivIcon(
                    icon_size=(size, size), icon_anchor=(size // 2, size // 2),
                    html=f'<div style="font-size:{size}px;line-height:{size}px;color:{col};text-align:center;'
                         f'text-shadow:0 0 2px #fff,0 0 2px #fff">{glyph}</div>')).add_to(fg)
        fg.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    title = "Brazos County Fire Access"
    m.get_root().header.add_child(folium.Element(f"<title>{title}</title>"))
    legend = "".join(
        f'<div><span style="display:inline-block;width:14px;height:10px;background:{ISO_COLORS[k]};'
        f'opacity:.6;margin-right:6px"></span>≤ {k} min drive</div>' for k in sorted(iso))
    m.get_root().html.add_child(folium.Element(
        '<div style="position:fixed;bottom:24px;left:12px;z-index:1000;background:rgba(255,255,255,0.94);'
        'padding:9px 12px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.25);font:12px/1.45 sans-serif;'
        f'color:#222;max-width:300px"><b style="font-size:14px">{title}</b><br>'
        "Drive time from the nearest fire station (free-flow speeds, travel time only). "
        "A planning exercise, not an assessment of any department.<hr style='margin:5px 0'>"
        f"{legend}"
        '<div><span style="display:inline-block;width:10px;height:10px;border:2px solid #1f2328;'
        'border-radius:50%;margin-right:6px"></span>Fire station</div>'
        f'<div><span style="color:{NEW1};font-size:15px;margin-right:4px">&#9733;</span>Best single new site</div>'
        f'<div><span style="color:{NEW2};font-size:13px;margin-right:5px">&#9670;</span>Best pair of new sites</div>'
        "</div>"))
    m.save(str(out))


def _round(gj, nd=5):
    """Round coordinates to keep the HTML small."""
    def r(x):
        if isinstance(x, (list, tuple)):
            return [r(v) for v in x]
        return round(x, nd) if isinstance(x, float) else x
    s = json.loads(json.dumps(gj))
    for f in s.get("features", []):
        f["geometry"]["coordinates"] = r(f["geometry"]["coordinates"])
    return s

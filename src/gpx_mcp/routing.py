"""Bike routing via the public BRouter instance.

One GeoJSON request yields geometry with elevation, per-segment OSM way tags
(`messages`), node tags (barriers, crossings) and BRouter's own time estimate.
Docs: https://brouter.de/brouter-web/ , https://github.com/abrensch/brouter
"""
from __future__ import annotations

from typing import Literal

from . import state, surface
from .gpx_utils import build_gpx
from .http import client
from .metrics import compute_metrics

BROUTER_URL = "https://brouter.de/brouter"

Profile = Literal[
    "trekking",
    "trekking-noferries",
    "trekking-steep",
    "trekking-ignore-cr",
    "safety",
    "fastbike",
    "fastbike-lowtraffic",
    "fastbike-verylowtraffic",
    "shortest",
    "gravel",
]

_HAZARD_NODE_KEYS = ("barrier", "railway", "ford", "highway")
_HAZARD_IGNORE = {
    ("highway", "crossing"), ("highway", "traffic_signals"), ("highway", "bus_stop"),
    ("highway", "street_lamp"), ("highway", "stop"), ("highway", "give_way"),
    ("railway", "unknown"), ("barrier", "kerb"), ("barrier", "entrance"),
}


def _lonlats(waypoints: list[tuple[float, float]]) -> str:
    return "|".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)


def _nogos_param(nogos: list[tuple[float, float, float]] | None) -> str | None:
    if not nogos:
        return None
    return "|".join(f"{lon:.6f},{lat:.6f},{int(r)}" for lat, lon, r in nogos)


def _polygons_param(polys: list[list[tuple[float, float]]] | None) -> str | None:
    if not polys:
        return None
    return "|".join(",".join(f"{lon:.6f},{lat:.6f}" for lat, lon in poly) for poly in polys)


async def fetch_geojson(
    waypoints: list[tuple[float, float]],
    profile: str,
    alternative_idx: int,
    nogos: list[tuple[float, float, float]] | None = None,
    nogo_polygons: list[list[tuple[float, float]]] | None = None,
) -> dict:
    params: dict[str, str | int] = {
        "lonlats": _lonlats(waypoints),
        "profile": profile,
        "alternativeidx": alternative_idx,
        "format": "geojson",
    }
    if (ng := _nogos_param(nogos)) is not None:
        params["nogos"] = ng
    if (pg := _polygons_param(nogo_polygons)) is not None:
        params["polygons"] = pg
    resp = await client().get(BROUTER_URL, params=params, timeout=90.0)
    text = resp.text
    if resp.status_code != 200 or not text.lstrip().startswith("{"):
        raise RuntimeError(f"BRouter request failed (status {resp.status_code}): {text[:300]}")
    return resp.json()


def hazards(segments: list[dict]) -> list[dict]:
    """Barriers, level crossings, fords, steps and ferries with km position."""
    out: list[dict] = []
    cum = 0.0
    prev_steps = prev_ferry = False
    for s in segments:
        cum += float(s["dist_m"])
        km = round(cum / 1000, 2)
        if s["steps"] and not prev_steps:
            out.append({"km": round((cum - s["dist_m"]) / 1000, 2), "kind": "steps", "length_m": s["dist_m"]})
        elif s["steps"]:
            out[-1]["length_m"] += s["dist_m"]
        if s["ferry"] and not prev_ferry:
            out.append({"km": round((cum - s["dist_m"]) / 1000, 2), "kind": "ferry", "length_m": s["dist_m"]})
        elif s["ferry"]:
            out[-1]["length_m"] += s["dist_m"]
        prev_steps, prev_ferry = s["steps"], s["ferry"]
        nt = s.get("node_tags") or {}
        for k in _HAZARD_NODE_KEYS:
            v = nt.get(k)
            if v and (k, v) not in _HAZARD_IGNORE:
                if k == "railway" and v not in ("level_crossing", "crossing"):
                    continue
                if k == "highway" and v != "elevator":
                    continue
                out.append({"km": km, "kind": f"{k}={v}"})
    return out


async def _route_once(
    waypoints: list[tuple[float, float]],
    profile: Profile = "trekking",
    alternative_idx: int = 0,
    nogos: list[tuple[float, float, float]] | None = None,
    nogo_polygons: list[list[tuple[float, float]]] | None = None,
) -> dict:
    """One BRouter request. Returns metadata + route_id; GPX stays in cache."""
    if len(waypoints) < 2:
        raise ValueError("Need at least 2 waypoints (start and end).")
    data = await fetch_geojson(waypoints, profile, alternative_idx, nogos, nogo_polygons)
    feats = data.get("features") or []
    if not feats:
        raise RuntimeError("BRouter returned no route feature.")
    feat = feats[0]
    coords = feat["geometry"]["coordinates"]
    props = feat.get("properties", {})

    points = [(c[1], c[0], c[2] if len(c) > 2 else None) for c in coords]
    gpx_text = build_gpx(points, name=f"brouter-{profile}")

    segments = surface.segments_from_messages(props.get("messages") or [])
    point_seg = surface.map_points_to_segments(segments, [(lat, lon) for lat, lon, _ in points])

    metadata = compute_metrics(gpx_text)
    metadata.update(
        {
            "profile": profile,
            "alternative_idx": alternative_idx,
            "waypoints": [[lat, lon] for lat, lon in waypoints],
            "nogos": [[lat, lon, r] for lat, lon, r in (nogos or [])] or None,
            "nogo_polygons": [[[lat, lon] for lat, lon in poly] for poly in (nogo_polygons or [])] or None,
            "brouter_time_min": round(int(props.get("total-time", 0)) / 60) if props.get("total-time") else None,
            "surface_breakdown": surface.breakdown(segments),
            "hazards": hazards(segments)[:40],
        }
    )
    route_id = state.store(gpx_text, metadata, segments=segments, point_seg=point_seg)
    return state.get(route_id).metadata


# --------------------------------------------------------------------------- avoid unpaved

AVOID_RADIUS_M = 45.0
_SERVICE_MIN_M = 150  # shorter service ways are driveways / parking aisles, not shortcuts
_WAYPOINT_CLEARANCE_M = 200  # never block the area around a requested waypoint
_UNWANTED_HIGHWAYS = {"track", "path", "footway", "pedestrian", "bridleway", "steps"}


def unwanted_segment_indices(segments: list[dict]) -> list[int]:
    """Indices of segments a road rider usually does not want: unpaved surfaces,
    tracks/paths, and long `highway=service` ways (cemetery / parking-lot shortcuts)."""
    bad = []
    for i, s in enumerate(segments):
        tags = s.get("tags") or {}
        if s.get("ferry"):
            continue
        highway = tags.get("highway", "")
        if s.get("paved") is False or highway in _UNWANTED_HIGHWAYS and s.get("klass") != "cycleway":
            bad.append(i)
        elif highway == "service" and s.get("dist_m", 0) >= _SERVICE_MIN_M:
            bad.append(i)
    return bad


def avoid_circles(
    points: list,  # gpxpy trackpoints (.latitude/.longitude)
    point_seg: list[int],
    bad: list[int],
    waypoints: list[tuple[float, float]],
    radius_m: float = AVOID_RADIUS_M,
    spacing_m: float = 90.0,
) -> list[tuple[float, float, float]]:
    """Nogo circles spaced along the bad segments, skipping areas near waypoints."""
    from .metrics import haversine_m

    bad_set = set(bad)
    circles: list[tuple[float, float, float]] = []
    last: tuple[float, float] | None = None
    for pt, si in zip(points, point_seg):
        lat, lon = pt.latitude, pt.longitude
        if si not in bad_set:
            last = None
            continue
        if any(haversine_m(lat, lon, wl, wo) < _WAYPOINT_CLEARANCE_M for wl, wo in waypoints):
            continue
        if last is None or haversine_m(lat, lon, last[0], last[1]) >= spacing_m:
            circles.append((lat, lon, radius_m))
            last = (lat, lon)
    return circles


async def route(
    waypoints: list[tuple[float, float]],
    profile: Profile = "trekking",
    alternative_idx: int = 0,
    nogos: list[tuple[float, float, float]] | None = None,
    nogo_polygons: list[list[tuple[float, float]]] | None = None,
    avoid_unpaved: bool = False,
    max_avoid_rounds: int = 3,
) -> dict:
    """Route between waypoints. Returns metadata + route_id; GPX stays in cache.

    With `avoid_unpaved`, re-routes up to `max_avoid_rounds` times, adding nogo circles on
    gravel/track/path segments and long service-way shortcuts found in the previous result.
    The returned metadata gains `avoid_unpaved` = {rounds, avoided_m_initial, avoided_m_final,
    busy_road_pct_before, busy_road_pct_after, note}. If a round makes the route impossible,
    the last working route is returned and the note says so.
    """
    first = await _route_once(waypoints, profile, alternative_idx, nogos, nogo_polygons)
    if not avoid_unpaved:
        return first

    def unwanted_m(meta_id: str) -> int:
        entry = state.get(meta_id)
        return sum(entry.segments[i].get("dist_m", 0) for i in unwanted_segment_indices(entry.segments))

    best = first
    initial_m = unwanted_m(first["route_id"])
    circles = list(nogos or [])
    note = "no unpaved or shortcut segments found"
    rounds = 0
    current = first
    while rounds < max_avoid_rounds:
        entry = state.get(current["route_id"])
        bad = unwanted_segment_indices(entry.segments)
        if not bad:
            break
        new = avoid_circles(entry.pts, entry.point_seg, bad, waypoints)
        if not new:
            note = "remaining unpaved/service segments are at your waypoints; left as is"
            break
        circles += new
        rounds += 1
        try:
            current = await _route_once(waypoints, profile, alternative_idx, circles, nogo_polygons)
        except Exception as e:  # BRouter: no route found with these areas blocked
            note = f"round {rounds} found no route with those areas blocked ({e}); returning previous route"
            break
        best = current
        note = "unpaved/shortcut segments avoided" if not unwanted_segment_indices(state.get(best["route_id"]).segments) else "some unpaved segments remain (no paved alternative nearby)"

    final_m = unwanted_m(best["route_id"])
    best["avoid_unpaved"] = {
        "rounds": rounds,
        "avoided_m_initial": initial_m,
        "avoided_m_final": final_m,
        "busy_road_pct_before": first["surface_breakdown"]["busy_road_pct"],
        "busy_road_pct_after": best["surface_breakdown"]["busy_road_pct"],
        "distance_km_before": first["distance_km"],
        "distance_km_after": best["distance_km"],
        "note": note,
    }
    return best


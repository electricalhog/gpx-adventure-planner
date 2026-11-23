"""Climb analysis page from a bike-computer FIT file.

    gpx-climb-map ride.fit                       # highest point of the ride
    gpx-climb-map ride.fit --summit 44.21,-72.93 # a specific summit (nearest point)
    gpx-climb-map ride.fit --serve               # open via http://localhost (OSM tiles need a referrer)

Writes a self-contained HTML page: map of the climb colored by heart rate / power / speed /
climb rate / gradient / cadence / elevation, linked to an elevation-profile chart. Needs the
`fit` extra (fitdecode). The page uses OpenStreetMap tiles, which block requests that carry no
referrer, so open it over http://localhost (use --serve), not from a file:// URL.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import math
import socketserver
import warnings
from datetime import datetime
from pathlib import Path

TEMPLATE = Path(__file__).with_name("climb_template.html")


# --------------------------------------------------------------------------- FIT parsing

def read_fit(path: str | Path) -> list[dict]:
    """FIT `record` messages that have a position and altitude → plain dicts."""
    try:
        import fitdecode
    except ImportError as e:  # pragma: no cover
        raise SystemExit("Install the FIT extra:  uv sync --extra fit") from e
    pts: list[dict] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with fitdecode.FitReader(str(path)) as fit:
            for frame in fit:
                if not (isinstance(frame, fitdecode.FitDataMessage) and frame.name == "record"):
                    continue
                d = {f.name: f.value for f in frame.fields if f.value is not None}
                if "position_lat" not in d or "position_long" not in d:
                    continue
                alt = d.get("enhanced_altitude", d.get("altitude"))
                if alt is None:
                    continue
                pts.append({
                    "ts": d["timestamp"],
                    "lat": d["position_lat"] * 180 / 2**31,
                    "lon": d["position_long"] * 180 / 2**31,
                    "d": d.get("distance", 0.0),
                    "alt": alt,
                    "hr": d.get("heart_rate"),
                    "pw": d.get("power"),
                    "cad": d.get("cadence"),
                    "spd": (d.get("enhanced_speed", d.get("speed")) or 0) * 3.6,
                })
    if not pts:
        raise SystemExit("No GPS/altitude records in that FIT file.")
    t0 = pts[0]["ts"]
    for p in pts:
        p["t"] = (p["ts"] - t0).total_seconds()
    return pts


# --------------------------------------------------------------------------- climb slicing

def _hav(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    p = math.pi / 180
    return 6371000 * math.hypot((b_lon - a_lon) * p * math.cos((a_lat + b_lat) / 2 * p), (b_lat - a_lat) * p)


def find_summit(pts: list[dict], near: tuple[float, float] | None = None) -> int:
    """Index of the highest point, or of the point closest to `near` (lat, lon)."""
    if near:
        return min(range(len(pts)), key=lambda i: _hav(pts[i]["lat"], pts[i]["lon"], *near))
    return max(range(len(pts)), key=lambda i: pts[i]["alt"])


def find_climb_start(pts: list[dict], summit: int, window_m: float) -> int:
    """Lowest point within `window_m` of riding before the summit."""
    j = summit
    while j > 0 and pts[summit]["d"] - pts[j]["d"] < window_m:
        j -= 1
    return min(range(j, max(summit, j + 1)), key=lambda i: pts[i]["alt"])


def _smooth(vals: list, k: int) -> list:
    out = []
    for i in range(len(vals)):
        w = [v for v in vals[max(0, i - k): i + k + 1] if v is not None]
        out.append(sum(w) / len(w) if w else None)
    return out


def build_rows(pts: list[dict], start: int, summit: int, after_m: float = 1200.0, lead: int = 20) -> dict:
    """Slice [start-lead, summit+after_m] and add smoothed climb rate (m/h) and gradient (%)."""
    end = summit
    while end < len(pts) - 1 and pts[end]["d"] - pts[summit]["d"] < after_m:
        end += 1
    first = max(0, start - lead)
    seg = pts[first: end + 1]
    d0, t0 = seg[0]["d"], seg[0]["t"]
    alt = _smooth([p["alt"] for p in seg], 3)

    def window(i: int, key: str, half: float) -> tuple[int, int]:
        a = b = i
        while a > 0 and seg[i][key] - seg[a][key] < half:
            a -= 1
        while b < len(seg) - 1 and seg[b][key] - seg[i][key] < half:
            b += 1
        return a, b

    cr, gr = [], []
    for i in range(len(seg)):
        a, b = window(i, "t", 15)
        dt = seg[b]["t"] - seg[a]["t"]
        cr.append((alt[b] - alt[a]) / dt * 3600 if dt > 0 else 0)
        a, b = window(i, "d", 50)
        dd = seg[b]["d"] - seg[a]["d"]
        gr.append((alt[b] - alt[a]) / dd * 100 if dd > 5 else 0)
    hr = _smooth([p["hr"] for p in seg], 3)
    pw = _smooth([p["pw"] for p in seg], 5)
    cad = _smooth([p["cad"] for p in seg], 3)
    spd = _smooth([p["spd"] for p in seg], 3)

    def r(v, n=0):
        return None if v is None else round(v, n) if n else round(v)

    rows = [
        {"lat": round(p["lat"], 6), "lon": round(p["lon"], 6), "km": round((p["d"] - d0) / 1000, 3),
         "t": int(p["t"] - t0), "alt": round(alt[i], 1), "hr": r(hr[i]), "pw": r(pw[i]), "cad": r(cad[i]),
         "spd": round(spd[i], 1), "cr": round(cr[i]), "gr": round(gr[i], 1)}
        for i, p in enumerate(seg)
    ]
    inside = [i for i in range(len(seg)) if pts[start]["d"] <= seg[i]["d"] <= pts[summit]["d"]]
    return {"rows": rows, "c0": inside[0], "c1": inside[-1]}


# --------------------------------------------------------------------------- page + server

def render(data: dict, title: str, subtitle: str) -> str:
    pws = [r["pw"] for r in data["rows"][data["c0"]: data["c1"] + 1] if r["pw"] is not None]
    pw_hi = int(math.ceil(max(pws + [300]) / 20) * 20)
    html = TEMPLATE.read_text()
    return (html.replace("__DATA__", json.dumps(data, separators=(",", ":")))
                .replace("__TITLE__", title).replace("__SUBTITLE__", subtitle)
                .replace("__PWHI__", str(pw_hi)))


def serve(directory: Path, filename: str, port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Serving http://localhost:{port}/{filename}  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Map + metrics page for a climb in a FIT file.")
    ap.add_argument("fit", type=Path)
    ap.add_argument("--out", type=Path, help="output HTML (default: <fit name>.climb.html next to the FIT)")
    ap.add_argument("--title", help="page title (default: 'Climb')")
    ap.add_argument("--summit", help="lat,lon of the summit; default is the ride's highest point")
    ap.add_argument("--window-km", type=float, default=8.0, help="how far back from the summit to look for the climb start")
    ap.add_argument("--after-km", type=float, default=1.2, help="descent shown after the summit")
    ap.add_argument("--serve", action="store_true", help="serve on http://localhost after writing")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)

    near = tuple(float(x) for x in a.summit.split(",")) if a.summit else None
    pts = read_fit(a.fit)
    si = find_summit(pts, near)
    st = find_climb_start(pts, si, a.window_km * 1000)
    data = build_rows(pts, st, si, a.after_km * 1000)
    c = data["rows"][data["c0"]: data["c1"] + 1]
    date = pts[0]["ts"].astimezone().strftime("%a %b %d").replace(" 0", " ")
    gain = c[-1]["alt"] - c[0]["alt"]
    title = a.title or "Climb"
    subtitle = (f"{date} &middot; {c[-1]['km'] - c[0]['km']:.1f} km, {gain:.0f} m to the {c[-1]['alt']:.0f} m summit, "
                f"with {a.after_km:g} km of the descent for context")
    out = a.out or a.fit.with_suffix(".climb.html")
    out.write_text(render(data, title, subtitle))
    print(f"climb: {c[-1]['km'] - c[0]['km']:.2f} km, +{gain:.0f} m, summit {c[-1]['alt']:.0f} m "
          f"at ({pts[si]['lat']:.4f}, {pts[si]['lon']:.4f}) -> {out}")
    if a.serve:
        serve(out.parent.resolve(), out.name, a.port)


if __name__ == "__main__":
    main()

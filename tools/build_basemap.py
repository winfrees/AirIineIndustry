"""
Build the committed US base map from Natural Earth. DEV-TIME ONLY.

    python tools/build_basemap.py --out airlinesim/data/basemap.json

Same discipline as the BTS ingest: this script is never imported by the
package, it runs by hand when the source data changes, and what ships is the
distilled artifact.

SOURCE
------
Natural Earth (naturalearthdata.com), via the nvkelso/natural-earth-vector
GitHub mirror. Natural Earth is in the **public domain** — "no permission
needed" — which is why it is the only vector base map that can be committed
to a repository like this one without a licence file travelling with it.

    ne_50m_admin_1_states_provinces_lakes   state outlines
    ne_10m_roads                            highways, filtered to US majors
    ne_50m_land / ne_50m_lakes              coastline and inland water
    ne_50m_rivers_lake_centerlines          major rivers

WHY IT IS SIMPLIFIED
--------------------
The raw layers are 55 MB, most of it in `ne_10m_roads`. The committed corpus
is ~364 KB and the Windows bundle ships everything offline, so the base map
has to be measured in hundreds of kilobytes, not tens of megabytes.

Every ring is decimated by Douglas-Peucker at a tolerance in DEGREES, and
rings that collapse below a minimum area are dropped entirely. That loses
small islands and fine coastal detail — deliberately. This is a map to fly a
simulated airline over, not a survey.

WHAT IS NOT HERE
----------------
**Terrain relief.** Shaded relief needs an elevation raster (ETOPO/SRTM),
which is tens of megabytes before it is an image and cannot be drawn from
vectors. The map draws land, water, state lines, rivers and highways — real
physical and political geography — but it does not draw mountains. Calling a
flat vector map "terrain" would be the kind of overclaim this project's docs
exist to prevent, so the GUI says what it is instead.
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.request
from pathlib import Path

NE = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
      "master/geojson")

LAYERS = {
    "states": "ne_50m_admin_1_states_provinces_lakes",
    "roads": "ne_10m_roads",
    "land": "ne_50m_land",
    "lakes": "ne_50m_lakes",
    "rivers": "ne_50m_rivers_lake_centerlines",
}

# The window the map draws. Alaska and Hawaii are outside it: the corpus has
# a handful of airports there, and including them would either shrink the
# lower 48 to a postage stamp or need inset panels. They are clipped, and the
# GUI says so rather than drawing them in the wrong place.
BBOX = (-125.0, 24.0, -66.5, 50.0)      # W, S, E, N


def fetch(name: str, cache: Path) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{name}.geojson"
    if not path.exists():
        url = f"{NE}/{name}.geojson"
        print(f"  fetching {url}")
        with urllib.request.urlopen(url, timeout=300) as r:
            path.write_bytes(r.read())
    return json.loads(path.read_text())


# ---------------------------------------------------------------- geometry

def _perp(p, a, b) -> float:
    """Perpendicular distance from p to segment ab, in degrees."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points, tol: float):
    """Douglas-Peucker, iterative so a long coastline can't blow the stack."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        worst, worst_i = tol, -1
        for i in range(lo + 1, hi):
            d = _perp(points[i], points[lo], points[hi])
            if d > worst:
                worst, worst_i = d, i
        if worst_i >= 0:
            keep[worst_i] = True
            stack.append((lo, worst_i))
            stack.append((worst_i, hi))
    return [p for p, k in zip(points, keep) if k]


def _area(ring) -> float:
    """Shoelace area in square degrees — only used to drop specks."""
    s = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def in_bbox(points) -> bool:
    w, s, e, n = BBOX
    return any(w <= x <= e and s <= y <= n for x, y in points)


# -- clipping ---------------------------------------------------------------
# Keeping a whole ring because ONE of its points is in the window is not
# enough: Natural Earth's land layer carries North America as a single ring,
# so "touches the window" kept Canada and Mexico in full and they drew right
# across the frame. Rings are clipped to the window, lines are split at it.

_EDGES = (  # (inside test, intersection parameter) per bbox edge
    ("w", lambda p, b: p[0] >= b[0]),
    ("e", lambda p, b: p[0] <= b[2]),
    ("s", lambda p, b: p[1] >= b[1]),
    ("n", lambda p, b: p[1] <= b[3]),
)


def _cross(a, b, edge, box):
    """Where segment a->b crosses the named bbox edge."""
    (ax, ay), (bx, by) = a, b
    if edge in ("w", "e"):
        x = box[0] if edge == "w" else box[2]
        t = (x - ax) / (bx - ax) if bx != ax else 0.0
        return (x, ay + (by - ay) * t)
    y = box[1] if edge == "s" else box[3]
    t = (y - ay) / (by - ay) if by != ay else 0.0
    return (ax + (bx - ax) * t, y)


def clip_ring(ring, box):
    """Sutherland-Hodgman: clip a closed ring to the window."""
    out = list(ring)
    for edge, inside in _EDGES:
        if not out:
            return []
        src, out = out, []
        for i, cur in enumerate(src):
            prev = src[i - 1]
            ci, pi = inside(cur, box), inside(prev, box)
            if ci:
                if not pi:
                    out.append(_cross(prev, cur, edge, box))
                out.append(cur)
            elif pi:
                out.append(_cross(prev, cur, edge, box))
    return out


def clip_line(line, box):
    """Split an open line at the window, returning the pieces inside it."""
    w, s, e, n = box
    inside = lambda p: w <= p[0] <= e and s <= p[1] <= n   # noqa: E731
    pieces, cur = [], []
    for i, p in enumerate(line):
        if inside(p):
            if i and not inside(line[i - 1]):
                cur.append(_enter(line[i - 1], p, box))
            cur.append(p)
        else:
            if cur:
                cur.append(_enter(p, line[i - 1], box))
                pieces.append(cur)
                cur = []
    if cur:
        pieces.append(cur)
    return [p for p in pieces if len(p) >= 2]


def _enter(outside, inside_pt, box):
    """Walk from an outside point to an inside one, stopping at the window."""
    p = outside
    for edge, test in _EDGES:
        if not test(p, box):
            p = _cross(p, inside_pt, edge, box)
    return p


def rings_of(geom):
    """Every ring/line in a geometry, as plain coordinate lists."""
    t, c = geom["type"], geom["coordinates"]
    if t == "Polygon":
        return list(c)
    if t == "MultiPolygon":
        return [ring for poly in c for ring in poly]
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return list(c)
    return []


def clean(rings, tol: float, min_area: float, closed: bool):
    """Simplify THEN clip: the window edge is exact, the interior is decimated.

    Simplifying after the clip would nudge points off the frame edge and leave
    slivers of background showing along it.
    """
    out = []
    for ring in rings:
        pts = [(round(float(x), 3), round(float(y), 3)) for x, y in ring]
        if not in_bbox(pts):
            continue
        pts = simplify(pts, tol)
        if closed:
            pts = clip_ring(pts, BBOX)
            if len(pts) < 4 or _area(pts) < min_area:
                continue
            out.append([list(p) for p in pts])
        else:
            for piece in clip_line(pts, BBOX):
                out.append([list(p) for p in piece])
    return out


# ---------------------------------------------------------------- build

def build(cache: Path, out: Path, tol: float, road_tol: float):
    print("[basemap] states")
    states = fetch(LAYERS["states"], cache)
    state_shapes = []
    for f in states["features"]:
        p = f["properties"]
        if p.get("iso_a2") != "US":
            continue
        rings = clean(rings_of(f["geometry"]), tol, min_area=0.05, closed=True)
        if rings:
            state_shapes.append({"code": p.get("postal") or "", "rings": rings})
    print(f"          {len(state_shapes)} states, "
          f"{sum(len(s['rings']) for s in state_shapes)} rings")

    print("[basemap] land + lakes + rivers")
    land = clean(rings_of_all(fetch(LAYERS["land"], cache)), tol, 0.08, True)
    lakes = clean(rings_of_all(fetch(LAYERS["lakes"], cache)), tol, 0.05, True)
    rivers = clean(rings_of_all(fetch(LAYERS["rivers"], cache)), tol, 0.0, False)
    print(f"          land {len(land)} rings, lakes {len(lakes)} rings, "
          f"{len(rivers)} river segments")

    print("[basemap] highways")
    roads = fetch(LAYERS["roads"], cache)
    # Interstates and equivalents only. The Secondary Highway layer triples
    # the size and turns the map into hatching at the zoom this is drawn at.
    keep = {"Major Highway", "Beltway"}
    lines = []
    for f in roads["features"]:
        p = f["properties"]
        if p.get("sov_a3") != "USA" or p.get("type") not in keep:
            continue
        for ln in clean(rings_of(f["geometry"]), road_tol, 0.0, False):
            lines.append(ln)
    print(f"          {len(lines)} highway segments")

    doc = {
        "attribution": "Natural Earth (naturalearthdata.com), public domain",
        "source_layers": sorted(LAYERS.values()),
        "bbox": list(BBOX),
        "simplify_deg": tol,
        "road_simplify_deg": road_tol,
        "note": ("Simplified for size: small islands and fine coastal detail "
                 "are dropped, and only major highways are kept. Everything "
                 "is clipped to the bbox, so Alaska and Hawaii are not drawn. "
                 "No terrain relief — that needs an elevation raster."),
        "land": land,
        "lakes": lakes,
        "rivers": rivers,
        "states": state_shapes,
        "highways": lines,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"[basemap] wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


def rings_of_all(fc) -> list:
    return [r for f in fc["features"] for r in rings_of(f["geometry"])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="airlinesim/data/basemap.json")
    ap.add_argument("--cache", default=".basemap-cache")
    ap.add_argument("--tol", type=float, default=0.06,
                    help="polygon simplification tolerance, degrees")
    ap.add_argument("--road-tol", type=float, default=0.10,
                    help="highway simplification tolerance, degrees")
    a = ap.parse_args(argv)
    build(Path(a.cache), Path(a.out), a.tol, a.road_tol)


if __name__ == "__main__":
    main()

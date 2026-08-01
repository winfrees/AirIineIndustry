"""
NETWORK MAP CHECK
=================

The map is a GUI feature, so most of it can only be verified in a browser.
What a scenario CAN pin is everything the browser depends on, and that is
where this feature would break silently:

  1. CORPUS        the committed Natural Earth base map exists, parses, and
                   every layer is present and non-empty
  2. CLIPPING      every ring and line is INSIDE the declared window. This is
                   the invariant that was broken first: Natural Earth carries
                   North America as one ring, so "keep the ring if any point
                   is in the window" kept Canada and Mexico in full and drew
                   them across the frame.
  3. WIRING        `/api/basemap` is a route on the server, map.js is in the
                   webui directory that ships, and index.html loads it. A map
                   whose script isn't in the wheel is not delivered.
  4. GEOGRAPHY     `/api/catalog` carries lat/lon per airport, and the corpus
                   airports the map draws are real coordinates — including
                   the ones OUTSIDE the window, which the GUI must name
                   rather than plot in the wrong place.
  5. SNAPSHOT      the per-op fields the map reads exist and mean what the
                   map assumes: `eff_freq` gates whether an aircraft is drawn
                   at all, `block_h` sets how fast it moves, and the weather
                   system snapshot carries position, radius and kind.
  6. ORIENTATION   NORTH IS AT THE TOP, and the magnetic model the map can
                   orient to reproduces the WMM's own published test values.
                   The projection drew mirrored top-to-bottom at first: the
                   textbook Albers formula is written for a frame where +y is
                   north, and SVG's +y points DOWN the screen. A flipped US
                   still reads as a plausible landmass at a glance, which is
                   exactly why it needs a check rather than an eyeball.

Run:  airlinesim run map
"""
import json
import math
from pathlib import Path

from airlinesim import geomag
from airlinesim.game import new_game
from airlinesim.server import BASEMAP_PATH, WEBUI_DIR

CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")


def _load():
    if not BASEMAP_PATH.is_file():
        return None
    return json.loads(BASEMAP_PATH.read_text())


# ------------------------------------------------------------------
# 1 + 2 — the committed base map, and the window it claims to be in
# ------------------------------------------------------------------
def check_basemap(bm):
    print("\n=== BASE MAP CORPUS ===")
    if bm is None:
        check("airlinesim/data/basemap.json is committed", False,
              "run tools/build_basemap.py")
        return
    check("airlinesim/data/basemap.json is committed and parses", True,
          f"{BASEMAP_PATH.stat().st_size / 1024:.0f} KB")

    # Natural Earth is public domain; that is the only reason a vector base
    # map can be committed here at all, so the attribution travels with it.
    check("carries its attribution", "Natural Earth" in bm.get("attribution", ""),
          bm.get("attribution", "<missing>"))

    layers = {"land": True, "lakes": True, "rivers": False,
              "states": True, "highways": False}
    for name in layers:
        n = len(bm.get(name) or [])
        check(f"layer '{name}' is present and non-empty", n > 0, f"{n} shapes")

    n_rings = sum(len(s["rings"]) for s in bm["states"])
    check("all 48 contiguous states are drawn", len(bm["states"]) == 48,
          f"{len(bm['states'])} states, {n_rings} rings")

    print("\n=== CLIPPED TO THE WINDOW ===")
    w, s, e, n = bm["bbox"]
    check("the window is the lower 48", w < -120 and e > -70 and s < 26 and n > 48,
          f"W {w} S {s} E {e} N {n}")

    def rings(name):
        if name == "states":
            return [r for st in bm["states"] for r in st["rings"]]
        return bm.get(name) or []

    # A tenth of a degree of slack for the coordinate rounding the builder
    # does; anything beyond that is a ring that was never clipped.
    tol = 0.1
    for name in ("land", "lakes", "rivers", "states", "highways"):
        pts = [p for r in rings(name) for p in r]
        if not pts:
            continue
        bad = [p for p in pts if not (w - tol <= p[0] <= e + tol
                                      and s - tol <= p[1] <= n + tol)]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        check(f"'{name}' lies inside the window", not bad,
              f"{len(pts)} points, "
              f"lon {min(xs):.1f}..{max(xs):.1f} lat {min(ys):.1f}..{max(ys):.1f}"
              + (f" — {len(bad)} OUTSIDE" if bad else ""))

    # Rings are polygons, lines are not: a "ring" of two points is a bug the
    # simplifier or the clipper introduced, and it renders as a stray spike.
    thin = [r for name in ("land", "lakes", "states") for r in rings(name)
            if len(r) < 4]
    check("no polygon collapsed below a drawable ring", not thin,
          f"{len(thin)} degenerate rings" if thin else "every ring has 4+ points")

    check("the corpus stays small enough to ship offline",
          BASEMAP_PATH.stat().st_size < 400 * 1024,
          f"{BASEMAP_PATH.stat().st_size / 1024:.0f} KB "
          f"(raw Natural Earth layers are ~55 MB)")

    check("it says what it is NOT", "relief" in (bm.get("note") or ""),
          bm.get("note", "<missing>")[:96] + "…")


# ------------------------------------------------------------------
# 3 — wiring: the browser has to be able to reach all of it
# ------------------------------------------------------------------
def check_wiring():
    print("\n=== GUI WIRING ===")
    src = (Path(__file__).parent.parent / "server.py").read_text()
    check("GET /api/basemap is a route on the server",
          '"/api/basemap"' in src and "_send_basemap" in src)
    check("an absent base map degrades instead of raising",
          'no basemap.json' in src)

    js = WEBUI_DIR / "map.js"
    check("webui/map.js ships with the package", js.is_file(),
          f"{js.stat().st_size / 1024:.0f} KB" if js.is_file() else "MISSING")

    html = (WEBUI_DIR / "index.html").read_text()
    check("index.html loads map.js", '/map.js' in html)
    check("index.html has the map card and its selection readout",
          'id="mapWrap"' in html and 'id="mapSel"' in html
          and 'id="mapLegend"' in html)

    # The one honesty claim that has to be visible in the product, not just
    # in a docstring: aircraft positions are derived, not simulated.
    check("the GUI says aircraft positions are DERIVED",
          "DERIVED" in html and "not simulated" in html)

    if js.is_file():
        j = js.read_text()
        check("the map reads eff_freq before drawing an aircraft",
              "eff_freq" in j)
        check("selection reaches the panels through data-rowop/data-rowtail",
              "data-rowop" in j and "data-rowtail" in j)
        app = (WEBUI_DIR / "app.js").read_text()
        check("the panels emit the rows the map selects",
              'data-rowop="' in app and 'data-rowtail="' in app)
        check("the panels render the weather the map draws",
              "weatherCell" in app and "reliability" in app)

    # pyproject has to carry both globs or the wheel ships a map with no data
    pp = (Path(__file__).parent.parent.parent / "pyproject.toml")
    if pp.is_file():
        txt = pp.read_text()
        check("package-data covers webui/, data/*.json and data/*.cof",
              'webui/**/*' in txt and 'data/*.json' in txt
              and 'data/*.cof' in txt)


# ------------------------------------------------------------------
# 4 + 5 — what the map reads out of the running game
# ------------------------------------------------------------------
def check_snapshot(bm):
    print("\n=== SNAPSHOT SEAM ===")
    # The GUI's own entry point, weather on, so this reads exactly what the
    # browser reads rather than a world assembled a second way.
    session = new_game(world="data", n_destinations=4,
                       ai_profiles={"lowcost": "LOW_COST"}, weather=True,
                       weather_seed=20240101)
    samples = []
    try:
        # Sampled across the run, not read off one tick: whether a given route
        # operates in a given hour depends on crew, gates and the sky, and the
        # roster is deliberately conservative. A single-tick read of "is
        # anything flying?" is a coin toss; the run as a whole is not.
        for _ in range(8):
            session.advance_days(5)
            samples.append(session.snapshot())
        cat = session.catalog()
        snap = samples[-1]
    finally:
        session.stop()

    aps = cat["airports"]
    check("/api/catalog carries a coordinate per airport",
          all("lat" in a and "lon" in a for a in aps), f"{len(aps)} airports")
    placed = [a for a in aps if a["lat"] or a["lon"]]
    check("every corpus airport has real geography",
          len(placed) == len(aps), f"{len(placed)}/{len(aps)} placed")
    check("coordinates are real points on Earth",
          all(-180 <= a["lon"] <= 180 and -90 <= a["lat"] <= 90 for a in placed))
    # The corpus is US airports, so all but the Western Pacific territories
    # sit in the western hemisphere. GUM and SPN are east-longitude and are
    # the reason the plausibility test above can't just be "lon < 0".
    east = sorted(a["iata"] for a in placed if a["lon"] > 0)
    check("only the Western Pacific territories are east-longitude",
          set(east) <= {"GUM", "SPN"}, f"east of Greenwich: {east or 'none'}")

    # The window is the lower 48 on purpose. Airports outside it exist in the
    # corpus and the GUI names them instead of drawing them somewhere wrong —
    # this check exists so that stays a KNOWN set rather than a surprise.
    if bm:
        w, s, e, n = bm["bbox"]
        off = sorted(a["iata"] for a in placed
                     if not (w <= a["lon"] <= e and s <= a["lat"] <= n))
        check("some corpus airports fall outside the window, as documented",
              len(off) > 0,
              f"{len(off)} off-window: {' '.join(off[:12])}"
              + (" …" if len(off) > 12 else ""))

    all_ops = [o for s in samples for p in s["players"] for o in p["route_ops"]]
    ops = [o for p in snap["players"] for o in p["route_ops"]]
    check("the game has routes to draw", len(ops) > 0, f"{len(ops)} route ops")
    if ops:
        need = ("origin", "dest", "tail_number", "eff_freq", "block_h",
                "daily_frequency", "load_factor", "suitable", "weather")
        missing = [f for f in need if f not in ops[0]]
        check("every field the map reads is on the op snapshot", not missing,
              f"missing: {missing}" if missing else " ".join(need))

        # eff_freq is what decides whether an aeroplane is in the air. If it
        # were absent the JS comparison would silently pass for every route
        # and a crew-short carrier would keep flying ghosts.
        check("eff_freq never exceeds the scheduled frequency",
              all(o["eff_freq"] <= o["daily_frequency"] + 1e-6 for o in all_ops),
              "  ".join(f'{o["origin"]}-{o["dest"]}:'
                        f'{o["eff_freq"]:.2f}/{o["daily_frequency"]}'
                        for o in ops[:5]))
        check("block time is positive and airline-plausible",
              all(0.05 < o["block_h"] < 20 for o in all_ops),
              "  ".join(f'{o["origin"]}-{o["dest"]}:{o["block_h"]:.2f}h'
                        for o in ops[:5]))

        # The map's aircraft path has to be reachable: if nothing ever
        # operates, every icon is suppressed and the map is routes only.
        flew = [o for o in all_ops if o["eff_freq"] > 0]
        check("at least one route operates during the run, so aircraft draw",
              bool(flew),
              f"{len(flew)}/{len(all_ops)} sampled ops operating — "
              f"the rest are gate-, crew- or weather-blocked")
        # ...and the suppression has to be reachable too, otherwise the dashed
        # "nothing operated" styling is dead code nobody would ever see.
        check("and at least one is blocked, so the idle styling is reachable",
              len(flew) < len(all_ops),
              f"{len(all_ops) - len(flew)} sampled ops flew nothing")

    sysl = snap.get("weather_systems")
    check("weather systems reach the snapshot", sysl is not None,
          f"{len(sysl or [])} live systems")
    if sysl:
        need = ("kind", "lat", "lon", "radius_km", "intensity")
        missing = [f for f in need if f not in sysl[0]]
        check("each system carries position, size and kind", not missing,
              f"missing: {missing}" if missing else
              "  ".join(f'{s["kind"][:4]}@{s["lat"]:.0f},{s["lon"]:.0f}'
                        f'/{s["radius_km"]:.0f}km' for s in sysl[:5]))
        check("system intensities are in [0, 1]",
              all(0.0 <= s["intensity"] <= 1.0 for s in sysl))
        check("system radii are drawable, not points or continents",
              all(20 <= s["radius_km"] <= 2500 for s in sysl),
              f'{min(s["radius_km"] for s in sysl):.0f}'
              f'..{max(s["radius_km"] for s in sysl):.0f} km')

    aps_live = snap.get("airports") or {}
    has_rel = any("reliability" in a for a in aps_live.values())
    check("per-airport conditions reach the snapshot", has_rel,
          f"{len(aps_live)} airports in play")


# ------------------------------------------------------------------
# 6 — orientation: north up, and a real magnetic model
# ------------------------------------------------------------------
def _albers_py(lon, lat):
    """The projection map.js uses, reimplemented so the SIGN can be asserted.

    Only the vertical sense is being checked here, which is the thing that was
    wrong and the thing a reimplementation can actually catch — if the JS is
    edited to flip y again, the two stop agreeing about which way is up and
    the check below fails.
    """
    rad = math.pi / 180
    lat0, lon0 = 37.5 * rad, -96 * rad
    p1, p2 = 29.5 * rad, 45.5 * rad
    n = 0.5 * (math.sin(p1) + math.sin(p2))
    C = math.cos(p1) ** 2 + 2 * n * math.sin(p1)
    rho0 = math.sqrt(C - 2 * n * math.sin(lat0)) / n
    theta = n * (lon * rad - lon0)
    rho = math.sqrt(C - 2 * n * math.sin(lat * rad)) / n
    return rho * math.sin(theta), rho * math.cos(theta) - rho0


def check_orientation(bm):
    print("\n=== ORIENTATION: NORTH IS UP ===")
    js = (WEBUI_DIR / "map.js").read_text() if (WEBUI_DIR / "map.js").is_file() else ""
    # The JS must carry the negated form. `rho0 - rho*cos(theta)` is the
    # textbook expression and is upside down in screen coordinates.
    check("map.js uses the screen-space (negated) Albers northing",
          "rho * Math.cos(theta) - rho0" in js,
          "found the flipped form 'rho0 - rho*cos(theta)'"
          if "rho0 - rho * Math.cos(theta)" in js else "ok")

    # In SVG, y grows DOWNWARD, so a more northerly point must have a SMALLER y.
    south = _albers_py(-96.0, 25.0)     # southern tip of Texas latitude
    north = _albers_py(-96.0, 49.0)     # the Canadian border
    check("a northern point projects ABOVE a southern one", north[1] < south[1],
          f"lat 49 -> y {north[1]:+.4f}   lat 25 -> y {south[1]:+.4f} "
          f"(smaller y is higher on screen)")
    east = _albers_py(-70.0, 40.0)
    west = _albers_py(-120.0, 40.0)
    check("an eastern point projects to the RIGHT of a western one",
          east[0] > west[0],
          f"lon -70 -> x {east[0]:+.4f}   lon -120 -> x {west[0]:+.4f}")

    print("\n=== MAGNETIC DECLINATION (World Magnetic Model) ===")
    # The three geomagnetic test values published in the WMM2020 technical
    # report, at epoch 2020.0 and zero altitude. Reproducing all three to a
    # tenth of a nanotesla is what says the spherical-harmonic synthesis and
    # the Schmidt normalisation are right — a double-normalised recursion
    # gives plausible-looking magnitudes with the wrong signs.
    cases = [(80.0, 0.0, 6570.4, -146.3, 54606.0, -1.28),
             (0.0, 120.0, 39624.3, 109.9, -10932.5, 0.16),
             (-80.0, 240.0, 5940.6, 15772.1, -52480.8, 69.36)]
    worst_nt = worst_deg = 0.0
    for lat, lon, X, Y, Z, D in cases:
        x, y, z = geomag.field(lat, lon, 2020.0)
        d = geomag.declination(lat, lon, 2020.0)
        worst_nt = max(worst_nt, abs(x - X), abs(y - Y), abs(z - Z))
        worst_deg = max(worst_deg, abs(d - D))
    check("reproduces the WMM's published test values", worst_nt < 0.5,
          f"worst component error {worst_nt:.2f} nT, "
          f"worst declination error {worst_deg:.3f} deg over {len(cases)} points")

    # Global sanity: total intensity is between about 22 and 67 microtesla
    # everywhere on Earth. A normalisation bug sails past a single spot check
    # but not past a sweep.
    lo, hi = 1e12, 0.0
    for la in range(-80, 81, 20):
        for lo_ in range(-180, 180, 30):
            x, y, z = geomag.field(float(la), float(lo_), 2020.0)
            f = math.sqrt(x * x + y * y + z * z)
            lo, hi = min(lo, f), max(hi, f)
    check("total field intensity is physical everywhere", 20_000 < lo and hi < 70_000,
          f"{lo:,.0f} .. {hi:,.0f} nT (Earth's field runs ~22,000-67,000)")

    # The US declinations are the ones a player would recognise, and their
    # SIGNS are the point: east in the west, west in the east.
    year = geomag.epoch() + 5.0
    sea = geomag.declination(47.45, -122.31, year)
    bgr = geomag.declination(44.81, -68.83, year)
    check("declination is EAST in the west and WEST in the east",
          sea > 5.0 > -5.0 > bgr,
          f"Seattle {sea:+.1f} deg, Bangor ME {bgr:+.1f} deg")

    bbox = (bm or {}).get("bbox", [-125.0, 24.0, -66.5, 50.0])
    dlo, dhi = geomag.declination_range(bbox, year)
    check("the window spans too much variation for one magnetic rotation",
          (dhi - dlo) > 20.0,
          f"{dlo:+.1f} to {dhi:+.1f} deg across the lower 48 "
          f"({dhi - dlo:.0f} deg spread) — so magnetic-north-up can only be "
          f"exact on the reference meridian, and the GUI says so")

    # ...and the GUI has to actually say it, not just the docstring.
    html = (WEBUI_DIR / "index.html").read_text()
    check("the map offers a true/magnetic north reference",
          'id="mapNorthRef"' in html and 'id="mapNorth"' in html)
    check("the caveat is in the product, not only in the code",
          "no single rotation" in js and "reference meridian" in js)

    src = (Path(__file__).parent.parent / "server.py").read_text()
    check("GET /api/magnetic is a route on the server", '"/api/magnetic"' in src)
    check("the WMM coefficients are committed", geomag.COF_PATH.is_file(),
          f"{geomag.COF_PATH.name}, {geomag.model_name()} epoch "
          f"{geomag.epoch():.1f} "
          f"({geomag.COF_PATH.stat().st_size / 1024:.1f} KB, public domain)"
          if geomag.COF_PATH.is_file() else "MISSING")


def main():
    print("NETWORK MAP CHECK")
    print("=" * 70)
    bm = _load()
    check_basemap(bm)
    check_wiring()
    check_snapshot(bm)
    check_orientation(bm)
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(CHECKS) else 'FAILURES ABOVE'}")


if __name__ == "__main__":
    main()

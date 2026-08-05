// AirlineSim network map.
// =======================
//
// Draws the committed Natural Earth base map (states, coast, major highways)
// in an Albers Equal-Area Conic projection — the standard projection for the
// lower 48, and the reason the map looks like the US rather than a stretched
// rectangle — then overlays the live simulation: routes, aircraft, airports
// and weather.
//
// WHAT IS SIMULATED AND WHAT IS DRAWN
// -----------------------------------
// The engine does NOT track individual flights. It models each route as a
// daily FREQUENCY smeared across the tick (see the weather design doc), so
// there is no aircraft with a departure time and a position to read off.
//
// So there are TWO DRAWINGS, and the map says which one you are looking at.
//
//   TIMETABLE (detail <= 1 h). A derived schedule spreads each route's
//   OPERATED frequency across a service window, and an icon is drawn for every
//   flight actually in the air at this moment. The count, the direction, the
//   ground speed and how far along each leg is are all real consequences of
//   what the model flew. The one invention is the time of day a flight leaves,
//   because the engine schedules no departures. See "the timetable" below.
//
//   SCHEMATIC (coarser detail). One icon per operating route, phase from the
//   clock. A 24-hour tick cannot say where in the day it is, so a timetable
//   drawn against it would be precision the model does not have; at that
//   resolution the engine's own unit IS the day and the single icon standing
//   for a day's flying is the honest picture.
//
// Either way the aeroplane at a specific point is a rendering of the schedule,
// not a simulated object — a map that looks like live radar and isn't would be
// the most misleading thing in the GUI.
//
// Everything else is read straight from the model: route endpoints and
// frequencies, which carrier owns what, weather system positions and radii,
// per-airport conditions.
//
// SVG rather than canvas: the base map is drawn once into a <g> that never
// changes, only the live layer is re-rendered per snapshot, and click
// handling comes free — which is what makes aircraft and routes selectable
// without hit-testing geometry by hand.

const MAP = {
  svg: null, base: null, live: null, wrap: null,
  basemap: null,
  W: 1000, H: 620,
  selection: null,      // {kind: "route"|"plane", id}
  northRef: "magnetic", // "magnetic" (aviation convention) or "true"
  declination: null,    // deg east at the projection's reference point
  magnetic: null,       // the whole /api/magnetic payload, for the note
};

// Carrier colours, assigned by order of appearance so they're stable within a
// game. Chosen to stay distinguishable on a dark map and against each other.
const CARRIER_COLORS = ["#2ea8ff", "#ffb020", "#35c17a", "#e5556b", "#b98cff",
                        "#4dd0e1", "#f06292"];

function carrierColor(snap, playerId) {
  const i = (snap?.players || []).findIndex((p) => p.player_id === playerId);
  return CARRIER_COLORS[(i < 0 ? 0 : i) % CARRIER_COLORS.length];
}

// -- projection -------------------------------------------------------------
// Albers Equal-Area Conic, standard parallels 29.5N/45.5N (the USGS values for
// the contiguous US). Pure arithmetic, no dependency.
//
// NORTH IS UP, WHICH TAKES A SIGN FLIP. The textbook Albers formula is
// y = rho0 - rho*cos(theta), written for a mathematical frame where +y points
// north. SVG's +y points DOWN the screen, so using that formula unchanged
// draws the map mirrored top-to-bottom: Florida ends up at the top and the
// Great Lakes at the bottom. It still looks like a plausible landmass at a
// glance, which is why it survived a first look. Negating y puts north at the
// top where it belongs.
const ALBERS = (() => {
  const rad = Math.PI / 180;
  const lat0 = 37.5 * rad, lon0 = -96 * rad;
  const p1 = 29.5 * rad, p2 = 45.5 * rad;
  const n = 0.5 * (Math.sin(p1) + Math.sin(p2));
  const C = Math.cos(p1) ** 2 + 2 * n * Math.sin(p1);
  const rho0 = Math.sqrt(C - 2 * n * Math.sin(lat0)) / n;
  return (lon, lat) => {
    const theta = n * (lon * rad - lon0);
    const rho = Math.sqrt(C - 2 * n * Math.sin(lat * rad)) / n;
    // screen coordinates: y grows downward, so negate the projected northing
    return [rho * Math.sin(theta), rho * Math.cos(theta) - rho0];
  };
})();

// -- north reference --------------------------------------------------------
// The map can be oriented to TRUE north or to MAGNETIC north, which is the one
// a pilot's chart uses — runway numbers, headings and radials are all
// magnetic. `MAP.declination` is the magnetic variation at the projection's
// reference point (37.5N 96W), served from the committed World Magnetic Model.
//
// THE HONEST PART: declination is not a constant. Across the lower 48 it runs
// from about +16 deg east in Washington State to -17 deg west in Maine, a
// spread of some 33 degrees, so NO single rotation puts magnetic north at the
// top everywhere on this map. Orienting to magnetic north means orienting to
// it at ONE reference meridian; every other longitude is off by the local
// difference. The panel states the reference and the spread rather than
// letting the label imply a precision it hasn't got.
//
// It happens to be a small rotation here — the agonic line (zero declination)
// runs close to the 96W reference — so this is a couple of degrees, not a
// dramatic tilt. That is the real answer, not a bug.
let NORTH_ROT = 0;      // radians the map is rotated to put "north" at the top

function setNorthReference(kind) {
  MAP.northRef = kind;
  const d = (kind === "magnetic" && MAP.declination != null) ? MAP.declination : 0;
  // Declination is degrees EAST of true north, so on a true-north-up map the
  // magnetic-north direction lies that many degrees CLOCKWISE from straight
  // up. Rotating the map by the same angle anticlockwise brings it upright.
  NORTH_ROT = -d * Math.PI / 180;
}

// Fit the projection to the drawing area, computed once from the base map's
// bbox so the same transform serves every layer. The rotation is applied
// BEFORE the fit, so a tilted map is still framed to the window instead of
// hanging over the edge.
let FIT = { s: 1, dx: 0, dy: 0 };

function _rot(x, y) {
  const c = Math.cos(NORTH_ROT), s = Math.sin(NORTH_ROT);
  return [x * c - y * s, x * s + y * c];
}

function fitProjection(bbox, w, h, pad = 12) {
  const [W_, S_, E_, N_] = bbox;
  const corners = [];
  for (let i = 0; i <= 10; i++) {
    const f = i / 10;
    corners.push(ALBERS(W_ + (E_ - W_) * f, S_), ALBERS(W_ + (E_ - W_) * f, N_));
    corners.push(ALBERS(W_, S_ + (N_ - S_) * f), ALBERS(E_, S_ + (N_ - S_) * f));
  }
  const pts = corners.map((c) => _rot(c[0], c[1]));
  const xs = pts.map((c) => c[0]), ys = pts.map((c) => c[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const s = Math.min((w - 2 * pad) / (maxX - minX), (h - 2 * pad) / (maxY - minY));
  FIT = {
    s,
    dx: pad + (w - 2 * pad - (maxX - minX) * s) / 2 - minX * s,
    dy: pad + (h - 2 * pad - (maxY - minY) * s) / 2 - minY * s,
  };
}

function project(lon, lat) {
  const [x0, y0] = ALBERS(lon, lat);
  const [x, y] = _rot(x0, y0);
  return [x * FIT.s + FIT.dx, y * FIT.s + FIT.dy];
}

function pathOf(ring, close) {
  let d = "";
  for (let i = 0; i < ring.length; i++) {
    const [x, y] = project(ring[i][0], ring[i][1]);
    d += (i ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1);
  }
  return d + (close ? "Z" : "");
}

// -- great circles ----------------------------------------------------------
// Routes are drawn as great circles because that is the path the engine
// charges fuel and block time for (route.haversine).
function greatCircle(a, b, steps = 24) {
  const rad = Math.PI / 180;
  const [lo1, la1] = [a.lon * rad, a.lat * rad];
  const [lo2, la2] = [b.lon * rad, b.lat * rad];
  const d = 2 * Math.asin(Math.sqrt(
    Math.sin((la2 - la1) / 2) ** 2 +
    Math.cos(la1) * Math.cos(la2) * Math.sin((lo2 - lo1) / 2) ** 2));
  if (!d) return [[a.lon, a.lat]];
  const out = [];
  for (let i = 0; i <= steps; i++) {
    const f = i / steps;
    const A = Math.sin((1 - f) * d) / Math.sin(d);
    const B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(la1) * Math.cos(lo1) + B * Math.cos(la2) * Math.cos(lo2);
    const y = A * Math.cos(la1) * Math.sin(lo1) + B * Math.cos(la2) * Math.sin(lo2);
    const z = A * Math.sin(la1) + B * Math.sin(la2);
    out.push([Math.atan2(y, x) / rad, Math.atan2(z, Math.hypot(x, y)) / rad]);
  }
  return out;
}

function pointOn(path, f) {
  const i = Math.max(0, Math.min(path.length - 2, Math.floor(f * (path.length - 1))));
  const t = f * (path.length - 1) - i;
  const [x1, y1] = project(path[i][0], path[i][1]);
  const [x2, y2] = project(path[i + 1][0], path[i + 1][1]);
  return [x1 + (x2 - x1) * t, y1 + (y2 - y1) * t,
          Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI];
}

// -- aircraft icons ---------------------------------------------------------
// One silhouette per plane CLASS, scaled by seat count, with engine pylons
// drawn for the bigger types. Honest limit: this distinguishes a regional jet
// from a narrowbody from a widebody, not a 737 from an A320 — sixteen hand
// drawn type silhouettes would be a different kind of project. The tail
// number and type are on the tooltip and in the panel on selection.
// A PLAN VIEW per type, DERIVED from the type's published length and wingspan
// rather than drawn by hand. `length_m` and `wingspan_m` are measured figures
// on AircraftSpec that nothing in the simulation reads — they exist to make
// this drawing honest.
//
// What that buys: the icon's proportions are the aeroplane's proportions. A
// CRJ900 is long and narrow-winged (24.9 m span on 36.2 m of fuselage, ratio
// 0.69); a 787 is the opposite (60.1 on 56.7, ratio 1.06) and its wings reach
// wider than it is long. An A321 is visibly a stretched A319 because they
// share a span and differ by eleven metres of fuselage. None of that is a
// judgement call in this file — it falls out of two numbers per type.
//
// Absolute size is COMPRESSED on purpose: a 777 really is 2.3x an E175 by
// length, which at map scale would be a blob beside a speck, so screen size
// goes as length^0.6 — the ordering survives and both stay legible.
const ICON_HALF_LEN = 10;        // drawing units; scale is applied by the caller
const ICON_REF_LEN_M = 40.0;     // a mid-size narrowbody sits at scale 1

function planeIcon(spec) {
  // Unpublished dimensions fall back to a band off plane_class, the same way
  // cabin.py bands an unpublished abreast.
  const cls = spec.plane_class;
  const L = spec.length_m || (cls === "WIDEBODY" ? 60 : cls === "REGIONAL" ? 34 : 38);
  const S = spec.wingspan_m || (cls === "WIDEBODY" ? 60 : cls === "REGIONAL" ? 26 : 35);

  const hl = ICON_HALF_LEN;
  const hs = hl * (S / L);         // half-span, TRUE to the real ratio
  const fw = hl * 0.105;           // fuselage half-width
  const r = (v) => v.toFixed(2);

  // fuselage: pointed nose at +x, squared tail at -x
  const body =
    `M${r(hl)},0 L${r(hl * 0.62)},${r(fw)} L${r(-hl * 0.78)},${r(fw)} ` +
    `L${r(-hl)},${r(fw * 0.42)} L${r(-hl)},${r(-fw * 0.42)} ` +
    `L${r(-hl * 0.78)},${r(-fw)} L${r(hl * 0.62)},${r(-fw)} Z`;

  const wLE = hl * 0.20, wRoot = hl * 0.52, wSweep = hl * 0.46, wTip = hl * 0.15;
  const halfWing = (g) =>
    `M${r(wLE)},${r(g * fw)} L${r(wLE - wSweep)},${r(g * hs)} ` +
    `L${r(wLE - wSweep - wTip)},${r(g * hs)} L${r(wLE - wRoot)},${r(g * fw)} Z`;

  const tLE = -hl * 0.70, tSpan = hs * 0.38, tRoot = hl * 0.24,
        tSweep = hl * 0.22, tTip = hl * 0.09;
  const halfTail = (g) =>
    `M${r(tLE)},${r(g * fw * 0.8)} L${r(tLE - tSweep)},${r(g * tSpan)} ` +
    `L${r(tLE - tSweep - tTip)},${r(g * tSpan)} L${r(tLE - tRoot)},${r(g * fw * 0.8)} Z`;

  const eY = hs * 0.36, eX = wLE - wSweep * 0.36, eLen = hl * 0.30, eW = hl * 0.055;
  const nacelle = (g) =>
    `M${r(eX + eLen * 0.55)},${r(g * eY - eW)} L${r(eX - eLen * 0.45)},${r(g * eY - eW)} ` +
    `L${r(eX - eLen * 0.45)},${r(g * eY + eW)} L${r(eX + eLen * 0.55)},${r(g * eY + eW)} Z`;

  return {
    parts: [
      { d: halfWing(1) + halfWing(-1) + halfTail(1) + halfTail(-1), fill: "body" },
      { d: nacelle(1) + nacelle(-1), fill: "shade" },
      { d: body, fill: "body" },
    ],
    scale: Math.pow(L / ICON_REF_LEN_M, 0.6),
  };
}

// -- weather ----------------------------------------------------------------
const WX_COLOR = {
  RAIN: "#4a90d9", FOG: "#9bb0c4", THUNDERSTORM: "#8a5cf6", SNOW: "#cfe8ff",
  ICING: "#7fd8e8", BLIZZARD: "#e6f2ff", HURRICANE: "#ff4d6d",
  WILDFIRE_SMOKE: "#d98032", VOLCANIC_ASH: "#8d8d8d",
};

// -- the timetable ----------------------------------------------------------
// THE ENGINE HAS NO DEPARTURE TIMES. It models a daily FREQUENCY, deliberately
// — so a per-flight position has to come from somewhere, and it comes from
// here: a DERIVED TIMETABLE built from what the route actually operated
// (`eff_freq`), how long the leg takes (`block_h`) and the clock.
//
// This is a real schedule, not the placeholder it replaces. The old drawing
// put ONE icon on a route however many times a day it flew, at a phase taken
// from a hash of the op id. Now the number in the air at any moment is the
// number the schedule puts there, an aircraft is airborne only between its own
// departure and arrival, and a route with eight daily frequencies looks eight
// times busier than one with one — because it is.
//
// ROTATIONS, NOT LEGS. A tail flies out and back, so both legs of a market
// share one base offset and the return is phased by the outbound's block time
// plus a turnaround. You watch an aircraft go out, turn, and come home, which
// is what the fleet is actually doing.
//
// WHAT IS STILL INVENTED, and must not be presented otherwise: the time of day
// each flight leaves. The engine schedules no departures, so the service
// window and the even spacing inside it are this file's invention. Everything
// else — how many flew, how long they take, which aircraft, which route — is
// read from the model.
const SERVICE_START_H = 6.0;    // first departure of the day, sim clock
const SERVICE_END_H = 22.0;     // last departure
const TURN_H = 0.75;            // ground time before the return leg

// Real per-flight positions need to know WHERE IN THE DAY it is, and a tick
// coarser than an hour cannot say. At 24-hour resolution the engine's own unit
// IS the day, so the schematic single-icon drawing is the honest picture there.
const TIMETABLE_MAX_TICK_H = 1.0;

function opOffset(key) {
  return [...key].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 997, 7) / 997;
}

// Flights of this op IN THE AIR at `now`, as fractions along the leg. Returns
// [] when nothing is airborne — a schedule has gaps, and an empty sky between
// banks is a true statement about the timetable, not a missing icon.
function airborneOn(op, now) {
  const n = Math.max(0, Math.round(op.eff_freq || 0));
  if (!n) return [];
  const block = Math.max(0.25, op.block_h || 2.0);
  const market = [op.origin, op.dest].sort().join("-") + ":" + op.tail_number;
  const isReturn = op.origin > op.dest;
  const window = Math.max(1.0, SERVICE_END_H - SERVICE_START_H);
  const spacing = window / n;
  const base = SERVICE_START_H + opOffset(market) * spacing
             + (isReturn ? block + TURN_H : 0);
  const clock = ((now % 24) + 24) % 24;
  const out = [];
  for (let i = 0; i < n; i++) {
    const dep = base + i * spacing;
    // check yesterday's departures too, so a leg crossing midnight is still
    // airborne at 01:00 rather than vanishing at the date line
    for (const shift of (dep + block > 24 ? [0, 24] : [0])) {
      const t = clock + shift;
      if (t >= dep && t < dep + block) out.push((t - dep) / block);
    }
  }
  return out;
}

// -- rendering --------------------------------------------------------------
function svgEl(tag, attrs, parent) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

function drawBase() {
  const bm = MAP.basemap;
  MAP.base.innerHTML = "";
  if (!bm) return;
  // ocean/background is the card itself; land is a filled body so the coast
  // reads even where no state outline covers it (the Great Lakes shoreline).
  for (const ring of bm.land || []) {
    svgEl("path", { d: pathOf(ring, true), class: "mapLand" }, MAP.base);
  }
  for (const st of bm.states || []) {
    for (const ring of st.rings) {
      svgEl("path", { d: pathOf(ring, true), class: "mapState" }, MAP.base);
    }
  }
  for (const ring of bm.lakes || []) {
    svgEl("path", { d: pathOf(ring, true), class: "mapLake" }, MAP.base);
  }
  for (const line of bm.rivers || []) {
    svgEl("path", { d: pathOf(line, false), class: "mapRiver" }, MAP.base);
  }
  for (const line of bm.highways || []) {
    svgEl("path", { d: pathOf(line, false), class: "mapRoad" }, MAP.base);
  }
}

function airportsByCode() {
  const out = {};
  for (const a of (catalog?.airports || [])) {
    if (a.lat || a.lon) out[a.iata] = a;
  }
  return out;
}

// The window is the lower 48. The corpus has airports outside it (Alaska,
// Hawaii, Puerto Rico) and a route to one of them runs off the frame. Rather
// than draw it in the wrong place or silently drop it, name it.
function offWindow(a) {
  const b = MAP.basemap?.bbox || [-125, 24, -66.5, 50];
  return !(a.lon >= b[0] && a.lon <= b[2] && a.lat >= b[1] && a.lat <= b[3]);
}

function drawLive(snap) {
  if (!MAP.live) return;
  MAP.timetable = (snap.tick_hours || 24) <= TIMETABLE_MAX_TICK_H;
  const ap = airportsByCode();
  MAP.live.innerHTML = "";
  const sel = MAP.selection;

  // --- weather systems, underneath everything else
  for (const w of (snap.weather_systems || [])) {
    const [x, y] = project(w.lon, w.lat);
    // radius_km -> pixels via a local scale sample, so a system's footprint is
    // drawn at its real size rather than a fixed blob
    const [x2] = project(w.lon + w.radius_km / 88.0, w.lat);
    const color = WX_COLOR[w.kind] || "#88a";
    const c = svgEl("circle", {
      cx: x.toFixed(1), cy: y.toFixed(1), r: Math.abs(x2 - x).toFixed(1),
      fill: color, "fill-opacity": (0.07 + 0.22 * w.intensity).toFixed(3),
      stroke: color, "stroke-opacity": 0.35, class: "mapWx",
    }, MAP.live);
    svgEl("title", {}, c).textContent =
      `${w.kind.replace(/_/g, " ").toLowerCase()} — ` +
      `${(w.intensity * 100).toFixed(0)}% · ${w.radius_km.toFixed(0)} km`;
  }

  // --- routes
  const paths = {};
  for (const p of snap.players) {
    const color = carrierColor(snap, p.player_id);
    for (const o of p.route_ops) {
      const A = ap[o.origin], B = ap[o.dest];
      if (!A || !B) continue;
      const gc = greatCircle(A, B);
      paths[o.route_op_id] = gc;
      const on = sel && sel.kind === "route" && sel.id === o.route_op_id;
      const dim = sel && !on;
      // A route that operated nothing this tick has no aircraft on it. Draw
      // it dashed and faint so the map SHOWS the gap rather than just losing
      // an icon — the tooltip carries the reason (crew, gates, weather).
      const idle = !(o.eff_freq > 0);
      const el = svgEl("path", {
        d: pathOf(gc, false), fill: "none", stroke: color,
        "stroke-width": on ? 3.2 : 1.4,
        "stroke-opacity": dim ? 0.15 : (!o.suitable ? 0.3 : idle ? 0.35 : 0.75),
        "stroke-dasharray": o.suitable && !idle ? "" : "4 3",
        class: "mapRoute", "data-op": o.route_op_id,
      }, MAP.live);
      const why = !o.suitable ? o.suitability_reasons.join("; ")
                : o.crew_block ? o.crew_block
                : idle ? "nothing operated this tick" : "";
      svgEl("title", {}, el).textContent =
        `${p.name}: ${o.origin}→${o.dest} · ${o.daily_frequency}/day scheduled, ` +
        `${o.eff_freq.toFixed(1)} operated · ${(o.load_factor * 100).toFixed(0)}% LF` +
        (o.weather ? `\n${o.weather}` : "") + (why ? `\n${why}` : "");
    }
  }

  // --- aircraft (positions DERIVED from the schedule — see the header)
  const specs = {};
  for (const s of (catalog?.aircraft || [])) specs[s.spec_id] = s;
  for (const p of snap.players) {
    const color = carrierColor(snap, p.player_id);
    for (const o of p.route_ops) {
      const gc = paths[o.route_op_id];
      // nothing operated -> nothing in the air. This is the whole reason
      // eff_freq is in the snapshot: a weather-closed or crew-short route
      // should empty its sky, not keep flying a ghost.
      if (!gc || !(o.eff_freq > 0)) continue;
      const plane = p.fleet.find((f) => f.tail_number === o.tail_number);
      if (!plane || plane.retired) continue;
      const spec = specs[plane.spec_id] || {};
      const on = sel && ((sel.kind === "plane" && sel.id === o.tail_number) ||
                         (sel.kind === "route" && sel.id === o.route_op_id));
      const dim = sel && !on;
      const icon = planeIcon(spec);

      // TIMETABLE at the finest resolution; the schematic phase otherwise.
      const fracs = MAP.timetable
        ? airborneOn(o, snap.sim_time_hours)
        : [((snap.sim_time_hours / Math.max(0.5, o.block_h || 2.0))
            + opOffset(o.route_op_id)) % 1];

      for (const f of fracs) {
        const [x, y, ang] = pointOn(gc, f);
        const g = svgEl("g", {
          transform: `translate(${x.toFixed(1)},${y.toFixed(1)}) `
                   + `rotate(${ang.toFixed(1)}) scale(${icon.scale.toFixed(2)})`,
          class: "mapPlane", "data-tail": o.tail_number, "data-op": o.route_op_id,
          opacity: dim ? 0.2 : 1,
        }, MAP.live);
        for (const part of icon.parts) {
          svgEl("path", {
            d: part.d, fill: part.fill === "body" ? color : "#0a1622",
            "fill-opacity": part.fill === "body" ? 1 : 0.75,
            stroke: on ? "#fff" : "rgba(0,0,0,.55)",
            "stroke-width": on ? 1.1 : 0.5,
          }, g);
        }
        svgEl("title", {}, g).textContent =
          `${plane.tail_number} · ${plane.display_name} · ${p.name}\n` +
          `${o.origin}→${o.dest} · ${o.pax.toFixed(0)} pax` +
          (MAP.timetable
            ? `\n${(f * 100).toFixed(0)}% of the leg flown, `
              + `${((1 - f) * (o.block_h || 0)).toFixed(1)}h to run`
            : "");
      }
    }
  }

  // --- airports actually in play
  const live = new Set();
  for (const p of snap.players) {
    for (const o of p.route_ops) { live.add(o.origin); live.add(o.dest); }
    for (const h of p.hubs) live.add(h);
  }
  const offscreen = [];
  for (const iata of live) {
    const a = ap[iata];
    if (!a) continue;
    if (offWindow(a)) { offscreen.push(iata); continue; }
    const [x, y] = project(a.lon, a.lat);
    const info = snap.airports[iata] || {};
    const wx = info.weather || {};
    const isHub = snap.players.some((p) => p.hubs.includes(iata));
    const g = svgEl("g", { class: "mapPort", "data-iata": iata }, MAP.live);
    svgEl("circle", {
      cx: x.toFixed(1), cy: y.toFixed(1), r: isHub ? 4.5 : 2.6,
      class: wx.closed ? "mapPortClosed" : "mapPortDot",
    }, g);
    svgEl("text", { x: (x + 6).toFixed(1), y: (y + 3).toFixed(1), class: "mapLabel" },
          g).textContent = iata;
    svgEl("title", {}, g).textContent =
      `${iata} — ${a.display_name}${isHub ? " (hub)" : ""}` +
      (wx.text ? `\n${wx.text}` : "") +
      (info.reliability?.reliability != null
        ? `\nreliability ${(info.reliability.reliability * 100).toFixed(0)}%` : "");
  }

  drawLegend(snap, offscreen);
}

// A colour with no key is decoration. The legend names the carriers and the
// weather kinds actually on the map right now, so nothing needs a tooltip to
// be identified — and it names any airport in play that the window can't show.
//
// TWO GROUPS, PINNED TO OPPOSITE ENDS: carriers left, weather right. They are
// different kinds of thing — one is who you are competing with, the other is
// what the sky is doing — and in a single run-on row the boundary between
// them moved every tick as carriers entered or a storm cleared, so neither
// list had a stable place to look. The off-window note rides with the weather
// group: like the sky it is a property of the map, not of a carrier.
function drawLegend(snap, offscreen) {
  const box = document.getElementById("mapLegend");
  if (!box) return;
  box.innerHTML = "";
  const left = document.createElement("div");
  left.className = "legendGroup";
  const right = document.createElement("div");
  right.className = "legendGroup";
  box.append(left, right);

  for (const p of snap.players) {
    const s = document.createElement("span");
    s.innerHTML = `<i style="background:${carrierColor(snap, p.player_id)}"></i>` +
                  `${p.name}${p.is_ai ? "" : " (you)"}`;
    left.appendChild(s);
  }
  const kinds = [...new Set((snap.weather_systems || []).map((w) => w.kind))].sort();
  for (const k of kinds) {
    const s = document.createElement("span");
    s.className = "wxKey";
    s.innerHTML = `<i style="background:${WX_COLOR[k] || "#88a"}"></i>` +
                  k.replace(/_/g, " ").toLowerCase();
    right.appendChild(s);
  }
  if (offscreen && offscreen.length) {
    const s = document.createElement("span");
    s.className = "wxKey";
    s.textContent = `off window: ${offscreen.sort().join(" ")}`;
    right.appendChild(s);
  }

  // WHICH DRAWING the viewer is looking at. "These are the flights the
  // schedule has airborne right now" and "this icon stands for the route's
  // daily flying" are materially different claims, so the map says which one
  // it is making rather than leaving the viewer to assume the stronger.
  const mode = document.createElement("span");
  mode.className = "wxKey";
  mode.textContent = MAP.timetable
    ? `timetable · ${document.querySelectorAll(".mapPlane").length} airborne`
    : "schematic · set detail to 1 h for the timetable";
  left.appendChild(mode);
}

// -- selection --------------------------------------------------------------
// Selecting on the map highlights the matching rows in the Routes and Fleet
// panels, which is what makes the map a control surface rather than a poster.
function applySelection() {
  const sel = MAP.selection;
  document.querySelectorAll("[data-rowop],[data-rowtail]").forEach((el) => {
    const isOp = sel && sel.kind === "route" && el.dataset.rowop === sel.id;
    const isTail = sel && sel.kind === "plane" && el.dataset.rowtail === sel.id;
    el.classList.toggle("selectedRow", !!(isOp || isTail));
  });
  // Live state only. The "click an aircraft to highlight it" instruction moved
  // into the About dialog — under the map it was a permanent line of prose
  // saying the same thing every tick, which is what the About button exists to
  // absorb. When something IS selected this line is genuinely status.
  const label = document.getElementById("mapSel");
  if (label) {
    label.textContent = sel
      ? `selected ${sel.kind}: ${sel.id}  (click empty map to clear)`
      : "";
  }
}

function selectOn(kind, id) {
  MAP.selection = (MAP.selection && MAP.selection.kind === kind &&
                   MAP.selection.id === id) ? null : { kind, id };
  if (latest) drawLive(latest);
  applySelection();
}

// -- boot -------------------------------------------------------------------
async function initMap() {
  MAP.wrap = document.getElementById("mapWrap");
  if (!MAP.wrap) return;
  MAP.svg = svgEl("svg", {
    viewBox: `0 0 ${MAP.W} ${MAP.H}`, class: "mapSvg",
    preserveAspectRatio: "xMidYMid meet",
  }, MAP.wrap);
  MAP.base = svgEl("g", {}, MAP.svg);
  MAP.live = svgEl("g", {}, MAP.svg);

  const [basemap, magnetic] = await Promise.all([
    fetch("/api/basemap").then((r) => r.json()).catch(() => null),
    fetch("/api/magnetic").then((r) => r.json()).catch(() => null),
  ]);
  MAP.basemap = basemap;
  MAP.magnetic = magnetic;
  if (magnetic && !magnetic.error) MAP.declination = magnetic.declination;
  setNorthReference(MAP.northRef);

  if (!MAP.basemap || MAP.basemap.error) {
    MAP.basemap = null;
    const note = document.getElementById("mapNote");
    if (note) {
      note.textContent = "No base map committed — run tools/build_basemap.py. " +
        "Routes, aircraft and weather still draw.";
    }
    // Without a base map there is no bbox; fall back to the continental window
    // so the overlay still lands in sensible places.
    fitProjection([-125, 24, -66.5, 50], MAP.W, MAP.H);
  } else {
    fitProjection(MAP.basemap.bbox, MAP.W, MAP.H);
    drawBase();
  }
  drawCompass();
  wireNorthToggle();

  MAP.svg.addEventListener("click", (e) => {
    const plane = e.target.closest(".mapPlane");
    if (plane) return selectOn("plane", plane.dataset.tail);
    const route = e.target.closest(".mapRoute");
    if (route) return selectOn("route", route.dataset.op);
    if (MAP.selection) { MAP.selection = null; if (latest) drawLive(latest); applySelection(); }
  });
  applySelection();
}

// -- north reference: compass, toggle, and the caveat -----------------------
// A compass rose showing BOTH norths, which is the standard way a chart
// resolves this: the map is drawn to one of them, and the other is shown at
// its angle so the difference is visible rather than asserted in prose.
function drawCompass() {
  if (!MAP.svg) return;
  MAP.compass && MAP.compass.remove();
  const g = svgEl("g", { class: "mapCompass",
                         transform: `translate(${MAP.W - 62},72)` }, MAP.svg);
  MAP.compass = g;
  const d = MAP.declination;
  // On screen, "up" is whichever north the map is drawn to.
  const trueUp = (MAP.northRef === "magnetic" && d != null) ? d : 0;
  const magUp = (MAP.northRef === "magnetic" || d == null) ? 0 : d;
  // Labels sit at different radii on purpose: over the contiguous US the two
  // norths can be under two degrees apart, and side-by-side labels would
  // overlap into an unreadable smudge exactly where the map is most correct.
  const arm = (deg, len, cls, label, labelAt) => {
    const a = (deg - 90) * Math.PI / 180;
    const x = Math.cos(a) * len, y = Math.sin(a) * len;
    svgEl("line", { x1: 0, y1: 0, x2: x.toFixed(1), y2: y.toFixed(1), class: cls }, g);
    const lx = Math.cos(a) * labelAt, ly = Math.sin(a) * labelAt;
    svgEl("text", { x: lx.toFixed(1), y: (ly + 3).toFixed(1),
                    class: "mapCompassLabel", "text-anchor": "middle" },
          g).textContent = label;
  };
  svgEl("circle", { cx: 0, cy: 0, r: 34, class: "mapCompassRing" }, g);
  svgEl("circle", { cx: 0, cy: 0, r: 2.5, class: "mapCompassHub" }, g);
  arm(trueUp, 34, "mapCompassTrue", "TN", 46);
  if (d != null) arm(magUp, 26, "mapCompassMag", "MN", 26 - 9);
  svgEl("title", {}, g).textContent = d == null
    ? "true north"
    : `map drawn ${MAP.northRef} north up\n` +
      `variation ${d > 0 ? d.toFixed(1) + "°E" : Math.abs(d).toFixed(1) + "°W"} ` +
      `at ${MAP.magnetic.reference.lat}°N ${Math.abs(MAP.magnetic.reference.lon)}°W`;
}

function wireNorthToggle() {
  const el = document.getElementById("mapNorthRef");
  if (!el) return;
  el.value = MAP.northRef;
  el.onchange = () => {
    setNorthReference(el.value);
    if (MAP.basemap) fitProjection(MAP.basemap.bbox, MAP.W, MAP.H);
    else fitProjection([-125, 24, -66.5, 50], MAP.W, MAP.H);
    drawBase();
    drawCompass();
    if (latest) drawLive(latest);
    northNote();
  };
  northNote();
}

function northNote() {
  const el = document.getElementById("mapNorth");
  if (!el) return;
  const m = MAP.magnetic;
  if (!m || m.error) { el.textContent = "Oriented to true north."; return; }
  const d = m.declination;
  const fmt = (v) => (v >= 0 ? `${v.toFixed(1)}°E` : `${Math.abs(v).toFixed(1)}°W`);
  const lead = MAP.northRef === "magnetic"
    ? `Oriented to MAGNETIC north at ${m.reference.lat}°N `
      + `${Math.abs(m.reference.lon)}°W, where the variation is ${fmt(d)}. `
    : "Oriented to TRUE north. ";
  const caveat = "no single rotation puts magnetic north up everywhere";
  const where = "only the reference meridian is exact";
  el.textContent = lead
    + `Across the window variation runs ${fmt(m.min)} to ${fmt(m.max)}, so `
    + `${caveat} — ${where}. ${m.model}, evaluated ${m.year}.`;
}

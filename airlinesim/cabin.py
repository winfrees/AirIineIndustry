"""
CABIN GEOMETRY — what actually fits inside an airframe.
=======================================================

``finance_cabin`` says what a seat *class* is worth (fare multiplier, demand
share, elasticity). This module says how much *aeroplane* one costs, and is
the single place that answers "can this configuration be installed?".

WHY IT EXISTS
-------------
The original model was one number per class — a "footprint" in economy-seat
equivalents, with an airframe's capacity equal to its all-economy seat count.
That is dimensionally fine but has no handle a player can reason about: it
cannot say how many business seats fit *given* an economy count, cannot snap
a cabin to installable rows, and gives every airframe the same trade-off
regardless of how wide it is. It also silently allowed nonsense (a widebody
configured with a business cabin no widebody could physically hold).

THE MODEL
---------
A cabin is a box of fixed width and fixed length. Width is already captured
by ABREAST (seats per row), so the constraint reduces to one dimension:

    Σ over cabins  rows(cabin) × pitch(cabin)   ≤   cabin_length_m

with ``rows = ceil(seats / abreast)``. Floor area is ``length × width``, so
this IS the area model — width just cancels once abreast is known. Seats
therefore come in whole rows, exactly as they are installed on real seat
tracks, and a premium cabin's cost in economy seats falls out of geometry
rather than being asserted:

    footprint(class) = (pitch_class / pitch_economy) × (abreast_econ / abreast_class)

which is why a lie-flat business seat costs ~2.2 economy seats on a 6-abreast
narrowbody but ~4.2 on a 9-abreast widebody — the economy it displaces is
denser. That difference is a real fleet-planning fact the flat footprint
table could not express.

WHAT IS MEASURED, DERIVED AND HEURISTIC (see also CLAUDE.md)
------------------------------------------------------------
MEASURED   ``AircraftSpec.cabin_abreast`` — published economy seats-per-row
           for the type (6 on a 737, 9 on a 787, 4 on an E175).
DERIVED    ``cabin_length_m`` — NOT a measured fuselage dimension. It is
           back-computed as ``ceil(max_seats / abreast) × economy pitch`` so
           that an all-economy layout fills the cabin EXACTLY. That keeps the
           engine's existing invariant (all-economy == ``max_seats``) true by
           construction, and means the geometry is calibrated to the catalog's
           seat counts rather than pretending to know real cabin lengths.
HEURISTIC  The pitch table and the premium-abreast fractions. They are
           industry-SHAPED (31in economy, ~38in premium, lie-flat business)
           and tuned for game balance, not certified cabin drawings.

An airframe with no ``cabin_abreast`` on its spec (hand-authored specs, old
saves) gets an estimate banded off ``plane_class`` + ``max_seats``, flagged
in ``CabinGeometry.abreast_source`` so an estimate is never mistaken for the
published figure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from airlinesim.engine import AircraftSpec, PlaneClass
from airlinesim.finance_cabin import CabinClass, SeatLayout


# ============================================================
# REFERENCE DATA (tunable; lives as data, not inside the logic)
# ============================================================

# The density baseline the whole model is calibrated to. Cabin length is
# derived at this pitch, so changing it rescales lengths and pitches together
# and leaves every all-economy layout unchanged.
ECONOMY_PITCH_M = 0.79          # 31 in

# Seat pitch by plane class, metres. Economy is the baseline above; premium
# is extra-legroom/premium-economy; business is domestic recliner on the
# smaller types and lie-flat on widebodies; first is a suite.
PITCH_M = {
    PlaneClass.REGIONAL: {
        CabinClass.ECONOMY: 0.79, CabinClass.PREMIUM: 0.91,
        CabinClass.BUSINESS: 1.02, CabinClass.FIRST: 1.14,
    },
    PlaneClass.NARROWBODY: {
        CabinClass.ECONOMY: 0.79, CabinClass.PREMIUM: 0.94,
        CabinClass.BUSINESS: 1.14, CabinClass.FIRST: 1.52,
    },
    PlaneClass.WIDEBODY: {
        CabinClass.ECONOMY: 0.79, CabinClass.PREMIUM: 0.97,
        CabinClass.BUSINESS: 1.47, CabinClass.FIRST: 2.03,
    },
}

# Premium cabins are laid out with fewer seats across. Expressed as a
# fraction of the economy abreast so it scales with fuselage width: a
# 9-abreast widebody economy becomes 7-abreast premium, 4-abreast business
# (1-2-1) and 3-abreast first, while a 6-abreast narrowbody becomes 4-abreast
# for both business and first (2-2 recliner / lie-flat).
ABREAST_FRAC = {
    PlaneClass.REGIONAL: {
        CabinClass.ECONOMY: 1.0, CabinClass.PREMIUM: 1.0,
        CabinClass.BUSINESS: 0.75, CabinClass.FIRST: 0.5,
    },
    PlaneClass.NARROWBODY: {
        CabinClass.ECONOMY: 1.0, CabinClass.PREMIUM: 1.0,
        CabinClass.BUSINESS: 0.67, CabinClass.FIRST: 0.67,
    },
    PlaneClass.WIDEBODY: {
        CabinClass.ECONOMY: 1.0, CabinClass.PREMIUM: 0.78,
        CabinClass.BUSINESS: 0.44, CabinClass.FIRST: 0.33,
    },
}

# Fallback economy abreast when a spec doesn't carry one, banded off seat
# count within a plane class. Bands are (max_seats_at_or_below, abreast);
# the last entry catches everything larger.
ABREAST_BANDS = {
    PlaneClass.REGIONAL: ((50, 3), (10_000, 4)),
    PlaneClass.NARROWBODY: ((100, 4), (150, 5), (10_000, 6)),
    PlaneClass.WIDEBODY: ((250, 7), (300, 8), (350, 9), (10_000, 10)),
}

# Cabins run front to back in this order. It is also the order the fitter
# HONOURS requests in (first class first) and the reverse of the order it
# trims in — economy is the filler that gives way, which is how a real cabin
# plan works: the premium cabins are the decision, economy is what's left.
CABIN_ORDER = (CabinClass.FIRST, CabinClass.BUSINESS,
               CabinClass.PREMIUM, CabinClass.ECONOMY)

# Named starting points, expressed as the FRACTION OF CABIN LENGTH given to
# each premium cabin. Economy takes whatever remains, so a preset means the
# same thing on any airframe. Presets that don't fit an airframe (a first
# cabin on a 76-seat regional jet) simply come back trimmed, with notes.
PRESETS = {
    "all-economy": {},
    "two-class": {CabinClass.BUSINESS: 0.20},
    "premium-heavy": {CabinClass.BUSINESS: 0.35, CabinClass.PREMIUM: 0.15},
    "three-class": {CabinClass.FIRST: 0.12, CabinClass.BUSINESS: 0.30,
                    CabinClass.PREMIUM: 0.15},
}


def _round_half_up(x: float) -> int:
    """Python's round() is banker's rounding; seat maths wants half-up."""
    return int(math.floor(x + 0.5))


# ============================================================
# GEOMETRY
# ============================================================

@dataclass(frozen=True)
class ClassGeometry:
    """How one cabin class is installed on a particular airframe."""
    cabin_class: CabinClass
    pitch_m: float
    abreast: int

    def rows_for(self, seats: int) -> int:
        return int(math.ceil(max(0, seats) / self.abreast)) if seats > 0 else 0

    def length_for(self, seats: int) -> float:
        return self.rows_for(seats) * self.pitch_m

    def seats_in(self, length_m: float) -> int:
        """Most seats of this class installable in a run of cabin length."""
        return max(0, int(length_m / self.pitch_m + 1e-9)) * self.abreast


@dataclass(frozen=True)
class CabinGeometry:
    """
    The installable cabin of one airframe type. Everything the fitter, the
    validator and the UI need to answer "what fits?".
    """
    spec_id: str
    display_name: str
    plane_class: PlaneClass
    max_seats: int
    cabin_length_m: float
    abreast_economy: int
    abreast_source: str                  # "spec" (published) | "estimated"
    classes: dict = field(default_factory=dict)   # CabinClass -> ClassGeometry

    # -- queries -------------------------------------------------------
    def geom(self, cc: CabinClass) -> ClassGeometry:
        return self.classes[cc]

    def footprint(self, cc: CabinClass) -> float:
        """This class's cost in economy seats — derived, not asserted."""
        e = self.classes[CabinClass.ECONOMY]
        g = self.classes[cc]
        return (g.pitch_m / e.pitch_m) * (e.abreast / g.abreast)

    def length_used(self, seats: dict) -> float:
        return sum(self.classes[cc].length_for(n)
                   for cc, n in seats.items() if n > 0)

    def headroom_m(self, seats: dict) -> float:
        return self.cabin_length_m - self.length_used(seats)

    def fits(self, seats: dict) -> bool:
        return (self.headroom_m(seats) >= -1e-9
                and sum(seats.values()) <= self.max_seats)

    def max_seats_of(self, cc: CabinClass, others: Optional[dict] = None) -> int:
        """
        Most seats of `cc` installable given the OTHER cabins in `others`
        (any count already present for `cc` is ignored — this answers "what
        could this cabin be?", which is what a seat-count field needs).

        Bounded by BOTH the length left over and the type's ``max_seats``,
        which is read as the certified occupancy limit: the derived cabin
        rounds up to a whole row, so without this an all-economy 787-9 would
        come out at 297 seats against a 290-seat type. The limit only ever
        binds on a dense single-class cabin — a premium seat eats more length
        per seat, so a mixed cabin is under it by construction.
        """
        rest = {k: v for k, v in (others or {}).items() if k != cc and v > 0}
        by_length = self.classes[cc].seats_in(self.headroom_m(rest))
        by_limit = self.max_seats - sum(rest.values())
        return max(0, min(by_length, by_limit))

    def describe(self) -> dict:
        """JSON-safe projection for the catalog / UI."""
        return {
            "spec_id": self.spec_id,
            "cabin_length_m": round(self.cabin_length_m, 2),
            "abreast_economy": self.abreast_economy,
            "abreast_source": self.abreast_source,
            "max_seats": self.max_seats,
            "classes": {
                cc.name: {
                    "pitch_m": round(g.pitch_m, 3),
                    "pitch_in": round(g.pitch_m / 0.0254),
                    "abreast": g.abreast,
                    "footprint": round(self.footprint(cc), 2),
                    "max_alone": self.max_seats_of(cc),
                } for cc, g in self.classes.items()
            },
        }


def estimate_abreast(plane_class: PlaneClass, max_seats: int) -> int:
    """Economy seats per row when the spec doesn't publish one."""
    bands = ABREAST_BANDS.get(plane_class, ABREAST_BANDS[PlaneClass.NARROWBODY])
    for ceiling, abreast in bands:
        if max_seats <= ceiling:
            return abreast
    return bands[-1][1]


def geometry_for(spec: AircraftSpec) -> CabinGeometry:
    """
    The installable cabin of an aircraft type. Uses the spec's published
    ``cabin_abreast`` and ``cabin_length_m`` when present, and derives what
    isn't given — length from ``max_seats`` at economy pitch, so all-economy
    fills the cabin exactly.
    """
    plane_class = spec.plane_class if spec.plane_class in PITCH_M \
        else PlaneClass.NARROWBODY
    published = int(getattr(spec, "cabin_abreast", 0) or 0)
    abreast_e = published if published > 0 \
        else estimate_abreast(plane_class, spec.max_seats)

    pitches = PITCH_M[plane_class]
    fracs = ABREAST_FRAC[plane_class]
    classes = {}
    for cc in CabinClass:
        abreast = max(2, _round_half_up(abreast_e * fracs[cc]))
        if cc == CabinClass.ECONOMY:
            abreast = abreast_e
        classes[cc] = ClassGeometry(cc, pitches[cc], abreast)

    length = float(getattr(spec, "cabin_length_m", 0.0) or 0.0)
    if length <= 0:
        rows = int(math.ceil(max(1, spec.max_seats) / abreast_e))
        length = rows * pitches[CabinClass.ECONOMY]

    return CabinGeometry(
        spec_id=spec.spec_id, display_name=spec.display_name,
        plane_class=plane_class, max_seats=spec.max_seats,
        cabin_length_m=length, abreast_economy=abreast_e,
        abreast_source="spec" if published > 0 else "estimated",
        classes=classes)


# ============================================================
# PARSING + FITTING
# ============================================================

def parse_seats(seats) -> tuple:
    """
    ({CabinClass: int}, error). Accepts cabin names or enum keys, and either
    None or a blank/absent value to mean "not specified" — which is what lets
    a blank economy field mean "fill the rest of the cabin".
    """
    if not seats:
        return {}, None
    out = {}
    for k, v in seats.items():
        try:
            cc = k if isinstance(k, CabinClass) else CabinClass[str(k).strip().upper()]
        except KeyError:
            return None, f"unknown cabin class '{k}'"
        if v is None or (isinstance(v, str) and not v.strip()):
            continue                     # explicitly unspecified
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return None, f"{cc.name} seat count must be a number, got '{v}'"
        if n < 0:
            return None, f"{cc.name} seat count can't be negative"
        out[cc] = n
    return out, None


@dataclass
class CabinFit:
    """
    The result of fitting a requested cabin to an airframe: the layout that
    will actually be installed, plus every adjustment made getting there.
    Nothing is changed silently — `notes` is the record shown to the player.
    """
    layout: SeatLayout
    geometry: CabinGeometry
    notes: list = field(default_factory=list)
    exact: bool = True                   # nothing had to be trimmed or snapped

    @property
    def seats(self) -> dict:
        return self.layout.seats

    def total_seats(self) -> int:
        return self.layout.total_seats()

    def length_used_m(self) -> float:
        return self.geometry.length_used(self.layout.seats)

    def summary(self) -> str:
        parts = [f"{n} {cc.name.lower()}"
                 for cc, n in sorted(self.layout.seats.items(),
                                     key=lambda kv: CABIN_ORDER.index(kv[0]))
                 if n > 0]
        return ", ".join(parts) or "empty cabin"


def fit_layout(spec: AircraftSpec, seats, fill_economy: bool = True) -> CabinFit:
    """
    Turn a requested seat count per cabin into one that can actually be
    installed on `spec`.

    Three things happen, each recorded in ``CabinFit.notes``:

      1. SNAP TO ROWS. Seats are installed a row at a time, so a request is
         rounded UP to a whole row where that still fits, and down where it
         doesn't. Asking for 18 business at 4-abreast gets you 20 or 16, never
         18 — the same answer a cabin engineer would give.
      2. TRIM TO LENGTH. If the request overflows the cabin, cabins give way
         in reverse order — economy first, first class last — because the
         premium cabins are the decision being made and economy is the filler.
      3. FILL. With ``fill_economy`` (the default), any cabin length left over
         becomes economy seats. That is the "auto-calculate what is possible"
         behaviour: name the premium cabins you want, get the largest legal
         economy cabin behind them. Pass an explicit economy count to opt out
         for that cabin.

    Never raises for an over-large request; it returns the biggest legal
    cabin plus the note saying what it had to cut.
    """
    geom = geometry_for(spec)
    # parse_seats accepts both enum and string keys, so callers inside the
    # package and callers coming off the wire take the identical path.
    requested, err = parse_seats(seats)
    if err:
        # Caller-facing parse errors are caught by build_layout() before it
        # gets here; reaching this with one means an internal caller passed
        # junk, so fail loudly rather than installing a cabin nobody asked for.
        raise ValueError(err)

    economy_specified = CabinClass.ECONOMY in requested
    fitted: dict = {}
    trimmed: set = set()

    # 1) snap each requested cabin to whole rows, honouring the premium
    #    cabins first so a big economy request can't crowd them out.
    for cc in CABIN_ORDER:
        want = int(requested.get(cc, 0) or 0)
        if want <= 0:
            continue
        g = geom.geom(cc)
        rows_up = int(math.ceil(want / g.abreast))
        snapped = rows_up * g.abreast
        if snapped != want:
            # only snap up if the extra row still fits alongside what's placed
            trial = dict(fitted)
            trial[cc] = snapped
            if not geom.fits(trial):
                snapped = (rows_up - 1) * g.abreast
        if snapped > 0:
            fitted[cc] = snapped

    # 2) trim, cheapest cabin first, until the plan fits the tube
    for cc in reversed(CABIN_ORDER):
        if geom.fits(fitted):
            break
        if fitted.get(cc, 0) <= 0:
            continue
        others = {k: v for k, v in fitted.items() if k != cc}
        allowed = geom.max_seats_of(cc, others)
        if allowed < fitted[cc]:
            trimmed.add(cc)
            if allowed > 0:
                fitted[cc] = allowed
            else:
                fitted.pop(cc)

    # 3) fill what's left with economy
    if fill_economy and not economy_specified:
        room = geom.max_seats_of(CabinClass.ECONOMY, fitted)
        if room > 0:
            fitted[CabinClass.ECONOMY] = room

    fitted = {cc: n for cc, n in fitted.items() if n > 0}
    notes = []
    for cc in CABIN_ORDER:
        want = int(requested.get(cc, 0) or 0)
        got = fitted.get(cc, 0)
        if want == got:
            continue
        if cc == CabinClass.ECONOMY and not economy_specified:
            continue          # the auto-fill is the feature, not an adjustment
        g = geom.geom(cc)
        # one note per cabin, saying what it became and why — a request that
        # is both snapped to a row AND trimmed for space is one adjustment
        # from the player's side, not two.
        why = (f"cabin is {geom.cabin_length_m:.1f}m, {geom.max_seats} seats max"
               if cc in trimmed else
               f"{got // g.abreast} rows of {g.abreast}" if got else
               f"no room for a row of {g.abreast}")
        notes.append(f"{cc.name.lower()} {want} -> {got} ({why})")

    if not fitted:
        # An airframe always has *some* cabin; an empty plan means the request
        # was unfittable in every cabin, so fall back to all-economy rather
        # than handing back an aircraft with no seats.
        fitted = {CabinClass.ECONOMY: geom.max_seats_of(CabinClass.ECONOMY)}
        notes.append("nothing requested would fit — configured all-economy")

    return CabinFit(SeatLayout(fitted), geom, notes, not notes)


def preset_layout(spec: AircraftSpec, name: str) -> CabinFit:
    """
    A named cabin plan (see ``PRESETS``), allocated as fractions of cabin
    LENGTH rather than seat counts, so "two-class" means the same thing on a
    regional jet and on a 777.
    """
    plan = PRESETS.get(name)
    if plan is None:
        raise KeyError(f"unknown cabin preset '{name}'")
    geom = geometry_for(spec)
    seats = {}
    for cc, frac in plan.items():
        g = geom.geom(cc)
        seats[cc] = g.seats_in(geom.cabin_length_m * frac)
    return fit_layout(spec, seats, fill_economy=True)


def presets_for(spec: AircraftSpec) -> dict:
    """Every preset resolved against one airframe — what the UI offers."""
    out = {}
    for name in PRESETS:
        fit = preset_layout(spec, name)
        out[name] = {cc.name: n for cc, n in fit.layout.seats.items() if n > 0}
    return out


def fit_report(spec: AircraftSpec, seats=None) -> dict:
    """
    JSON-safe answer to "what would this cabin be, and what else could I
    fit?" — the single source the acquisition/recabin UI reads, so the
    browser never re-implements the geometry.
    """
    fit = fit_layout(spec, seats or {})
    geom = fit.geometry
    placed = fit.layout.seats
    return {
        "spec_id": spec.spec_id,
        "geometry": geom.describe(),
        "seats": {cc.name: n for cc, n in placed.items() if n > 0},
        "total_seats": fit.total_seats(),
        "max_alone": {cc.name: geom.max_seats_of(cc) for cc in CabinClass},
        # The live per-field maximum a seat input should clamp to. A premium
        # cabin is measured against the OTHER PREMIUM cabins only, because
        # economy is what gives way when space runs short (same order the
        # fitter trims in) — measuring it against an auto-filled economy would
        # report a maximum of zero for every field the moment economy fills.
        "max_with_plan": {
            cc.name: geom.max_seats_of(
                cc, placed if cc == CabinClass.ECONOMY
                else {k: v for k, v in placed.items() if k != CabinClass.ECONOMY})
            for cc in CabinClass},
        "cabin_length_m": round(geom.cabin_length_m, 2),
        "length_used_m": round(fit.length_used_m(), 2),
        "exact": fit.exact,
        "notes": list(fit.notes),
        "presets": presets_for(spec),
    }

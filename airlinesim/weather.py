"""
WEATHER — geography, season, and moving systems.
================================================

What this module answers: *what is the sky doing at this airport, at this
hour, and does it matter to this flight?* What it deliberately does NOT do is
decide the operational consequence — that is ``disruption.py``, which turns a
sky into cancelled flights, blown duty limits, stranded passengers and hotel
bills. Keep that split: this file is meteorology, that file is operations.

THE THREE LAYERS
----------------
1. CLIMATE      Each airport gets a climatology from its measured lat/lon: how
                warm it is this month, how continental, how coastal, whether
                it sits in the convective south, the freezing-rain belt, the
                lake-effect snow band, the hurricane basin, the wildfire west.
                This decides what KIND of weather is even possible, and when.
2. SYSTEMS      Weather is not per-airport dice. A ``WeatherSystem`` is a
                disturbance with a position, a radius, an intensity and a
                velocity, and it MOVES — mid-latitude fronts sweep west to
                east along the storm track, hurricanes run west then recurve
                northeast, ash and smoke drift downwind. So a front closes
                ORD, then six hours later it is closing DTW, and a network
                built along one corridor suffers in a way a scattered one
                doesn't. That is the whole point of modelling geography.
3. LOCAL        At an airport, the systems overhead plus the local climate
                give a ceiling/visibility category, a wind, and a runway
                condition. Those are what operations reads.

DETERMINISM (load-bearing — do not break)
-----------------------------------------
``engine.py`` contains no ``random`` call, and ``explorer.py`` depends on
that: a forked state re-run with the same edits must give a byte-identical
result, which is what makes a tree of branches a map rather than noise.

So weather is **deterministic**: every system that will ever exist is a pure
function of ``(world_seed, time_slot, basin)`` via ``_h01()``. Re-running the
same hours produces the same storms; forking and replaying reproduces them
exactly; two processes agree.

``_h01()`` uses **blake2b, not Python's ``hash()``**. ``hash()`` on a string
is salted per process by PYTHONHASHSEED, so a weather model built on it would
generate a different climate in every process — including between the
explorer's parent and child runs, which is precisely the determinism the tree
rests on. If you ever "optimize" this to ``hash()``, `airlinesim run weather`
turns red, and that is the alarm working.

WHAT IS MEASURED, DERIVED AND HEURISTIC
---------------------------------------
MEASURED   Airport latitude/longitude (OurAirports, via the committed corpus).
           Volcano locations — real Cascade and Alaskan volcanoes.
           Hurricane season dates and basin geography.
HEURISTIC  **Everything else in this file.** The seasonal temperature curve,
           the frequencies with which each system spawns, the sizes and
           speeds of systems, the capacity hit each condition causes. These
           are industry- and climate-SHAPED for game balance; none of it is
           fitted to a weather record, because no weather record is committed
           to this repository.

The honest source for the frequencies is NOAA's 1991-2020 Climate Normals
(per-station monthly temperature, precipitation, snowfall), and for the
IMPACTS it is BTS On-Time Performance, which carries actual weather-delay
minutes and weather-cancellation counts per airport per month. Both are
dev-time ingests this repo cannot reach from a sandbox, so the loader seam
exists (`airlinesim/data/weather.json`, absent by default) and the model runs
on the heuristic climatology until it is filled. See
``docs/weather-design.md`` for the ingest plan and what each source fixes.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from airlinesim.route import haversine


# ============================================================
# DETERMINISTIC NOISE
# ============================================================

def _h01(*parts) -> float:
    """
    A stable pseudo-random number in [0,1) from any tuple of values.

    blake2b, NOT hash(): string hashing is salted per process, and a weather
    model that changes between processes would break explorer determinism and
    make a save unreproducible. This is stable across runs, machines and
    Python versions.
    """
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big") / 18_446_744_073_709_551_616.0


def _h_range(lo: float, hi: float, *parts) -> float:
    return lo + (hi - lo) * _h01(*parts)


# ============================================================
# 1. CLIMATE — what geography makes possible
# ============================================================

class WeatherKind(Enum):
    """
    What the sky is doing. Ordered roughly by how much it hurts an airport.
    CLEAR is the common case and costs nothing.
    """
    CLEAR = auto()
    RAIN = auto()               # steady precipitation: minor rate reduction
    FOG = auto()                # low visibility: big rate reduction, few cancels
    THUNDERSTORM = auto()       # convection: ground stops, holding, diversions
    SNOW = auto()               # accumulating: de-icing, runway clearing
    ICING = auto()              # freezing rain: the worst per hour, closes fields
    BLIZZARD = auto()           # snow + wind: closes fields for many hours
    HURRICANE = auto()          # multi-day closure, days of advance cancels
    WILDFIRE_SMOKE = auto()     # visibility, mostly western, summer/autumn
    VOLCANIC_ASH = auto()       # rare, absolute: engines cannot ingest ash


# Real volcanoes with real coordinates, and the only MEASURED geography here
# besides airport positions. An eruption is rare but shuts a whole downwind
# corridor rather than one field, which is why it is worth modelling at all.
VOLCANOES = (
    ("Rainier", 46.85, -121.76), ("St Helens", 46.19, -122.19),
    ("Hood", 45.37, -121.70), ("Shasta", 41.41, -122.19),
    ("Redoubt", 60.49, -152.74), ("Spurr", 61.30, -152.25),
    ("Cleveland", 52.82, -169.94),
)

# Coarse coastline anchors used only to estimate how continental a place is
# (distance to the nearest large water body). A real coastline would be a
# shapefile and a GIS dependency; this is a dozen points and gets the
# maritime/continental contrast right, which is all the seasonal curve needs.
_OCEAN_COAST = (
    (47.6, -122.3), (44.6, -124.1), (37.8, -122.5), (32.7, -117.2),   # Pacific
    (29.3, -94.8), (30.4, -88.9), (27.8, -82.6), (25.8, -80.2),        # Gulf
    (32.1, -81.1), (35.2, -75.5), (38.9, -76.0), (40.7, -74.0),        # Atlantic
    (42.4, -71.0), (44.8, -68.8),
    (61.2, -149.9), (58.3, -134.4), (21.3, -157.9),                    # AK / HI
)
# The Great Lakes moderate temperature and make fog exactly like an ocean
# does, but they do not produce hurricanes. Kept separate for that reason:
# folding them into one coastline gave Chicago — 27 km from Lake Michigan —
# a third of Miami's hurricane exposure.
# The Gulf of Mexico, source of most of the moisture that feeds US convection.
# Used only to scale thunderstorm propensity with distance.
_GULF = (
    (29.3, -94.8), (29.7, -91.2), (30.4, -88.9), (30.1, -85.7), (27.8, -82.6),
    (25.8, -80.2), (26.0, -97.2),
)

_LAKE_COAST = (
    (41.9, -87.6), (43.0, -83.0), (43.6, -79.4), (46.5, -84.3), (43.5, -76.5),
)


def _dist_to_ocean_km(lat: float, lon: float) -> float:
    return min(haversine(lat, lon, cl, cn) for cl, cn in _OCEAN_COAST)


def _dist_to_water_km(lat: float, lon: float) -> float:
    """Nearest large water body of any kind — ocean, Gulf or Great Lake."""
    return min(_dist_to_ocean_km(lat, lon),
               min(haversine(lat, lon, cl, cn) for cl, cn in _LAKE_COAST))


@dataclass(frozen=True)
class Climate:
    """
    One airport's climatology, derived from its position. Every field is a
    HEURISTIC standing in for a NOAA normal — see the module docstring.
    """
    iata: str
    lat: float
    lon: float
    coast_km: float
    continentality: float       # 0 maritime .. 1 deep interior
    convective: float           # thunderstorm propensity
    winter_severity: float      # snow/blizzard propensity
    icing_belt: float           # freezing-rain propensity
    fog_prone: float
    hurricane_exposure: float
    wildfire_exposure: float
    ash_exposure: float

    # -- seasonal temperature -------------------------------------------
    def mean_temp_c(self, day_of_year: float) -> float:
        """
        Monthly-mean-shaped temperature. Warm at low latitude, colder going
        north, with a seasonal swing that grows with latitude and with
        distance from moderating water. Peaks around day 200 (mid-July).

        The amplitude is CAPPED. Growing it linearly with latitude gave
        Anchorage a 25 C swing — a January of -22 C and a July of +29 C, which
        made every high-latitude summer tropical and every winter Siberian.
        Real continental interiors top out around a 20 C seasonal amplitude.
        """
        base = 26.0 - 0.62 * (self.lat - 20.0)
        amp = min(19.0, 3.0 + 0.39 * max(0.0, self.lat - 20.0)) \
            * (0.62 + 0.55 * self.continentality)
        return base + amp * math.cos(2 * math.pi * (day_of_year - 200.0) / 365.0)

    def freezing(self, day_of_year: float) -> float:
        """How near freezing this airport is now: 0 warm .. 1 well below."""
        t = self.mean_temp_c(day_of_year)
        # Steeper than it looks: a monthly MEAN of 4 C already means
        # freezing nights and frozen precipitation some of the time, and a
        # mean of -5 C means winter operations continuously. A gentler
        # ramp left Chicago at 0.47 in January and it never closed.
        return max(0.0, min(1.0, (4.0 - t) / 9.0))


def climate_for(iata: str, lat: float, lon: float) -> Climate:
    """
    Build a climatology from position alone. The bands below are geography a
    US-network player would recognise — the convective South, the Great Lakes
    snow belt, the Ohio Valley ice belt, the Gulf/Atlantic hurricane coast,
    the fire-season West — expressed as smooth functions rather than a lookup
    table so any airport in the corpus gets a defensible answer.
    """
    coast = _dist_to_water_km(lat, lon)
    ocean = _dist_to_ocean_km(lat, lon)
    # Continentality is measured against the OCEAN. A Great Lake makes fog and
    # lake-effect snow but it does not moderate a winter the way an ocean
    # does — it partly freezes, and it is small. Measuring against any water
    # made Chicago (27 km from Lake Michigan) as maritime as Boston, which
    # flattened its seasonal swing and left it too warm to snow in January.
    continentality = max(0.0, min(1.0, 1.0 - math.exp(-ocean / 420.0)))

    # Convection needs warmth AND moisture. Latitude supplies the warmth; the
    # Gulf of Mexico supplies almost all of the moisture that feeds US
    # thunderstorms, so propensity decays with distance from it. Without the
    # moisture term this was latitude-only, and Los Angeles — as warm as
    # Atlanta and at almost the same latitude — scored a full 1.0 and drew
    # 200 thunderstorm hours a year, which is not a description of Southern
    # California. The 0.15 floor leaves the desert Southwest its monsoon.
    warmth = max(0.0, min(1.0, 1.15 - abs(lat - 33.0) / 18.0))
    gulf_km = min(haversine(lat, lon, gl, gn) for gl, gn in _GULF)
    moisture = 0.15 + 0.85 * math.exp(-gulf_km / 1400.0)
    convective = max(0.0, min(1.0, warmth * moisture * 1.35))

    # Winter severity rises with latitude, then is DAMPED by maritime
    # moderation: on latitude alone Seattle scored a full 1.0, the same as
    # Minneapolis, which is not a description of the Pacific Northwest. The
    # lake-effect bonus is applied after the damping, so Buffalo — mild by
    # ocean standards but downwind of Erie — still lands high.
    winter = max(0.0, min(1.0, (lat - 31.0) / 16.0)) * (0.5 + 0.5 * continentality)
    if 41.0 <= lat <= 47.0 and -88.0 <= lon <= -75.0:
        winter = min(1.0, winter + 0.30)              # lake-effect belt

    # Freezing rain needs a shallow cold layer under warm air: a band, not a
    # gradient. Centred on the Ohio Valley / mid-Atlantic piedmont.
    icing = max(0.0, 1.0 - abs(lat - 39.5) / 6.0) * max(0.35, min(1.0, (lon + 100.0) / 25.0))

    # Fog: coastal and valley. Strongest right on the water.
    fog = max(0.0, min(1.0, math.exp(-coast / 160.0) * 0.9 + 0.1))

    # Hurricanes: Gulf and Atlantic coast only, decaying inland and north.
    # Measured against the OCEAN coastline — a Great Lake is not a basin.
    if lon > -100.0 and lat < 43.0:
        hurricane = max(0.0, min(1.0, math.exp(-ocean / 260.0)
                                 * (1.0 - max(0.0, lat - 30.0) / 18.0)))
    else:
        hurricane = 0.0

    # Wildfire smoke: the interior and mountain West.
    wildfire = max(0.0, min(1.0, (-(lon) - 103.0) / 18.0)) * max(0.0, min(1.0, (lat - 31.0) / 12.0))

    # Ash: proximity to a real volcano, and only near one.
    nearest_v = min(haversine(lat, lon, vlat, vlon) for _, vlat, vlon in VOLCANOES)
    ash = max(0.0, min(1.0, math.exp(-nearest_v / 700.0)))

    return Climate(iata=iata, lat=lat, lon=lon, coast_km=coast,
                   continentality=continentality, convective=convective,
                   winter_severity=winter, icing_belt=max(0.0, min(1.0, icing)),
                   fog_prone=fog, hurricane_exposure=hurricane,
                   wildfire_exposure=wildfire, ash_exposure=ash)


# ============================================================
# 2. SYSTEMS — disturbances that exist in space and move
# ============================================================

# Per-kind behaviour. radius/speed/life are HEURISTIC but scaled to the real
# thing: a thunderstorm complex is small, fast and short-lived; a hurricane is
# large, slow and lasts days; an ash cloud drifts for a day and is absolute.
#
#   radius_km      how far the disturbance reaches
#   speed_kmh      how fast its centre travels
#   life_h         how long it lasts
#   bearing_deg    direction of travel (90 = due east, meteorological convention
#                  here is "toward": mid-latitude systems run west -> east)
#   cap_floor      worst-case capacity multiplier at the centre at full intensity
#   delay_h        delay added per departure at the centre at full intensity
@dataclass(frozen=True)
class KindProfile:
    radius_km: float
    speed_kmh: float
    life_h: float
    bearing_deg: float
    cap_floor: float
    delay_h: float
    closes: bool = False       # can shut a field outright at high intensity


KIND_PROFILE = {
    WeatherKind.RAIN:           KindProfile(320, 45, 14, 80, 0.86, 0.30),
    WeatherKind.FOG:            KindProfile(120, 12, 8, 70, 0.55, 0.70),
    # Convection does not CLOSE an airport, it throttles it — a ground stop
    # and a holding stack, not a shut field. Marked closing, Dallas shut
    # outright for 100 hours a year, which is four days Dallas does not have.
    WeatherKind.THUNDERSTORM:   KindProfile(150, 55, 6, 75, 0.30, 1.30, False),
    WeatherKind.SNOW:           KindProfile(380, 50, 16, 85, 0.55, 1.10),
    WeatherKind.ICING:          KindProfile(220, 40, 12, 85, 0.30, 2.10, True),
    WeatherKind.BLIZZARD:       KindProfile(420, 45, 22, 85, 0.12, 3.20, True),
    WeatherKind.HURRICANE:      KindProfile(520, 22, 72, 45, 0.05, 6.00, True),
    WeatherKind.WILDFIRE_SMOKE: KindProfile(340, 25, 40, 75, 0.70, 0.55),
    WeatherKind.VOLCANIC_ASH:   KindProfile(450, 60, 30, 80, 0.02, 8.00, True),
}


@dataclass
class WeatherSystem:
    """
    One disturbance. Position and intensity are functions of age, so a system
    is fully described by its birth parameters — which is what lets the whole
    field be regenerated deterministically from the clock.
    """
    system_id: str
    kind: WeatherKind
    born_at: float              # sim hours
    life_h: float
    lat0: float
    lon0: float
    bearing_deg: float
    speed_kmh: float
    radius_km: float
    peak_intensity: float       # 0..1

    def age(self, now: float) -> float:
        return now - self.born_at

    def alive(self, now: float) -> bool:
        return 0.0 <= self.age(now) <= self.life_h

    def position(self, now: float) -> tuple:
        """Great-circle dead reckoning from birth point along a fixed bearing."""
        km = self.speed_kmh * max(0.0, self.age(now))
        d = km / 6371.0
        br = math.radians(self.bearing_deg)
        la1, lo1 = math.radians(self.lat0), math.radians(self.lon0)
        la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(br))
        lo2 = lo1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(la1),
                               math.cos(d) - math.sin(la1) * math.sin(la2))
        return math.degrees(la2), math.degrees(lo2)

    def intensity(self, now: float) -> float:
        """
        Ramps up, peaks mid-life, decays — so a system arrives, bites and
        clears rather than switching on and off. A step function would make
        every disruption start and end on a tick boundary.
        """
        if not self.alive(now):
            return 0.0
        phase = self.age(now) / max(1e-6, self.life_h)
        return self.peak_intensity * math.sin(math.pi * phase) ** 0.7

    def effect_at(self, lat: float, lon: float, now: float) -> float:
        """
        Strength of this system at a point: intensity tapered by distance from
        the centre. 0 outside the radius.
        """
        i = self.intensity(now)
        if i <= 0:
            return 0.0
        clat, clon = self.position(now)
        d = haversine(lat, lon, clat, clon)
        if d >= self.radius_km:
            return 0.0
        # A storm has a CORE, not a single worst point. A pure cosine taper
        # peaked at the exact centre meant an airport was only ever clipped by
        # the edge of something, and nothing was ever badly hit. Full strength
        # out to 45% of the radius, then a cosine fade to zero at the edge —
        # which is much closer to how a convective complex or a snow shield
        # actually covers ground.
        core = 0.45 * self.radius_km
        if d <= core:
            return i
        frac = (d - core) / (self.radius_km - core)
        return i * math.cos(0.5 * math.pi * frac)


# ============================================================
# 3. THE MODEL — spawning, and what an airport sees
# ============================================================

# Systems are spawned on a fixed grid of time slots so the set of systems is a
# pure function of the clock. Every slot, every basin gets one deterministic
# roll per kind.
SPAWN_SLOT_H = 6.0

# Basins are coarse spawn regions, not real meteorology: a system is born
# somewhere in a basin and then travels. Mid-latitude systems are born on the
# WEST side so they sweep east across the network, which is what makes a
# north-south network and an east-west one different propositions.
#   (name, lat_lo, lat_hi, lon_lo, lon_hi)
BASINS = (
    ("nw", 42.0, 49.0, -125.0, -110.0),
    ("sw", 31.0, 42.0, -125.0, -110.0),
    ("nplains", 41.0, 49.0, -110.0, -95.0),
    ("splains", 29.0, 41.0, -108.0, -95.0),
    ("midwest", 38.0, 47.0, -95.0, -82.0),
    ("south", 27.0, 38.0, -95.0, -80.0),
    ("northeast", 38.0, 47.0, -82.0, -68.0),
    ("tropics", 12.0, 24.0, -82.0, -50.0),        # hurricane genesis
)

# Base spawn probability per basin per 6-hour slot, before climate and season
# scale it. Tuned so a typical corpus airport sees materially disrupted
# operations on the order of a few days a month — the shape of the real thing
# without any claim to match a specific station's record.
BASE_SPAWN = {
    WeatherKind.RAIN: 0.18,
    # Convection is deliberately the most frequent SEVERE kind. It is the
    # single biggest summer disruptor in the real network — Atlanta alone has
    # around fifty thunderstorm days a year — and cells are small and
    # short-lived, so it takes many of them to cover a season. At a quarter of
    # this rate Atlanta saw 49 convective hours a year and read as a placid
    # airport, which is the opposite of the truth.
    WeatherKind.THUNDERSTORM: 0.85,
    WeatherKind.FOG: 0.22,
    WeatherKind.SNOW: 0.22,
    WeatherKind.ICING: 0.08,
    WeatherKind.BLIZZARD: 0.035,
    WeatherKind.HURRICANE: 0.020,
    WeatherKind.WILDFIRE_SMOKE: 0.05,
    # An eruption that reaches airline cruise levels over North America is a
    # once-in-years event, not a once-in-months one. At 0.0009 per basin-slot
    # this fired ~2.6 times a year and Chicago saw ash six times — which
    # turned the rarest event in the model into background noise. Two western
    # basins x 1460 slots/year puts this at roughly one eruption per 6 years.
    WeatherKind.VOLCANIC_ASH: 0.00006,
}


def _season(day_of_year: float, peak_day: float, strength: float = 1.0) -> float:
    """1.0 at the peak day, falling to (1-strength) half a year away."""
    c = math.cos(2 * math.pi * (day_of_year - peak_day) / 365.0)
    return max(0.0, 1.0 - strength + strength * (0.5 + 0.5 * c))


@dataclass
class AirportWeather:
    """What operations actually reads: one airport, one moment."""
    iata: str
    kind: WeatherKind
    intensity: float             # 0..1
    capacity_factor: float       # 1.0 normal, 0.0 field closed
    delay_h: float               # added block/turn time per departure
    closed: bool
    systems: tuple = ()          # ids of the systems responsible

    @property
    def disrupted(self) -> bool:
        return self.capacity_factor < 0.995 or self.delay_h > 0.01

    def describe(self) -> str:
        if self.kind == WeatherKind.CLEAR:
            return "clear"
        label = self.kind.name.lower().replace("_", " ")
        if self.closed:
            return f"{label} — field closed"
        return f"{label} ({self.intensity:.0%}, {self.capacity_factor:.0%} capacity)"


class WeatherModel:
    """
    The live weather field. Deterministic in ``seed`` and the clock: calling
    ``at()`` for the same airport and the same hour always gives the same
    answer, no matter how many times the world is forked and replayed.

    Systems are not stored as they spawn — they are DERIVED from the clock on
    demand and cached per slot, so a pickled world carries no weather state
    that could drift from the seed. That also means the explorer's forks share
    one weather history, which is what makes two branches comparable: they
    face the same storms and differ only by the decisions taken.
    """

    def __init__(self, seed: int = 20260729, climates: Optional[dict] = None):
        self.seed = int(seed)
        self.climates: dict = dict(climates or {})
        self._slot_cache: dict = {}      # slot index -> tuple[WeatherSystem]
        self._cache_lo = 0

    # -- setup ---------------------------------------------------------
    def add_airport(self, iata: str, lat: float, lon: float):
        if iata not in self.climates:
            self.climates[iata] = climate_for(iata, lat, lon)

    @classmethod
    def for_world(cls, world, seed: int = 20260729) -> "WeatherModel":
        """Build a model covering every airport in a world's repository."""
        from airlinesim.engine import AirportSpec
        m = cls(seed=seed)
        for spec in world.repo.all(AirportSpec):
            if spec.lat or spec.lon:
                m.add_airport(spec.iata, spec.lat, spec.lon)
        return m

    # -- system generation ---------------------------------------------
    def _slot_systems(self, slot: int) -> tuple:
        """Every system born in one 6-hour slot. Pure function of (seed, slot)."""
        cached = self._slot_cache.get(slot)
        if cached is not None:
            return cached
        born_at = slot * SPAWN_SLOT_H
        day_of_year = (born_at / 24.0) % 365.0
        out = []
        for basin, la_lo, la_hi, lo_lo, lo_hi in BASINS:
            for kind, base_p in BASE_SPAWN.items():
                p = base_p * self._seasonal_gate(kind, basin, day_of_year, la_lo, la_hi, lo_lo)
                if p <= 0:
                    continue
                if _h01(self.seed, "spawn", slot, basin, kind.name) >= p:
                    continue
                prof = KIND_PROFILE[kind]
                sid = f"{kind.name[:3]}{slot}{basin[:2]}"
                lat0 = _h_range(la_lo, la_hi, self.seed, "lat", slot, basin, kind.name)
                lon0 = _h_range(lo_lo, lo_hi, self.seed, "lon", slot, basin, kind.name)
                out.append(WeatherSystem(
                    system_id=sid, kind=kind, born_at=born_at,
                    life_h=prof.life_h * _h_range(0.6, 1.5, self.seed, "life", slot, basin, kind.name),
                    lat0=lat0, lon0=lon0,
                    bearing_deg=prof.bearing_deg
                    + _h_range(-25, 25, self.seed, "brg", slot, basin, kind.name),
                    speed_kmh=prof.speed_kmh
                    * _h_range(0.7, 1.35, self.seed, "spd", slot, basin, kind.name),
                    radius_km=prof.radius_km
                    * _h_range(0.65, 1.4, self.seed, "rad", slot, basin, kind.name),
                    peak_intensity=_h_range(0.35, 1.0, self.seed, "int", slot, basin, kind.name)))
        systems = tuple(out)
        self._slot_cache[slot] = systems
        return systems

    def _seasonal_gate(self, kind, basin, day_of_year, la_lo, la_hi, lo_lo) -> float:
        """
        Season and geography scaling on a spawn probability. This is where the
        climate story lives: blizzards in January in the north, convection in
        July, hurricanes only in the tropics between June and November, fires
        only in the West in late summer.
        """
        mid_lat = 0.5 * (la_lo + la_hi)
        if kind is WeatherKind.HURRICANE:
            if basin != "tropics":
                return 0.0
            # Atlantic season runs Jun 1 - Nov 30, peaking ~10 September.
            return _season(day_of_year, 253.0, 1.0) ** 2.5
        if kind is WeatherKind.VOLCANIC_ASH:
            # Eruptions are aseasonal, and only where there are volcanoes.
            return 1.0 if lo_lo <= -110.0 else 0.0
        if basin == "tropics":
            return 0.0                     # nothing else is born down there
        if kind is WeatherKind.THUNDERSTORM:
            return _season(day_of_year, 200.0, 0.85) * max(0.25, 1.4 - abs(mid_lat - 35.0) / 16.0)
        if kind is WeatherKind.RAIN:
            return 0.7 + 0.3 * _season(day_of_year, 100.0, 0.6)
        if kind is WeatherKind.FOG:
            return _season(day_of_year, 20.0, 0.5)
        if kind is WeatherKind.SNOW:
            return _season(day_of_year, 18.0, 1.0) ** 1.5 * max(0.0, (mid_lat - 32.0) / 14.0)
        if kind is WeatherKind.ICING:
            return _season(day_of_year, 25.0, 1.0) ** 1.5 * max(0.0, 1.0 - abs(mid_lat - 39.0) / 9.0)
        if kind is WeatherKind.BLIZZARD:
            return _season(day_of_year, 15.0, 1.0) ** 2.0 * max(0.0, (mid_lat - 37.0) / 11.0)
        if kind is WeatherKind.WILDFIRE_SMOKE:
            return _season(day_of_year, 225.0, 1.0) ** 2.0 * (1.0 if lo_lo <= -95.0 else 0.0)
        return 1.0

    def active(self, now: float) -> list:
        """
        Every system alive at `now`. Looks back far enough to catch the
        longest-lived kind (a hurricane runs for days) and no further.
        """
        longest = max(p.life_h for p in KIND_PROFILE.values()) * 1.5
        first = int((now - longest) // SPAWN_SLOT_H)
        last = int(now // SPAWN_SLOT_H)
        out = []
        for slot in range(first, last + 1):
            for s in self._slot_systems(slot):
                if s.alive(now):
                    out.append(s)
        # Slots older than the window can never matter again; dropping them
        # keeps a long game from growing an unbounded cache.
        if first - 40 > self._cache_lo:
            self._cache_lo = first - 40
            for k in [k for k in self._slot_cache if k < self._cache_lo]:
                self._slot_cache.pop(k, None)
        return out

    # -- what an airport sees ------------------------------------------
    def at(self, iata: str, now: float, systems: Optional[list] = None) -> AirportWeather:
        """
        The sky over one airport. Combines every system overhead, scaled by
        how susceptible this airport's climate is to that kind — a snowstorm
        over Miami is not a thing that happens, and the climate gate is what
        stops the basin model producing one.
        """
        climate = self.climates.get(iata)
        if climate is None:
            return AirportWeather(iata, WeatherKind.CLEAR, 0.0, 1.0, 0.0, False)
        systems = self.active(now) if systems is None else systems
        day_of_year = (now / 24.0) % 365.0

        worst_kind, worst_eff = WeatherKind.CLEAR, 0.0
        capacity, delay, closed = 1.0, 0.0, False
        closing_pressure = 0.0
        blamed = []
        for s in systems:
            eff = s.effect_at(climate.lat, climate.lon, now)
            if eff <= 0.02:
                continue
            eff *= self._susceptibility(climate, s.kind, day_of_year)
            if eff <= 0.02:
                continue
            prof = KIND_PROFILE[s.kind]
            # Each system multiplies what capacity is left: two systems over
            # one field are worse than either alone, and neither can push
            # capacity below its own floor on its own.
            capacity *= (1.0 - eff * (1.0 - prof.cap_floor))
            delay += prof.delay_h * eff
            blamed.append(s.system_id)
            if prof.closes:
                closing_pressure = max(closing_pressure, eff)
            if eff > worst_eff:
                worst_kind, worst_eff = s.kind, eff

        # A field closes when a CLOSING kind (ice, blizzard, hurricane, ash)
        # has driven what's left of its capacity into the ground. Tying this
        # to the computed capacity rather than to a separate threshold on
        # intensity means the two can't disagree: every closure is the end of
        # a visible slide, and tuning a system's severity tunes its closures
        # with it. An earlier threshold-on-intensity version never fired at
        # all, so no airport in the model ever actually shut.
        if closing_pressure >= 0.35 or (closing_pressure > 0.2 and capacity < 0.35):
            closed = True
            capacity = 0.0
        return AirportWeather(iata=iata, kind=worst_kind, intensity=worst_eff,
                              capacity_factor=max(0.0, min(1.0, capacity)),
                              delay_h=delay, closed=closed, systems=tuple(blamed))

    def _susceptibility(self, climate: Climate, kind: WeatherKind,
                        day_of_year: float) -> float:
        """
        How much this KIND of weather can affect THIS airport. The gate that
        keeps geography honest: a blizzard system passing over Florida does
        nothing, because Florida's winter severity is zero.
        """
        if kind is WeatherKind.THUNDERSTORM:
            return climate.convective
        if kind is WeatherKind.RAIN:
            return 0.55 + 0.45 * climate.fog_prone
        if kind is WeatherKind.FOG:
            return climate.fog_prone
        if kind in (WeatherKind.SNOW, WeatherKind.BLIZZARD):
            return climate.winter_severity * climate.freezing(day_of_year)
        if kind is WeatherKind.ICING:
            # Freezing rain needs the temperature to be near freezing, not
            # merely cold — deep cold gives dry snow instead.
            t = climate.mean_temp_c(day_of_year)
            near_freezing = max(0.0, 1.0 - abs(t - 0.0) / 7.0)
            return climate.icing_belt * near_freezing
        if kind is WeatherKind.HURRICANE:
            return climate.hurricane_exposure
        if kind is WeatherKind.WILDFIRE_SMOKE:
            return climate.wildfire_exposure
        if kind is WeatherKind.VOLCANIC_ASH:
            # Ash drifts far beyond the volcano, so exposure is not purely
            # local — but not so far that everywhere is equally affected.
            return max(climate.ash_exposure, 0.6)
        return 0.0

    # -- enroute -------------------------------------------------------
    def enroute(self, origin_iata: str, dest_iata: str, now: float,
                samples: int = 5, systems: Optional[list] = None) -> float:
        """
        Extra delay (hours) from weather BETWEEN the airports — convection or
        ash on the airway, which is flown around rather than through. Sampled
        along the great circle; endpoints are excluded because those are
        already counted by ``at()``.

        This is why a route can be disrupted with both its airports clear,
        which is the indirect effect the direct model misses.
        """
        a = self.climates.get(origin_iata)
        b = self.climates.get(dest_iata)
        if a is None or b is None:
            return 0.0
        systems = self.active(now) if systems is None else systems
        enroute_kinds = (WeatherKind.THUNDERSTORM, WeatherKind.VOLCANIC_ASH,
                         WeatherKind.HURRICANE)
        worst = 0.0
        for i in range(1, samples):
            f = i / samples
            lat = a.lat + (b.lat - a.lat) * f
            lon = a.lon + (b.lon - a.lon) * f
            for s in systems:
                if s.kind not in enroute_kinds:
                    continue
                eff = s.effect_at(lat, lon, now)
                if eff > worst:
                    worst = eff
        # Deviating around weather costs time, but an airway can always be
        # flown around at some cost — it never cancels a flight by itself.
        return worst * 0.9

    # -- projection for the UI -----------------------------------------
    def snapshot(self, now: float, iatas=None) -> dict:
        systems = self.active(now)
        out = {}
        for iata in (iatas if iatas is not None else self.climates):
            w = self.at(iata, now, systems)
            if w.disrupted:
                out[iata] = {
                    "kind": w.kind.name, "intensity": round(w.intensity, 3),
                    "capacity": round(w.capacity_factor, 3),
                    "delay_h": round(w.delay_h, 2), "closed": w.closed,
                    "text": w.describe(),
                }
        return out

    def system_snapshot(self, now: float) -> list:
        """Live systems with their positions — the map layer."""
        out = []
        for s in self.active(now):
            i = s.intensity(now)
            if i <= 0.05:
                continue
            lat, lon = s.position(now)
            out.append({"id": s.system_id, "kind": s.kind.name,
                        "lat": round(lat, 2), "lon": round(lon, 2),
                        "radius_km": round(s.radius_km),
                        "intensity": round(i, 3),
                        "age_h": round(s.age(now), 1),
                        "life_h": round(s.life_h, 1)})
        return out

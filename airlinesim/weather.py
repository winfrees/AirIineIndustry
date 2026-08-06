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

PROBABILISTIC, AND STILL REPRODUCIBLE
------------------------------------
Weather is a **stochastic process**: each tick, ``WeatherModel.advance()``
retires dead systems and rolls for new ones against season- and
geography-dependent probabilities. A player cannot know next week's storms,
and two playthroughs of the same opening diverge — weather is a risk to be
hedged, not a timetable to be read.

That does NOT cost the explorer its determinism, because the requirement
there is *reproducibility on fork*, not predictability. The draws come from
``WeatherModel.rng`` (a ``random.Random``) and the live systems live on the
model; both pickle with the world. Forking a node copies the generator state,
so re-running a branch replays the identical season and two branches differ
only by the decisions taken — which is exactly what `scenario_explorer`'s
"identical branches produce identical outcomes" check asserts.

The engine itself still contains no ``random`` call. Randomness lives here,
in state the world owns and carries.

Note for anyone tempted to reintroduce a hash-based shortcut here: Python's
``hash()`` on a string is salted per process by PYTHONHASHSEED, so weather
derived from it would differ between the explorer's parent and child runs.
Anything that must be stable across processes belongs in ``self.rng`` or in
``hashlib``, never in ``hash()``.

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

import math
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from airlinesim.route import haversine


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
    NOREASTER = auto()          # coastal low up the Eastern Seaboard: snow + wind
    LAKE_EFFECT = auto()        # narrow band downwind of a Great Lake
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
# The UPWIND coast. Continentality is measured to these, not to the nearest
# water — see climate_for(). Alaska's Pacific shore counts; Hawaii does not,
# being downwind of nothing that reaches the mainland.
_PACIFIC = (
    (47.6, -122.3), (44.6, -124.1), (37.8, -122.5), (32.7, -117.2),
    (61.2, -149.9), (58.3, -134.4),
)

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

# The Great Lakes as SEGMENTS — long axis endpoints plus a half-width — rather
# than as points. This is a second representation of the same water the shore
# anchors above describe, and it exists because the two answer different
# questions: the shore anchors say how far an airport is from moderating
# water, this says where a lake-effect band is BORN and who is downwind of it.
#
# The geometry has to be a segment because these lakes are long and thin.
# Erie is 400 km on its long axis and 90 km across, and Buffalo sits on its
# ENE tip: as a circle centred mid-lake, Buffalo came out 97 km "inland" and
# scored 0.24 on a scale where it should be the highest in the country. The
# same collapse gave Marquette and Traverse City — two of the snowiest fields
# in the US — exactly ZERO, because the nearest anchor was east of them and
# the downwind test reads east as downwind.
#   (name, lat1, lon1, lat2, lon2, half_width_km)
_LAKES = (
    ("Superior", 46.7, -92.1, 46.6, -84.6, 90.0),
    ("Michigan", 45.8, -86.9, 41.8, -87.3, 60.0),
    ("Huron",    46.0, -83.6, 43.1, -82.4, 80.0),
    ("Erie",     41.7, -83.4, 42.9, -78.9, 45.0),
    ("Ontario",  43.3, -79.3, 44.1, -76.4, 40.0),
)

# The prevailing lake-effect flow, and how wide a sector counts as downwind.
# Bands run with the low-level wind, which in a post-frontal cold outbreak is
# somewhere between WNW and WSW — so the snow lands anywhere from ENE round to
# SSE of the water. A narrow ESE-only sector missed the Tug Hill plateau
# east of Ontario, which is the single snowiest lake-effect belt there is.
_LAKE_DOWNWIND_DEG = 110.0
_LAKE_INLAND_KM = 120.0     # e-folding distance for a band's reach inland


def _seg_point_km(lat, lon, lat1, lon1, lat2, lon2):
    """
    Distance in km from a point to a line SEGMENT, plus the closest point on
    it. Works in a local equirectangular frame scaled at the segment's mean
    latitude — good to a fraction of a percent over a few hundred km, which is
    well inside the accuracy of everything else here.
    """
    coslat = math.cos(math.radians(0.5 * (lat1 + lat2)))
    ax, ay = lon1 * coslat, lat1
    bx, by = lon2 * coslat, lat2
    px, py = lon * coslat, lat
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    t = 0.0 if den <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / den))
    cx, cy = ax + t * dx, ay + t * dy
    return haversine(lat, lon, cy, cx / coslat), cy, cx / coslat


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
    # Atlantic seaboard, north of the Carolinas — where a coastal low bites.
    noreaster_exposure: float = 0.0
    # Downwind (east/south-east) of a Great Lake, close enough for a band to
    # reach. Distinct from winter_severity: this is a NARROW, local, very
    # heavy phenomenon, and it is why Buffalo is not Boston.
    lake_effect_exposure: float = 0.0

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
        # The latitude lapse is GENTLER where the ocean is upwind. Measuring
        # continentality to the Pacific fixed the eastern winters but left
        # Seattle 4 C too cold in January, because the amplitude term alone
        # cannot lift a winter — it only narrows the swing about a mean the
        # latitude line had already put too low. Onshore flow raises the mean
        # as well as damping the swing, which is why the Pacific Northwest is
        # mild rather than merely even.
        lapse = 0.62 - 0.18 * (1.0 - self.continentality)
        base = 26.0 - lapse * (self.lat - 20.0)
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
    # ...and against the UPWIND ocean, which over North America is the
    # PACIFIC. Prevailing flow is westerly, so an ocean to the west moderates
    # a winter and an ocean to the east barely does. Boston and Seattle are
    # both coastal at similar latitude: Seattle's February mean is about +5 C,
    # Boston's about -1.5 C, and the whole difference is which side the water
    # is on.
    #
    # Measuring to the NEAREST ocean gave Boston Seattle's answer — +5.1 C in
    # February — which set `freezing()` to zero and gated every cold-weather
    # kind off the entire Northeast coast. Nor'easters spawned, tracked over
    # Boston and did nothing, and the same silence applied to snow, ice and
    # blizzards there. Distance to the Pacific is what the seasonal swing
    # actually keys on; the Atlantic and the Gulf still supply moisture and
    # fog through `coast_km`, which is measured to any water.
    pacific = min(haversine(lat, lon, cl, cn) for cl, cn in _PACIFIC)
    continentality = max(0.0, min(1.0, 1.0 - math.exp(-pacific / 900.0)))

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

    # NOR'EASTER: an Atlantic coastal low, so exposure needs BOTH the Atlantic
    # (not the Gulf, not the Pacific) and the latitude band it actually hits.
    # Longitude east of -82 excludes the Gulf coast; the latitude ramp starts
    # at the Carolinas and saturates over New England, which is the real
    # gradient — Boston takes several a winter, Norfolk one, Jacksonville none.
    if lon > -82.0:
        noreaster = (math.exp(-ocean / 300.0)
                     * max(0.0, min(1.0, (lat - 34.0) / 8.0)))
    else:
        noreaster = 0.0

    # LAKE EFFECT: cold air crossing warm water, so the band falls DOWNWIND —
    # east and south-east of the lake, under the prevailing westerlies. That
    # directionality is the whole phenomenon: Milwaukee sits upwind of Lake
    # Michigan and gets little, Grand Rapids sits downwind of the same lake
    # and gets buried. Measured to the nearest point on each lake's long axis
    # and scored on distance BEYOND the shore, so a field on the water is at
    # full exposure however long the lake is. Best lake wins: an airport
    # downwind of any of them qualifies.
    lake = 0.0
    for _, la1, lo1, la2, lo2, half_w in _LAKES:
        d, cy, cx = _seg_point_km(lat, lon, la1, lo1, la2, lo2)
        inland = max(0.0, d - half_w)
        if inland > 3.0 * _LAKE_INLAND_KM:
            continue
        # Bearing from the water to the airport, against the prevailing band
        # axis. Cosine to the half power keeps the whole ENE-to-SSE arc in
        # play rather than only the exact downwind line.
        brg = math.degrees(math.atan2(
            (lon - cx) * math.cos(math.radians(lat)), lat - cy)) % 360.0
        align = math.cos(math.radians(brg - _LAKE_DOWNWIND_DEG))
        if align <= 0.0:
            continue
        lake = max(lake, math.exp(-inland / _LAKE_INLAND_KM) * math.sqrt(align))
    lake = max(0.0, min(1.0, lake))

    # Ash: proximity to a real volcano, and only near one.
    nearest_v = min(haversine(lat, lon, vlat, vlon) for _, vlat, vlon in VOLCANOES)
    ash = max(0.0, min(1.0, math.exp(-nearest_v / 700.0)))

    return Climate(iata=iata, lat=lat, lon=lon, coast_km=coast,
                   continentality=continentality, convective=convective,
                   winter_severity=winter, icing_belt=max(0.0, min(1.0, icing)),
                   fog_prone=fog, hurricane_exposure=hurricane,
                   wildfire_exposure=wildfire, ash_exposure=ash,
                   noreaster_exposure=max(0.0, min(1.0, noreaster)),
                   lake_effect_exposure=lake)


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
    # Degrees per hour the track bearing rotates CLOCKWISE as the system ages.
    # 0 = a straight line, which is right for everything that simply rides the
    # westerlies. A tropical cyclone does not: it runs west-northwest under
    # the subtropical ridge for days and then RECURVES north and northeast.
    # Modelling that as one constant bearing was a real bug — see the hurricane
    # entry below.
    curve_deg_per_h: float = 0.0


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
    # A NOR'EASTER is a coastal low that deepens off Hatteras and runs up the
    # seaboard, so it tracks NNE along the coast rather than west-to-east like
    # everything else, and it is big, slow and long-lived: a single storm can
    # shut Boston and New York on the same day and still be snowing on Maine
    # the next. Bearing 30 with a slight clockwise curve keeps it following
    # the coastline instead of driving inland.
    # Bearing 20 rather than a textbook north-east one: a nor'easter tracks
    # roughly PARALLEL to the coast, passing just offshore, which is what puts
    # the heavy snow on its western side over the cities. At 30 the track ran
    # out to sea and the storms landed on interior New England while missing
    # Boston, New York and Philadelphia entirely — the airports the storm is
    # operationally about.
    WeatherKind.NOREASTER:      KindProfile(480, 40, 34, 20, 0.15, 3.00, True,
                                            curve_deg_per_h=0.35),
    # LAKE EFFECT is the opposite shape: a NARROW band, nearly stationary,
    # that sits over the same few counties for a day or more. Small radius and
    # a low speed are the whole character — it does not sweep a region, it
    # parks on one. It throttles hard rather than closing: Buffalo is very
    # good at operating in snow it gets every winter.
    WeatherKind.LAKE_EFFECT:    KindProfile(110, 14, 30, 100, 0.35, 1.60, False),
    # HURRICANES RECURVE, and modelling that as one constant bearing meant
    # none of them ever reached the United States. At bearing 45 (north-east)
    # every storm left its genesis point heading away from the continent: born
    # at 15.9N 81.8W it died three days later at 24.7N 69.4W, out in the open
    # Atlantic. Ten a year spawned and zero were ever felt at an airport, so
    # the kind existed in the enum, in the profile table and in the seasonal
    # gate while being, in play, entirely absent.
    #
    # Real Atlantic tracks run WEST-NORTHWEST under the subtropical ridge for
    # days and then recurve north and north-east. So: an initial bearing of
    # 300 (WNW), a clockwise recurve, and a life long enough to cross the
    # basin. Storms born in the west Caribbean now make the Gulf coast; those
    # born far east recurve out to sea and miss, which is also what happens.
    WeatherKind.HURRICANE:      KindProfile(520, 24, 120, 300, 0.05, 6.00, True,
                                            curve_deg_per_h=0.42),
    WeatherKind.WILDFIRE_SMOKE: KindProfile(340, 25, 40, 75, 0.70, 0.55),
    WeatherKind.VOLCANIC_ASH:   KindProfile(450, 60, 30, 80, 0.02, 8.00, True),
}


# Track integration step. Three hours is far finer than any system's life and
# keeps a 120-hour hurricane to forty steps; the midpoint rule in position()
# makes the answer insensitive to it anyway.
_TRACK_STEP_H = 3.0


def _advance(lat: float, lon: float, bearing_deg: float, km: float) -> tuple:
    """Great-circle dead reckoning: `km` along `bearing_deg` from (lat, lon)."""
    d = km / 6371.0
    br = math.radians(bearing_deg)
    la1, lo1 = math.radians(lat), math.radians(lon)
    la2 = math.asin(math.sin(la1) * math.cos(d)
                    + math.cos(la1) * math.sin(d) * math.cos(br))
    lo2 = lo1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(la1),
                           math.cos(d) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


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
    peak_intensity: float       # 0..1 for a natural system; may exceed 1 if forced
    curve_deg_per_h: float = 0.0    # see KindProfile.curve_deg_per_h
    _pos_cache: object = None       # (now, (lat, lon)) memo for curved tracks
    # A STAGED event (WeatherModel.inject) rather than one the process rolled.
    # Forced systems answer to geography but not to the calendar — see
    # WeatherModel._susceptibility.
    forced: bool = False

    def age(self, now: float) -> float:
        return now - self.born_at

    def alive(self, now: float) -> bool:
        return 0.0 <= self.age(now) <= self.life_h

    def position(self, now: float) -> tuple:
        """
        Where the centre is. Great-circle dead reckoning from the birth point;
        a straight line when `curve_deg_per_h` is 0, an integrated curve when
        it is not.

        Still a pure function of age, so the whole field regenerates
        deterministically from the clock — which is what the explorer's
        "identical branches produce identical outcomes" check depends on.
        """
        age = max(0.0, self.age(now))
        if not self.curve_deg_per_h:
            return _advance(self.lat0, self.lon0, self.bearing_deg,
                            self.speed_kmh * age)
        # Cached because `at()` asks every system for its position once per
        # airport, and the answer only depends on `now`.
        if self._pos_cache is not None and self._pos_cache[0] == now:
            return self._pos_cache[1]
        la, lo, t = self.lat0, self.lon0, 0.0
        while t < age - 1e-9:
            step = min(_TRACK_STEP_H, age - t)
            # bearing at the MIDPOINT of the step: a midpoint rule keeps the
            # integrated track independent of the step size to first order
            br = self.bearing_deg + self.curve_deg_per_h * (t + 0.5 * step)
            la, lo = _advance(la, lo, br, self.speed_kmh * step)
            t += step
        self._pos_cache = (now, (la, lo))
        return la, lo

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
    # Nor'easter genesis: the Hatteras coastal-low breeding ground. Overlaps
    # the "south"/"northeast" boxes on purpose — it is a separate GENESIS
    # region, not a separate piece of land, and only NOREASTER spawns in it.
    ("atlantic", 32.0, 38.0, -79.0, -73.0),
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
    # Roughly a dozen coastal storms a winter reach the seaboard, of which a
    # handful matter operationally.
    WeatherKind.NOREASTER: 0.045,
    # Lake-effect bands are FREQUENT and small: Buffalo and Cleveland see them
    # on many winter days. High rate, tiny footprint.
    WeatherKind.LAKE_EFFECT: 0.55,
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
    The live weather field: a STOCHASTIC PROCESS the world carries forward.

    Every tick, ``advance()`` retires the systems that have died and rolls for
    new ones against a probability that depends on the basin, the season and
    the kind. Weather is therefore genuinely uncertain — a player cannot
    predict next week's storms, and two games played from the same opening
    diverge, which is the point of it being a risk rather than a schedule.

    REPRODUCIBILITY, WHICH IS NOT THE SAME AS PREDICTABILITY
    -------------------------------------------------------
    The draws come from ``self.rng``, a ``random.Random`` owned by the model,
    and the live systems are stored on it. Both pickle with the world. So:

      - a SAVE reloads into the same weather future it would have had;
      - a FORK (explorer, GameSession.save/load) carries the generator state
        with it, so re-running a branch replays the identical season and two
        branches differ only by the decisions taken — which is exactly what
        `scenario_explorer`'s determinism check asserts;
      - a NEW GAME with no seed gets a fresh one, so the next playthrough is a
        different season.

    An earlier version made weather a pure function of ``(seed, clock)``, with
    nothing stored. That is reproducible too, but it is not probabilistic: the
    entire future was fixed before the game began, and asking about hour 5,000
    was a lookup rather than a simulation. This is a process instead, and the
    RNG state is what makes it replayable.

    """

    def __init__(self, seed: Optional[int] = None, climates: Optional[dict] = None,
                 enabled: bool = True):
        self.seed = int(seed) if seed is not None else random.randrange(1 << 62)
        self.climates: dict = dict(climates or {})
        self.rng = random.Random(self.seed)
        self.systems: list = []          # live systems, carried forward
        self.enabled = bool(enabled)
        self._seq = 0
        self._spawned_through = 0.0      # sim time the process has been rolled to

    # -- setup ---------------------------------------------------------
    def add_airport(self, iata: str, lat: float, lon: float):
        if iata not in self.climates:
            self.climates[iata] = climate_for(iata, lat, lon)

    @classmethod
    def for_world(cls, world, seed: Optional[int] = None,
                  enabled: bool = True) -> "WeatherModel":
        """Build a model covering every airport in a world's repository."""
        from airlinesim.engine import AirportSpec
        m = cls(seed=seed, enabled=enabled)
        for spec in world.repo.all(AirportSpec):
            if spec.lat or spec.lon:
                m.add_airport(spec.iata, spec.lat, spec.lon)
        return m

    # -- the process ---------------------------------------------------
    def advance(self, now: float, dt: float):
        """
        Carry the weather forward by one tick: retire what has died, roll for
        what is born. Called by WeatherSubsystem before anything reads the sky.

        Spawn probability is scaled by ``dt / SPAWN_SLOT_H`` so the process is
        RESOLUTION-INDEPENDENT: an hourly game and a six-hourly one see the
        same amount of weather per simulated week. Getting this wrong is the
        same class of bug as the gate claim — a per-slot budget spent per tick.
        """
        if not self.enabled:
            return
        # Retire the dead. Systems only ever age, so this is the whole GC.
        if self.systems:
            self.systems = [s for s in self.systems if s.alive(now)]

        # Roll the interval that has actually elapsed. Guarding on
        # _spawned_through means a caller that ticks the model twice for one
        # hour cannot double the weather.
        if now < self._spawned_through:
            return
        span = min(max(dt, 0.0), max(0.0, now - self._spawned_through) + dt)
        self._spawned_through = now + dt
        if span <= 0:
            return

        day_of_year = (now / 24.0) % 365.0
        scale = span / SPAWN_SLOT_H
        # A tick longer than one spawn slot gets MULTIPLE draws, not one draw
        # at a scaled-up probability. Folding the scale into a single Bernoulli
        # saturates: at a 24-hour tick the common kinds ran p > 1 and spawned
        # exactly one system where four were due, so coarse resolution quietly
        # produced less weather than fine resolution.
        whole, frac = divmod(scale, 1.0)
        weights = [1.0] * int(whole) + ([frac] if frac > 1e-9 else [])
        for basin, la_lo, la_hi, lo_lo, lo_hi in BASINS:
            for kind, base_p in BASE_SPAWN.items():
                p_slot = base_p * self._seasonal_gate(kind, basin, day_of_year,
                                                      la_lo, la_hi, lo_lo)
                if p_slot <= 0.0:
                    continue
                for w in weights:
                    if self.rng.random() >= p_slot * w:
                        continue
                    # Born somewhere inside the tick rather than all at its
                    # start, so systems aren't synchronised to the clock.
                    born = now + self.rng.random() * span
                    self.systems.append(self._spawn(kind, born, la_lo, la_hi,
                                                    lo_lo, lo_hi, basin))

    def _spawn(self, kind: WeatherKind, born_at: float, la_lo, la_hi,
               lo_lo, lo_hi, basin: str) -> WeatherSystem:
        prof = KIND_PROFILE[kind]
        r = self.rng
        self._seq += 1
        lat0, lon0 = r.uniform(la_lo, la_hi), r.uniform(lo_lo, lo_hi)
        if kind is WeatherKind.LAKE_EFFECT:
            # Born over open water, not anywhere in the basin. The basin still
            # decides WHICH lakes are in play — the Midwest box covers
            # Superior/Michigan/Huron, the Northeast box Erie/Ontario — so the
            # per-basin spawn rate keeps its meaning.
            lakes = [k for k in _LAKES
                     if lo_lo <= 0.5 * (k[2] + k[4]) <= lo_hi]
            if lakes:
                _, la1, lo1, la2, lo2, _ = r.choice(lakes)
                t = r.random()          # anywhere along the lake's long axis
                lat0 = la1 + t * (la2 - la1)
                lon0 = lo1 + t * (lo2 - lo1)
        return WeatherSystem(
            system_id=f"{kind.name[:3]}{self._seq}{basin[:2]}", kind=kind,
            born_at=born_at, life_h=prof.life_h * r.uniform(0.6, 1.5),
            lat0=lat0, lon0=lon0,
            bearing_deg=prof.bearing_deg + r.uniform(-25, 25),
            speed_kmh=prof.speed_kmh * r.uniform(0.7, 1.35),
            radius_km=prof.radius_km * r.uniform(0.65, 1.4),
            peak_intensity=r.uniform(0.35, 1.0),
            # Recurve rate varies too, which is what decides whether a given
            # storm turns early and misses or runs west far enough to land.
            curve_deg_per_h=prof.curve_deg_per_h * r.uniform(0.6, 1.5))

    def inject(self, kind, iata: str, now: float, intensity: float = 0.9,
               life_h: Optional[float] = None) -> Optional[WeatherSystem]:
        """
        Put a named event over a named airport, right now.

        This is the explorer's "what if a blizzard hits my hub in week three?"
        — a deliberate, reproducible event rather than one waited for. The
        system is centred on the airport and stationary-ish, so it is the
        chosen airport that takes it.
        """
        if isinstance(kind, str):
            try:
                kind = WeatherKind[kind.strip().upper()]
            except KeyError:
                return None
        climate = self.climates.get(iata)
        if climate is None or kind is WeatherKind.CLEAR:
            return None
        # `intensity` is what should be DELIVERED at this airport, so the
        # system is sized to overcome the local susceptibility gate. Without
        # this, staging a blizzard in April looked like a broken knob: the
        # system existed, the seasonal gate multiplied it to nothing, and the
        # branch came back byte-identical to its sibling.
        sus = self._geo_susceptibility(climate, kind)
        if sus < 0.02:
            # Geographically impossible, not merely unlikely — a hurricane at
            # Chicago. Refused rather than staged as a no-op the caller would
            # have to diagnose from an unchanged number.
            return None
        prof = KIND_PROFILE[kind]
        self._seq += 1
        s = WeatherSystem(
            system_id=f"INJ{self._seq}{kind.name[:3]}", kind=kind, born_at=now,
            life_h=float(life_h if life_h is not None else prof.life_h),
            lat0=climate.lat, lon0=climate.lon,
            bearing_deg=prof.bearing_deg,
            # Slow enough that the target airport is the one that wears it,
            # rather than an event that has moved on before it bites.
            speed_kmh=prof.speed_kmh * 0.25,
            radius_km=prof.radius_km,
            # May exceed 1.0: it is a pre-susceptibility figure, and `at()`
            # clamps the delivered effect.
            peak_intensity=max(0.05, float(intensity)) / sus, forced=True)
        self.systems.append(s)
        return s

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
        if kind is WeatherKind.NOREASTER:
            # Genesis is off the Carolina/Virginia coast, and only there.
            # Season peaks in early February and is over by April.
            if basin != "atlantic":
                return 0.0
            return _season(day_of_year, 35.0, 1.0) ** 1.8
        if kind is WeatherKind.VOLCANIC_ASH:
            # Eruptions are aseasonal, and only where there are volcanoes.
            return 1.0 if lo_lo <= -110.0 else 0.0
        if basin in ("tropics", "atlantic"):
            # Genesis-only boxes: the tropics make hurricanes, the Hatteras
            # box makes nor'easters, and neither makes anything else.
            return 0.0
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
        if kind is WeatherKind.LAKE_EFFECT:
            # Needs cold air over OPEN water, which is a narrower window than
            # winter: the lakes are still warm in late autumn (peak season)
            # and increasingly ice-covered by late winter. Peaks around
            # mid-December, and only in the basins that contain the lakes.
            if basin not in ("midwest", "northeast"):
                return 0.0
            return _season(day_of_year, 349.0, 1.0) ** 2.0
        if kind is WeatherKind.BLIZZARD:
            return _season(day_of_year, 15.0, 1.0) ** 2.0 * max(0.0, (mid_lat - 37.0) / 11.0)
        if kind is WeatherKind.WILDFIRE_SMOKE:
            return _season(day_of_year, 225.0, 1.0) ** 2.0 * (1.0 if lo_lo <= -95.0 else 0.0)
        return 1.0

    def active(self, now: float) -> list:
        """
        Every system alive at `now`. A read, not a generator: the process is
        advanced by ``advance()``, so asking about the sky never changes it.
        Disabled weather reports a clear sky rather than an empty model, which
        is what lets the explorer switch it off mid-branch.
        """
        if not self.enabled:
            return []
        return [s for s in self.systems if s.alive(now)]

    # -- what an airport sees ------------------------------------------
    def at(self, iata: str, now: float, systems: Optional[list] = None) -> AirportWeather:
        """
        The sky over one airport. Combines every system overhead, scaled by
        how susceptible this airport's climate is to that kind — a snowstorm
        over Miami is not a thing that happens, and the climate gate is what
        stops the basin model producing one.
        """
        climate = self.climates.get(iata)
        if climate is None or not self.enabled:
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
            # Clamped: an INJECTED system carries a pre-susceptibility peak
            # that can exceed 1.0 so the requested intensity survives the gate,
            # and nothing downstream should ever see an effect above full.
            eff = min(1.0, eff * self._susceptibility(climate, s.kind, day_of_year,
                                                      s.forced))
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

    def over(self, iata: str, now: float, dt: float,
             samples: Optional[int] = None) -> AirportWeather:
        """
        The sky over an airport ACROSS a whole tick, not at its first instant.

        A thunderstorm lives about six hours. Sampled once per 24-hour tick it
        is usually invisible — born and dead between two looks — so a coarse
        run saw almost no weather while a fine one saw plenty, and the
        explorer's weather knob looked inert. Averaging capacity and delay over
        the tick makes a six-hour storm inside a 24-hour day cost about a
        quarter of that day's capacity, which is both the honest answer and
        the one that doesn't depend on resolution.

        `closed` is sticky: if the field was shut for any part of the window it
        counts as a closure, because the flights in that window didn't operate.
        """
        n = samples if samples is not None else max(1, min(6, int(round(dt))))
        if n <= 1:
            return self.at(iata, now)
        # Systems alive anywhere in the window, gathered once. Each sample then
        # evaluates them at its own instant, so movement is still honoured.
        window = [s for s in self.systems
                  if s.born_at <= now + dt and s.born_at + s.life_h >= now] \
            if self.enabled else []
        cap = delay = 0.0
        worst_kind, worst_i, closed = WeatherKind.CLEAR, 0.0, False
        blamed: set = set()
        for i in range(n):
            t = now + dt * (i + 0.5) / n
            w = self.at(iata, t, window)
            cap += w.capacity_factor
            delay += w.delay_h
            closed = closed or w.closed
            if w.intensity > worst_i:
                worst_kind, worst_i = w.kind, w.intensity
            blamed.update(w.systems)
        return AirportWeather(iata=iata, kind=worst_kind, intensity=worst_i,
                              capacity_factor=cap / n, delay_h=delay / n,
                              closed=closed, systems=tuple(sorted(blamed)))

    def _geo_susceptibility(self, climate: Climate, kind: WeatherKind) -> float:
        """
        Whether this airport's GEOGRAPHY can produce this kind at all, with no
        seasonal term. Chicago scores high for blizzards in July — it is the
        right place, in the wrong month — while Miami scores zero in any month.

        Staged events use this, so "blizzard at my hub in week three" works
        whatever week three happens to be, while "hurricane at Chicago" is
        still refused as the impossibility it is.
        """
        if kind is WeatherKind.THUNDERSTORM:
            return climate.convective
        if kind is WeatherKind.RAIN:
            return 0.55 + 0.45 * climate.fog_prone
        if kind is WeatherKind.FOG:
            return climate.fog_prone
        if kind in (WeatherKind.SNOW, WeatherKind.BLIZZARD):
            return climate.winter_severity
        if kind is WeatherKind.ICING:
            return climate.icing_belt
        if kind is WeatherKind.NOREASTER:
            # No freezing term here, for the same reason SNOW has none: a
            # staged nor'easter in July is a legitimate what-if at a field
            # that gets them, and the exposure alone already refuses Chicago
            # and Miami on geography.
            return climate.noreaster_exposure
        if kind is WeatherKind.LAKE_EFFECT:
            return climate.lake_effect_exposure
        if kind is WeatherKind.HURRICANE:
            return climate.hurricane_exposure
        if kind is WeatherKind.WILDFIRE_SMOKE:
            return climate.wildfire_exposure
        if kind is WeatherKind.VOLCANIC_ASH:
            return max(climate.ash_exposure, 0.6)
        return 0.0

    def _susceptibility(self, climate: Climate, kind: WeatherKind,
                        day_of_year: float, forced: bool = False) -> float:
        """
        How much this KIND of weather can affect THIS airport. The gate that
        keeps geography honest: a blizzard system passing over Florida does
        nothing, because Florida's winter severity is zero.

        A FORCED system skips the seasonal half — it was staged deliberately,
        and refusing to simulate it because of the month would make the
        explorer's event picker useless for nine months of the year.
        """
        if forced:
            return self._geo_susceptibility(climate, kind)
        if kind is WeatherKind.THUNDERSTORM:
            return climate.convective
        if kind is WeatherKind.RAIN:
            return 0.55 + 0.45 * climate.fog_prone
        if kind is WeatherKind.FOG:
            return climate.fog_prone
        if kind in (WeatherKind.SNOW, WeatherKind.BLIZZARD):
            return climate.winter_severity * climate.freezing(day_of_year)
        if kind is WeatherKind.NOREASTER:
            # Coastal AND cold: the same storm that buries Boston in February
            # falls as rain on Norfolk in November.
            return climate.noreaster_exposure * climate.freezing(day_of_year)
        if kind is WeatherKind.LAKE_EFFECT:
            return climate.lake_effect_exposure * climate.freezing(day_of_year)
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

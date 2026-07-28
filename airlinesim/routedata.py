"""
ROUTE DATA PROVIDER — historic when available, comparable otherwise.
==================================================================

The runtime half of the historic-route-data work. Answers "what does the real
world say about ORD->DEN?" in three tiers of descending confidence:

  Tier 1  EXACT       the directional pair is in the corpus. Real passenger
                      volumes, real distance, real 12-month seasonal shape,
                      and real capacity where a T-100 Segment export exists.

  Tier 2  COMPARABLE  the pair is absent, but both airports are known. Demand
                      is estimated from a gravity model fitted on the Tier-1
                      pairs, using each airport's measured outbound/inbound
                      traffic as its "size" — which is exactly the
                      comparable-route-by-endpoint-size idea, calibrated on
                      measured marginals instead of invented.

  Tier 3  SYNTHETIC   an airport is unknown. Falls back to route.py's
                      default_segments() behaviour, unchanged.

Every RouteSpec produced carries its tier and the corpus vintage, so a Tier-2
estimate can never be mistaken for a measurement downstream.

IMPORT RULE: this module must never import airlinesim.btsdata. Runtime reads
distilled artifacts; only the dev-time ingest touches BTS and the warehouse.
Both provider construction paths land here — from_dir() reads a committed
snapshot, and btsdata builds one straight from the warehouse via from_tables(),
so there is exactly ONE aggregation code path and one set of interpretive rules.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import csv
import gzip
import io
import json
import math
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Kept in step with btsdata.distill.TARGET_LOAD_FACTOR; used only for Tier-2
# estimates, where there is no observed load factor to divide by.
TARGET_LOAD_FACTOR = 0.85

GRAVITY_TERMS = ("intercept", "log_out_pax_origin", "log_in_pax_dest",
                 "log_distance_km", "log_distance_km_sq", "hub30_flag",
                 "hub10_flag")


# Economic seat window, shared with the distiller for the same train/serve
# reason as gravity_features.
#
# The band is DAILY FREQUENCIES a route might plausibly be flown at: an aircraft
# is "too small" if serving the demand would need more than FREQ_MAX departures a
# day, and "too big" if it couldn't fill up even at FREQ_MIN. FREQ_MIN below 1.0
# lets a thin market be served less than daily.
#
# The first version used (1, 6) and produced a 552-600 seat window for a 2,817
# pax/day trunk route — i.e. it rejected every real narrowbody on exactly the
# routes narrowbodies fly. Trunk routes run 15-25 departures a day.
#
# HEURISTIC. With a T-100 Segment export this is replaced by the measured
# seats-per-departure p10/p90 for the route.
FREQ_MIN, FREQ_MAX = 0.7, 20.0
SEAT_CLAMP = (30, 600)


def seat_window(demand_per_day: float) -> tuple:
    if demand_per_day <= 0:
        return SEAT_CLAMP
    lo = demand_per_day / (FREQ_MAX * TARGET_LOAD_FACTOR)
    hi = demand_per_day / (FREQ_MIN * TARGET_LOAD_FACTOR)
    lo = max(SEAT_CLAMP[0], min(SEAT_CLAMP[1], int(lo)))
    hi = max(lo + 10, min(SEAT_CLAMP[1], int(hi)))
    return lo, hi


def gravity_features(out_pax_origin: float, in_pax_dest: float,
                     distance_km: float, origin_rank: int, dest_rank: int):
    """
    The Tier-2 feature vector, defined ONCE and imported by the distiller that
    fits it as well as the provider that evaluates it. Train/serve skew in a
    hand-copied feature list is a silent, hard-to-spot class of bug; sharing the
    function makes it impossible.

    NO SIZE-INTERACTION TERM, deliberately. Adding log(out)·log(in) raised
    held-out within-2x from 60.6% to 64.0%, but it flipped the sign of the
    origin-size elasticity for destinations below ~1,100 inbound pax/day — which
    is 51% of the corpus airports, not a tail. A demand model that says a route
    gets THINNER as its origin airport grows is qualitatively wrong, and in a
    simulation people reason counterfactually about exactly that. 3.4 points of
    accuracy is the right price for monotonicity.

    Returns None when the inputs can't support an estimate.
    """
    if min(out_pax_origin, in_pax_dest, distance_km) <= 0:
        return None
    lo = math.log(out_pax_origin)
    li = math.log(in_pax_dest)
    ld = math.log(distance_km)
    return [1.0, lo, li, ld, ld * ld,
            1.0 if (origin_rank <= 30 or dest_rank <= 30) else 0.0,
            1.0 if (origin_rank <= 10 or dest_rank <= 10) else 0.0]


class DataTier(Enum):
    EXACT = "exact"
    COMPARABLE = "comparable"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class RouteObservation:
    """What the corpus says about one directional pair, whatever the tier."""
    origin: str
    dest: str
    distance_km: float
    demand_per_day: float
    tier: DataTier
    vintage: str = ""
    demand_basis: str = ""          # 'decensored' | 'censored' | 'estimated'
    pax_per_day: float = 0.0
    seats_per_day: float = 0.0
    load_factor: float = 0.0
    season_amp: float = 0.2
    season_peak_day: int = 200
    monthly: tuple = ()
    min_viable_seats: int = 0
    max_viable_seats: int = 0
    min_runway_m: float = 0.0
    # Fares from DB1B nonstop markets; 0.0 means no fare data for this pair.
    mean_fare: float = 0.0
    fare_p25: float = 0.0
    fare_median: float = 0.0
    fare_p75: float = 0.0
    # Measured share of this SEGMENT's passengers connecting onward.
    # -1.0 means unknown, which is NOT the same as zero connecting traffic.
    connecting_share: float = -1.0

    @property
    def has_capacity(self) -> bool:
        return self.seats_per_day > 0

    @property
    def has_fare(self) -> bool:
        return self.mean_fare > 0

    @property
    def has_connecting(self) -> bool:
        return self.connecting_share >= 0.0


@dataclass(frozen=True)
class AirportRecord:
    iata: str
    name: str = ""
    runway_m: float = 0.0
    out_pax_per_day: float = 0.0
    in_pax_per_day: float = 0.0
    hub_rank: int = 9999
    est_gates: int = 0
    est_fuel_l_per_day: int = 0
    lat: float = 0.0
    lon: float = 0.0


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class RouteDataProvider:
    """
    Serves the three-tier lookup from in-memory tables. Construct with
    from_dir() for a committed snapshot, or from_tables() for anything that can
    produce the same rows (the dev warehouse does exactly that).
    """

    def __init__(self, routes: dict, airports: dict, gravity: dict, manifest: dict):
        self._routes = routes            # (origin, dest) -> dict
        self._airports = airports        # iata -> AirportRecord
        self._gravity = gravity or {}
        self.manifest = manifest or {}

    # ---- construction ------------------------------------------------

    @classmethod
    def from_tables(cls, route_rows, airport_rows, gravity, manifest):
        routes = {(r["origin"], r["dest"]): r for r in route_rows}
        airports = {}
        for a in airport_rows:
            airports[a["iata"]] = AirportRecord(
                iata=a["iata"], name=a.get("name", ""),
                runway_m=_num(a.get("runway_m")),
                out_pax_per_day=_num(a.get("out_pax_per_day")),
                in_pax_per_day=_num(a.get("in_pax_per_day")),
                hub_rank=int(_num(a.get("hub_rank"), 9999)),
                est_gates=int(_num(a.get("est_gates"))),
                est_fuel_l_per_day=int(_num(a.get("est_fuel_l_per_day"))),
                lat=_num(a.get("lat")), lon=_num(a.get("lon")))
        return cls(routes, airports, gravity, manifest)

    @classmethod
    def from_dir(cls, directory: str = DATA_DIR):
        """Load a committed snapshot. Returns None when no snapshot is present."""
        rp = os.path.join(directory, "routes.csv.gz")
        ap = os.path.join(directory, "airports.csv.gz")
        if not (os.path.exists(rp) and os.path.exists(ap)):
            return None

        def read(path):
            with gzip.open(path, "rt", newline="") as fh:
                return list(csv.DictReader(fh))

        gravity, manifest = {}, {}
        gj = os.path.join(directory, "gravity.json")
        mj = os.path.join(directory, "MANIFEST.json")
        if os.path.exists(gj):
            with open(gj) as fh:
                gravity = json.load(fh)
        if os.path.exists(mj):
            with open(mj) as fh:
                manifest = json.load(fh)
        return cls.from_tables(read(rp), read(ap), gravity, manifest)

    # ---- introspection ----------------------------------------------

    @property
    def vintage(self) -> str:
        yrs = self.manifest.get("years") or []
        table = self.manifest.get("volume_table", "")
        if yrs:
            return f"{table} {yrs[0]}-{yrs[-1]}"
        return table or "unknown"

    def __len__(self):
        return len(self._routes)

    @property
    def airports(self) -> list:
        return sorted(self._airports)

    def airport(self, iata: str):
        return self._airports.get(iata)

    # ---- the three tiers --------------------------------------------

    def observation(self, origin: str, dest: str) -> RouteObservation:
        row = self._routes.get((origin, dest))
        if row is not None:
            return self._exact(row)
        est = self._comparable(origin, dest)
        if est is not None:
            return est
        return RouteObservation(origin, dest, 0.0, 0.0, DataTier.SYNTHETIC,
                                vintage=self.vintage, demand_basis="none")

    def _exact(self, row: dict) -> RouteObservation:
        monthly = tuple(_num(row.get(f"m{i}"), 1.0) for i in range(1, 13))
        return RouteObservation(
            origin=row["origin"], dest=row["dest"],
            distance_km=_num(row.get("distance_km")),
            demand_per_day=_num(row.get("demand_per_day")),
            tier=DataTier.EXACT, vintage=self.vintage,
            demand_basis=row.get("demand_basis", ""),
            pax_per_day=_num(row.get("pax_per_day")),
            seats_per_day=_num(row.get("seats_per_day")),
            load_factor=_num(row.get("load_factor")),
            season_amp=_num(row.get("season_amp"), 0.2),
            season_peak_day=int(_num(row.get("season_peak_day"), 200)),
            monthly=monthly,
            min_viable_seats=int(_num(row.get("min_viable_seats"))),
            max_viable_seats=int(_num(row.get("max_viable_seats"))),
            min_runway_m=_num(row.get("min_runway_m")),
            mean_fare=_num(row.get("mean_fare")),
            fare_p25=_num(row.get("fare_p25")),
            fare_median=_num(row.get("fare_median")),
            fare_p75=_num(row.get("fare_p75")),
            connecting_share=_num(row.get("connecting_share"), -1.0))

    def great_circle_km(self, a: AirportRecord, b: AirportRecord) -> float:
        from airlinesim.route import haversine
        return haversine(a.lat, a.lon, b.lat, b.lon)

    def _comparable(self, origin: str, dest: str):
        """
        Gravity estimate for a pair not in the corpus. Needs both airports known
        and a fitted model; otherwise the caller falls through to SYNTHETIC.
        """
        o, d = self._airports.get(origin), self._airports.get(dest)
        coef = self._gravity.get("coefficients") or []
        if not o or not d or len(coef) < 5:
            return None
        if min(o.out_pax_per_day, d.in_pax_per_day) <= 0:
            return None
        if not (o.lat and o.lon and d.lat and d.lon):
            return None

        dist = self.great_circle_km(o, d)
        feats = gravity_features(o.out_pax_per_day, d.in_pax_per_day, dist,
                                 o.hub_rank, d.hub_rank)
        if feats is None or len(feats) != len(coef):
            return None
        # exp() of a log-space OLS fit estimates the geometric mean, which sits
        # below the conditional median for right-skewed demand. `calibration` is
        # fitted so the median predicted/actual ratio is 1.0 — cross-validated at
        # median 1.01, so the estimate is unbiased in the median rather than
        # quietly low. See gravity.json's cross_validation block.
        calib = self._gravity.get("calibration", 1.0)
        demand = math.exp(sum(c * f for c, f in zip(coef, feats))) * calib
        if not math.isfinite(demand) or demand <= 0:
            return None

        # Seasonal shape: average the corpus routes that touch either endpoint —
        # a market's seasonality is a property of where it goes, and a
        # Florida-facing airport's routes look alike. Falls back to the
        # corpus-wide mean when neither endpoint has measured routes.
        amp, peak = self._neighbour_season(origin, dest)
        smin, smax = self._seat_window(demand)
        return RouteObservation(
            origin=origin, dest=dest, distance_km=round(dist, 1),
            demand_per_day=round(demand, 2), tier=DataTier.COMPARABLE,
            vintage=self.vintage, demand_basis="estimated",
            season_amp=amp, season_peak_day=peak,
            min_viable_seats=smin, max_viable_seats=smax,
            min_runway_m=self._runway_need(dist))

    def _neighbour_season(self, origin: str, dest: str) -> tuple:
        amps, peaks, n = 0.0, [], 0
        for (o, d), row in self._routes.items():
            if o in (origin, dest) or d in (origin, dest):
                amps += _num(row.get("season_amp"), 0.0)
                peaks.append(int(_num(row.get("season_peak_day"), 200)))
                n += 1
        if not n:
            return 0.2, 200
        # Circular mean for the peak day; a plain average of day-of-year would
        # put a January-peaking market in July.
        sx = sum(math.cos(2 * math.pi * p / 365) for p in peaks)
        sy = sum(math.sin(2 * math.pi * p / 365) for p in peaks)
        peak = int(round((math.degrees(math.atan2(sy, sx)) / 360.0 * 365) % 365))
        return round(amps / n, 4), peak

    @staticmethod
    def _seat_window(demand: float) -> tuple:
        return seat_window(demand)

    @staticmethod
    def _runway_need(distance_km: float) -> float:
        for limit, need in ((800, 1500.0), (2500, 2000.0), (5000, 2600.0),
                            (1e9, 3200.0)):
            if distance_km <= limit:
                return need
        return 3200.0

    # ---- spec construction (the engine seam) ------------------------

    def airport_spec(self, iata: str):
        """Build an AirportSpec from the corpus, or None if the airport is unknown."""
        from airlinesim.engine import AirportSpec, PlaneClass
        rec = self._airports.get(iata)
        if rec is None:
            return None
        # Fee schedules are HEURISTIC, but scaled off the one thing the corpus
        # does measure — how much traffic the airport actually handles. Busy
        # airports charge more, which is what makes a secondary field (MDW,
        # OAK, BUR) a genuine cost alternative to its primary (ORD, SFO, LAX)
        # rather than a flavourless duplicate. The per-tier spreads are game
        # balance: tier 1 is a remote stand and a bus, tier 3 is a good gate
        # with a lounge and careful bag handling.
        size = max(0.0, rec.out_pax_per_day)
        gate_base = max(250.0, min(4200.0, size / 28.0))
        return AirportSpec(
            spec_id=iata, display_name=rec.name or iata, iata=iata,
            runway_length_m=rec.runway_m,
            total_gates=rec.est_gates,                 # HEURISTIC
            has_maintenance_facility=rec.hub_rank <= 40,
            facility_max_class=PlaneClass.WIDEBODY if rec.hub_rank <= 40
            else PlaneClass.NARROWBODY,
            fuel_supply_per_day_l=rec.est_fuel_l_per_day,   # HEURISTIC
            landing_fee=max(150.0, min(3000.0, rec.out_pax_per_day / 40.0)),
            lat=rec.lat, lon=rec.lon, hub_rank=rec.hub_rank,
            # index 0 unused; index = RouteOp.service_tier (1..3)
            gate_fee_by_tier=(0.0, gate_base * 0.55, gate_base, gate_base * 1.7),
            amenities_fee_by_tier=(0.0, 0.0, 2.5, 9.0),     # HEURISTIC, per pax
            baggage_fee_by_tier=(0.0, 1.5, 3.0, 5.5),       # HEURISTIC, per pax
            hub_fee_per_day=max(4_000.0, min(90_000.0, size / 1.6)))

    def route_spec(self, origin: str, dest: str, *, business_frac=0.25,
                   leisure_frac=0.55, plane_class=None, spec_id=None):
        """
        Build a RouteSpec for the engine. Tier 1/2 use measured or estimated
        demand and the fitted seasonal shape; Tier 3 reproduces today's defaults.

        The traveler-segment mix uses the MEASURED connecting share when DB1B
        coupons are loaded, splitting the remaining non-connecting demand between
        business and leisure in the caller's ratio. Without it, the caller's
        defaults stand.

        Note what is NOT set from data even then: dow_profile and the segment
        elasticities stay route.py's constants, because T-100 is monthly and
        neither source carries trip purpose. Business-vs-leisure remains a
        split of what's left over, not a measurement.
        """
        from airlinesim.engine import RouteSpec, PlaneClass
        from airlinesim.route import (default_segments, EquipmentRequirements,
                                      CrewRequirements)

        obs = self.observation(origin, dest)
        rid = spec_id or f"{origin}-{dest}"

        if obs.tier is DataTier.SYNTHETIC:
            # No corpus knowledge: hand back the engine's own default shape.
            return RouteSpec(
                spec_id=rid, display_name=f"{origin}->{dest}",
                origin_iata=origin, dest_iata=dest,
                distance_km=obs.distance_km or 1500.0,
                base_demand_per_day=0, segments=(),
                equipment_req=None, crew_req=CrewRequirements(),
                data_tier=obs.tier.value, data_vintage=obs.vintage)

        demand = obs.demand_per_day
        b_frac, l_frac = business_frac, leisure_frac
        if obs.has_connecting:
            # Measured connecting share replaces the global default; the rest is
            # split between business and leisure in the caller's requested ratio.
            conn = min(0.9, obs.connecting_share)
            rest = 1.0 - conn
            denom = business_frac + leisure_frac
            if denom > 0:
                b_frac = rest * business_frac / denom
                l_frac = rest * leisure_frac / denom
        segs = default_segments(demand, b_frac, l_frac)
        # Replace the default seasonal shape with the fitted one, per segment.
        segs = tuple(
            type(s)(s.segment, s.base_per_day, s.elasticity,
                    obs.season_amp, obs.season_peak_day, s.dow_profile)
            for s in segs)

        eq = EquipmentRequirements(
            min_range_km=obs.distance_km * 1.15,      # reserve/diversion margin
            min_runway_m=obs.min_runway_m,            # HEURISTIC by stage length
            optimal_class=plane_class or (
                PlaneClass.NARROWBODY if obs.distance_km < 4000
                else PlaneClass.WIDEBODY),
            min_viable_seats=obs.min_viable_seats,
            max_viable_seats=obs.max_viable_seats)

        return RouteSpec(
            spec_id=rid, display_name=f"{origin}->{dest}",
            origin_iata=origin, dest_iata=dest,
            distance_km=obs.distance_km,
            base_demand_per_day=int(round(demand)),
            seasonality_amplitude=obs.season_amp,
            segments=segs, equipment_req=eq, crew_req=CrewRequirements(),
            data_tier=obs.tier.value, data_vintage=obs.vintage)

    # ---- reporting ---------------------------------------------------

    def suggested_price(self, origin: str, dest: str, default: float = 200.0,
                        premium: bool = False) -> tuple:
        """
        Starting ECONOMY fare for a route op, and where it came from.

        Returns (price, source). The measured nonstop-market median is used when
        available — the median rather than the mean because DB1B's fare
        distribution has a long premium tail that drags the mean above what a
        typical economy passenger paid. A premium carrier is nudged toward p75.

        Falls back to `default` (the engine's reference price) so a corpus with
        no fares behaves exactly as before rather than silently pricing at zero.
        """
        obs = self.observation(origin, dest)
        if not obs.has_fare:
            return default, "engine default (no DB1B fare for this pair)"
        price = obs.fare_p75 if premium and obs.fare_p75 > 0 else obs.fare_median
        return round(price, 2), f"db1b nonstop {'p75' if premium else 'median'}"

    def tier_of(self, origin: str, dest: str) -> DataTier:
        return self.observation(origin, dest).tier

    def summary(self) -> str:
        gaps = self.manifest.get("known_gaps") or []
        out = [f"corpus: {len(self._routes):,} routes, {len(self._airports):,} "
               f"airports, vintage {self.vintage}",
               f"demand basis: {', '.join(self.manifest.get('demand_basis', []))}",
               f"gravity: R²={self._gravity.get('r_squared')} "
               f"on n={self._gravity.get('n')}"]
        out += [f"GAP: {g}" for g in gaps]
        return "\n".join(out)


def load_provider(directory: str = DATA_DIR):
    """Convenience: the committed snapshot, or None when none is present."""
    return RouteDataProvider.from_dir(directory)

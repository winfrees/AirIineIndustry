"""
ROUTE ENTITY — brought to the Aircraft standard.
================================================

Turns Route from a single flat demand number into a real market with structure,
and ties in EQUIPMENT and CREW requirements so a route validates what's assigned
to fly it.

THREE LAYERS:

1. MARKET STRUCTURE
   Demand splits into traveler segments (business / leisure / connecting) that
   map onto cabin classes, each with its own size, elasticity, seasonality and
   day-of-week profile. Total route demand is the sum of segment demand, so the
   route now responds to WHO is travelling, not just a flat count.

2. STAGE-LENGTH ECONOMICS
   Distance drives block hours, fuel, and a per-seat cost curve. Short hops have
   high per-seat overhead (taxi/climb dominate); long stages amortize better but
   need range and may need augmented crew.

3. SUITABILITY / REQUIREMENTS  (the equipment + crew tie-in)
   A RouteSpec carries equipment requirements (min range, runway, optimal class)
   and crew requirements (type-rating, augmented-crew threshold). route_can_fly()
   validates an aircraft+crew pairing and returns the specific reasons it fails,
   so the sim can explain "A330 can't serve this 400km thin route economically"
   or "this 14h stage needs an augmented crew".
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
import math


# ============================================================
# 1. MARKET STRUCTURE — traveler segments
# ============================================================

class TravelerSegment(Enum):
    BUSINESS = auto()      # pays up, less elastic, midweek-heavy, fills premium cabins
    LEISURE = auto()       # price-driven, seasonal, weekend/holiday-heavy, economy
    CONNECTING = auto()    # feed traffic, price-sensitive, fills remaining economy


@dataclass(frozen=True)
class SegmentDemand:
    """One traveler segment's contribution to a route's demand."""
    segment: TravelerSegment
    base_per_day: float            # average daily travelers in this segment
    elasticity: float              # price sensitivity (negative)
    seasonality_amplitude: float   # +/- swing over the year
    seasonal_peak_day: int         # day-of-year the segment peaks
    dow_profile: tuple             # 7 multipliers (Mon..Sun), mean ~1.0

    def demand_on(self, sim_time_hours: float, price_ratio: float) -> float:
        day_of_year = (sim_time_hours / 24.0) % 365
        # seasonal sine centered on this segment's peak
        phase = 2 * math.pi * (day_of_year - self.seasonal_peak_day) / 365
        season = 1.0 + self.seasonality_amplitude * math.cos(phase)
        dow = self.dow_profile[int(sim_time_hours // 24) % 7]
        price_factor = max(0.0, price_ratio ** self.elasticity)
        return max(0.0, self.base_per_day * season * dow * price_factor)


# Reusable day-of-week profiles (Mon..Sun)
DOW_BUSINESS = (1.25, 1.2, 1.2, 1.25, 1.1, 0.5, 0.5)   # midweek heavy
DOW_LEISURE = (0.8, 0.75, 0.8, 0.9, 1.3, 1.4, 1.3)     # weekend heavy
DOW_FLAT = (1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)


def default_segments(total_per_day: float, business_frac: float = 0.25,
                     leisure_frac: float = 0.55) -> tuple:
    """Build a standard 3-segment market from a total demand figure + mix."""
    conn_frac = max(0.0, 1.0 - business_frac - leisure_frac)
    return (
        SegmentDemand(TravelerSegment.BUSINESS, total_per_day * business_frac,
                      elasticity=-0.7, seasonality_amplitude=0.10,
                      seasonal_peak_day=120, dow_profile=DOW_BUSINESS),
        SegmentDemand(TravelerSegment.LEISURE, total_per_day * leisure_frac,
                      elasticity=-1.6, seasonality_amplitude=0.35,
                      seasonal_peak_day=200, dow_profile=DOW_LEISURE),
        SegmentDemand(TravelerSegment.CONNECTING, total_per_day * conn_frac,
                      elasticity=-1.3, seasonality_amplitude=0.15,
                      seasonal_peak_day=200, dow_profile=DOW_FLAT),
    )


# ============================================================
# 2. STAGE-LENGTH ECONOMICS
# ============================================================

def block_hours(distance_km: float, cruise_speed_kmh: float) -> float:
    """Gate-to-gate time: cruise time plus a fixed taxi/climb/descent overhead."""
    cruise = distance_km / cruise_speed_kmh
    return cruise + 0.6   # ~36 min fixed overhead per flight


def per_seat_cost_index(distance_km: float) -> float:
    """
    Relative per-seat operating cost vs stage length. Short stages are penalized
    (fixed overhead spread over few miles); it flattens out for long hauls.
    Index ~1.0 at a medium 1500km stage.
    """
    # hyperbola: high at short distance, asymptotic at long
    return 0.7 + 250.0 / max(150.0, distance_km)


# ============================================================
# 3. SUITABILITY / REQUIREMENTS — equipment + crew tie-in
# ============================================================

@dataclass(frozen=True)
class EquipmentRequirements:
    """What an aircraft must satisfy to serve this route."""
    min_range_km: float
    min_runway_m: float            # both ends must meet this (checked vs airports)
    optimal_class: object          # PlaneClass the route is sized for
    # economic window: seats outside [min,max] make the route uneconomic
    min_viable_seats: int
    max_viable_seats: int


@dataclass(frozen=True)
class CrewRequirements:
    """Crew rules for this route, driven mostly by stage length."""
    augmented_crew_block_hours: float = 8.0   # stages longer than this need augmentation
    # minimum cockpit headcount; augmented routes need more pilots
    min_cockpit_standard: int = 2
    min_cockpit_augmented: int = 3


def route_can_fly(route_spec, aircraft_spec, origin_airport_spec,
                  dest_airport_spec, cockpit_crew=None) -> tuple:
    """
    Validate an aircraft (+ optional cockpit crew) against a route's equipment
    and crew requirements. Returns (ok: bool, reasons: list[str]). Empty reasons
    with ok=True means the pairing is legal AND economic.

    This is the central tie-in: Route reaches into equipment specs and crew to
    decide what may serve it, and explains every rejection.
    """
    reasons = []
    eq = getattr(route_spec, "equipment_req", None)
    cr = getattr(route_spec, "crew_req", None)

    # --- EQUIPMENT: range ---
    if aircraft_spec.max_range_km < route_spec.distance_km:
        reasons.append(f"range {aircraft_spec.max_range_km:.0f}km < "
                       f"route {route_spec.distance_km:.0f}km")

    # --- EQUIPMENT: runway at both ends ---
    if eq:
        for label, ap in (("origin", origin_airport_spec), ("dest", dest_airport_spec)):
            if ap is not None and ap.runway_length_m < eq.min_runway_m:
                reasons.append(f"{label} runway {ap.runway_length_m:.0f}m < "
                               f"required {eq.min_runway_m:.0f}m")

        # --- EQUIPMENT: economic seat window ---
        seats = aircraft_spec.max_seats
        if seats < eq.min_viable_seats:
            reasons.append(f"{seats} seats < min viable {eq.min_viable_seats} "
                           f"(too small to serve demand)")
        elif seats > eq.max_viable_seats:
            reasons.append(f"{seats} seats > max viable {eq.max_viable_seats} "
                           f"(uneconomic: too much capacity for this market)")

    # --- CREW: augmented requirement by stage length ---
    if cr and cockpit_crew is not None:
        bh = block_hours(route_spec.distance_km, aircraft_spec.cruise_speed_kmh)
        needs_aug = bh > cr.augmented_crew_block_hours
        required = cr.min_cockpit_augmented if needs_aug else cr.min_cockpit_standard
        have = getattr(cockpit_crew, "headcount", 0)
        if have < required:
            kind = "augmented" if needs_aug else "standard"
            reasons.append(f"{kind} crew needs {required} pilots, have {have} "
                           f"(block {bh:.1f}h)")

    return (len(reasons) == 0, reasons)


def augmented_crew_required(route_spec, aircraft_spec) -> bool:
    cr = getattr(route_spec, "crew_req", None)
    if not cr:
        return False
    bh = block_hours(route_spec.distance_km, aircraft_spec.cruise_speed_kmh)
    return bh > cr.augmented_crew_block_hours

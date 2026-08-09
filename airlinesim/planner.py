"""
ROUTE PLANNING — the shared forecast.
=====================================

What a route would earn, what would have to fly it, and how often it could
go. Read-only: nothing in this module mutates world or player state.

WHY IT LIVES HERE AND NOT IN ai.py
----------------------------------
The engine already contained a route forecast — `ai._evaluate()` — and it is
the arithmetic three AI carriers plan their whole networks with. Writing a
second one for the player would have drifted from it, and the drift would
have landed on the player: a planner promising $41k/day where the engine
charges costs producing $12k/day means being out-planned by rivals reading
truer numbers, for reasons invisible from inside the game.

So the forecast was EXTRACTED here and `ai._evaluate` is now a thin wrapper
over `evaluate_route()`. The AI and the player plan with one set of numbers
by construction — the same rule that makes `cabin.fit_layout` one fitter
behind three entry points, and `routedata.gravity_features` shared with
`btsdata` so the fit and its evaluation cannot diverge.

`scenario_planner` holds 450 recorded goldens asserting the AI's answers did
not move by a cent in the extraction, and they stay there permanently: every
future improvement to the PLAYER's forecast must not silently re-tune three
AI carriers.

POLICY vs FACT
--------------
`ForecastPolicy` carries everything that is a CHOICE — service tier, how far
above the reference fare to price, how much of a market to expect, what a
crew hour costs. Everything else is read from the world. That split is what
lets the AI pass its archetype's assumptions and the player pass theirs
through the identical code path.

WHAT IS NOT MODELLED, and must not be quietly added
---------------------------------------------------
- **No competitive response.** Share is diluted by the number of incumbents
  in the market and nothing else. A rival matching your fare, or adding
  frequency against you, is not forecast. This is the same limit the AI has
  always had, deliberately: a forecast that predicted the arbiter exactly
  would be an oracle, and the error is the seam a player out-plans it
  through.
- **Contribution margin only, so far.** `CostLines` charges what the engine
  charges a FLIGHT: fuel, maintenance, crew, landing, gate, amenities,
  baggage. It does NOT charge lease rent, loan service, payroll for crews
  that did not fly, or hub overhead — so a network of individually
  "profitable" routes can still burn cash, exactly as CLAUDE.md warns about
  `RouteOp.last_profit`. Every caller must label this line for what it is.
  The fully-absorbed line is phase 4 of `docs/route-planning-design.md`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from airlinesim.crew import DEFAULT_DUTY_LIMITS
from airlinesim.engine import AirportSpec, market_key
from airlinesim.route import block_hours, haversine, service_desirability

INF = float("inf")


# ============================================================
# CONSTANTS
# ============================================================
# These are game-balance figures, not certified ones, and they now live in
# ONE place. `databuilder` imported its world-construction copies from here
# rather than keeping its own, so the frequency a world is BUILT with and the
# frequency the planner PREDICTS cannot drift apart.

CARRIER_MARKET_SHARE = 0.45   # how much of a real market one carrier chases
DAILY_UTILIZATION_H = 14.0    # airframe hours available per day
CREW_DEPTH = 2.5              # crews per op at a station, for rest rotation
TARGET_LOAD_FACTOR = 0.85     # the load a frequency is sized to fill

# The AI's flat crew stand-in: two pilots at $220/h and four cabin at $60/h,
# charged per block hour. It is what `ai._evaluate` has always charged and the
# goldens pin it. The PLAYER's forecast should move to a real crew
# requirement (phase 4) — at which point this stays, unchanged, as the AI's.
AI_CREW_COST_PER_BLOCK_HOUR = 220 * 2 + 60 * 4


# ============================================================
# MARKET LOOKUP — the corpus seam
# ============================================================
# Moved here from `ai.py` so the player's forecast and the AI's read demand
# through the identical path. The AI gets no private oracle: where the data
# is a fitted guess for the player, it is a fitted guess here too.

def route_spec_for(world, origin, dest):
    """The corpus RouteSpec for a pair, or None where no provider is attached."""
    provider = getattr(world, "route_data", None)
    if provider is None:
        return None
    try:
        return provider.route_spec(origin.iata, dest.iata)
    except Exception:
        return None


def market_key_for(world, origin, dest) -> str:
    """The demand pool an airport pair would draw from."""
    spec = route_spec_for(world, origin, dest)
    return market_key(spec) if spec is not None else f"{origin.iata}-{dest.iata}"


def market_estimate(world, origin, dest) -> tuple:
    """
    ``(daily demand, reference fare)`` for a pair, from the committed corpus.

    NOTE ON THE FARE: the committed snapshot carries no DB1B fares, so
    `suggested_price` returns its engine default for every pair and this
    comes back $200.00 across the whole map. That is a corpus gap, not a
    modelling choice — see the caveats in CLAUDE.md.
    """
    spec = route_spec_for(world, origin, dest)
    if spec is not None:
        fare = getattr(spec, "reference_price", 0.0) or 0.0
        if not fare:
            provider = getattr(world, "route_data", None)
            if provider is not None:
                fare, _src = provider.suggested_price(origin.iata, dest.iata)
        return float(spec.base_demand_per_day), float(fare or 200.0)
    return 400.0, 200.0


def incumbent_count(world, players, origin, dest) -> int:
    """How many route ops already serve this pair's demand pool."""
    mkey = market_key_for(world, origin, dest)
    return sum(1 for pl in players for o in pl.route_ops
               if market_key(o.spec) == mkey)


# ============================================================
# FREQUENCY — four limits, and which one BINDS
# ============================================================
#
# "How often can I fly this?" is four different questions, and a bare number
# answers none of them. "6/day" tells a player nothing to act on; "6/day,
# demand-limited, the airframe could do 9" tells them to find a bigger
# market, and "3/day, gate-limited at LGA" tells them to look at a different
# field. So every limit is computed and the binding one is named — the same
# reason `route_can_fly` returns reasons rather than a bool.

@dataclass(frozen=True)
class FrequencyLimit:
    name: str          # airframe | demand | gates | crew
    value: float       # rotations per day this limit permits (INF = no limit)
    detail: str

    def to_json(self) -> dict:
        return {"name": self.name,
                "value": None if self.value == INF else round(self.value, 2),
                "detail": self.detail}


@dataclass(frozen=True)
class FrequencyPlan:
    limits: tuple

    @property
    def binding(self) -> FrequencyLimit:
        return min(self.limits, key=lambda l: l.value)

    @property
    def rotations(self) -> int:
        """The schedule that survives every limit. Never negative."""
        v = self.binding.value
        return 0 if v == INF else max(0, int(v))

    def to_json(self) -> dict:
        return {"rotations": self.rotations,
                "binding": self.binding.name,
                "limits": [l.to_json() for l in self.limits]}


def airframe_frequency(distance_km: float, cruise_kmh: float, *,
                       rotation: bool = True,
                       utilisation_h: float = DAILY_UTILIZATION_H) -> float:
    """
    How many departures a day one airframe can physically make.

    `rotation=True` charges the RETURN LEG to the same tail, because that is
    how the engine actually flies it: `ai._open_rotation` and
    `databuilder._with_return_legs` both put the out-and-back on one
    aeroplane. `databuilder.daily_frequency` divides by a single leg and so
    permits twice what a tail can fly; it is left alone because changing it
    would rebuild every data world, but the planner must not repeat it.
    """
    if cruise_kmh <= 0:
        return INF
    leg = block_hours(distance_km, cruise_kmh)
    per_departure = leg * (2.0 if rotation else 1.0)
    if per_departure <= 0:
        return INF
    return utilisation_h / per_departure


def demand_frequency(demand_per_day: float, share: float, seats: int,
                     target_load: float = TARGET_LOAD_FACTOR) -> float:
    """
    Departures needed to carry the traffic this offer can expect, at
    `target_load`. Flying more than this is flying empty seats.

    Shared with `databuilder.daily_frequency`, which is how a data world is
    built — so the frequency a world starts with and the frequency the
    planner recommends come off one formula.
    """
    if seats <= 0 or target_load <= 0:
        return INF
    return (demand_per_day * share) / (seats * target_load)


def gate_frequency(world, players, origin, dest, *,
                   rotation: bool = True) -> tuple:
    """
    Departures a day the GATES will take, as ``(value, detail)``.

    Read as a WHOLE-DAY view rather than off `GateLedger.free()`. The ledger
    is a live, partly-consumed tick figure that resets each calendar day, so
    reading it mid-day answers "what is left right now", which is not the
    question a plan asks. This instead totals every carrier's committed daily
    frequency into an airport against its gate count.

    A rotation claims a gate at BOTH ends — the outbound op claims one at the
    destination and the return op claims one at the origin (see the GATE
    claim in `OperationsSubsystem`), so the tighter end binds.

    LIMIT: this is capacity, not contention. The arbiter allocates
    oversubscribed gates by priority, and a hub multiplies its owner's, so a
    plan that fits on paper can still be outbid at a contested field.
    """
    ends = (dest,) if not rotation else (origin, dest)
    worst, where = INF, ""
    for ap in ends:
        ledger = world.gates.get(ap.iata)
        if ledger is None:
            continue
        committed = sum(max(0, o.daily_frequency) for pl in players
                        for o in pl.route_ops if o.spec.dest_iata == ap.iata)
        headroom = max(0.0, ledger.total_gates - committed)
        if headroom < worst:
            worst, where = headroom, ap.iata
    if worst == INF:
        return INF, "no gate ledger for either end"
    return worst, f"{worst:.0f} of {where}'s gates uncommitted"


def available_crews(player, iata: str, aircraft_spec) -> tuple:
    """
    ``(cockpit, cabin)`` crews this carrier has based at an airport and rated
    for the type. Counts the ROSTERING POOLS, via `crew._all_crews` — a crew
    sitting in a pool is the normal state, and the most expensive bug in the
    crew model was a walk that missed them.
    """
    from airlinesim.crew import _all_crews, crew_is_type_rated
    from airlinesim.engine import CrewType
    cockpit = cabin = 0
    for c in _all_crews(player):
        if not crew_is_type_rated(c, aircraft_spec):
            continue
        if (c.home_iata or c.location_iata) != iata:
            continue
        if c.spec.crew_type is CrewType.COCKPIT:
            cockpit += 1
        elif c.spec.crew_type is CrewType.CABIN:
            cabin += 1
    return cockpit, cabin


def crew_frequency(distance_km: float, cruise_kmh: float, cockpit: int,
                   cabin: int, limits=DEFAULT_DUTY_LIMITS) -> tuple:
    """
    Departures a day the crew POOL at the origin can sustain, as
    ``(value, detail)``.

    A flight needs BOTH a cockpit and a cabin crew, so the shallower of the
    two pools sets the ceiling — and no crew at all means the route is
    grounded, not merely thin, which is the one case where this limit reads
    zero.

    Sized as the exact inverse of `ai._crew_target`, which is also the rule
    `databuilder` builds starting pools with: a pool of N sustains
    ``N x max_daily_flight_hours / CREW_DEPTH`` block hours a day, where
    CREW_DEPTH is the rest-rotation factor that accounts for the crews resting
    at any moment. Requirement and ceiling therefore cannot drift apart.

    TWO DELIBERATE CONSERVATISMS, both inherited rather than invented:
    - Charged on BLOCK hours, matching the sizing rule, while the engine's
      duty gate bills CRUISE hours only (`fh_per_rotation = distance /
      cruise_speed_kmh` — taxi and climb are free on the crew's clock). The
      real legal ceiling is therefore a little looser than this. Being
      conservative is the right direction: a plan that assumes the looser
      number strands crews at spokes.
    - At a 24-hour tick the roster assigns ONE crew per op for the whole
      tick, so a coarse run is tighter than this steady-state view. A played
      game runs hourly, where crews hand over through the day as the
      incumbent hits its cap.
    """
    crews = min(cockpit, cabin)
    if crews <= 0:
        return 0.0, (f"no rated crew based here (cockpit {cockpit}, "
                     f"cabin {cabin}) — the route would be grounded")
    if cruise_kmh <= 0:
        return INF, "unknown cruise speed"
    per_leg = block_hours(distance_km, cruise_kmh)
    if per_leg <= 0:
        return INF, "zero block time"
    sustainable = crews * limits.max_daily_flight_hours / CREW_DEPTH
    return (sustainable / per_leg,
            f"{crews} rated crew pair(s) x {limits.max_daily_flight_hours:.0f}h "
            f"/ {CREW_DEPTH:g} depth over a {per_leg:.1f}h leg")


def frequency_plan(world, players, origin, dest, aircraft_spec, *,
                   demand_per_day: float, share: float, player=None,
                   rotation: bool = True,
                   duty_limits=DEFAULT_DUTY_LIMITS) -> FrequencyPlan:
    """
    Every limit on how often this offer can go, and which one binds.

    `player` is optional: without one there is no crew pool to count, so the
    crew limit is reported as unknown rather than as unlimited. Silently
    treating "I don't know" as "no constraint" is how a planner promises a
    schedule nobody can fly.
    """
    dist = haversine(origin.lat, origin.lon, dest.lat, dest.lon)
    cruise = aircraft_spec.cruise_speed_kmh
    seats = max(0, aircraft_spec.max_seats)

    af = airframe_frequency(dist, cruise, rotation=rotation)
    dm = demand_frequency(demand_per_day, share, seats)
    gt, gate_detail = gate_frequency(world, players, origin, dest,
                                     rotation=rotation)
    if player is None:
        cw, crew_detail = INF, "no carrier given — crew pool not counted"
    else:
        cockpit, cabin = available_crews(player, origin.iata, aircraft_spec)
        cw, crew_detail = crew_frequency(dist, cruise, cockpit, cabin,
                                         duty_limits)

    leg_h = block_hours(dist, cruise) if cruise > 0 else 0.0
    return FrequencyPlan((
        FrequencyLimit("airframe", af,
                       f"{DAILY_UTILIZATION_H:.0f}h/day over a "
                       f"{leg_h * (2 if rotation else 1):.1f}h "
                       f"{'rotation' if rotation else 'leg'}"),
        FrequencyLimit("demand", dm,
                       f"{demand_per_day:.0f} pax/day x {share:.0%} share "
                       f"over {seats} seats at {TARGET_LOAD_FACTOR:.0%}"),
        FrequencyLimit("gates", gt, gate_detail),
        FrequencyLimit("crew", cw, crew_detail),
    ))


# ============================================================
# THE FORECAST
# ============================================================

class CostBasis(Enum):
    """
    Which costs a forecast charges.

    CONTRIBUTION is what the engine charges a FLIGHT and what
    `RouteOp.last_profit` reports. It is NOT profit: lease rent, loan
    service, payroll for crews that did not fly and hub overhead are all
    outside it, which is how a carrier shows every route profitable while the
    company burns cash. Any surface that shows this number must say so.
    """
    CONTRIBUTION = "contribution"


@dataclass(frozen=True)
class ForecastPolicy:
    """
    The CHOICE half of a forecast. Everything else is read from the world.

    The AI passes its archetype's values; the player's planner passes theirs.
    One code path, two callers, no drift.
    """
    service_tier: int = 2
    fare_vs_reference: float = 1.0
    # Business-model preference for this pair, as a multiplier on share.
    # 1.0 = none, which is what the PLAYER gets: a human has no archetype,
    # and quietly applying one would rank their map against a preference they
    # never set.
    fit: float = 1.0
    load_cap: float = 0.75          # pax ceiling as a fraction of seats offered
    min_stage_km: float = 0.0
    max_stage_km: float = INF
    fuel_price_per_l: float = 0.9
    crew_cost_per_block_hour: float = AI_CREW_COST_PER_BLOCK_HOUR
    cost_basis: CostBasis = CostBasis.CONTRIBUTION


@dataclass(frozen=True)
class CostLines:
    """
    Itemised, because "costs: $38,400" is not something a player can act on.
    Every line is charged the way `OperationsSubsystem` charges it.
    """
    fuel: float = 0.0
    maintenance: float = 0.0
    crew: float = 0.0
    landing: float = 0.0
    gate: float = 0.0
    amenities: float = 0.0
    baggage: float = 0.0

    @property
    def fees(self) -> float:
        return self.landing + self.gate + self.amenities + self.baggage

    @property
    def total(self) -> float:
        return self.fuel + self.maintenance + self.crew + self.fees

    def to_json(self) -> dict:
        return {"fuel": round(self.fuel, 2),
                "maintenance": round(self.maintenance, 2),
                "crew": round(self.crew, 2),
                "landing": round(self.landing, 2),
                "gate": round(self.gate, 2),
                "amenities": round(self.amenities, 2),
                "baggage": round(self.baggage, 2),
                "fees": round(self.fees, 2),
                "total": round(self.total, 2)}


@dataclass(frozen=True)
class RouteForecast:
    """One (pair, type, frequency) case, itemised and explained."""
    origin: str
    dest: str
    spec_id: str
    distance_km: float
    block_h: float                  # per leg
    frequency: int
    # feasibility
    feasible: bool                  # can this aeroplane legally serve the pair
    reasons: tuple                  # why not, verbatim from the checks
    # market
    demand_per_day: float = 0.0
    reference_fare: float = 0.0
    fare: float = 0.0
    share: float = 0.0
    incumbents: int = 0
    seats_offered: float = 0.0
    pax: float = 0.0
    revenue: float = 0.0
    costs: CostLines = CostLines()
    # provenance — an estimate must never be mistaken for a measurement
    data_tier: str = ""
    data_vintage: str = ""

    @property
    def viable(self) -> bool:
        """Feasible AND there is a market to carry."""
        return self.feasible and self.pax > 0

    @property
    def contribution(self) -> float:
        """Revenue less the costs the engine charges a flight. NOT profit."""
        return self.revenue - self.costs.total

    @property
    def load_factor(self) -> float:
        return (self.pax / self.seats_offered) if self.seats_offered > 0 else 0.0

    @property
    def break_even_fare(self) -> float:
        """The fare at which this schedule stops losing money, at this load."""
        return (self.costs.total / self.pax) if self.pax > 0 else INF

    @property
    def break_even_load(self) -> float:
        """
        The load factor that covers costs at this fare. Above 1.0 means the
        schedule cannot pay for itself however full it flies.
        """
        if self.fare <= 0 or self.seats_offered <= 0:
            return INF
        return self.costs.total / (self.fare * self.seats_offered)

    def to_json(self) -> dict:
        be_load = self.break_even_load
        be_fare = self.break_even_fare
        return {
            "origin": self.origin, "dest": self.dest, "spec_id": self.spec_id,
            "distance_km": round(self.distance_km, 1),
            "block_h": round(self.block_h, 2),
            "frequency": self.frequency,
            "feasible": self.feasible, "viable": self.viable,
            "reasons": list(self.reasons),
            "demand_per_day": round(self.demand_per_day, 1),
            "reference_fare": round(self.reference_fare, 2),
            "fare": round(self.fare, 2),
            "share": round(self.share, 4), "incumbents": self.incumbents,
            "seats_offered": round(self.seats_offered, 1),
            "pax": round(self.pax, 1),
            "load_factor": round(self.load_factor, 4),
            "revenue": round(self.revenue, 2),
            "costs": self.costs.to_json(),
            "contribution": round(self.contribution, 2),
            "break_even_load": (None if be_load == INF else round(be_load, 4)),
            "break_even_fare": (None if be_fare == INF else round(be_fare, 2)),
            "data_tier": self.data_tier, "data_vintage": self.data_vintage,
        }


def structural_reasons(origin, dest, aircraft_spec, distance_km: float,
                       policy: ForecastPolicy) -> list:
    """
    Why this aeroplane cannot serve this pair — stage band, range, runway.

    Returned as a list of sentences rather than a bool, because "why is the
    787 not on this list?" is the question a planner screen exists to answer.
    """
    out = []
    if distance_km < policy.min_stage_km:
        out.append(f"stage {distance_km:.0f}km below the "
                   f"{policy.min_stage_km:.0f}km floor for this plan")
    if distance_km > policy.max_stage_km:
        out.append(f"stage {distance_km:.0f}km above the "
                   f"{policy.max_stage_km:.0f}km ceiling for this plan")
    if aircraft_spec.max_range_km < distance_km:
        out.append(f"range {aircraft_spec.max_range_km:.0f}km < route "
                   f"{distance_km:.0f}km")
    for label, ap in (("origin", origin), ("dest", dest)):
        if ap.runway_length_m < aircraft_spec.takeoff_runway_m:
            out.append(f"{label} {ap.iata} runway {ap.runway_length_m:.0f}m < "
                       f"{aircraft_spec.spec_id} takeoff "
                       f"{aircraft_spec.takeoff_runway_m:.0f}m")
    return out


def suitability_reasons(world, origin, dest, aircraft_spec) -> tuple:
    """
    Why `open_route` would REFUSE this pairing, verbatim from
    `route.route_can_fly` — the same check `RouteSuitabilitySubsystem` runs
    every tick and `actions.validate_equipment` gates route opening on.

    Deliberately NOT called by `evaluate_route`, and the distinction matters.
    The two check different things:

      route_can_fly     the ROUTE's requirements — its banded min_runway_m
                        and the corpus's economic seat window
      structural_reasons the AIRCRAFT's physics — its own takeoff length, its
                        range, and the plan's stage band

    A player needs both: one is what will block the button, the other is what
    is actually true. Folding suitability into the forecast would also have
    made the AI start rejecting pairs it has always accepted (it has never
    consulted the seat window when scoring a candidate), which is a balance
    change wearing a refactor's clothes. Callers report the two side by side.
    """
    from airlinesim.route import route_can_fly
    spec = route_spec_for(world, origin, dest)
    if spec is None:
        return ()
    _ok, reasons = route_can_fly(spec, aircraft_spec, origin, dest)
    return tuple(reasons)


def evaluate_route(world, players, origin, dest, aircraft_spec, frequency: int,
                   policy: ForecastPolicy = ForecastPolicy()) -> RouteForecast:
    """
    Forecast one offer: this aeroplane, on this pair, at this frequency.

    Deliberately a rough forecast and not a simulation — it prices the
    aircraft's own seats against a share of the market and subtracts the
    costs the engine will actually charge. See the module docstring for what
    it does not model.

    THE COST LINES REPRODUCE `OperationsSubsystem`'s CHARGES, including two
    asymmetries that look like bugs and are not, because the engine has them:
    a landing fee is charged at the DESTINATION only (the origin's landing was
    paid by whatever flight brought the aeroplane there), while gate,
    amenities and baggage are charged at BOTH ends. Changing either here
    would make the planner disagree with the ledger.
    """
    dist = haversine(origin.lat, origin.lon, dest.lat, dest.lon)
    spec = aircraft_spec
    leg_h = block_hours(dist, spec.cruise_speed_kmh) if spec.cruise_speed_kmh > 0 else 0.0

    route_spec = route_spec_for(world, origin, dest)
    tier = getattr(route_spec, "data_tier", "") if route_spec is not None else ""
    vintage = getattr(route_spec, "data_vintage", "") if route_spec is not None else ""

    reasons = structural_reasons(origin, dest, spec, dist, policy)
    if reasons:
        return RouteForecast(
            origin=origin.iata, dest=dest.iata, spec_id=spec.spec_id,
            distance_km=dist, block_h=leg_h, frequency=frequency,
            feasible=False, reasons=tuple(reasons),
            data_tier=tier, data_vintage=vintage)

    demand, ref_fare = market_estimate(world, origin, dest)
    freq = frequency
    seats = spec.max_seats * freq

    incumbents = incumbent_count(world, players, origin, dest)
    desir = service_desirability(policy.service_tier, origin.access_index,
                                 dest.access_index)
    share = desir / (1.0 + incumbents)
    share *= policy.fit
    pax = min(seats * policy.load_cap, demand * share)

    fare = ref_fare * policy.fare_vs_reference
    revenue = pax * fare

    bh = leg_h * freq
    fuel = spec.fuel_burn_lph * bh * policy.fuel_price_per_l
    crew = policy.crew_cost_per_block_hour * bh
    maint = spec.maint_cost_per_hour * bh

    landing = gate = amenities = baggage = 0.0
    for ap, landings in ((dest, freq), (origin, 0)):
        landing += ap.landing_fee * landings
        gate += ap.fee_at_tier(ap.gate_fee_by_tier, policy.service_tier) * freq
        amenities += ap.fee_at_tier(ap.amenities_fee_by_tier,
                                    policy.service_tier) * pax
        baggage += ap.fee_at_tier(ap.baggage_fee_by_tier,
                                  policy.service_tier) * pax

    no_market = () if pax > 0 else (
        f"no carriable demand: {demand:.0f} pax/day x {share:.1%} share",)

    return RouteForecast(
        origin=origin.iata, dest=dest.iata, spec_id=spec.spec_id,
        distance_km=dist, block_h=leg_h, frequency=freq,
        feasible=True, reasons=no_market,
        demand_per_day=demand, reference_fare=ref_fare, fare=fare,
        share=share, incumbents=incumbents, seats_offered=seats, pax=pax,
        revenue=revenue,
        costs=CostLines(fuel=fuel, maintenance=maint, crew=crew,
                        landing=landing, gate=gate, amenities=amenities,
                        baggage=baggage),
        data_tier=tier, data_vintage=vintage)

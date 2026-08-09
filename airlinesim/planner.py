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
- **Two margin lines, and the difference matters.** `contribution` is what
  the engine charges a FLIGHT — fuel, maintenance, crew, landing, gate,
  amenities, baggage — and is exactly what `RouteOp.last_profit` reports and
  what the AI reads. `absorbed` subtracts the ownership a plan would ADD.
  Ranking on contribution alone puts the biggest aeroplane first on every
  route it can fill: on a corpus world the A350 out-earns everything on
  ORD-DEN contribution and loses $42k a day once its lease is paid. Charging
  ownership only where an aeroplane must be ACQUIRED is deliberate — a tail
  already owned is being paid for either way, so putting it to work costs
  nothing extra.
- **Still outside both lines:** payroll for crews that did not fly, and hub
  overhead. The crew figure is a flat per-block-hour estimate rather than a
  headcount; phase 4 of `docs/route-planning-design.md` replaces it. So a
  network of individually "profitable" routes can still burn cash, exactly as
  CLAUDE.md warns about `RouteOp.last_profit`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from airlinesim.crew import DEFAULT_DUTY_LIMITS
from airlinesim.engine import AircraftSpec, market_key
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


def expected_share(world, players, origin, dest,
                   policy: "ForecastPolicy") -> float:
    """
    The fraction of a market this offer can expect: every operator already in
    the metro pair dilutes it, desirability lifts it, business-model fit
    tilts it.

    Independent of frequency, which is what lets `frequency_plan` size a
    schedule off the same share the forecast then prices — without it the two
    would each need their own share model and would answer differently for
    the same offer.
    """
    incumbents = incumbent_count(world, players, origin, dest)
    desir = service_desirability(policy.service_tier, origin.access_index,
                                 dest.access_index)
    return (desir / (1.0 + incumbents)) * policy.fit


TIER_NOTE = {
    "exact": "measured: a BTS-observed pair",
    "comparable": ("ESTIMATED: no BTS observation for this pair, so demand is "
                   "a fitted gravity estimate — around a third of these are "
                   "out by more than 2x"),
    "synthetic": ("NOT MEASURED: no corpus data for this pair at all, so "
                  "demand is an engine default"),
}


def tier_note(tier: str) -> str:
    """
    The caveat that must travel with a number of this provenance.

    Lives here rather than in the UI so a new surface cannot forget it: a
    fitted estimate presented like a measurement is the single most
    misleading thing this tool could do.
    """
    return TIER_NOTE.get((tier or "").lower(), "provenance unknown")


def fuel_price_at(world, iata: str, default: float = 0.9) -> float:
    """
    The steady-state fuel price at an airport.

    Deliberately the BASE price, not `spot_price()`: spot rises as the day's
    supply depletes, so reading it mid-tick would make the same plan cost
    different amounts depending on when the player opened the screen.
    """
    fm = getattr(world, "fuel", {}).get(iata)
    return float(getattr(fm, "base_price_per_l", default) or default)


def player_policy(world, origin, *, service_tier: int = 2,
                  fare_vs_reference: float = 1.0) -> "ForecastPolicy":
    """
    A human carrier's forecasting assumptions.

    No `fit` and no stage band — a player has no archetype, and quietly
    applying one would rank their map against a preference they never set.
    Fuel is priced at the origin's own base rate rather than the AI's flat
    assumption.

    The crew line is still the flat per-block-hour stand-in. Phase 4 of
    `docs/route-planning-design.md` replaces it with a real crew requirement;
    until then any surface showing this must call the bottom line a
    CONTRIBUTION MARGIN, because that is what it is.
    """
    return ForecastPolicy(
        service_tier=service_tier,
        fare_vs_reference=fare_vs_reference,
        fit=1.0,
        fuel_price_per_l=fuel_price_at(world, origin.iata),
    )


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
    # An ADVISORY limit is reported but does not bind. There is exactly one
    # use: a type the carrier does not operate has no crew rated for it, which
    # is not a constraint on the plan — hiring the crew is PART of acquiring
    # the aeroplane. Treating it as binding made every "you could buy this"
    # row read zero rotations, which is both wrong and useless.
    advisory: bool = False

    def to_json(self) -> dict:
        return {"name": self.name,
                "value": None if self.value == INF else round(self.value, 2),
                "detail": self.detail, "advisory": self.advisory}


@dataclass(frozen=True)
class FrequencyPlan:
    limits: tuple

    @property
    def binding(self) -> FrequencyLimit:
        live = [l for l in self.limits if not l.advisory]
        return min(live or self.limits, key=lambda l: l.value)

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


def operates_type(player, aircraft_spec) -> bool:
    """
    Does this carrier fly this type today? Matched on the TYPE RATING, so an
    A319 operator counts as operating the A321 — one rating covers the family
    and its crews are already qualified.
    """
    rating = getattr(aircraft_spec, "type_rating", "")
    for a in getattr(player, "fleet", ()):
        if a.retired:
            continue
        if a.spec.spec_id == aircraft_spec.spec_id:
            return True
        if rating and getattr(a.spec, "type_rating", "") == rating:
            return True
    return False


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

    For a type the carrier does NOT operate, the crew limit is marked
    ADVISORY: there are no rated crews because the aeroplane has not been
    bought yet, and hiring them is part of buying it. It is still reported —
    the headcount is a real cost of the plan — but it does not bind.
    """
    dist = haversine(origin.lat, origin.lon, dest.lat, dest.lon)
    cruise = aircraft_spec.cruise_speed_kmh
    seats = max(0, aircraft_spec.max_seats)

    af = airframe_frequency(dist, cruise, rotation=rotation)
    dm = demand_frequency(demand_per_day, share, seats)
    gt, gate_detail = gate_frequency(world, players, origin, dest,
                                     rotation=rotation)
    crew_advisory = False
    if player is None:
        cw, crew_detail = INF, "no carrier given — crew pool not counted"
    else:
        cockpit, cabin = available_crews(player, origin.iata, aircraft_spec)
        cw, crew_detail = crew_frequency(dist, cruise, cockpit, cabin,
                                         duty_limits)
        if not operates_type(player, aircraft_spec):
            crew_advisory = True
            crew_detail = (f"no crew rated for the {aircraft_spec.spec_id} yet "
                           f"— hiring is part of acquiring the type, so this "
                           f"does not bind the plan")

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
        FrequencyLimit("crew", cw, crew_detail, advisory=crew_advisory),
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
    # Lease rent or loan service, per day, for an airframe the plan requires
    # ACQUIRING. Zero for metal already owned, where it is sunk — see
    # `plan_pair`. Kept out of `direct` so `contribution` stays exactly the
    # number `RouteOp.last_profit` reports and the AI reads.
    ownership: float = 0.0

    @property
    def fees(self) -> float:
        return self.landing + self.gate + self.amenities + self.baggage

    @property
    def direct(self) -> float:
        """What the engine charges a FLIGHT."""
        return self.fuel + self.maintenance + self.crew + self.fees

    @property
    def total(self) -> float:
        return self.direct + self.ownership

    def to_json(self) -> dict:
        return {"fuel": round(self.fuel, 2),
                "maintenance": round(self.maintenance, 2),
                "crew": round(self.crew, 2),
                "landing": round(self.landing, 2),
                "gate": round(self.gate, 2),
                "amenities": round(self.amenities, 2),
                "baggage": round(self.baggage, 2),
                "fees": round(self.fees, 2),
                "ownership": round(self.ownership, 2),
                "direct": round(self.direct, 2),
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
        """
        Revenue less the costs the engine charges a FLIGHT. NOT profit — this
        is exactly what `RouteOp.last_profit` reports and what the AI reads,
        and it excludes ownership, payroll for crews that did not fly, and hub
        overhead.
        """
        return self.revenue - self.costs.direct

    @property
    def absorbed(self) -> float:
        """
        Contribution less the ownership this plan would ADD.

        The honest ranking number when a plan requires buying an aeroplane:
        a widebody can out-earn a narrowbody on contribution and still lose
        money once its lease is paid, and ranking on contribution alone
        recommends exactly that aeroplane.
        """
        return self.revenue - self.costs.total

    @property
    def has_ownership(self) -> bool:
        """Whether `absorbed` says anything `contribution` does not."""
        return self.costs.ownership > 0

    @property
    def load_factor(self) -> float:
        return (self.pax / self.seats_offered) if self.seats_offered > 0 else 0.0

    @property
    def break_even_fare(self) -> float:
        """The fare at which this schedule stops losing money, at this load."""
        return (self.costs.total / self.pax) if self.pax > 0 else INF

    @property
    def break_even_fare_direct(self) -> float:
        """The same, ignoring ownership — the contribution break-even."""
        return (self.costs.direct / self.pax) if self.pax > 0 else INF

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
            "absorbed": round(self.absorbed, 2),
            "has_ownership": self.has_ownership,
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
                   policy: ForecastPolicy = ForecastPolicy(),
                   ownership_per_day: float = 0.0) -> RouteForecast:
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
    share = expected_share(world, players, origin, dest, policy)
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
                        baggage=baggage, ownership=max(0.0, ownership_per_day)),
        data_tier=tier, data_vintage=vintage)


# ============================================================
# Q1 — "I have an origin and a destination. What can fly it?"
# ============================================================

# Where an aeroplane for this route would come from. The distinction is the
# question the screen exists to answer, so it is a value, not a formatted
# string.
SOURCE_IDLE = "idle"            # owned, unassigned, already at the origin
SOURCE_ELSEWHERE = "elsewhere"  # owned and unassigned, but based somewhere else
SOURCE_COMMITTED = "committed"  # owned but already flying something
SOURCE_ACQUIRE = "acquire"      # not owned

SOURCE_RANK = {SOURCE_IDLE: 0, SOURCE_COMMITTED: 1,
               SOURCE_ELSEWHERE: 2, SOURCE_ACQUIRE: 3}


@dataclass(frozen=True)
class TailOption:
    """One owned airframe, and what standing it up on this route would cost."""
    tail_number: str
    location_iata: str
    source: str
    switching_cost: float       # daily contribution given up to free it
    note: str

    def to_json(self) -> dict:
        return {"tail_number": self.tail_number,
                "location_iata": self.location_iata, "source": self.source,
                "switching_cost": round(self.switching_cost, 2),
                "note": self.note}


@dataclass(frozen=True)
class FleetOption:
    """One aircraft type, judged against one city pair."""
    spec_id: str
    display_name: str
    plane_class: str
    max_seats: int
    max_range_km: float
    takeoff_runway_m: float
    forecast: RouteForecast
    frequency: FrequencyPlan
    suitability: tuple          # route_can_fly's reasons; empty = open_route allows it
    tails: tuple                # TailOption, best source first
    quotes: tuple               # AcquisitionQuote, one per method
    source: str                 # the best way to fly it today

    @property
    def open_route_ok(self) -> bool:
        """Would `open_route` accept this pairing? (route_can_fly's verdict.)"""
        return not self.suitability

    @property
    def physics_ok(self) -> bool:
        """Can the aeroplane actually make the trip and the runways?"""
        return self.forecast.feasible

    @property
    def engine_would_allow(self) -> bool:
        """
        THE ENGINE IS LOOSER THAN PHYSICS, and where the two disagree a player
        needs to know which is which.

        `route_can_fly` checks the ROUTE's banded `min_runway_m`, never the
        AIRCRAFT's own `takeoff_runway_m`. So the engine will happily fly an
        A321 (2,300 m balanced field) into LGA (2,134 m) — `databuilder` does
        exactly that on ORD-LGA in every data world. The AI's route evaluation
        has always checked the aircraft figure and so would never open it.

        The planner reports both and takes the STRICTER line for `operable`,
        because recommending a takeoff the aeroplane cannot make would be the
        planner endorsing an engine gap. This property is what lets the screen
        say "the game will let you, but the aeroplane can't".
        """
        return self.open_route_ok and not self.physics_ok

    @property
    def operable(self) -> bool:
        """Could this be flown today, as planned, without lying to anyone?"""
        return (self.forecast.viable and self.open_route_ok
                and self.physics_ok and self.frequency.rotations > 0)

    def to_json(self) -> dict:
        return {
            "spec_id": self.spec_id, "display_name": self.display_name,
            "plane_class": self.plane_class, "max_seats": self.max_seats,
            "max_range_km": self.max_range_km,
            "takeoff_runway_m": self.takeoff_runway_m,
            "source": self.source, "operable": self.operable,
            "open_route_ok": self.open_route_ok,
            "physics_ok": self.physics_ok,
            "engine_would_allow": self.engine_would_allow,
            "forecast": self.forecast.to_json(),
            "frequency": self.frequency.to_json(),
            "suitability": list(self.suitability),
            "tails": [t.to_json() for t in self.tails],
            "quotes": [q.to_json() for q in self.quotes],
        }


@dataclass(frozen=True)
class PairPlan:
    """Every way to serve one city pair, ranked."""
    origin: str
    dest: str
    distance_km: float
    demand_per_day: float
    reference_fare: float
    share: float
    incumbents: int
    data_tier: str
    data_vintage: str
    service_tier: int
    options: tuple
    notes: tuple

    def to_json(self) -> dict:
        return {
            "origin": self.origin, "dest": self.dest,
            "distance_km": round(self.distance_km, 1),
            "demand_per_day": round(self.demand_per_day, 1),
            "reference_fare": round(self.reference_fare, 2),
            "share": round(self.share, 4), "incumbents": self.incumbents,
            "data_tier": self.data_tier, "data_vintage": self.data_vintage,
            "tier_note": tier_note(self.data_tier),
            "service_tier": self.service_tier,
            "options": [o.to_json() for o in self.options],
            "notes": list(self.notes),
        }


def _tail_options(world, player, origin, spec_id: str) -> tuple:
    """
    This carrier's airframes of a type, and how available each one is.

    An aircraft is where it was BASED, permanently: `location_iata` is set at
    acquisition and no subsystem ever updates it — there are no ferry flights
    and no repositioning in the engine. So a tail based somewhere else is not
    "a short flight away", it is unavailable for a route out of this origin,
    and the planner says so rather than ranking a plan that cannot start.
    """
    out = []
    for a in player.fleet:
        if a.retired or a.spec.spec_id != spec_id:
            continue
        ops = [o for o in player.route_ops if o.plane.tail_number == a.tail_number]
        at_origin = a.location_iata == origin.iata
        if not at_origin:
            out.append(TailOption(
                a.tail_number, a.location_iata, SOURCE_ELSEWHERE, 0.0,
                f"based at {a.location_iata}; the engine has no ferry flights, "
                f"so this tail cannot start a route from {origin.iata}"))
        elif ops:
            lost = sum(max(0.0, getattr(o, "last_profit", 0.0)) for o in ops)
            out.append(TailOption(
                a.tail_number, a.location_iata, SOURCE_COMMITTED, lost,
                f"flying {', '.join(o.spec.spec_id for o in ops)} — freeing it "
                f"gives up ${lost:,.0f}/day of contribution"))
        else:
            out.append(TailOption(a.tail_number, a.location_iata, SOURCE_IDLE,
                                  0.0, f"idle at {origin.iata}"))
    out.sort(key=lambda t: (SOURCE_RANK[t.source], t.switching_cost,
                            t.tail_number))
    return tuple(out)


def plan_pair(world, players, player, origin, dest, *, service_tier: int = 2,
              fare_vs_reference: float = 1.0, bank=None) -> PairPlan:
    """
    "I want to fly ORD-LGA. What can do it, what would it earn, and where
    would the aeroplane come from?"

    Read-only. Every type in the catalog is judged, and the ones that CANNOT
    serve the pair are returned with their reasons rather than filtered out —
    "why is the 787 not on this list?" is the question this exists to answer.

    Two independent rejection paths are reported side by side, because they
    mean different things to a player:

      `suitability`        what `open_route` would refuse — the route's banded
                           runway requirement and the corpus seat window
      `forecast.reasons`   what is physically true — the aircraft's own
                           takeoff length, its range, no market to carry

    Ranked by daily CONTRIBUTION MARGIN, which is revenue less what the engine
    charges a flight. It is not profit: lease rent, loan service, payroll and
    hub overhead sit outside it. Any surface showing this number must say so.
    """
    from airlinesim.actions import METHOD_BY_NAME, TERMS_BY_METHOD, bank_for
    policy = player_policy(world, origin, service_tier=service_tier,
                           fare_vs_reference=fare_vs_reference)
    bank = bank or bank_for(world)
    demand, ref_fare = market_estimate(world, origin, dest)
    share = expected_share(world, players, origin, dest, policy)
    dist = haversine(origin.lat, origin.lon, dest.lat, dest.lon)
    route_spec = route_spec_for(world, origin, dest)

    options = []
    for spec in world.repo.all(AircraftSpec):
        fplan = frequency_plan(world, players, origin, dest, spec,
                               demand_per_day=demand, share=share,
                               player=player)
        tails = _tail_options(world, player, origin, spec.spec_id)
        quotes = tuple(bank.quote(player, spec, m, TERMS_BY_METHOD[m])
                       for m in (METHOD_BY_NAME["CASH"],
                                 METHOD_BY_NAME["FINANCE"],
                                 METHOD_BY_NAME["LEASE"]))
        source = tails[0].source if tails else SOURCE_ACQUIRE
        # OWNERSHIP IS INCREMENTAL OR SUNK, and treating the two alike ranks
        # the wrong aeroplane. A tail already owned is being paid for whether
        # or not it flies this route, so putting it to work costs nothing
        # extra. An aeroplane that has to be bought does — and on a corpus
        # world the widebodies out-earn the narrowbodies on contribution while
        # losing money once their lease is paid, so a ranking that ignored
        # this recommended precisely the aircraft that bankrupts you.
        #
        # It is charged at the LEASE RATE whichever way the aeroplane is
        # actually paid for. Buying outright has no daily payment, but it does
        # not make the aeroplane free — it converts capital, and the lease
        # rate is what that capital's use is worth on the open market. Taking
        # the cheapest quote by daily payment would price a cash purchase at
        # $0/day and rank a $290M 787 as the best way to fly a thin route.
        # `ai._rank_aircraft` already expenses ownership this way, at the same
        # 11%/year that `actions.LEASE_TERMS` charges.
        if source == SOURCE_ACQUIRE:
            lease = next((q for q in quotes
                          if q.method is METHOD_BY_NAME["LEASE"]), None)
            ownership = lease.daily if lease else 0.0
        else:
            ownership = 0.0
        forecast = evaluate_route(world, players, origin, dest, spec,
                                  max(1, fplan.rotations), policy,
                                  ownership_per_day=ownership)
        options.append(FleetOption(
            spec_id=spec.spec_id, display_name=spec.display_name,
            plane_class=spec.plane_class.name, max_seats=spec.max_seats,
            max_range_km=spec.max_range_km,
            takeoff_runway_m=spec.takeoff_runway_m,
            forecast=forecast, frequency=fplan,
            suitability=suitability_reasons(world, origin, dest, spec),
            tails=tails, quotes=quotes, source=source))

    # Operable options first, then by what they earn. Types that cannot fly
    # the pair sort last but are still present, with their reasons.
    # Ranked on the ABSORBED line — contribution less the ownership the plan
    # would add. Ranking on contribution puts the biggest aeroplane first on
    # every route it can fill, which is the most expensive wrong answer this
    # screen could give.
    options.sort(key=lambda o: (not o.operable, -o.forecast.absorbed,
                                o.spec_id))

    notes = [tier_note(getattr(route_spec, "data_tier", ""))]
    if not any(o.operable for o in options):
        notes.append("no type in the catalog can fly this pair as planned — "
                     "each row says why")
    # Where the engine is looser than the aeroplane, say so once, plainly.
    loose = [o for o in options if o.engine_would_allow]
    if loose:
        notes.append(
            f"{', '.join(o.spec_id for o in loose)}: the game would let you "
            f"open this ({dest.iata}'s runway clears the ROUTE's banded "
            f"requirement) but the aircraft's own takeoff length does not "
            f"fit. route_can_fly never checks the airframe figure — the "
            f"planner takes the stricter line.")
    idle = [o for o in options if o.source == SOURCE_IDLE and o.operable]
    if idle:
        notes.append(f"{len(idle)} type(s) could fly this today with metal "
                     f"already idle at {origin.iata}")
    elif any(o.operable for o in options):
        notes.append(f"no idle aircraft at {origin.iata} — every option here "
                     f"means acquiring, or taking a tail off another route")
    notes.append(
        "ranked on ABSORBED margin per day: revenue less fuel, maintenance, "
        "crew and airport fees, less the lease or loan this plan would ADD. "
        "Ownership is charged only where the aeroplane must be acquired — a "
        "tail you already own is paid for either way, so flying it costs "
        "nothing extra.")
    notes.append(
        "ownership is charged at the LEASE rate however you would pay. Buying "
        "outright has no daily payment but does not make an aeroplane free — "
        "it converts capital, and the lease rate is what its use is worth. "
        "The three quotes on each row are what the bank would actually do.")
    notes.append(
        "still NOT deducted, on either line: payroll for crews that did not "
        "fly, and hub overhead. The crew figure here is a flat per-block-hour "
        "estimate, not a headcount.")

    return PairPlan(
        origin=origin.iata, dest=dest.iata, distance_km=dist,
        demand_per_day=demand, reference_fare=ref_fare, share=share,
        incumbents=incumbent_count(world, players, origin, dest),
        data_tier=getattr(route_spec, "data_tier", ""),
        data_vintage=getattr(route_spec, "data_vintage", ""),
        service_tier=service_tier, options=tuple(options),
        notes=tuple(notes))


# ============================================================
# Q3 — "I have this aircraft / this station. Where should it fly?"
# ============================================================

# How a destination list can be ordered. Every key is a function of the
# candidate, so adding one is a table entry rather than a branch in the
# sorter — and the GUI's picker is driven off this table, so the two cannot
# offer different sets.
RANK_KEYS = {
    "absorbed": ("absorbed margin $/day",
                 lambda c: -c.forecast.absorbed),
    "contribution": ("contribution margin $/day",
                     lambda c: -c.forecast.contribution),
    "margin": ("margin as % of revenue",
               lambda c: -(c.forecast.absorbed / c.forecast.revenue
                           if c.forecast.revenue > 0 else -INF)),
    "demand": ("market size, pax/day",
               lambda c: -c.forecast.demand_per_day),
    "frequency": ("achievable rotations/day",
                  lambda c: -c.frequency.rotations),
    "load": ("forecast load factor",
             lambda c: -c.forecast.load_factor),
    "distance": ("stage length, nearest first",
                 lambda c: c.forecast.distance_km),
}
DEFAULT_RANK = "absorbed"


@dataclass(frozen=True)
class Candidate:
    """One destination, with the best aircraft for it."""
    dest: str
    dest_name: str
    lat: float
    lon: float
    spec_id: str
    source: str
    forecast: RouteForecast
    frequency: FrequencyPlan
    suitability: tuple
    operable: bool
    alternatives: int          # how many other types could also fly it

    def to_json(self) -> dict:
        return {"dest": self.dest, "dest_name": self.dest_name,
                "lat": self.lat, "lon": self.lon,
                "spec_id": self.spec_id, "source": self.source,
                "operable": self.operable,
                "alternatives": self.alternatives,
                "suitability": list(self.suitability),
                "forecast": self.forecast.to_json(),
                "frequency": self.frequency.to_json()}


@dataclass(frozen=True)
class DestinationPlan:
    origin: str
    tail_number: str
    rank_by: str
    rank_label: str
    scanned: int
    measured_only: bool
    candidates: tuple
    notes: tuple

    def to_json(self) -> dict:
        return {"origin": self.origin, "tail_number": self.tail_number,
                "rank_by": self.rank_by, "rank_label": self.rank_label,
                "scanned": self.scanned, "measured_only": self.measured_only,
                "rank_keys": {k: v[0] for k, v in RANK_KEYS.items()},
                "candidates": [c.to_json() for c in self.candidates],
                "notes": list(self.notes)}


def plan_from(world, players, player, *, origin=None, tail_number: str = "",
              rank_by: str = DEFAULT_RANK, limit: int = 25,
              measured_only: bool = False, service_tier: int = 2,
              fare_vs_reference: float = 1.0, bank=None) -> DestinationPlan:
    """
    "Where should this aeroplane fly?" or "what should I fly out of here?"

    Given a TAIL, the origin is that aircraft's base and the type is fixed —
    both are constraints, not preferences. `location_iata` is set at
    acquisition and no subsystem ever updates it, so a tail genuinely cannot
    start a route anywhere else, and offering destinations from another
    station would be offering a plan that cannot be executed.

    Given an AIRPORT, every type in the catalog is tried against every
    destination and the best one is reported per destination, with a count of
    the others that would also work.

    Read-only. Costs one corpus lookup per destination (~0.19 s over 300
    airports cold, nothing once `RouteDataProvider.route_spec`'s cache is
    warm) plus trivial arithmetic per type — so it must NOT run under
    `GameSession.lock`. See `GameSession.plan_from`.
    """
    from airlinesim.actions import METHOD_BY_NAME, TERMS_BY_METHOD, bank_for
    from airlinesim.engine import AirportSpec

    bank = bank or bank_for(world)
    plane = None
    if tail_number:
        plane = next((a for a in player.fleet
                      if a.tail_number == tail_number and not a.retired), None)
        if plane is None:
            raise KeyError(f"no such aircraft {tail_number}")
        origin = next((ap for ap in world.repo.all(AirportSpec)
                       if ap.iata == plane.location_iata), None)
        if origin is None:
            raise KeyError(f"{tail_number} is based at {plane.location_iata}, "
                           f"which is not in this world")
    if origin is None:
        raise ValueError("plan_from needs an origin airport or a tail number")

    rank_by = rank_by if rank_by in RANK_KEYS else DEFAULT_RANK
    rank_label, rank_fn = RANK_KEYS[rank_by]
    policy = player_policy(world, origin, service_tier=service_tier,
                           fare_vs_reference=fare_vs_reference)

    # Fixed by the tail if there is one; otherwise the whole catalog.
    specs = ([plane.spec] if plane is not None
             else sorted(world.repo.all(AircraftSpec), key=lambda s: s.spec_id))
    # Lease rate per type, once, rather than per destination.
    lease_daily = {}
    for s in specs:
        q = bank.quote(player, s, METHOD_BY_NAME["LEASE"],
                       TERMS_BY_METHOD[METHOD_BY_NAME["LEASE"]])
        lease_daily[s.spec_id] = q.daily

    candidates, scanned, skipped_tier = [], 0, 0
    for dest in sorted(world.repo.all(AirportSpec), key=lambda a: a.iata):
        if dest.iata == origin.iata:
            continue
        scanned += 1
        route_spec = route_spec_for(world, origin, dest)
        tier = getattr(route_spec, "data_tier", "")
        if measured_only and tier != "exact":
            skipped_tier += 1
            continue
        demand, _fare = market_estimate(world, origin, dest)
        share = expected_share(world, players, origin, dest, policy)

        best, workable = None, 0
        for spec in specs:
            owned = (plane is not None
                     or any(a.spec.spec_id == spec.spec_id and not a.retired
                            and a.location_iata == origin.iata
                            for a in player.fleet))
            ownership = 0.0 if owned else lease_daily.get(spec.spec_id, 0.0)
            fplan = frequency_plan(world, players, origin, dest, spec,
                                   demand_per_day=demand, share=share,
                                   player=player)
            fc = evaluate_route(world, players, origin, dest, spec,
                                max(1, fplan.rotations), policy,
                                ownership_per_day=ownership)
            suit = suitability_reasons(world, origin, dest, spec)
            ok = (fc.viable and fc.feasible and not suit
                  and fplan.rotations > 0)
            if ok:
                workable += 1
            cand = Candidate(
                dest=dest.iata, dest_name=dest.display_name,
                lat=dest.lat, lon=dest.lon, spec_id=spec.spec_id,
                source=(SOURCE_IDLE if owned else SOURCE_ACQUIRE),
                forecast=fc, frequency=fplan, suitability=suit, operable=ok,
                alternatives=0)
            # Prefer a workable option over a better-looking impossible one.
            if best is None or (ok, -rank_fn(cand)) > (best.operable,
                                                       -rank_fn(best)):
                best = cand
        if best is None:
            continue
        candidates.append(
            Candidate(**{**best.__dict__,
                         "alternatives": max(0, workable - 1)}))

    # Operable first, then the chosen key. A destination nothing can serve is
    # still returned — with its reason — rather than silently dropped.
    candidates.sort(key=lambda c: (not c.operable, rank_fn(c), c.dest))
    shown = tuple(candidates[:max(1, limit)])

    notes = [f"ranked by {rank_label}",
             f"{sum(1 for c in candidates if c.operable)} of {scanned} "
             f"destinations can be served from {origin.iata} as planned"]
    if plane is not None:
        notes.append(
            f"{plane.tail_number} is based at {origin.iata} and the engine "
            f"has no ferry flights, so every plan here starts there")
    est = sum(1 for c in shown if c.forecast.data_tier != "exact")
    if est:
        notes.append(
            f"{est} of the {len(shown)} shown rest on a FITTED demand "
            f"estimate rather than a BTS measurement — around a third of "
            f"those are out by more than 2x, so read this as a shortlist to "
            f"check, not a ranking to trust")
    if measured_only:
        notes.append(f"measured pairs only: {skipped_tier} estimated "
                     f"destinations were excluded")
    notes.append(
        "demand is CENSORED — the corpus counts passengers FLOWN, so the "
        "busiest pairs are understated and rank lower here than they should")
    return DestinationPlan(
        origin=origin.iata, tail_number=(plane.tail_number if plane else ""),
        rank_by=rank_by, rank_label=rank_label, scanned=scanned,
        measured_only=measured_only, candidates=shown, notes=tuple(notes))

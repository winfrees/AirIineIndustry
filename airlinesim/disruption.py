"""
DISRUPTION — what weather does to an airline.
=============================================

``weather.py`` says what the sky is doing. This module is the operational
half: it turns a sky into cancelled flights, hours of delay, crews that run
out of legal duty time, passengers who don't get home, and the bills that
follow. Keep the split — meteorology there, consequences here.

THE CHAIN, AND WHY IT IS A CHAIN
--------------------------------
The direct cost of weather is the smaller half. The expensive half is what it
sets off:

  weather  ->  reduced airport capacity        -> flights CANCELLED
           ->  longer taxi/hold/de-ice times   -> flights DELAYED
                                                     |
       delay consumes the crew's legal duty day  <---+
                                                     |
       crew times out -> the NEXT rotation cancels <-+   (indirect, and this
                                                          is the one that
                                                          turns a bad morning
                                                          into a bad week)

       cancelled + timed-out flights -> passengers STRANDED
                                              |
                       rebooked on your own later flights (cheap)
                       refunded and lost                  (expensive)
                       overnight -> HOTEL + MEALS         (expensive)
                       crew stuck away from base -> CREW HOTEL

Every one of those is charged to the ledger, so a network built through a
weather-exposed hub genuinely costs more to run than one that isn't — which
is the point. ``world.disruption_history`` accumulates per airport, so the
penalty is visible and grows over a season rather than being re-rolled daily.

TWO SUBSYSTEMS, ORDERED
-----------------------
``WeatherSubsystem`` runs BEFORE Operations: it advances the weather field
and annotates each route op with the capacity and delay it faces, which
Operations then applies when it decides how many rotations actually fly.

``DisruptionSubsystem`` runs AFTER Operations: Operations has by then
recorded what actually flew, so the shortfall against the schedule is known
and can be turned into passengers and money.

Splitting them is what keeps Operations the single place that decides how
much flying happens. A version that cancelled flights here would have two
authorities on the same number.

HONESTY
-------
Every figure in ``DisruptionCosts`` is a game-balance HEURISTIC in the shape
of the real thing (hotel and meal vouchers, denied-boarding compensation,
crew hotels). None is fitted to an airline's disclosed costs. The rebooking
model is deliberately simple — spare seats on the same carrier, same market,
same tick — and does NOT search alternate routings or other carriers; see
``docs/weather-design.md`` for what a real itinerary-rebooking model needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from airlinesim.engine import AirportSpec, Subsystem
from airlinesim.weather import WeatherKind, WeatherModel


# ============================================================
# COSTS — all HEURISTIC, all data rather than logic
# ============================================================

@dataclass(frozen=True)
class DisruptionCosts:
    """
    What a disrupted passenger and a stranded crew cost. Industry-SHAPED
    figures for game balance, not any carrier's disclosed numbers.
    """
    hotel_per_pax: float = 145.0          # a room, when the delay runs overnight
    meal_per_pax: float = 22.0            # meal vouchers during a long delay
    # Paid when the airline cannot re-seat someone and hands the fare back.
    # The refund itself is handled separately (the revenue is simply never
    # earned); this is the goodwill/compensation on top.
    compensation_per_pax: float = 190.0
    crew_hotel_per_head: float = 175.0    # a crew stuck away from base
    # Fraction of stranded passengers whose disruption runs overnight rather
    # than being cleared the same day. Rises with how long the airport is out.
    overnight_frac_base: float = 0.35
    # Delay past which passengers get fed even if they fly the same day.
    meal_delay_h: float = 3.0


DEFAULT_COSTS = DisruptionCosts()

# How much of a cancelled flight's seats would actually have been sold. Using
# the op's own recent load factor where there is one, this is the fallback for
# a route with no history yet.
ASSUMED_LOAD_FACTOR = 0.80

# A stranded passenger is re-seated on the same carrier's spare capacity in
# the same market. This caps how much of that spare capacity is usable — a
# flight that is already 85% full cannot absorb a whole cancelled flight, and
# in practice recovery is spread over days rather than taken all at once.
REBOOK_CAPTURE = 0.6


# ============================================================
# PER-CARRIER AND PER-AIRPORT ACCUMULATORS
# ============================================================

@dataclass
class DisruptionTally:
    """Running totals for one carrier. Reset only by a new game."""
    cancelled_flights: float = 0.0
    delayed_flights: float = 0.0
    delay_hours: float = 0.0
    stranded_pax: float = 0.0
    rebooked_pax: float = 0.0
    refunded_pax: float = 0.0
    hotel_cost: float = 0.0
    meal_cost: float = 0.0
    compensation_cost: float = 0.0
    crew_hotel_cost: float = 0.0
    lost_revenue: float = 0.0
    crew_timeouts: float = 0.0
    # Most recent tick, for the UI
    last_cancelled: float = 0.0
    last_stranded: float = 0.0
    last_cost: float = 0.0

    def total_cost(self) -> float:
        return (self.hotel_cost + self.meal_cost + self.compensation_cost
                + self.crew_hotel_cost)


@dataclass
class AirportDisruption:
    """
    How much trouble one airport has caused, cumulatively. This is what makes
    a hub choice have a long-run cost: a carrier based somewhere that closes
    every January pays for it every January, and can see that it did.
    """
    iata: str
    disrupted_hours: float = 0.0
    closed_hours: float = 0.0
    cancelled_flights: float = 0.0
    delay_hours: float = 0.0
    cost: float = 0.0
    worst_kind: str = ""
    worst_intensity: float = 0.0

    def reliability(self, elapsed_hours: float) -> float:
        """1.0 = never disrupted, 0.0 = always. What the UI shows as a score."""
        if elapsed_hours <= 0:
            return 1.0
        return max(0.0, 1.0 - self.disrupted_hours / elapsed_hours)


# ============================================================
# SUBSYSTEM 1 — advance the weather, annotate the ops
# ============================================================

class WeatherSubsystem(Subsystem):
    """
    Runs FIRST in the pipeline. Reads the weather field and writes, onto every
    route op, the capacity multiplier and delay it faces this tick. Nothing is
    decided here — Operations owns how much flying happens.
    """

    def __init__(self, model: WeatherModel, costs: DisruptionCosts = DEFAULT_COSTS):
        self.model = model
        self.costs = costs

    def tick(self, world, players, dt: float, ctx: dict):
        now = world.sim_time
        # Carry the stochastic process forward BEFORE anything reads the sky,
        # so this tick's flying faces this tick's weather.
        self.model.advance(now, dt)
        if not self.model.enabled:
            # Switched off mid-game (the explorer can do this at any node):
            # clear the annotations so ops go back to flying a clear sky
            # instead of being frozen at whatever the last storm left behind.
            for p in players:
                for op in p.route_ops:
                    op.weather_capacity = 1.0
                    op.weather_delay_h = 0.0
                    op.weather_kind = ""
                    op.weather_text = ""
            ctx["weather_systems"] = []
            ctx["weather_at"] = {}
            return
        systems = self.model.active(now)
        ctx["weather_systems"] = systems
        # One lookup per airport per tick, shared by every op that touches it:
        # a hub with thirty departures should not recompute its own sky thirty
        # times.
        seen: dict = {}

        def sky(iata):
            w = seen.get(iata)
            if w is None:
                # Averaged ACROSS the tick, not sampled at its first instant —
                # otherwise a storm shorter than the tick is invisible and the
                # amount of weather a game sees depends on its resolution.
                w = self.model.over(iata, now, dt)
                seen[iata] = w
            return w

        for p in players:
            for op in p.route_ops:
                o, d = sky(op.spec.origin_iata), sky(op.spec.dest_iata)
                enroute = self.model.enroute(op.spec.origin_iata, op.spec.dest_iata,
                                             now, systems=systems)
                # Both ends must work. A closed field at either end is a
                # cancelled flight, not a slow one.
                capacity = 0.0 if (o.closed or d.closed) else min(o.capacity_factor,
                                                                  d.capacity_factor)
                op.weather_capacity = capacity
                op.weather_delay_h = o.delay_h + d.delay_h + enroute
                worst = o if o.intensity >= d.intensity else d
                op.weather_kind = (worst.kind.name
                                   if worst.kind is not WeatherKind.CLEAR else "")
                op.weather_text = worst.describe() if worst.disrupted else ""

        ctx["weather_at"] = seen
        self._record_airports(world, seen, dt)

    def _record_airports(self, world, seen: dict, dt: float):
        hist = _history(world)
        for iata, w in seen.items():
            if not w.disrupted:
                continue
            rec = hist.get(iata) or AirportDisruption(iata)
            rec.disrupted_hours += dt
            if w.closed:
                rec.closed_hours += dt
            if w.intensity > rec.worst_intensity:
                rec.worst_intensity = w.intensity
                rec.worst_kind = w.kind.name
            hist[iata] = rec


def _history(world) -> dict:
    """The world's per-airport disruption ledger, created on first use so a
    world pickled before weather existed still loads."""
    hist = getattr(world, "disruption_history", None)
    if hist is None:
        hist = {}
        world.disruption_history = hist
    return hist


def tally_for(player) -> DisruptionTally:
    t = getattr(player, "disruption", None)
    if t is None:
        t = DisruptionTally()
        player.disruption = t
    return t


# ============================================================
# SUBSYSTEM 2 — consequences
# ============================================================

class DisruptionSubsystem(Subsystem):
    """
    Runs AFTER Operations, which has already recorded what actually flew.
    Turns the gap between schedule and reality into passengers and money.
    """

    def __init__(self, costs: DisruptionCosts = DEFAULT_COSTS):
        self.costs = costs

    def tick(self, world, players, dt: float, ctx: dict):
        day_frac = dt / 24.0
        hist = _history(world)
        for p in players:
            tally = tally_for(p)
            tally.last_cancelled = 0.0
            tally.last_stranded = 0.0
            tally.last_cost = 0.0
            # Spare seats this carrier still has in each market this tick —
            # what a stranded passenger could actually be re-seated onto.
            spare = self._spare_capacity(p, day_frac)

            for op in p.route_ops:
                cap = getattr(op, "weather_capacity", 1.0)
                delay = getattr(op, "weather_delay_h", 0.0)
                if cap >= 0.999 and delay <= 0.01:
                    op.last_weather_cancelled = 0.0
                    op.last_weather_cost = 0.0
                    continue

                scheduled = op.daily_frequency * day_frac
                flown = op.last_eff_freq * day_frac
                cancelled = max(0.0, scheduled - flown)
                # Only the share of the shortfall weather can be blamed for.
                # Gates and crew cancel flights too, and charging their
                # cancellations to the weather budget would double-count the
                # ones Operations already handled.
                weather_share = min(1.0, 1.0 - cap) if scheduled > 0 else 0.0
                wx_cancelled = cancelled * weather_share

                op.last_weather_cancelled = wx_cancelled
                tally.cancelled_flights += wx_cancelled
                tally.last_cancelled += wx_cancelled
                if flown > 0 and delay > 0.01:
                    tally.delayed_flights += flown
                    tally.delay_hours += delay * flown

                cost = 0.0
                if wx_cancelled > 0:
                    cost += self._strand(p, op, tally, wx_cancelled, spare, delay)
                if flown > 0 and delay >= self.costs.meal_delay_h:
                    # Everyone on a long-delayed flight gets fed.
                    fed = op.last_pax
                    meals = fed * self.costs.meal_per_pax
                    tally.meal_cost += meals
                    cost += meals

                if cost > 0:
                    p.ledger.debit(cost, f"disruption {op.spec.origin_iata}->"
                                         f"{op.spec.dest_iata}", p.log)
                op.last_weather_cost = cost
                tally.last_cost += cost
                self._charge_airport(hist, op, wx_cancelled, delay * max(flown, 0.0), cost)

            self._crew_away_from_base(world, p, tally, dt)

    # -- passengers ----------------------------------------------------
    def _spare_capacity(self, player, day_frac: float) -> dict:
        """
        Seats this carrier is flying but not selling, per market. A stranded
        passenger's best outcome is a seat on the operator's own next
        departure, so that is what the rebooking model looks for first.
        """
        from airlinesim.engine import market_key
        spare: dict = {}
        for op in player.route_ops:
            flown = op.last_eff_freq * day_frac
            if flown <= 0:
                continue
            seats = op.effective_layout().total_seats() * flown
            free = max(0.0, seats - op.last_pax)
            spare[market_key(op.spec)] = spare.get(market_key(op.spec), 0.0) + free
        return spare

    def _strand(self, player, op, tally, cancelled_flights: float,
                spare: dict, delay_h: float) -> float:
        """
        Seat, refund or accommodate the people who were booked on the flights
        that didn't operate. Returns the cash cost.
        """
        from airlinesim.engine import market_key
        lf = op.last_load_factor if op.last_load_factor > 0 else ASSUMED_LOAD_FACTOR
        seats = op.effective_layout().total_seats()
        stranded = cancelled_flights * seats * min(1.0, max(0.0, lf))
        if stranded <= 0:
            return 0.0
        tally.stranded_pax += stranded
        tally.last_stranded += stranded

        key = market_key(op.spec)
        room = spare.get(key, 0.0) * REBOOK_CAPTURE
        rebooked = min(stranded, room)
        spare[key] = max(0.0, spare.get(key, 0.0) - rebooked / max(REBOOK_CAPTURE, 1e-6))
        left = stranded - rebooked
        tally.rebooked_pax += rebooked
        tally.refunded_pax += left

        # The fare on a seat that never flew is revenue the airline simply
        # never earns; it is not an extra debit. Tracked for reporting so the
        # player can see the hole, not charged twice.
        tally.lost_revenue += left * op.ticket_price

        overnight_frac = min(0.95, self.costs.overnight_frac_base + 0.08 * delay_h)
        overnight = left * overnight_frac
        hotels = overnight * self.costs.hotel_per_pax
        meals = left * self.costs.meal_per_pax
        comp = left * self.costs.compensation_per_pax
        tally.hotel_cost += hotels
        tally.meal_cost += meals
        tally.compensation_cost += comp
        return hotels + meals + comp

    # -- crew ----------------------------------------------------------
    def _crew_away_from_base(self, world, player, tally, dt: float):
        """
        A crew that is out of position and out of legal duty hours has to be
        put up for the night. This is the indirect cost the whole model exists
        to capture: the delay itself was cheap, the crew it stranded was not,
        and tomorrow's flight has nobody to operate it.
        """
        cost = 0.0
        for crew in list(getattr(player, "cockpit_pool", [])) + \
                list(getattr(player, "cabin_pool", [])):
            duty = getattr(crew, "duty", None)
            if duty is None or not crew.home_iata:
                continue
            if crew.location_iata == crew.home_iata:
                continue
            if not getattr(duty, "resting", False):
                continue
            cost += crew.headcount * self.costs.crew_hotel_per_head * (dt / 24.0)
        if cost > 0:
            tally.crew_hotel_cost += cost
            tally.last_cost += cost
            player.ledger.debit(cost, "crew hotels (out of base)", player.log)

    def _charge_airport(self, hist: dict, op, cancelled: float,
                        delay_hours: float, cost: float):
        for iata in (op.spec.origin_iata, op.spec.dest_iata):
            rec = hist.get(iata) or AirportDisruption(iata)
            rec.cancelled_flights += cancelled * 0.5
            rec.delay_hours += delay_hours * 0.5
            rec.cost += cost * 0.5
            hist[iata] = rec


# ============================================================
# WIRING
# ============================================================

def attach_weather(world, engine, seed=None,
                   costs: DisruptionCosts = DEFAULT_COSTS,
                   enabled: bool = True) -> WeatherModel:
    """
    Give a world weather. Builds the model over its airports and slots both
    subsystems into the pipeline at the two points that matter:

        WeatherSubsystem   FIRST — before RouteSuitability and Operations, so
                           every op knows what it faces before anything
                           decides how much of the schedule operates.
        DisruptionSubsystem LAST — after Operations has recorded what flew, so
                           the shortfall is known and can be paid for.

    Opt-in on purpose: a world without it runs exactly as it did, which is
    what keeps the existing scenarios comparable and lets the weather
    scenario A/B the same world with and without a sky.

    `seed=None` draws a fresh one, so each new game gets its own season.
    Pass a seed to replay a specific one. `enabled=False` attaches the
    machinery dormant, which is how the explorer can switch weather on at a
    node partway down a branch.
    """
    model = WeatherModel.for_world(world, seed=seed, enabled=enabled)
    world.weather = model
    engine.subsystems.insert(0, WeatherSubsystem(model, costs))
    engine.subsystems.append(DisruptionSubsystem(costs))
    return model


# ============================================================
# PROJECTION
# ============================================================

def disruption_snapshot(world, player) -> dict:
    t = tally_for(player)
    return {
        "cancelled_flights": round(t.cancelled_flights, 1),
        "delay_hours": round(t.delay_hours, 1),
        "stranded_pax": round(t.stranded_pax),
        "rebooked_pax": round(t.rebooked_pax),
        "refunded_pax": round(t.refunded_pax),
        "hotel_cost": round(t.hotel_cost),
        "meal_cost": round(t.meal_cost),
        "compensation_cost": round(t.compensation_cost),
        "crew_hotel_cost": round(t.crew_hotel_cost),
        "total_cost": round(t.total_cost()),
        "last_cancelled": round(t.last_cancelled, 2),
        "last_stranded": round(t.last_stranded, 1),
        "last_cost": round(t.last_cost),
    }


def airport_reliability(world, iatas=None) -> dict:
    """Per-airport disruption record — the "this hub costs you" view."""
    hist = _history(world)
    elapsed = max(1.0, world.sim_time)
    out = {}
    for iata, rec in hist.items():
        if iatas is not None and iata not in iatas:
            continue
        out[iata] = {
            "reliability": round(rec.reliability(elapsed), 3),
            "disrupted_hours": round(rec.disrupted_hours, 1),
            "closed_hours": round(rec.closed_hours, 1),
            "cancelled_flights": round(rec.cancelled_flights, 1),
            "cost": round(rec.cost),
            "worst": rec.worst_kind,
        }
    return out

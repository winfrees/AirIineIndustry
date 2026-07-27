"""
AI CARRIERS — competitors that actually run an airline.
=======================================================

The original ``AIStrategySubsystem`` (engine.py) only moved price and
frequency on routes somebody else had opened for it. It defended a network;
it could not build one. This module gives AI carriers the rest of the job:

  NETWORK   open profitable routes, close persistently losing ones
  FLEET     lease/buy aircraft when there's flying to do, shed idle metal
  PRODUCT   pick a cabin layout at acquisition, pick a service tier
  STAFFING  hire crew when routes are grounded for want of a legal crew
  HUBS      make sure there's somewhere to do maintenance

Everything goes through ``actions.py`` — the same functions the human's
commands call. The AI passes the same equipment validation, the same credit
gate, the same fees, the same teardown. It has no private mutators and no
information the player can't also see, so it cannot cheat by construction.

ARCHETYPES
----------
One policy engine, several personalities. A Low-Cost carrier floods dense
short-haul with dense all-economy metal and undercuts on price; a Legacy
carrier defends premium cabins at big hubs and buys service quality; a
Regional carrier connects thinner markets with small aircraft. They are the
same code with different weights, which keeps the behavior consistent and
makes each opponent feel distinct and — importantly — counterable.

CADENCE
-------
Network and fleet reviews are expensive decisions and would thrash if run
every tick, so each runs on its own multi-day cycle, staggered per carrier
so competitors don't all move on the same day. Pricing stays per-tick,
because that's the fast-moving competitive signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from airlinesim import actions
from airlinesim.engine import (
    AircraftSpec, AirportSpec, PlaneClass, Subsystem, World, market_key,
)
from airlinesim.finance_cabin import CabinClass, aircraft_value


# ============================================================
# ARCHETYPES — the personality parameter sets
# ============================================================

@dataclass(frozen=True)
class Archetype:
    """
    A competitor's playing style. All game-balance figures, tunable data —
    no strategy logic lives in here, only weights the shared policy reads.
    """
    name: str
    blurb: str

    # --- pricing ---
    price_step: float = 0.03           # hill-climb step per tick
    margin_floor_mult: float = 1.15    # never price below unit cost * this
    undercut_frac: float = 0.99        # shade to this fraction of a rival's fare
    price_floor: float = 60.0
    price_ceiling: float = 900.0
    fare_vs_reference: float = 1.0     # opening fare as a multiple of market ref

    # --- capacity ---
    lf_add: float = 0.85               # add a rotation above this load factor
    lf_cut: float = 0.45               # cut one below it
    max_freq_per_plane: int = 6

    # --- network ---
    network_review_days: int = 7
    candidates_per_review: int = 14
    max_routes_per_plane: float = 1.0  # network size scales with fleet size
    min_stage_km: float = 250.0
    max_stage_km: float = 6000.0
    min_est_daily_profit: float = 8_000.0    # bar a candidate must clear to open
    close_below_daily_profit: float = -6_000.0
    bad_days_before_close: int = 21

    # --- fleet ---
    fleet_review_days: int = 14
    min_cash_buffer: float = 25_000_000.0    # never spend below this
    # Growth ceiling. Must leave headroom above the fleet a carrier STARTS
    # with, or it can never acquire, never has an idle aircraft to deploy, and
    # so never opens a route — a cap set at the starting size reads as "this
    # archetype is broken" rather than "this archetype is disciplined".
    max_fleet: int = 20
    acquisition_method: str = "LEASE"
    plane_classes: tuple = (PlaneClass.NARROWBODY,)
    idle_days_before_shedding: int = 21

    # --- product ---
    service_tier: int = 2
    business_seat_frac: float = 0.0    # share of cabin slots given to business
    premium_seat_frac: float = 0.0

    # --- hubs ---
    # A hub costs daily overhead and buys preferential gates plus the only
    # place this carrier can do maintenance. A network carrier wants several;
    # a low-cost point-to-point operator deliberately runs lean on bases.
    max_hubs: int = 1
    hub_min_routes_each: int = 4       # routes per hub before opening another


LOW_COST = Archetype(
    name="Low-Cost",
    blurb="dense all-economy metal, basic service, undercuts on fare",
    price_step=0.04, margin_floor_mult=1.08, undercut_frac=0.96,
    fare_vs_reference=0.82, price_ceiling=520.0,
    lf_add=0.82, lf_cut=0.40, max_freq_per_plane=8,
    network_review_days=6, candidates_per_review=16,
    min_stage_km=300.0, max_stage_km=4200.0,
    min_est_daily_profit=6_000.0, bad_days_before_close=14,
    fleet_review_days=12, min_cash_buffer=18_000_000.0, max_fleet=24,
    acquisition_method="LEASE", plane_classes=(PlaneClass.NARROWBODY,),
    service_tier=1, business_seat_frac=0.0, premium_seat_frac=0.0,
    max_hubs=1, hub_min_routes_each=99,
)

LEGACY = Archetype(
    name="Legacy",
    blurb="hub-and-premium: business cabins, top service tier, yield over volume",
    price_step=0.025, margin_floor_mult=1.25, undercut_frac=1.0,
    fare_vs_reference=1.12, price_ceiling=1_400.0,
    lf_add=0.88, lf_cut=0.48, max_freq_per_plane=5,
    network_review_days=9, candidates_per_review=12,
    min_stage_km=600.0, max_stage_km=12_000.0,
    min_est_daily_profit=12_000.0, bad_days_before_close=28,
    fleet_review_days=18, min_cash_buffer=40_000_000.0, max_fleet=20,
    acquisition_method="LEASE",
    plane_classes=(PlaneClass.NARROWBODY, PlaneClass.WIDEBODY),
    service_tier=3, business_seat_frac=0.14, premium_seat_frac=0.10,
    max_hubs=3, hub_min_routes_each=4,
)

REGIONAL = Archetype(
    name="Regional",
    blurb="thin short-haul markets small jets can serve profitably",
    price_step=0.035, margin_floor_mult=1.18, undercut_frac=0.98,
    fare_vs_reference=1.0, price_ceiling=620.0,
    lf_add=0.80, lf_cut=0.42, max_freq_per_plane=7,
    network_review_days=7, candidates_per_review=18,
    min_stage_km=200.0, max_stage_km=2_400.0,
    min_est_daily_profit=3_500.0, bad_days_before_close=18,
    fleet_review_days=14, min_cash_buffer=12_000_000.0, max_fleet=22,
    acquisition_method="LEASE",
    plane_classes=(PlaneClass.REGIONAL, PlaneClass.NARROWBODY),
    service_tier=2, business_seat_frac=0.0, premium_seat_frac=0.06,
    max_hubs=2, hub_min_routes_each=6,
)

ARCHETYPES = {a.name: a for a in (LOW_COST, LEGACY, REGIONAL)}
DEFAULT_ARCHETYPE = LOW_COST


# ============================================================
# PER-CARRIER MEMORY
# ============================================================

@dataclass
class CarrierMemory:
    """
    What the AI remembers between ticks. Kept here rather than on engine
    dataclasses so the AI's bookkeeping never leaks into the simulation's
    own state (and so a world with no AI carries no AI baggage).
    """
    archetype: Archetype = DEFAULT_ARCHETYPE
    bad_days: dict = field(default_factory=dict)     # route_op_id -> consecutive bad days
    idle_days: dict = field(default_factory=dict)    # tail_number -> consecutive idle days
    tried: set = field(default_factory=set)          # pairs evaluated and rejected
    tail_seq: int = 0
    next_network_review: float = 0.0
    next_fleet_review: float = 0.0
    recent: list = field(default_factory=list)       # human-readable move log


# ============================================================
# THE SUBSYSTEM
# ============================================================

class AICarrierSubsystem(Subsystem):
    """
    Full-airline AI. Runs BEFORE Operations so decisions take effect on the
    same tick, matching AIStrategySubsystem's ordering contract.

    Assign archetypes with ``profiles={player_id: "Legacy"}``; carriers with
    no assignment get ``default_archetype``.
    """

    def __init__(self, profiles: Optional[dict] = None,
                 default_archetype: str = "Low-Cost", enabled: bool = True):
        self.profiles = dict(profiles or {})
        self.default_archetype = default_archetype
        self.enabled = enabled
        self.memory: dict = {}      # player_id -> CarrierMemory
        self._players: list = []    # set each tick; read by route evaluation

    # -- memory ------------------------------------------------------------
    def _mem(self, player, world) -> CarrierMemory:
        m = self.memory.get(player.player_id)
        if m is None:
            arch = ARCHETYPES.get(self.profiles.get(player.player_id,
                                                    self.default_archetype),
                                  DEFAULT_ARCHETYPE)
            m = CarrierMemory(archetype=arch)
            # Stagger the first reviews so competitors don't all restructure
            # on the same day — otherwise every AI opens routes in lockstep.
            spread = (abs(hash(player.player_id)) % 5) * 24.0
            m.next_network_review = world.sim_time + spread
            m.next_fleet_review = world.sim_time + spread + 48.0
            self.memory[player.player_id] = m
        return m

    def _note(self, player, mem: CarrierMemory, msg: str):
        player.log.append(f"  [AI:{mem.archetype.name}] {msg}")
        mem.recent.append(msg)
        del mem.recent[:-12]

    # -- tick --------------------------------------------------------------
    def tick(self, world: World, players: list, dt: float, ctx: dict):
        if not self.enabled:
            return
        self._players = players
        rivals = self._rival_prices(players)
        for p in players:
            if not p.is_ai:
                continue
            mem = self._mem(p, world)
            arch = mem.archetype

            self._price_and_capacity(world, p, mem, arch, rivals)
            self._track_health(world, p, mem, arch, dt)

            if world.sim_time >= mem.next_network_review:
                mem.next_network_review = world.sim_time + arch.network_review_days * 24.0
                self._ensure_hub(world, p, mem)
                self._align_product(world, p, mem, arch)
                self._close_bad_routes(world, p, mem, arch)
                self._staff_up(world, p, mem)
                self._deploy_idle_aircraft(world, p, mem, arch)

            if world.sim_time >= mem.next_fleet_review:
                mem.next_fleet_review = world.sim_time + arch.fleet_review_days * 24.0
                self._fleet_review(world, p, mem, arch)

    # ------------------------------------------------------------------
    # PRICING + CAPACITY (per tick)
    # ------------------------------------------------------------------
    def _rival_prices(self, players) -> dict:
        out: dict = {}
        for p in players:
            for op in p.route_ops:
                out.setdefault(market_key(op.spec), []).append((p.player_id, op.ticket_price))
        return out

    # Below this many passengers, "cost per passenger" is arithmetic noise:
    # a route that carried two people divides a full day of fuel, crew and
    # fees by two. Pricing off that number is how a cost-plus rule runs away.
    MIN_PAX_FOR_UNIT_COST = 5.0

    @staticmethod
    def _unit_cost(op) -> float:
        """
        Variable cost per passenger from last tick's actuals, fees included.
        Returns 0.0 (meaning "no usable estimate") when the sample is too thin
        to divide by — see MIN_PAX_FOR_UNIT_COST.
        """
        if op.last_pax >= AICarrierSubsystem.MIN_PAX_FOR_UNIT_COST \
                and op.last_variable_cost > 0:
            return (op.last_variable_cost + getattr(op, "last_fees", 0.0)) / op.last_pax
        return 0.0

    def _price_and_capacity(self, world, p, mem, arch, rivals):
        """
        Profit hill-climb, same shape as AIStrategySubsystem: if the last move
        improved profit keep going, otherwise reverse. Archetype-flavored via
        step size, margin floor and how hard it undercuts.
        """
        for op in list(p.route_ops):
            old = op.ticket_price
            unit_cost = self._unit_cost(op)
            cost_floor = max(arch.price_floor, unit_cost * arch.margin_floor_mult)

            if op.prev_price > 0 and op.last_profit <= op.prev_profit:
                op.price_dir *= -1          # overshot the peak, turn around
            target = old * (1 + op.price_dir * arch.price_step)

            others = [pr for (pid, pr) in rivals.get(market_key(op.spec), [])
                      if pid != p.player_id]
            if others and arch.undercut_frac < 1.0:
                cheapest = min(others)
                shaded = cheapest * arch.undercut_frac
                if cheapest < old and shaded > cost_floor:
                    target = min(target, shaded)

            # The CEILING is the outermost bound, deliberately. Clamping the
            # cost floor last lets a thin route bid its own fare upward without
            # limit: costs spread over few passengers raise the floor, the
            # higher fare sheds more passengers, and the loop diverges — it
            # reached nine-figure fares before this was pinned down. A cost
            # floor above what the market bears means the route is unviable,
            # which _close_bad_routes is what answers; it is never a reason to
            # price beyond the ceiling.
            target = min(arch.price_ceiling, max(cost_floor, target))
            op.prev_profit = op.last_profit
            op.prev_price = old
            actions.set_price(world, p, actions.op_id(op), round(target, 2))

            # --- capacity ---
            old_freq = op.daily_frequency
            gate_denied = op.last_eff_freq < old_freq - 1e-6
            new_freq = old_freq
            if op.last_pax <= 0 and op.last_eff_freq <= 0:
                pass                        # no operating history yet
            elif gate_denied:
                new_freq = max(1, int(round(op.last_eff_freq)))
            elif (op.last_load_factor >= arch.lf_add and op.last_profit > 0
                  and old_freq < arch.max_freq_per_plane):
                new_freq = old_freq + 1
            elif op.last_load_factor <= arch.lf_cut and old_freq > 1:
                new_freq = old_freq - 1
            if new_freq != old_freq:
                actions.set_frequency(world, p, actions.op_id(op), new_freq)

    # ------------------------------------------------------------------
    # HEALTH TRACKING (per tick, feeds the review cycles)
    # ------------------------------------------------------------------
    def _track_health(self, world, p, mem, arch, dt):
        days = dt / 24.0
        flying = set()
        for op in p.route_ops:
            oid = actions.op_id(op)
            net = op.last_profit - getattr(op, "last_fees", 0.0)
            if op.last_eff_freq > 0 or op.last_pax > 0:
                flying.add(op.plane.tail_number)
            if net < arch.close_below_daily_profit:
                mem.bad_days[oid] = mem.bad_days.get(oid, 0) + days
            else:
                mem.bad_days.pop(oid, None)
        for a in p.fleet:
            if a.tail_number in flying:
                mem.idle_days.pop(a.tail_number, None)
            else:
                mem.idle_days[a.tail_number] = mem.idle_days.get(a.tail_number, 0) + days

    # ------------------------------------------------------------------
    # HUBS + STAFFING
    # ------------------------------------------------------------------
    def _ensure_hub(self, world, p, mem):
        """
        Hub policy. The first hub is survival: without one there is nowhere to
        do maintenance and the fleet eventually flies on risk. Beyond that a
        hub is a real trade — daily overhead against preferential gates and
        maintenance reach at a second station — so how many to run is an
        archetype decision, not a universal one.
        """
        arch = mem.archetype
        if not p.hub_iatas:
            base = p.fleet[0].location_iata if p.fleet else None
            for iata in ([base] if base else []) + [
                    ap.iata for ap in world.repo.all(AirportSpec)
                    if ap.has_maintenance_facility]:
                if not iata:
                    continue
                ok, msg = actions.set_hub(world, p, iata, True)
                if ok:
                    self._note(p, mem, msg)
                    return
            return

        if len(p.hub_iatas) >= arch.max_hubs:
            return
        # Only add a base once the existing ones are carrying real traffic —
        # a second hub bought too early is pure overhead.
        if len(p.route_ops) < arch.hub_min_routes_each * len(p.hub_iatas):
            return
        cand = self._best_new_hub(world, p)
        if cand is None:
            return
        ap = actions.airport(world, cand)
        # don't take on overhead the carrier can't carry for long
        if ap is not None and ap.hub_fee_per_day * 60 > p.ledger.cash:
            return
        ok, msg = actions.set_hub(world, p, cand, True)
        if ok:
            self._note(p, mem, f"{msg} — second base for "
                               f"{len(p.route_ops)} routes")

    def _best_new_hub(self, world, p):
        """
        The station this carrier already flies to most, excluding current
        hubs. Basing where the network already is turns overhead into
        preferential gates on routes it actually operates.
        """
        traffic = {}
        for op in p.route_ops:
            if op.spec.dest_iata in p.hub_iatas:
                continue
            traffic[op.spec.dest_iata] = traffic.get(op.spec.dest_iata, 0) + 1
        for iata, _n in sorted(traffic.items(), key=lambda kv: -kv[1]):
            ap = actions.airport(world, iata)
            if ap is not None and ap.has_maintenance_facility:
                return iata
        return None

    def _align_product(self, world, p, mem, arch):
        """
        Run ONE product standard across the network. A carrier that sells
        basic service on the routes it opened and standard service on the
        ones it inherited isn't running a strategy, it's running whatever it
        was handed — and the archetype stops being legible to the player.
        Service tier is a per-route cost, so this is also a real spend
        decision, not cosmetic.
        """
        changed = 0
        for op in p.route_ops:
            if op.service_tier != arch.service_tier:
                ok, _ = actions.set_service_tier(world, p, actions.op_id(op),
                                                 arch.service_tier)
                changed += bool(ok)
        if changed:
            self._note(p, mem, f"moved {changed} route(s) to service tier "
                               f"{arch.service_tier}")

    def _staff_up(self, world, p, mem):
        """
        Hire against routes that reported a crew block. Rostering is what
        turns an opened route into an operated one, so an AI that expands
        without hiring just accumulates grounded schedules.
        """
        blocked = [op for op in p.route_ops
                   if op.last_crew_block or op.cockpit is None or op.cabin is None]
        if not blocked:
            return
        base = p.hub_iatas[0] if p.hub_iatas else (
            p.fleet[0].location_iata if p.fleet else "")
        if not base:
            return
        ratings = tuple({a.spec.type_rating for a in p.fleet if a.spec.type_rating})
        # one crew set per blocked route, capped so a bad tick can't trigger
        # a hiring spree the carrier can't pay for
        for _ in range(min(len(blocked), 2)):
            ok, _msg = actions.hire_crew(world, p, "COCKPIT", base, 2, 220, ratings)
            actions.hire_crew(world, p, "CABIN", base, 4, 60, ())
        self._note(p, mem, f"hired crew at {base} for {len(blocked)} blocked route(s)")

    # ------------------------------------------------------------------
    # NETWORK
    # ------------------------------------------------------------------
    def _close_bad_routes(self, world, p, mem, arch):
        for op in list(p.route_ops):
            oid = actions.op_id(op)
            if mem.bad_days.get(oid, 0) >= arch.bad_days_before_close:
                ok, msg = actions.close_route(world, p, oid)
                if ok:
                    mem.bad_days.pop(oid, None)
                    mem.tried.add(f"{op.spec.origin_iata}-{op.spec.dest_iata}")
                    self._note(p, mem, f"{msg} — {arch.bad_days_before_close}d unprofitable")

    def _idle_tails(self, p, mem):
        busy = {op.plane.tail_number for op in p.route_ops}
        return [a for a in p.fleet if a.tail_number not in busy and not a.retired]

    def _deploy_idle_aircraft(self, world, p, mem, arch):
        """Find the best unserved market each idle aircraft can profitably fly."""
        idle = self._idle_tails(p, mem)
        if not idle:
            return
        max_routes = int(arch.max_routes_per_plane * len(p.fleet)) + 1
        if len(p.route_ops) >= max_routes:
            return
        for plane in idle[:2]:              # at most two openings per review
            cand = self._best_candidate(world, p, mem, arch, plane)
            if cand is None:
                continue
            pair, est_profit, ref_fare = cand
            price = round(ref_fare * arch.fare_vs_reference, 2)
            ok, msg = actions.open_route(
                world, p, pair, plane.tail_number, price,
                freq=max(1, min(3, arch.max_freq_per_plane - 2)),
                service_tier=arch.service_tier)
            if ok:
                self._note(p, mem, f"{msg} @ ${price:,.0f} "
                                   f"(est ${est_profit:,.0f}/day)")
            else:
                mem.tried.add(pair)

    def _origins(self, p) -> list:
        """Where this carrier can realistically start a route from."""
        out = list(p.hub_iatas)
        for a in p.fleet:
            if a.location_iata and a.location_iata not in out:
                out.append(a.location_iata)
        return out

    def _best_candidate(self, world, p, mem, arch, plane):
        """
        Score unserved airport pairs this aircraft could fly and return the
        best one clearing the archetype's profit bar, as
        ``(pair, est_daily_profit, reference_fare)``.
        """
        served = {f"{o.spec.origin_iata}-{o.spec.dest_iata}" for o in p.route_ops}
        airports = world.repo.all(AirportSpec)
        best = None
        checked = 0
        for origin_iata in self._origins(p):
            origin = actions.airport(world, origin_iata)
            if origin is None:
                continue
            for dest in airports:
                if checked >= arch.candidates_per_review:
                    break
                if dest.iata == origin_iata:
                    continue
                pair = f"{origin_iata}-{dest.iata}"
                if pair in served or pair in mem.tried:
                    continue
                est = self._evaluate(world, p, arch, plane, origin, dest)
                checked += 1
                if est is None:
                    mem.tried.add(pair)     # structurally impossible, don't retry
                    continue
                profit, ref_fare = est
                if profit < arch.min_est_daily_profit:
                    continue
                if best is None or profit > best[1]:
                    best = (pair, profit, ref_fare)
        return best

    def _market_estimate(self, world, origin, dest, dist):
        """
        (daily demand, reference fare) for a candidate pair, from the same
        corpus the player's route-opening consults — measured where BTS has
        the pair, a fitted estimate where it doesn't. The AI gets no private
        oracle: if the data is a guess for the player, it's a guess here too.
        """
        spec = _spec_for(world, origin, dest)
        if spec is not None:
            fare = getattr(spec, "reference_price", 0.0) or 0.0
            if not fare:
                provider = getattr(world, "route_data", None)
                if provider is not None:
                    fare, _src = provider.suggested_price(origin.iata, dest.iata)
            return float(spec.base_demand_per_day), float(fare or 200.0)
        return 400.0, 200.0

    def _evaluate(self, world, p, arch, plane, origin, dest):
        """
        Estimate a candidate's daily profit. Returns ``(profit, ref_fare)``,
        or None if the aircraft physically can't serve the pair.

        Deliberately a rough forecast, not a simulation: it prices the
        aircraft's own seats against a share of the market and subtracts the
        costs the engine will actually charge (fuel, crew, landing, gate,
        amenities, baggage). It does NOT model how rivals will respond — an
        AI that could perfectly predict the arbiter would be unbeatable, and
        the resulting errors are what make it possible to out-plan.
        """
        from airlinesim.route import haversine, block_hours, service_desirability

        dist = haversine(origin.lat, origin.lon, dest.lat, dest.lon)
        if dist < arch.min_stage_km or dist > arch.max_stage_km:
            return None
        spec = plane.spec
        if spec.max_range_km < dist:
            return None
        if (origin.runway_length_m < spec.takeoff_runway_m
                or dest.runway_length_m < spec.takeoff_runway_m):
            return None

        # Demand and a starting fare come from the same corpus the player's
        # route-opening uses — measured where BTS has the pair, a fitted
        # estimate where it doesn't. No private AI oracle.
        demand, ref_fare = self._market_estimate(world, origin, dest, dist)

        freq = max(1, min(3, arch.max_freq_per_plane - 2))
        seats = spec.max_seats * freq

        # share of the market this offer can expect: every operator already
        # in the metro-pair dilutes it, desirability lifts it
        mkey = market_key_for(world, origin, dest)
        incumbents = sum(1 for pl in self._players
                         for o in pl.route_ops if market_key(o.spec) == mkey)
        desir = service_desirability(arch.service_tier, origin.access_index,
                                     dest.access_index)
        share = desir / (1.0 + incumbents)
        pax = min(seats * 0.75, demand * share)
        if pax <= 0:
            return None

        fare = ref_fare * arch.fare_vs_reference
        revenue = pax * fare

        bh = block_hours(dist, spec.cruise_speed_kmh) * freq
        fuel = spec.fuel_burn_lph * bh * 0.9
        crew = (220 * 2 + 60 * 4) * bh
        maint = spec.maint_cost_per_hour * bh
        fees = 0.0
        for ap, landings in ((dest, freq), (origin, 0)):
            fees += ap.landing_fee * landings
            fees += ap.fee_at_tier(ap.gate_fee_by_tier, arch.service_tier) * freq
            fees += (ap.fee_at_tier(ap.amenities_fee_by_tier, arch.service_tier)
                     + ap.fee_at_tier(ap.baggage_fee_by_tier, arch.service_tier)) * pax
        return revenue - (fuel + crew + maint + fees), ref_fare

    # ------------------------------------------------------------------
    # FLEET
    # ------------------------------------------------------------------
    def _fleet_review(self, world, p, mem, arch):
        self._shed_idle(world, p, mem, arch)
        self._maybe_acquire(world, p, mem, arch)

    def _shed_idle(self, world, p, mem, arch):
        """Metal that isn't flying still costs rent or capital — let it go."""
        for a in list(p.fleet):
            if mem.idle_days.get(a.tail_number, 0) < arch.idle_days_before_shedding:
                continue
            if len(p.fleet) <= 1:
                break                        # never sell the last aircraft
            fn = actions.break_lease if not a.owned else actions.sell_aircraft
            ok, msg = fn(world, p, a.tail_number)
            if ok:
                mem.idle_days.pop(a.tail_number, None)
                self._note(p, mem, f"{msg} — idle "
                                   f"{arch.idle_days_before_shedding}d")

    def _cabin_for(self, spec, arch) -> Optional[dict]:
        """
        Cabin configuration at acquisition — the cheap moment to choose it.
        Business/premium seats consume more slots than they add in count, so
        the economy cabin shrinks by each cabin's footprint.
        """
        from airlinesim.finance_cabin import DEFAULT_SEAT_CLASSES, cabin_slots_for
        if arch.business_seat_frac <= 0 and arch.premium_seat_frac <= 0:
            return None                      # all-economy: let the default stand
        slots = cabin_slots_for(spec.max_seats)
        biz = int(slots * arch.business_seat_frac
                  / DEFAULT_SEAT_CLASSES[CabinClass.BUSINESS].footprint)
        prem = int(slots * arch.premium_seat_frac
                   / DEFAULT_SEAT_CLASSES[CabinClass.PREMIUM].footprint)
        used = (biz * DEFAULT_SEAT_CLASSES[CabinClass.BUSINESS].footprint
                + prem * DEFAULT_SEAT_CLASSES[CabinClass.PREMIUM].footprint)
        econ = int(max(0.0, slots - used))
        if econ <= 0:
            return None
        cabin = {"ECONOMY": econ}
        if biz > 0:
            cabin["BUSINESS"] = biz
        if prem > 0:
            cabin["PREMIUM"] = prem
        return cabin

    def _maybe_acquire(self, world, p, mem, arch):
        """
        Add an aircraft when the network is fully deployed, there's cash
        headroom, and the existing routes are actually making money — the AI
        expands off demonstrated profit, not hope.
        """
        if len(p.fleet) >= arch.max_fleet:
            return
        if self._idle_tails(p, mem):
            return                           # deploy what's already owned first
        if p.ledger.cash < arch.min_cash_buffer:
            return
        net = sum(op.last_profit - getattr(op, "last_fees", 0.0) for op in p.route_ops)
        if p.route_ops and net <= 0:
            return                           # not earning; don't add cost

        spec = self._pick_aircraft(world, p, arch)
        if spec is None:
            return
        mem.tail_seq += 1
        tail = f"{p.player_id}-{mem.tail_seq:03d}"
        base = p.hub_iatas[0] if p.hub_iatas else (
            p.fleet[0].location_iata if p.fleet else None)
        ok, msg = actions.acquire_aircraft(
            world, p, spec.spec_id, tail, arch.acquisition_method, base,
            seats=self._cabin_for(spec, arch))
        if ok:
            self._note(p, mem, msg)
            # a new type needs rated pilots or it will never be rostered
            if spec.type_rating and base:
                rated = any(spec.type_rating in (c.spec.certifications or ())
                            for c in p.cockpit_pool)
                if not rated:
                    actions.hire_crew(world, p, "COCKPIT", base, 2, 220,
                                      (spec.type_rating,))
                    actions.hire_crew(world, p, "CABIN", base, 4, 60, ())

    # Ownership is expensed as an annual fraction of list price spread over a
    # working year, so capital and fuel land in the same units. Game-balance
    # figures, consistent with the lease rate in actions.LEASE_TERMS.
    OWNERSHIP_RATE_PER_YEAR = 0.11
    ANNUAL_BLOCK_HOURS = 3_000.0
    ASSUMED_FUEL_PRICE = 0.9

    def _pick_aircraft(self, world, p, arch):
        """
        Choose equipment on cost per available seat-km over the mission this
        carrier actually flies — the metric real fleet planners use — rather
        than sticker price. Capital, fuel and maintenance all count, so an
        old airframe with a cheap list price doesn't win on the strength of
        being cheap to acquire while burning the profit off in fuel.

        Restricted to the archetype's plane classes, to types that can make
        the stage length, and to what its hubs' runways can take. The upshot
        is that long-stage carriers grow into widebodies on their own,
        because that's genuinely where the seat-km economics go.
        """
        from airlinesim.route import block_hours

        stages = [o.spec.distance_km for o in p.route_ops]
        target = sum(stages) / len(stages) if stages else 1200.0
        runway = min((ap.runway_length_m for ap in
                      (actions.airport(world, i) for i in p.hub_iatas) if ap),
                     default=99_999.0)
        best, best_score = None, None
        for spec in world.repo.all(AircraftSpec):
            if spec.plane_class not in arch.plane_classes:
                continue
            if spec.max_range_km < target * 1.15:
                continue
            if spec.takeoff_runway_m > runway:
                continue
            if spec.max_seats <= 0 or spec.cruise_speed_kmh <= 0:
                continue
            hours = block_hours(target, spec.cruise_speed_kmh)
            trip = (spec.fuel_burn_lph * hours * self.ASSUMED_FUEL_PRICE
                    + spec.maint_cost_per_hour * hours
                    + (spec.list_price * self.OWNERSHIP_RATE_PER_YEAR
                       / self.ANNUAL_BLOCK_HOURS) * hours)
            score = trip / (spec.max_seats * target)     # cost per seat-km
            if best_score is None or score < best_score:
                best, best_score = spec, score
        return best

    # -- introspection for the GUI ------------------------------------
    def profile_of(self, player_id: str) -> dict:
        mem = self.memory.get(player_id)
        if mem is None:
            name = self.profiles.get(player_id, self.default_archetype)
            arch = ARCHETYPES.get(name, DEFAULT_ARCHETYPE)
            return {"archetype": arch.name, "blurb": arch.blurb, "recent": []}
        return {"archetype": mem.archetype.name, "blurb": mem.archetype.blurb,
                "recent": list(mem.recent[-6:])}


def market_key_for(world, origin, dest) -> str:
    """The demand pool an airport pair would draw from."""
    spec = _spec_for(world, origin, dest)
    return market_key(spec) if spec is not None else f"{origin.iata}-{dest.iata}"


def _spec_for(world, origin, dest):
    provider = getattr(world, "route_data", None)
    if provider is None:
        return None
    try:
        return provider.route_spec(origin.iata, dest.iata)
    except Exception:
        return None

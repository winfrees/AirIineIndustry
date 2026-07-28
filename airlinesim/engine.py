"""
Airline Asset & Resource Management Sim — INTEGRATED ENGINE
===========================================================

This module ties the standalone subsystems into one continuous-time simulation
and establishes the multi-player `World`.

ARCHITECTURE (the load-bearing decisions):

  SpecRepository ...... immutable reference data (the DB layer / import seam)
  World ............... SHARED, CONTESTED state: gates, fuel, labor pools,
                        passenger demand markets, the clock. Owns nothing
                        player-specific.
  Player .............. OWNED assets: fleet, routes, staff, ledger. Many players.
  ResourceArbiter ..... the competition seam. Every claim on a finite world
                        resource (a gate slot, fuel, a hireable crew, a seat of
                        demand) is submitted as a Claim and resolved fairly each
                        tick. Single-player = trivially-satisfied claims; adding
                        competitors = adding claimants, NOT rewriting logic.
  Subsystem ........... pluggable tick-stage (pricing, maintenance, finance...).
                        Ordered, each gets (world, players, dt).

The tick pipeline is: collect claims -> arbitrate -> apply outcomes -> accrue.
That ordering is what makes competition fair and deterministic.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional, Type, TypeVar, Iterable
import math

from airlinesim import gamelog

# The engine logs STRUCTURAL events only — the handful of places it takes
# something away from a player on its own initiative (lease expiry today).
# Per-tick accounting stays in `Player.log`, where a GUI can read it and a
# day-long session doesn't turn it into gigabytes. With no handler attached
# (a scenario, the explorer) every call here is a no-op.
_log = gamelog.get("engine")


# ============================================================
# SPEC LAYER (condensed from prior drafts — the import seam)
# ============================================================

class PlaneClass(Enum):
    REGIONAL = auto(); NARROWBODY = auto(); WIDEBODY = auto()


class CheckTier(Enum):
    A = auto(); B = auto(); C = auto(); D = auto()


@dataclass(frozen=True)
class SpecBase:
    spec_id: str
    display_name: str
    capabilities: dict = field(default_factory=dict)
    requirements: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CheckDefinition:
    tier: CheckTier
    interval_flight_hours: float
    interval_calendar_days: float
    downtime_hours: float
    labor_hours: float
    base_cost: float
    min_facility_class: PlaneClass


@dataclass(frozen=True)
class StructuralLayover:
    extra_downtime_hours: float
    extra_labor_hours: float
    extra_base_cost: float


@dataclass(frozen=True)
class MaintenanceProgram:
    checks: tuple = ()
    b_folded_into_a: bool = False
    b_fold_every_n_a: int = 4
    c_escalates_to_3c: bool = False
    c_3c_every_n_c: int = 3
    c_3c_deferred_to_d: bool = False
    layover: Optional[StructuralLayover] = None

    def by_tier(self, tier: CheckTier) -> Optional[CheckDefinition]:
        return next((c for c in self.checks if c.tier == tier), None)


@dataclass(frozen=True)
class AircraftSpec(SpecBase):
    manufacturer: str = ""
    plane_class: PlaneClass = PlaneClass.NARROWBODY
    list_price: float = 0.0
    max_seats: int = 0
    max_range_km: float = 0.0
    cruise_speed_kmh: float = 0.0
    fuel_burn_lph: float = 0.0
    maint_cost_per_hour: float = 0.0
    maint_program: Optional[MaintenanceProgram] = None
    economic_life_d_checks: int = 3
    # Balanced-field takeoff length at typical MTOW/sea level/ISA. 0 = unset,
    # which skips the runway check (keeps pre-existing hand-authored specs valid).
    takeoff_runway_m: float = 0.0
    # Shared type ratings are real: the A320 family is one rating, 737NG/MAX
    # one, 757/767 one. "" = fall back to spec_id/manufacturer certs matching.
    type_rating: str = ""
    # Cabin reconfiguration: cost per cabin slot, and how long the tail is down.
    reconfig_cost_per_slot: float = 0.0
    reconfig_days: float = 0.0
    # --- cabin geometry (airlinesim.cabin) ---
    # Published economy seats per row for the type (6 on a 737, 9 on a 787).
    # 0 = unpublished, which makes cabin.py band an estimate off plane_class
    # and max_seats and flag it as estimated. Usable seating length in metres
    # is likewise optional: left at 0 it is DERIVED from max_seats at economy
    # pitch, so an all-economy cabin holds exactly max_seats by construction.
    cabin_abreast: int = 0
    cabin_length_m: float = 0.0


@dataclass(frozen=True)
class AirportSpec(SpecBase):
    iata: str = ""
    runway_length_m: float = 0.0
    total_gates: int = 0                 # contested capacity
    has_maintenance_facility: bool = False
    facility_max_class: Optional[PlaneClass] = None
    fuel_supply_per_day_l: float = 0.0   # contested capacity
    landing_fee: float = 0.0             # per landing, scaled by plane class
    # --- geography (great-circle distance + metro-pair markets) ---
    lat: float = 0.0
    lon: float = 0.0
    metro_id: str = ""
    # Traffic rank among all airports in the corpus (1 = busiest). 0 = unknown,
    # which keeps hand-authored specs valid. Measured, and the cheapest signal
    # for "is this a metro's primary field or its reliever?".
    hub_rank: int = 0
    # How easily this airport's catchment population can reach it, relative to
    # a typical airport (1.0 = typical). This is the seam for real catchment /
    # census data: with it, the LGA-vs-JFK-vs-EWR choice becomes real. The
    # committed BTS corpus carries no catchment data, so it stays 1.0 there and
    # only service tier moves desirability — see docs and route.py.
    access_index: float = 1.0
    # Median household income of this airport's catchment, relative to the
    # national median (1.0 = typical, 0 = unknown). The companion seam to
    # access_index and, like it, EMPTY in the committed corpus: BTS carries no
    # income data, so filling this needs a census join that hasn't been built.
    # It is the input a per-route premium propensity would be derived from —
    # docs/cabin-demand-design.md lays out the options. Nothing reads it yet;
    # it is declared so the field, the plan and the caveat travel together.
    catchment_income_index: float = 0.0
    # --- fee schedule; tier-indexed tuples, index = RouteOp.service_tier ---
    # Empty tuple = no such fee at this airport (keeps legacy specs free).
    gate_fee_by_tier: tuple = ()         # per departure turn
    hub_fee_per_day: float = 0.0         # per day to run this as a MX base
    amenities_fee_by_tier: tuple = ()    # per pax (lounge tier 0..2)
    baggage_fee_by_tier: tuple = ()      # per pax (handling tier 1..3)

    def fee_at_tier(self, table: tuple, tier: int) -> float:
        """Read a tier-indexed fee, clamping out-of-range tiers rather than
        raising mid-tick. Empty table -> no fee."""
        if not table:
            return 0.0
        return table[max(0, min(int(tier), len(table) - 1))]


class CrewType(Enum):
    COCKPIT = auto(); CABIN = auto(); GROUND = auto()
    METEOROLOGY = auto(); BAGGAGE = auto(); MAINTENANCE = auto()


@dataclass(frozen=True)
class CrewSpec(SpecBase):
    crew_type: CrewType = CrewType.GROUND
    cost_per_member_hour: float = 0.0
    certifications: tuple = ()


@dataclass(frozen=True)
class RouteSpec(SpecBase):
    origin_iata: str = ""
    dest_iata: str = ""
    distance_km: float = 0.0
    base_demand_per_day: int = 0
    seasonality_amplitude: float = 0.2
    # Which demand pool this route draws from. "" means the route is its own
    # market (legacy behavior, keyed by spec_id). A metro-pair key like
    # "LA|NYC" makes every airport pair between two metros compete for the
    # SAME travelers — JFK-LAX and EWR-LAX chasing one New York-Los Angeles
    # market, which is the point of modeling airport choice at all.
    market_id: str = ""
    # market structure (route.py) — tuple of SegmentDemand; None -> flat fallback
    segments: tuple = ()
    # How premium this market is relative to the global cabin-split default:
    # 1.0 = the default split, >1 tilts demand toward the front of the
    # aircraft, <1 toward the back. Left at 1.0 everywhere in the shipped
    # corpus — no committed dataset measures it. See route.cabin_split_for()
    # and docs/cabin-demand-design.md.
    premium_propensity: float = 1.0
    # equipment + crew requirements (route.py)
    equipment_req: object = None      # EquipmentRequirements
    crew_req: object = None           # CrewRequirements
    # provenance, set by routedata.RouteDataProvider (empty for hand-authored
    # specs). 'exact' = measured BTS pair, 'comparable' = gravity estimate,
    # 'synthetic' = engine defaults. Carried so a fitted estimate can never be
    # mistaken for a measurement downstream.
    data_tier: str = ""
    data_vintage: str = ""


# ============================================================
# SPEC REPOSITORY — DB layer / import seam
# ============================================================

S = TypeVar("S", bound=SpecBase)


class SpecRepository:
    def __init__(self):
        self._tables: dict[Type, dict[str, SpecBase]] = defaultdict(dict)

    def load(self, spec_cls: Type[S], rows: Iterable[dict], builder: Callable[[dict], S]):
        for row in rows:
            spec = builder(row)
            self._tables[spec_cls][spec.spec_id] = spec

    def get(self, spec_cls: Type[S], spec_id: str) -> S:
        return self._tables[spec_cls][spec_id]

    def all(self, spec_cls: Type[S]) -> list[S]:
        return list(self._tables[spec_cls].values())


# ============================================================
# INSTANCES
# ============================================================

@dataclass
class Airplane:
    spec: AircraftSpec
    tail_number: str
    owner_id: str                        # which player owns this
    owned: bool = True
    lease_cost_per_hour: float = 0.0
    airframe_hours: float = 0.0
    in_service: bool = True
    grounded_until: float = 0.0
    location_iata: str = ""
    acquired_at: float = 0.0             # sim_time when acquired (for depreciation)
    hours_since: dict = field(default_factory=dict)
    days_since: dict = field(default_factory=dict)
    a_checks_completed: int = 0
    c_checks_completed: int = 0
    d_checks_completed: int = 0
    structural_work_pending: bool = False
    retired: bool = False
    # Cabin configuration belongs to the AIRFRAME: chosen at acquisition and
    # changed only by a paid, downtime-incurring reconfiguration. None means
    # all-economy. RouteOp.layout stays supported as a per-op override for
    # pre-existing scenarios (see effective_layout()).
    layout: object = None                # SeatLayout
    reconfiguring_until: float = 0.0     # sim_time the recabin completes

    def __post_init__(self):
        for t in CheckTier:
            self.hours_since.setdefault(t, 0.0)
            self.days_since.setdefault(t, 0.0)

    def effective_layout(self):
        """
        The layout revenue and demand claims should use. Per-op override wins
        (legacy scenarios set RouteOp.layout), then the airframe's own
        configuration, then all-economy.
        """
        from airlinesim.finance_cabin import SeatLayout
        return self.layout or SeatLayout.all_economy(self.spec.max_seats)


@dataclass
class CrewUnit:
    spec: CrewSpec
    headcount: int
    owner_id: str
    home_iata: str = ""
    location_iata: str = ""    # where the crew currently is (for positioning)
    busy_until: float = 0.0
    duty: object = None        # CrewDutyState (lazily created)
    limits: object = None      # DutyLimits (defaults by crew type if None)

    def __post_init__(self):
        from airlinesim.crew import (CrewDutyState, DEFAULT_DUTY_LIMITS, GROUND_DUTY_LIMITS)
        if self.duty is None:
            self.duty = CrewDutyState()
        if not self.location_iata:
            self.location_iata = self.home_iata
        if self.limits is None:
            # flight crews get the real envelope; others get a permissive one
            if self.spec.crew_type in (CrewType.COCKPIT, CrewType.CABIN):
                self.limits = DEFAULT_DUTY_LIMITS
            else:
                self.limits = GROUND_DUTY_LIMITS

    def hourly_cost(self) -> float:
        return self.headcount * self.spec.cost_per_member_hour


@dataclass
class RouteOp:
    """A player's operation of a route: plane + crews + cabin layout + pricing."""
    spec: RouteSpec
    plane: Airplane
    cockpit: CrewUnit
    cabin: CrewUnit
    ticket_price: float                 # ECONOMY base fare; classes scale off this
    daily_frequency: int = 1
    owner_id: str = ""
    layout: object = None               # SeatLayout (None -> treated all-economy)
    # Per-cabin fares for THIS route: {CabinClass: price}. A cabin with no
    # entry falls back to ticket_price * that class's price_multiplier, which
    # is the behavior every route had before per-cabin pricing existed — so
    # an op that sets nothing here is unchanged, and setting one cabin's fare
    # doesn't disturb the others. Priced per route (not per fleet) because
    # what a business seat is worth is a property of the MARKET, and the
    # cabins worth pricing are the ones the assigned airframe actually has.
    cabin_prices: dict = field(default_factory=dict)
    # market feedback (written by OperationsSubsystem, read by AIStrategySubsystem)
    last_load_factor: float = 0.0
    last_pax: float = 0.0
    last_revenue: float = 0.0          # ticket revenue this op earned last tick
    last_variable_cost: float = 0.0    # fuel + flight crew last tick (the marginal cost)
    last_profit: float = 0.0           # revenue - variable cost last tick
    last_class_pax: dict = field(default_factory=dict)  # per-class carriage
    last_class_seats: dict = field(default_factory=dict)  # per-class seats offered
    last_class_revenue: dict = field(default_factory=dict)  # per-class ticket revenue
    prev_profit: float = 0.0           # profit the tick before (for hill-climbing)
    prev_price: float = 0.0            # price that produced prev_profit
    price_dir: int = -1                # current search direction (+1 up, -1 down)
    last_eff_freq: float = 0.0         # frequencies actually operated (gate-limited)
    last_crew_block: str = ""          # why crew limited the flight, if it did
    deadhead_seats: int = 0            # seats reserved for repositioning crew this tick
    suitable: bool = True              # does equipment+crew satisfy route requirements
    suitability_reasons: list = field(default_factory=list)
    # Service level bought at the airports this op uses: 1 = remote stand/bus
    # boarding + basic handling (cheap, unpleasant), 2 = standard jetbridge,
    # 3 = premium gate + flagship lounge + priority baggage. Drives both the
    # fees charged and the desirability passengers assign to this op.
    service_tier: int = 2
    last_fees: float = 0.0             # airport fees paid last tick

    def effective_layout(self):
        """The cabin this op sells: its own override, else the airframe's."""
        return self.layout or self.plane.effective_layout()

    def fare_for(self, cabin) -> float:
        """
        What this op charges for one seat in `cabin`.

        A per-cabin price set on the route wins; otherwise the fare is the
        economy base fare scaled by the class's multiplier — the original
        behavior, kept as the default so a route nobody has priced by cabin
        behaves exactly as it did. Non-positive overrides are ignored rather
        than sold at zero.
        """
        from airlinesim.finance_cabin import DEFAULT_SEAT_CLASSES
        # getattr, not attribute access: a game saved before per-cabin pricing
        # unpickles without the field at all, and every such route should
        # simply keep behaving the way it did.
        override = (getattr(self, "cabin_prices", None) or {}).get(cabin, 0.0)
        if override and override > 0:
            return float(override)
        return self.ticket_price * DEFAULT_SEAT_CLASSES[cabin].price_multiplier


@dataclass
class Ledger:
    cash: float = 0.0
    co2_emitted_t: float = 0.0
    co2_offset_t: float = 0.0

    def credit(self, amt: float, note: str, log: list):
        self.cash += amt
        log.append(f"  +${amt:,.0f}  {note}")

    def debit(self, amt: float, note: str, log: list) -> bool:
        if amt > self.cash:
            log.append(f"  DENIED ${amt:,.0f}  {note} (insufficient cash)")
            return False
        self.cash -= amt
        log.append(f"  -${amt:,.0f}  {note}")
        return True


# ============================================================
# PLAYER — owned state. One per airline (human or AI competitor).
# ============================================================

@dataclass
class Player:
    player_id: str
    name: str
    is_ai: bool = False
    ledger: Ledger = field(default_factory=Ledger)
    fleet: list = field(default_factory=list)        # Airplane
    crews: list = field(default_factory=list)        # CrewUnit (standing staff)
    cockpit_pool: list = field(default_factory=list) # CrewUnit pool for rostering
    cabin_pool: list = field(default_factory=list)   # CrewUnit pool for rostering
    route_ops: list = field(default_factory=list)    # RouteOp
    loans: list = field(default_factory=list)        # Loan (financed aircraft)
    leases: list = field(default_factory=list)       # Lease (operating leases)
    log: list = field(default_factory=list)
    # Airports this carrier runs as maintenance bases. Costs hub_fee_per_day
    # each, and (once any hub is declared) restricts where its aircraft can
    # get checks done. Empty = legacy behavior: maintenance anywhere rated.
    hub_iatas: list = field(default_factory=list)

    def maintenance_crews(self) -> list:
        return [c for c in self.crews if c.spec.crew_type == CrewType.MAINTENANCE]


# ============================================================
# WORLD — shared, contested state. No player-owned assets here.
# ============================================================

@dataclass
class GateLedger:
    """Per-airport contested gate capacity, reset each tick."""
    iata: str
    total_gates: int
    allocated: dict = field(default_factory=dict)   # player_id -> gates held

    def used(self) -> int:
        return sum(self.allocated.values())

    def free(self) -> int:
        return self.total_gates - self.used()


@dataclass
class FuelMarket:
    """Per-airport fuel: finite daily supply, price rises as it depletes."""
    iata: str
    supply_per_day_l: float
    base_price_per_l: float
    drawn_today_l: float = 0.0

    def reset_day(self):
        self.drawn_today_l = 0.0

    def spot_price(self) -> float:
        # price climbs as supply depletes (scarcity → competition pressure)
        if self.supply_per_day_l <= 0:
            return self.base_price_per_l
        depletion = min(1.0, self.drawn_today_l / self.supply_per_day_l)
        return self.base_price_per_l * (1.0 + 0.5 * depletion)


@dataclass
class DemandMarket:
    """
    A passenger demand pool contested by every carrier serving it. Keyed by
    a route's market_id: one pool per route by default, or one pool per
    metro-pair when routes declare a shared market.
    """
    route_id: str
    base_demand_per_day: int
    seasonality_amplitude: float
    segments: tuple = ()      # SegmentDemand tuple; empty -> flat fallback
    # Fare this market is calibrated around. Wealthy, business-heavy markets
    # sustain higher fares before demand falls off. 0 -> use the global
    # PricingModel.reference_price (legacy behavior).
    reference_price: float = 0.0
    # How much more (or less) of this market's demand seeks a premium cabin
    # than the global default split. 1.0 = the default, and every route in the
    # shipped corpus is 1.0 — the committed data carries nothing that measures
    # premium propensity. See route.cabin_split_for() and
    # docs/cabin-demand-design.md for what would drive it.
    premium_propensity: float = 1.0


# What a hub buys at its own airport, on top of being the only place its
# carrier can do maintenance. Preferential gates are real: a hub carrier
# leases its gates rather than competing for common-use ones per turn, so
# under contention it is served first. This multiplies the gate claim's
# priority, so it decides ONLY when gates are actually oversubscribed —
# at an uncongested field it changes nothing.
#
# The number is game balance. Too low and a hub is pure cost (the state this
# model was in: one cheap hub was always optimal); too high and hub carriers
# become unassailable at their own airports and nobody can contest a hub.
HUB_GATE_PRIORITY = 1.6


def market_key(rs: "RouteSpec") -> str:
    """The demand pool a route draws from — its market_id, else its own id."""
    return getattr(rs, "market_id", "") or rs.spec_id


class World:
    """Shared simulation state. The contested commons."""
    def __init__(self, repo: SpecRepository):
        self.repo = repo
        self.sim_time: float = 0.0           # hours
        self.gates: dict[str, GateLedger] = {}
        self.fuel: dict[str, FuelMarket] = {}
        self.demand: dict[str, DemandMarket] = {}
        self.hireable_crews: list = []       # labor pool players compete to hire
        self.log: list = []

    # --- world setup helpers ---
    def add_airport_resources(self, spec: AirportSpec, fuel_base_price: float):
        self.gates[spec.iata] = GateLedger(spec.iata, spec.total_gates)
        self.fuel[spec.iata] = FuelMarket(spec.iata, spec.fuel_supply_per_day_l, fuel_base_price)

    def add_demand_market(self, rs: RouteSpec):
        key = market_key(rs)
        self.demand[key] = DemandMarket(
            key, rs.base_demand_per_day, rs.seasonality_amplitude,
            segments=getattr(rs, "segments", ()),
            premium_propensity=getattr(rs, "premium_propensity", 1.0))

    def reset_daily_markets(self):
        for fm in self.fuel.values():
            fm.reset_day()
        for gl in self.gates.values():
            gl.allocated.clear()


# ============================================================
# RESOURCE ARBITER — the competition seam.
# Players submit Claims on world resources; the arbiter resolves contention
# fairly and deterministically. This is where gate/fuel/crew/passenger
# competition lives. Single-player just means uncontested claims.
# ============================================================

class ResourceKind(Enum):
    GATE = auto(); FUEL = auto(); CREW_HIRE = auto(); DEMAND = auto()


@dataclass
class Claim:
    player_id: str
    kind: ResourceKind
    key: str                 # iata or route_id or crew spec_id
    amount: float            # gates, litres, headcount, or seats requested
    priority: float = 0.0    # higher wins under contention (e.g. willingness to pay)
    payload: object = None   # optional ref (the RouteOp, the plane, etc.)
    # DEMAND only: the cabin name when the route is segmented (each cabin is
    # its own priced pool), None for the legacy flat whole-route pool.
    sub_key: Optional[str] = None
    # DEMAND only: how attractive this offer is beyond its fare — how easy the
    # airport is to reach from where the market's travelers actually live, and
    # what level of service is being bought. 1.0 = neutral, so legacy claims
    # are unaffected.
    desirability: float = 1.0


@dataclass
class Allocation:
    claim: Claim
    granted: float           # how much was actually allocated (<= requested)


class ResourceArbiter:
    """
    Resolves contention. Strategy per resource kind:
      GATE / FUEL / CREW_HIRE : proportional-by-priority when oversubscribed.
      DEMAND                  : market-share split by price attractiveness.
    Deterministic given the same claim set (sorted by priority then player_id).
    """
    def __init__(self, world: World, pricing: "PricingModel"):
        self.world = world
        self.pricing = pricing

    def resolve(self, claims: list[Claim], market: "MarketConditions") -> list[Allocation]:
        out: list[Allocation] = []
        by_kind: dict[ResourceKind, list[Claim]] = defaultdict(list)
        for c in claims:
            by_kind[c.kind].append(c)

        for kind, group in by_kind.items():
            if kind == ResourceKind.GATE:
                out += self._resolve_capacity(group, self._gate_capacity)
            elif kind == ResourceKind.FUEL:
                out += self._resolve_capacity(group, self._fuel_capacity)
            elif kind == ResourceKind.CREW_HIRE:
                out += self._resolve_capacity(group, self._crew_capacity)
            elif kind == ResourceKind.DEMAND:
                out += self._resolve_demand(group, market)
        return out

    # capacity-style resources: proportional allocation by priority when scarce
    def _resolve_capacity(self, claims: list[Claim],
                          cap_fn: Callable[[str], float]) -> list[Allocation]:
        out = []
        by_key: dict[str, list[Claim]] = defaultdict(list)
        for c in claims:
            by_key[c.key].append(c)
        for key, group in by_key.items():
            capacity = cap_fn(key)
            requested = sum(c.amount for c in group)
            if requested <= capacity:
                for c in group:
                    out.append(Allocation(c, c.amount))
                    self._commit(c, c.amount)
            else:
                # oversubscribed: weight by priority (willingness to pay)
                total_pri = sum(max(0.001, c.priority) for c in group)
                for c in sorted(group, key=lambda x: (-x.priority, x.player_id)):
                    share = (max(0.001, c.priority) / total_pri) * capacity
                    grant = min(c.amount, share)
                    out.append(Allocation(c, grant))
                    self._commit(c, grant)
        return out

    def _gate_capacity(self, iata: str) -> float:
        return self.world.gates[iata].free() if iata in self.world.gates else 0

    def _fuel_capacity(self, iata: str) -> float:
        fm = self.world.fuel.get(iata)
        return (fm.supply_per_day_l - fm.drawn_today_l) if fm else 0

    def _crew_capacity(self, spec_id: str) -> float:
        return sum(c.headcount for c in self.world.hireable_crews
                   if c.spec.spec_id == spec_id)

    def _commit(self, claim: Claim, granted: float):
        if granted <= 0:
            return
        if claim.kind == ResourceKind.GATE and claim.key in self.world.gates:
            gl = self.world.gates[claim.key]
            gl.allocated[claim.player_id] = gl.allocated.get(claim.player_id, 0) + granted
        elif claim.kind == ResourceKind.FUEL and claim.key in self.world.fuel:
            self.world.fuel[claim.key].drawn_today_l += granted

    # demand: logit market-share split by price attractiveness, with spill
    # reallocation so demand a capped carrier can't seat flows to rivals.
    # Claims are grouped by (route, sub_key): sub_key is None for the legacy
    # flat whole-route pool, or a cabin name for a segmented route — each
    # cabin is then its own priced, capacity-bound market, so cross-carrier
    # AND cross-cabin (e.g. rival economy fares vs this op's business fare)
    # competition resolve through the exact same mechanism.
    def _resolve_demand(self, claims: list[Claim],
                        market: "MarketConditions") -> list[Allocation]:
        out = []
        groups: dict[tuple, list[Claim]] = defaultdict(list)
        for c in claims:
            groups[(c.key, c.sub_key)].append(c)

        for (route_id, sub_key), group in groups.items():
            dm = self.world.demand.get(route_id)
            if not dm:
                out += [Allocation(c, 0) for c in group]
                continue

            # market price signal for THIS pool: capacity-weighted average of
            # what's on offer (Σ price·seats / Σ seats), vs the route
            # reference price. A pricier market this tick sizes a smaller
            # total demand pool — this is what makes segment elasticity (and,
            # for the flat fallback, nothing — it doesn't use price_ratio)
            # actually bite, instead of always sizing the pool at price_ratio=1.
            # A market can carry its own reference fare (wealthier, more
            # business-heavy metro pairs sustain higher prices before demand
            # sags); otherwise fall back to the global one.
            ref = dm.reference_price or self.pricing.reference_price
            total_amt = sum(c.amount for c in group)
            if total_amt > 1e-9:
                avg_price = sum(c.priority * c.amount for c in group) / total_amt
            else:
                avg_price = ref
            price_ratio = (avg_price / ref) if ref > 0 else 1.0

            if sub_key is None:
                total_demand = self.pricing.route_demand(dm, self.world.sim_time, price_ratio)
            else:
                from airlinesim.route import cabin_demand_on
                total_demand = cabin_demand_on(
                    dm.segments, sub_key, self.world.sim_time, price_ratio,
                    getattr(dm, "premium_propensity", 1.0))

            out += self._split_pool(group, total_demand)
        return out

    def _split_pool(self, group: list[Claim], total_demand: float) -> list[Allocation]:
        """
        Logit market-share split with spill: claims sharing a demand pool
        (rival carriers on a route, or rival cabins/carriers within one
        cabin's segment-fed pool) compete by price attractiveness; a claim
        that fills up spills its remainder to whoever still has room.
        """
        # attractiveness via logit: weight = price^elasticity (elasticity<0,
        # so a LOWER price yields a HIGHER weight) SCALED by desirability —
        # convenience of the airport and level of service bought. That product
        # is the trade a traveler actually makes: a cheap fare from an
        # awkward airport can lose to a pricier one that's easy to reach.
        remaining = {id(c): c.amount for c in group}   # seat capacity left
        weights = {id(c): max(1e-9, (c.priority ** self.pricing.elasticity)
                              * max(0.0, c.desirability))
                   for c in group}
        granted = {id(c): 0.0 for c in group}

        pool = total_demand
        # iterate: assign by share, spill the overflow from capped carriers,
        # re-split the spill among those who still have seats. Converges fast.
        active = list(group)
        for _ in range(len(group) + 2):
            if pool <= 1e-6 or not active:
                break
            tw = sum(weights[id(c)] for c in active)
            if tw <= 0:
                break
            spill = 0.0
            still_active = []
            for c in active:
                want = pool * (weights[id(c)] / tw)
                can = remaining[id(c)]
                take = min(want, can)
                granted[id(c)] += take
                remaining[id(c)] -= take
                if want > can:                 # carrier filled up; rest spills
                    spill += (want - can)
                elif remaining[id(c)] > 1e-6:   # still has seats for spill rounds
                    still_active.append(c)
            pool = spill
            active = still_active

        return [Allocation(c, granted[id(c)]) for c in group]


# ============================================================
# PRICING MODEL
# ============================================================

@dataclass
class MarketConditions:
    fuel_index: float = 1.0


@dataclass
class PricingModel:
    elasticity: float = -1.3
    reference_price: float = 200.0

    def seasonal_factor(self, sim_time_hours: float, amplitude: float) -> float:
        day = (sim_time_hours / 24.0) % 365
        return 1.0 + amplitude * math.sin(2 * math.pi * day / 365)

    def route_demand(self, dm: DemandMarket, sim_time_hours: float,
                     price_ratio: float = 1.0) -> float:
        # structured market: sum segment demand (business/leisure/connecting),
        # each with its own elasticity, seasonality, and day-of-week profile.
        if getattr(dm, "segments", ()):
            return sum(seg.demand_on(sim_time_hours, price_ratio)
                       for seg in dm.segments)
        # flat fallback (legacy routes without segments)
        season = self.seasonal_factor(sim_time_hours, dm.seasonality_amplitude)
        return dm.base_demand_per_day * season


# ============================================================
# MAINTENANCE ENGINE (integrated: pulls instances from players/world)
# Job tags promoted to real fields, as flagged.
# ============================================================

@dataclass
class MaintenanceJob:
    plane: Airplane
    tier: CheckTier
    facility_iata: str
    crew: CrewUnit
    starts_at: float
    completes_at: float
    cost: float
    absorbed_b: bool = False
    was_3c: bool = False


class MaintenanceEngine:
    def __init__(self, repo: SpecRepository):
        self.repo = repo
        self.active: list[MaintenanceJob] = []

    def accrue(self, plane: Airplane, flight_hours: float, calendar_days: float):
        if plane.retired:
            return
        plane.airframe_hours += flight_hours
        for t in CheckTier:
            plane.hours_since[t] += flight_hours
            plane.days_since[t] += calendar_days

    def highest_due_tier(self, plane: Airplane) -> Optional[CheckTier]:
        prog = plane.spec.maint_program
        if not prog:
            return None
        for t in (CheckTier.D, CheckTier.C, CheckTier.B, CheckTier.A):
            cd = prog.by_tier(t)
            if not cd:
                continue
            if t == CheckTier.B and prog.b_folded_into_a:
                continue
            if (plane.hours_since[t] >= cd.interval_flight_hours or
                    plane.days_since[t] >= cd.interval_calendar_days):
                return t
        return None

    def _next_a_absorbs_b(self, plane: Airplane) -> bool:
        prog = plane.spec.maint_program
        if not (prog and prog.b_folded_into_a):
            return False
        return ((plane.a_checks_completed + 1) % prog.b_fold_every_n_a) == 0

    def _c_is_3c(self, plane: Airplane) -> bool:
        prog = plane.spec.maint_program
        if not (prog and prog.c_escalates_to_3c and prog.layover):
            return False
        return ((plane.c_checks_completed + 1) % prog.c_3c_every_n_c) == 0

    def _find_facility(self, plane: Airplane, cd: CheckDefinition,
                       hubs: Optional[list] = None) -> Optional[str]:
        # Resolves against real airport specs in the repo. If the owner has
        # declared hubs, only those can do the work — a carrier's maintenance
        # reach is the network it pays to run. No hubs declared = legacy
        # behavior (any rated field will do).
        for ap in self.repo.all(AirportSpec):
            if not ap.has_maintenance_facility or ap.facility_max_class is None:
                continue
            if hubs and ap.iata not in hubs:
                continue
            if ap.facility_max_class.value >= cd.min_facility_class.value:
                return ap.iata
        return None

    def _find_crew(self, plane: Airplane, crews: list, now: float) -> Optional[CrewUnit]:
        for c in crews:
            if c.spec.crew_type != CrewType.MAINTENANCE or now < c.busy_until:
                continue
            certs = c.spec.certifications
            if plane.spec.spec_id in certs or plane.spec.manufacturer in certs:
                return c
        return None

    def try_schedule(self, plane: Airplane, crews: list, now: float, log: list,
                     hubs: Optional[list] = None):
        if plane.retired or not plane.in_service:
            return None
        tier = self.highest_due_tier(plane)
        if tier is None:
            return None
        prog = plane.spec.maint_program
        cd = prog.by_tier(tier)

        if tier == CheckTier.D:
            from airlinesim.finance_cabin import aircraft_value
            d_cost = cd.base_cost   # the heavy-check sticker (labor added later)
            value_now = aircraft_value(plane, now)
            past_life = plane.d_checks_completed >= plane.spec.economic_life_d_checks
            uneconomic = d_cost > value_now   # overhaul costs more than the plane is worth
            if past_life or uneconomic:
                plane.retired = True
                plane.in_service = False
                reason = "past economic life" if past_life else \
                    f"D-check ${d_cost:,.0f} > value ${value_now:,.0f}"
                log.append(f"  {plane.tail_number}: RETIRED ({reason})")
                return None

        extra_dt = extra_lab = extra_cost = 0.0
        label = tier.name
        absorbed_b = (tier == CheckTier.A and self._next_a_absorbs_b(plane))
        if absorbed_b:
            b = prog.by_tier(CheckTier.B)
            if b:
                extra_dt += b.downtime_hours * 0.7
                extra_lab += b.labor_hours * 0.7
                extra_cost += b.base_cost * 0.7
            label = "A+B"
        was_3c = False
        if tier == CheckTier.C and self._c_is_3c(plane):
            lay = prog.layover
            if prog.c_3c_deferred_to_d:
                plane.structural_work_pending = True
                label = "C(3C->D)"
            else:
                extra_dt += lay.extra_downtime_hours
                extra_lab += lay.extra_labor_hours
                extra_cost += lay.extra_base_cost
                label = "3C/IL"
                was_3c = True
        if tier == CheckTier.D and plane.structural_work_pending and prog.layover:
            lay = prog.layover
            extra_dt += lay.extra_downtime_hours
            extra_lab += lay.extra_labor_hours
            extra_cost += lay.extra_base_cost
            label = "D+struct"

        facility = self._find_facility(plane, cd, hubs)
        if facility is None:
            where = " at any hub" if hubs else ""
            log.append(f"  {plane.tail_number}: {tier.name} DUE but no rated facility{where} — flying on risk")
            return None
        crew = self._find_crew(plane, crews, now)
        if crew is None:
            log.append(f"  {plane.tail_number}: {tier.name} DUE, facility {facility} ready, no certified crew")
            return None

        cost = cd.base_cost + extra_cost + (cd.labor_hours + extra_lab) * crew.spec.cost_per_member_hour
        completes = now + cd.downtime_hours + extra_dt
        plane.in_service = False
        plane.grounded_until = completes
        crew.busy_until = completes
        job = MaintenanceJob(plane, tier, facility, crew, now, completes, cost,
                             absorbed_b=absorbed_b, was_3c=was_3c)
        self.active.append(job)
        log.append(f"  {plane.tail_number}: {label}-check @ {facility} — down {completes-now:.0f}h, ${cost:,.0f}")
        return job

    def update(self, now: float, players_by_id: dict, log: list):
        still = []
        for job in self.active:
            if now >= job.completes_at:
                p = job.plane
                p.in_service = True
                order = [CheckTier.A, CheckTier.B, CheckTier.C, CheckTier.D]
                for t in order[:order.index(job.tier) + 1]:
                    p.hours_since[t] = 0.0
                    p.days_since[t] = 0.0
                if job.tier == CheckTier.A:
                    p.a_checks_completed += 1
                    if job.absorbed_b:
                        p.hours_since[CheckTier.B] = 0.0
                        p.days_since[CheckTier.B] = 0.0
                if job.tier == CheckTier.C:
                    p.c_checks_completed += 1
                if job.tier == CheckTier.D:
                    p.d_checks_completed += 1
                    p.structural_work_pending = False
                # charge the owner now that work is complete
                owner = players_by_id.get(p.owner_id)
                if owner:
                    owner.ledger.debit(job.cost, f"{job.tier.name}-check {p.tail_number}", owner.log)
                log.append(f"  {p.tail_number}: {job.tier.name}-check complete, back in service")
            else:
                still.append(job)
        self.active = still


# ============================================================
# SUBSYSTEM INTERFACE + CONCRETE STAGES
# ============================================================

class Subsystem(ABC):
    @abstractmethod
    def tick(self, world: World, players: list, dt: float, ctx: dict):
        ...


class RouteSuitabilitySubsystem(Subsystem):
    """
    Validates each route op's EQUIPMENT and CREW against the route's
    requirements, BEFORE Operations. An unsuitable pairing (wrong range, runway,
    seat economics, or insufficient/un-augmented crew) marks the op unsuitable so
    Operations grounds it, with the specific reasons surfaced. This is the Route
    <-> equipment <-> crew tie-in at the operational layer.
    """
    def tick(self, world: World, players: list, dt: float, ctx: dict):
        from airlinesim.route import route_can_fly
        repo = world.repo
        for p in players:
            for op in p.route_ops:
                origin = self._airport(repo, op.spec.origin_iata)
                dest = self._airport(repo, op.spec.dest_iata)
                ok, reasons = route_can_fly(op.spec, op.plane.spec, origin, dest,
                                            cockpit_crew=op.cockpit)
                op.suitable = ok
                op.suitability_reasons = reasons

    def _airport(self, repo, iata):
        try:
            return repo.get(AirportSpec, iata)
        except Exception:
            return None


class OperationsSubsystem(Subsystem):
    """
    The core competitive loop: each player's route ops submit claims for gates,
    fuel, and passenger demand; the arbiter resolves; revenue/costs apply.
    """
    def __init__(self, arbiter: ResourceArbiter, pricing: PricingModel,
                 maint: MaintenanceEngine):
        self.arbiter = arbiter
        self.pricing = pricing
        self.maint = maint

    def tick(self, world: World, players: list, dt: float, ctx: dict):
        from airlinesim.finance_cabin import CabinClass, DEFAULT_SEAT_CLASSES, SeatLayout
        from airlinesim.route import SEGMENT_CABIN_SPLIT
        day_frac = dt / 24.0
        claims: list[Claim] = []
        ops_with_claims: set = set()

        # 1) COLLECT claims from every active route op across all players
        for p in players:
            for op in p.route_ops:
                if op.plane.retired or not op.plane.in_service:
                    continue
                # equipment/crew must satisfy the route's requirements
                if not getattr(op, "suitable", True):
                    op.last_eff_freq = 0.0
                    op.last_pax = 0.0
                    continue

                mkey = market_key(op.spec)
                dm = world.demand.get(mkey)
                # how appealing this op is beyond its fare: how reachable its
                # origin airport is for the market's travelers, and what
                # service level it buys them. 1.0 for worlds without geography.
                desirability = self._desirability(op)
                if dm and dm.segments:
                    # one priced claim per (op, cabin fed by a segment) — NOT
                    # per segment. Capacity belongs to a cabin; if two
                    # segments target the same cabin (leisure + connecting ->
                    # economy) their demand sums into ONE pool for it instead
                    # of each claiming the same physical seats twice.
                    layout = op.layout or op.plane.effective_layout()
                    fed_cabins = {name for seg in dm.segments
                                 for name, _ in SEGMENT_CABIN_SPLIT.get(seg.segment, ())}
                    for cabin_name in fed_cabins:
                        cabin = CabinClass[cabin_name]
                        seats_cfg = layout.seats_of(cabin)
                        if seats_cfg <= 0:
                            continue
                        seat_capacity = seats_cfg * op.daily_frequency * day_frac
                        # the fare this cabin is actually offered at — the
                        # route's own per-cabin price where one is set, else
                        # the base fare times the class multiplier
                        fare = op.fare_for(cabin)
                        claims.append(Claim(p.player_id, ResourceKind.DEMAND, mkey,
                                            amount=seat_capacity, priority=fare,
                                            payload=(op, cabin), sub_key=cabin_name,
                                            desirability=desirability))
                        ops_with_claims.add(id(op))
                else:
                    # legacy flat pool (route has no segments configured)
                    seats = op.plane.spec.max_seats * op.daily_frequency * day_frac
                    claims.append(Claim(p.player_id, ResourceKind.DEMAND, mkey,
                                        amount=seats, priority=op.ticket_price, payload=op,
                                        desirability=desirability))
                    ops_with_claims.add(id(op))

                # Gate claim: one gate per frequency. Priority is willingness to
                # pay, LIFTED at an airport this carrier runs as a hub — a hub
                # carrier holds preferential gates rather than bidding for
                # common-use ones every morning. That is what a hub buys for its
                # daily overhead: without it the cheapest single hub is always
                # optimal and there is no reason to ever open a second.
                gate_priority = op.ticket_price
                if op.spec.dest_iata in getattr(p, "hub_iatas", ()):
                    gate_priority *= HUB_GATE_PRIORITY
                claims.append(Claim(p.player_id, ResourceKind.GATE, op.spec.dest_iata,
                                    amount=op.daily_frequency, priority=gate_priority,
                                    payload=op))
                # fuel claim
                fh = (op.spec.distance_km / op.plane.spec.cruise_speed_kmh) * op.daily_frequency * day_frac
                litres = op.plane.spec.fuel_burn_lph * fh
                claims.append(Claim(p.player_id, ResourceKind.FUEL, op.spec.dest_iata,
                                    amount=litres, priority=op.ticket_price, payload=op))

        # 2) ARBITRATE
        allocations = self.arbiter.resolve(claims, ctx["market"])

        # 3) APPLY outcomes
        alloc_by_op_demand: dict[int, float] = {}
        alloc_by_op_class: dict[tuple, float] = defaultdict(float)
        alloc_by_op_fuel: dict[int, float] = defaultdict(float)
        gates_granted: dict[int, float] = {}
        for a in allocations:
            if a.claim.kind == ResourceKind.DEMAND:
                payload = a.claim.payload
                if isinstance(payload, tuple):
                    op, cabin = payload
                    alloc_by_op_class[(id(op), cabin.name)] += a.granted
                elif isinstance(payload, RouteOp):
                    alloc_by_op_demand[id(payload)] = a.granted
                continue
            op = a.claim.payload
            if not isinstance(op, RouteOp):
                continue
            if a.claim.kind == ResourceKind.FUEL:
                alloc_by_op_fuel[id(op)] += a.granted
            elif a.claim.kind == ResourceKind.GATE:
                gates_granted[id(op)] = a.granted

        for p in players:
            for op in p.route_ops:
                if id(op) not in ops_with_claims:
                    continue
                # effective frequency = min(desired, gates actually granted).
                # A denied gate means that flight physically can't operate.
                desired_freq = op.daily_frequency
                granted_gates = gates_granted.get(id(op), desired_freq)
                eff_freq = max(0.0, min(desired_freq, granted_gates))
                freq_ratio = (eff_freq / desired_freq) if desired_freq > 0 else 0.0

                # --- CREW LEGALITY GATE ---
                # Each operated rotation adds flight hours to cockpit+cabin crew.
                # If the crew can't legally absorb the full schedule, the flight
                # count is reduced to what duty/rest limits allow. No legal crew
                # -> no flight, even with plane and gate available.
                from airlinesim.crew import is_legal_for_flight, crew_is_type_rated
                fh_per_rotation = (op.spec.distance_km / op.plane.spec.cruise_speed_kmh)
                crew_flew = ctx.setdefault("_crew_flew_this_tick", set())
                legal_rotations = eff_freq
                # a flight needs BOTH cockpit and cabin crew; a missing roster
                # assignment grounds the op entirely.
                if op.cockpit is None or op.cabin is None:
                    legal_rotations = 0.0
                    op.last_crew_block = op.last_crew_block or "no crew rostered"
                for crew in (op.cockpit, op.cabin):
                    if crew is None:
                        continue
                    # type-rating is a hard block (wrong aircraft type)
                    if not crew_is_type_rated(crew, op.plane.spec):
                        legal_rotations = 0.0
                        op.last_crew_block = f"{crew.spec.crew_type.name} not type-rated"
                        break
                    # how many rotations can this crew legally fly this tick?
                    added = fh_per_rotation * eff_freq * day_frac
                    ok, reason = is_legal_for_flight(crew, world.sim_time, added, crew.limits)
                    if not ok:
                        # find the max rotations that WOULD be legal (linear scan down)
                        max_legal = 0.0
                        for r in range(int(eff_freq), 0, -1):
                            a = fh_per_rotation * r * day_frac
                            ok2, _ = is_legal_for_flight(crew, world.sim_time, a, crew.limits)
                            if ok2:
                                max_legal = r
                                break
                        legal_rotations = min(legal_rotations, max_legal)
                        op.last_crew_block = f"{crew.spec.crew_type.name} {reason}"
                eff_freq = max(0.0, min(eff_freq, legal_rotations))
                freq_ratio = (eff_freq / desired_freq) if desired_freq > 0 else 0.0
                op.last_eff_freq = eff_freq

                # log the actual flight hours onto the crews that flew
                if eff_freq > 0:
                    flown_fh = fh_per_rotation * eff_freq * day_frac
                    for crew in (op.cockpit, op.cabin):
                        if crew is not None:
                            crew.duty.log_flight(world.sim_time, flown_fh)
                            crew_flew.add(id(crew))

                layout = op.layout or op.plane.effective_layout()
                revenue = 0.0
                class_pax = {}
                class_seats = {}
                class_revenue = {}
                total_seats_offered = 0.0
                # deadheading crew occupy economy seats, removing them from sale
                dh_seats = getattr(op, "deadhead_seats", 0)
                dm = world.demand.get(market_key(op.spec))

                if dm and dm.segments:
                    # --- SEGMENT-DRIVEN REVENUE ---
                    # The arbiter already resolved each cabin's allocation
                    # against a segment-sized, price-elastic pool, in
                    # competition with rival carriers (and rival cabins on
                    # this same op, for a cabin fed by multiple segments) —
                    # no local re-splitting needed, just apply freq_ratio for
                    # flights that ended up gate/crew-limited.
                    for cc, cspec in DEFAULT_SEAT_CLASSES.items():
                        seats_cfg = layout.seats_of(cc)
                        if seats_cfg <= 0:
                            continue
                        seat_capacity = seats_cfg * eff_freq * day_frac
                        if cc == CabinClass.ECONOMY and dh_seats > 0:
                            seat_capacity = max(0.0, seat_capacity - dh_seats)
                        total_seats_offered += seat_capacity
                        granted = alloc_by_op_class.get((id(op), cc.name), 0.0)
                        seated = min(seat_capacity, granted * freq_ratio)
                        fare = op.fare_for(cc)
                        revenue += seated * fare
                        class_pax[cc.name] = seated
                        class_seats[cc.name] = seat_capacity
                        class_revenue[cc.name] = seated * fare
                else:
                    # --- LEGACY FLAT-POOL REVENUE (no segments configured) ---
                    # Each class draws from its demand_share of the carrier's
                    # won demand, THEN scales that by its own price response.
                    # Business is inelastic (raising fares barely dents
                    # demand); economy is elastic (the same % hike sheds far
                    # more). Class fare is the op's per-cabin price (or the
                    # base fare times the class multiplier), measured against
                    # a class reference (route reference * mult).
                    pax = alloc_by_op_demand.get(id(op), 0.0)
                    # carriage is bounded by the seats the OPERABLE flights provide
                    pax *= freq_ratio
                    route_ref = self.pricing.reference_price
                    for cc, cspec in DEFAULT_SEAT_CLASSES.items():
                        seats_cfg = layout.seats_of(cc)
                        if seats_cfg <= 0:
                            continue
                        seat_capacity = seats_cfg * eff_freq * day_frac
                        # crew deadhead in economy: those seats can't be sold
                        if cc == CabinClass.ECONOMY and dh_seats > 0:
                            seat_capacity = max(0.0, seat_capacity - dh_seats)
                        total_seats_offered += seat_capacity
                        fare = op.fare_for(cc)
                        # The reference a fare is measured against stays the
                        # class's multiple of the route reference, so pricing
                        # a cabin ABOVE the class norm sheds demand exactly as
                        # raising the base fare by the same ratio would.
                        class_ref = route_ref * cspec.price_multiplier
                        # per-class price response: (fare/ref)^elasticity. elasticity<0,
                        # so fare above ref -> factor<1 (demand falls). Inelastic classes
                        # (business/first) barely move; economy swings a lot.
                        price_factor = (fare / class_ref) ** cspec.elasticity \
                            if class_ref > 0 else 1.0
                        class_demand = pax * cspec.demand_share * price_factor
                        seated = min(seat_capacity, class_demand)
                        revenue += seated * fare
                        class_pax[cc.name] = seated
                        class_seats[cc.name] = seat_capacity
                        class_revenue[cc.name] = seated * fare

                op.last_class_pax = class_pax
                op.last_class_seats = class_seats
                op.last_class_revenue = class_revenue
                actual_pax = sum(class_pax.values())
                op.last_pax = actual_pax

                # record load factor against actual offered seats
                offered = total_seats_offered if total_seats_offered > 1e-6 else \
                    op.plane.spec.max_seats * eff_freq * day_frac
                op.last_load_factor = (actual_pax / offered) if offered > 1e-6 else 0.0

                p.ledger.credit(revenue,
                                f"tickets {op.spec.origin_iata}->{op.spec.dest_iata} ({actual_pax:.0f}px)", p.log)
                # fuel cost at the spot price (scarcity-driven) — only for flights flown
                fm = world.fuel.get(op.spec.dest_iata)
                fuel_l = alloc_by_op_fuel[id(op)] * freq_ratio
                fuel_cost = fuel_l * (fm.spot_price() if fm else 0.9)
                p.ledger.debit(fuel_cost, f"fuel {op.spec.dest_iata} ({fuel_l:,.0f}L)", p.log)
                # crew + maintenance accrual — scale to flights actually operated
                fh = (op.spec.distance_km / op.plane.spec.cruise_speed_kmh) * eff_freq * day_frac
                cockpit_cost = op.cockpit.hourly_cost() if op.cockpit else 0.0
                cabin_cost = op.cabin.hourly_cost() if op.cabin else 0.0
                crew_cost = (cockpit_cost + cabin_cost) * fh
                if crew_cost > 0:
                    p.ledger.debit(crew_cost, f"flight crew {op.plane.tail_number}", p.log)
                self.maint.accrue(op.plane, flight_hours=fh, calendar_days=day_frac)

                # --- AIRPORT FEES ---
                # Per operated flight: landing (dest, scaled by how much
                # aircraft is being put on the runway) and gate turns at both
                # ends. Per carried passenger: amenities and baggage handling.
                # All indexed by the op's service tier, so buying a better
                # passenger experience genuinely costs more.
                fees = self._airport_fees(world, op, eff_freq * day_frac, actual_pax)
                if fees > 0:
                    p.ledger.debit(fees, f"airport fees {op.spec.origin_iata}/"
                                         f"{op.spec.dest_iata} (tier {op.service_tier})", p.log)
                op.last_fees = fees

                op.last_revenue = revenue
                op.last_variable_cost = fuel_cost + crew_cost + fees
                op.last_profit = revenue - (fuel_cost + crew_cost + fees)

    def _desirability(self, op: RouteOp) -> float:
        """
        Non-price attractiveness of this op — what makes a passenger pick one
        carrier over another at the same fare. Neutral 1.0 for a default
        service tier, so worlds that never touch service behave exactly as
        they did before this existed.
        """
        from airlinesim.route import service_desirability
        repo = self.arbiter.world.repo

        def access(iata):
            try:
                return repo.get(AirportSpec, iata).access_index
            except KeyError:
                return 1.0
        return service_desirability(op.service_tier, access(op.spec.origin_iata),
                                    access(op.spec.dest_iata))

    # landing fees are weight-based in reality; a class multiplier stands in.
    _LANDING_CLASS_MULT = {
        PlaneClass.REGIONAL: 0.55, PlaneClass.NARROWBODY: 1.0, PlaneClass.WIDEBODY: 1.9,
    }

    def _airport_fees(self, world: World, op: RouteOp, flights: float,
                      pax: float) -> float:
        """
        Airport charges for the flights actually operated this tick. Returns 0
        for worlds whose airport specs carry no fee schedule, so legacy
        scenarios are unaffected.
        """
        if flights <= 0:
            return 0.0
        repo = world.repo
        tier = op.service_tier
        total = 0.0
        try:
            origin = repo.get(AirportSpec, op.spec.origin_iata)
        except KeyError:
            origin = None
        try:
            dest = repo.get(AirportSpec, op.spec.dest_iata)
        except KeyError:
            dest = None

        if dest is not None and dest.landing_fee:
            mult = self._LANDING_CLASS_MULT.get(op.plane.spec.plane_class, 1.0)
            total += dest.landing_fee * mult * flights
        # a turn is bought at both ends of the leg
        for ap in (origin, dest):
            if ap is not None:
                total += ap.fee_at_tier(ap.gate_fee_by_tier, tier) * flights
        # per-passenger service costs, charged where the pax are handled
        if pax > 0:
            for ap in (origin, dest):
                if ap is None:
                    continue
                total += ap.fee_at_tier(ap.amenities_fee_by_tier, tier) * pax
                total += ap.fee_at_tier(ap.baggage_fee_by_tier, tier) * pax
        return total


class MaintenanceSubsystem(Subsystem):
    def __init__(self, maint: MaintenanceEngine):
        self.maint = maint

    def tick(self, world: World, players: list, dt: float, ctx: dict):
        players_by_id = {p.player_id: p for p in players}
        for p in players:
            for plane in p.fleet:
                # a tail in the shop for a recabin isn't available for checks,
                # and returns to service on its own schedule (no MX job, no crew)
                if plane.reconfiguring_until > 0:
                    if world.sim_time >= plane.reconfiguring_until:
                        plane.reconfiguring_until = 0.0
                        plane.in_service = True
                        p.log.append(f"  {plane.tail_number}: reconfiguration complete, back in service")
                    continue
                self.maint.try_schedule(plane, p.maintenance_crews(), world.sim_time,
                                        p.log, getattr(p, "hub_iatas", None))
        self.maint.update(world.sim_time, players_by_id, world.log)


class BankingSubsystem(Subsystem):
    """
    Services player debt each tick: amortizing loan payments and lease rent.
    Expired leases incur the return-condition cost and are retired. This is the
    cash-flow consequence of the acquisition method chosen at purchase time.
    """
    def tick(self, world: World, players: list, dt: float, ctx: dict):
        for p in players:
            # loan payments (interest + principal)
            for loan in list(p.loans):
                due = loan.accrue_and_bill(dt)
                if due > 0:
                    p.ledger.debit(due, f"loan {loan.loan_id} pmt ({loan.tail_number})", p.log)
                if loan.remaining <= 0:
                    p.log.append(f"  loan {loan.loan_id} PAID OFF ({loan.tail_number})")
                    p.loans.remove(loan)
            # lease rent
            for lease in list(p.leases):
                rent = lease.accrue_and_bill(dt)
                if rent > 0:
                    p.ledger.debit(rent, f"lease {lease.lease_id} rent ({lease.tail_number})", p.log)
                if lease.expired():
                    p.ledger.debit(lease.return_cost,
                                   f"lease {lease.lease_id} return condition", p.log)
                    p.log.append(f"  lease {lease.lease_id} EXPIRED ({lease.tail_number}) — "
                                 f"aircraft returned to lessor")
                    # This is the one place the engine takes an aircraft AND
                    # its routes away from a player without the player asking,
                    # so it gets a durable record: "where did my fleet go?" is
                    # otherwise only answerable from the in-memory log.
                    dropped = [o.spec.spec_id for o in p.route_ops
                               if o.plane.tail_number == lease.tail_number]
                    _log.info("day %d %s: lease %s expired, %s returned to "
                              "lessor%s", int(world.sim_time // 24), p.player_id,
                              lease.lease_id, lease.tail_number,
                              f", closing {dropped}" if dropped else "")
                    # remove the leased aircraft from the fleet (lessor reclaims it)
                    p.fleet = [a for a in p.fleet if a.tail_number != lease.tail_number]
                    p.route_ops = [o for o in p.route_ops if o.plane.tail_number != lease.tail_number]
                    p.leases.remove(lease)


class FinanceSubsystem(Subsystem):
    """Standing payroll for non-flight staff, plus hub overhead."""
    def tick(self, world: World, players: list, dt: float, ctx: dict):
        day_frac = dt / 24.0
        for p in players:
            for c in p.crews:
                if c.spec.crew_type in (CrewType.GROUND, CrewType.BAGGAGE,
                                        CrewType.METEOROLOGY, CrewType.MAINTENANCE):
                    p.ledger.debit(c.hourly_cost() * dt, f"{c.spec.crew_type.name} payroll", p.log)
            # running an airport as a maintenance base costs whether or not
            # anything is in the hangar today — that's what makes hub choice
            # a real commitment rather than a free capability.
            for iata in getattr(p, "hub_iatas", []):
                try:
                    ap = world.repo.get(AirportSpec, iata)
                except KeyError:
                    continue
                if ap.hub_fee_per_day:
                    p.ledger.debit(ap.hub_fee_per_day * day_frac, f"hub overhead {iata}", p.log)


class AIStrategySubsystem(Subsystem):
    """
    Profit-aware reactive NPC. Runs BEFORE Operations so changes take effect now.

    Core idea: hill-climb on PROFIT, not load factor. Each tick the AI compares
    its latest profit to the previous tick's. If its last price move improved
    profit, it keeps moving that way; if profit fell, it reverses. This converges
    on the profit-maximizing price instead of racing to the floor.

    Guardrails:
      - never price below estimated unit variable cost (no selling at a loss)
      - shade toward undercutting a rival ONLY while still above unit cost
      - small step size with reversal = damped search, not wild swings
    """
    def __init__(self, step_frac: float = 0.03, price_floor: float = 60.0,
                 price_ceiling: float = 450.0, margin_floor_mult: float = 1.15,
                 max_freq_per_plane: int = 8, lf_add_threshold: float = 0.85,
                 lf_cut_threshold: float = 0.45):
        self.step = step_frac
        self.floor = price_floor
        self.ceiling = price_ceiling
        self.margin_floor_mult = margin_floor_mult   # min price = unit_cost * this
        # frequency strategy bounds
        self.max_freq_per_plane = max_freq_per_plane  # physical daily-rotation cap
        self.lf_add = lf_add_threshold                # add a flight above this LF
        self.lf_cut = lf_cut_threshold                # cut a flight below this LF

    def _unit_cost(self, op) -> float:
        """Estimated variable cost per passenger from last tick's actuals."""
        if op.last_pax > 1e-6 and op.last_variable_cost > 0:
            return op.last_variable_cost / op.last_pax
        return 0.0

    def tick(self, world: World, players: list, dt: float, ctx: dict):
        route_prices: dict[str, list] = defaultdict(list)
        for p in players:
            for op in p.route_ops:
                route_prices[op.spec.spec_id].append((p.player_id, op.ticket_price))

        for p in players:
            if not p.is_ai:
                continue
            for op in p.route_ops:
                old = op.ticket_price
                unit_cost = self._unit_cost(op)
                # dynamic price floor: cost-plus margin (never sell below this)
                cost_floor = max(self.floor, unit_cost * self.margin_floor_mult)

                # --- profit hill-climb ---
                # Did the LAST move help? Compare last_profit to prev_profit.
                if op.prev_price > 0:
                    improved = op.last_profit > op.prev_profit
                    if not improved:
                        op.price_dir *= -1            # reverse: we overshot the peak
                # take a damped step in the current direction
                target = old * (1 + op.price_dir * self.step)

                # --- competitive shading (only while profitable) ---
                rivals = [pr for (pid, pr) in route_prices[op.spec.spec_id]
                          if pid != p.player_id]
                if rivals:
                    cheapest = min(rivals)
                    # if a rival undercuts us AND we have margin room, edge just under
                    if cheapest < old and cheapest * 0.99 > cost_floor:
                        target = min(target, cheapest * 0.99)

                # --- clamp + commit ---
                target = max(cost_floor, min(self.ceiling, target))
                # remember state for next tick's comparison BEFORE overwriting
                op.prev_profit = op.last_profit
                op.prev_price = old
                op.ticket_price = round(target, 2)

                if abs(op.ticket_price - old) >= 0.01:
                    p.log.append(f"  [AI] {op.spec.spec_id}: ${old:.0f}->${op.ticket_price:.0f} "
                                 f"(profit ${op.last_profit:,.0f}, unit-cost ${unit_cost:.0f}, "
                                 f"dir {'+' if op.price_dir>0 else '-'})")

                # --- CAPACITY STRATEGY: add/cut frequency, capacity-aware ---
                # Signals: was the last flight gate-denied? is it selling out at margin?
                old_freq = op.daily_frequency
                lf = op.last_load_factor
                profitable = op.last_profit > 0
                # gate-denied if we operated fewer flights than we asked for
                gate_denied = op.last_eff_freq < old_freq - 1e-6

                new_freq = old_freq
                if op.last_pax <= 0 and op.last_eff_freq <= 0:
                    # no operating history yet (first tick) -> don't touch frequency
                    pass
                elif gate_denied:
                    # no point requesting flights we can't get gates for —
                    # trim toward what actually operated (frees the wasted claim)
                    new_freq = max(1, int(round(op.last_eff_freq)))
                elif lf >= self.lf_add and profitable and old_freq < self.max_freq_per_plane:
                    # selling out profitably and physically able -> add a rotation
                    new_freq = old_freq + 1
                elif lf <= self.lf_cut and old_freq > 1:
                    # chronically empty -> drop a rotation to cut fuel/crew burn
                    new_freq = old_freq - 1

                if new_freq != old_freq:
                    op.daily_frequency = new_freq
                    p.log.append(f"  [AI] {op.spec.spec_id}: freq {old_freq}->{new_freq} "
                                 f"(LF {lf:.0%}, {'gate-denied' if gate_denied else 'profit-led'})")


# ============================================================
# ENGINE — the tick pipeline
# ============================================================

class SimulationEngine:
    def __init__(self, world: World):
        self.world = world
        self.players: list = []
        self.subsystems: list = []
        self.dt = 24.0   # hours per tick (resolution knob)
        self._last_day = -1

    def add_player(self, p: Player):
        self.players.append(p)

    def add_subsystem(self, s: Subsystem):
        self.subsystems.append(s)

    def tick(self, ctx: dict):
        # daily market reset (gates free up, fuel resupplies)
        day = int(self.world.sim_time // 24)
        if day != self._last_day:
            self.world.reset_daily_markets()
            self._last_day = day
            # roll crew calendar day BEFORE any subsystem runs, so daily duty
            # counters are fresh when Operations logs to them and stay readable.
            for p in self.players:
                seen = set()
                pools = (list(p.crews) + list(getattr(p, "cockpit_pool", []))
                         + list(getattr(p, "cabin_pool", [])))
                for op in p.route_ops:
                    pools += [op.cockpit, op.cabin]
                for c in pools:
                    if c is not None and id(c) not in seen and getattr(c, "duty", None):
                        seen.add(id(c))
                        c.duty.roll_day(day)
        # reset per-tick crew-flew tracking (Operations fills it, CrewLegality reads it)
        ctx["_crew_flew_this_tick"] = set()
        ctx["_crew_deadheaded_this_tick"] = set()
        for s in self.subsystems:
            s.tick(self.world, self.players, self.dt, ctx)
        self.world.sim_time += self.dt

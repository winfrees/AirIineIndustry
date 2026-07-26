"""
BUILD A WORLD FROM REAL DATA — the engine seam for historic route data.
=====================================================================

build_world_from_data() is build_demo_world()'s data-driven twin: same pipeline,
same subsystems, same competition model, but the airports, routes, demand,
seasonality and equipment requirements come from the BTS corpus in
airlinesim/data/ instead of hand-authored constants.

Specs are loaded through SpecRepository.load() — the import seam CLAUDE.md
reserved for exactly this ("hand-authored dicts today, real-world data later, no
engine changes"), and which nothing used until now. No engine or subsystem change
was needed to get here beyond RouteSpec's two additive provenance fields.

Equipment is CHOSEN, not assumed: each route's data-derived seat window and stage
length pick the smallest type in the catalog that satisfies them, so a thin
regional pair gets an E175 and a transcon gets a widebody. That's what makes a
data-built world actually fly — a fixed A320 fleet would be marked unsuitable on
half the corpus by the existing RouteSuitabilitySubsystem.

What is NOT data-driven here, and why: ticket prices and elasticity are engine
defaults because no DB1B fares are loaded yet, and the traveler-segment mix is
route.py's global default for the same reason. Both are flagged in the returned
report rather than hidden.
"""
from __future__ import annotations

from airlinesim.engine import (
    SpecRepository, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    MaintenanceProgram, CheckDefinition, CheckTier, PlaneClass, CrewType,
    World, Player, Airplane, CrewUnit, RouteOp, Ledger,
    PricingModel, MarketConditions, ResourceArbiter, MaintenanceEngine,
    SimulationEngine, OperationsSubsystem, MaintenanceSubsystem,
    FinanceSubsystem, BankingSubsystem, RouteSuitabilitySubsystem,
)
from airlinesim.crew import (
    CrewLegalitySubsystem, RosterSubsystem, CrewPositioningSubsystem,
    DeadheadSubsystem, DEFAULT_DUTY_LIMITS,
)
from airlinesim.finance_cabin import (
    CabinClass, SeatLayout, AcquisitionMethod, FinancingTerms, Bank,
)
from airlinesim.routedata import load_provider, DataTier


# ------------------------------------------------------------
# Fleet catalog — industry-SHAPED, not certified figures (see CLAUDE.md).
# Ordered small to large so equipment selection can take the first fit.
# ------------------------------------------------------------

def _program(narrow_or_wide: PlaneClass, scale: float = 1.0) -> MaintenanceProgram:
    return MaintenanceProgram(checks=(
        CheckDefinition(CheckTier.A, 750, 90, 10, int(60 * scale),
                        int(8000 * scale), PlaneClass.NARROWBODY),
        CheckDefinition(CheckTier.C, 6000, 730, 240, int(6000 * scale),
                        int(350000 * scale), narrow_or_wide),
        CheckDefinition(CheckTier.D, 20000, 2920, 720, int(40000 * scale),
                        int(3000000 * scale), PlaneClass.WIDEBODY)),
        b_folded_into_a=True)


FLEET_CATALOG = (
    dict(spec_id="E175", display_name="E175", manufacturer="Embraer",
         plane_class=PlaneClass.REGIONAL if hasattr(PlaneClass, "REGIONAL")
         else PlaneClass.NARROWBODY,
         list_price=38_000_000, max_seats=76, max_range_km=3900,
         cruise_speed_kmh=797, fuel_burn_lph=1300, maint_cost_per_hour=700,
         scale=0.55),
    dict(spec_id="A320", display_name="A320neo", manufacturer="Airbus",
         plane_class=PlaneClass.NARROWBODY,
         list_price=110_000_000, max_seats=180, max_range_km=6300,
         cruise_speed_kmh=833, fuel_burn_lph=2400, maint_cost_per_hour=1100,
         scale=1.0),
    dict(spec_id="B789", display_name="787-9", manufacturer="Boeing",
         plane_class=PlaneClass.WIDEBODY,
         list_price=290_000_000, max_seats=290, max_range_km=14000,
         cruise_speed_kmh=903, fuel_burn_lph=5600, maint_cost_per_hour=2600,
         scale=2.4),
)


def _aircraft_specs() -> list:
    out = []
    for row in FLEET_CATALOG:
        row = dict(row)
        scale = row.pop("scale")
        cls = row["plane_class"]
        out.append(AircraftSpec(
            maint_program=_program(
                PlaneClass.WIDEBODY if cls is PlaneClass.WIDEBODY
                else PlaneClass.NARROWBODY, scale),
            **row))
    return out


def choose_aircraft(route_spec, fleet: list):
    """
    Smallest type that satisfies the route's data-derived requirements: range
    with the reserve margin already folded in, and the economic seat window from
    the corpus. Returns None when nothing in the catalog fits — the caller skips
    that route rather than knowingly building an unsuitable op.
    """
    eq = route_spec.equipment_req
    for spec in fleet:                       # catalog is ordered small -> large
        if spec.max_range_km < route_spec.distance_km:
            continue
        if eq is not None:
            if not (eq.min_viable_seats <= spec.max_seats <= eq.max_viable_seats):
                continue
        return spec
    return None


# How much of a real market one simulated carrier goes after, and how hard it
# works an airframe. Real ORD-LGA is served by several carriers at high
# frequency; a single op at one flight a day would fly ~50% full against a
# 3,400 px/day market and make the corpus look wrong when it isn't.
CARRIER_MARKET_SHARE = 0.45      # two carriers, neither expecting the whole market
DAILY_UTILIZATION_H = 14.0       # airframe hours available per day
CREW_DEPTH = 2.5                 # crews per op based at a station (rest rotation)


def daily_frequency(route_spec, aircraft_spec) -> int:
    """
    Frequency implied by the measured market and the chosen aircraft, capped by
    what one airframe can physically fly in a day.

    Without this the demand side is real but the supply side is a token single
    rotation, and every load factor reads as a capacity failure rather than a
    market outcome. Crew duty limits and gate contention still cut this down
    during the tick — that's the simulation doing its job, not a miscount.
    """
    from airlinesim.route import block_hours
    seats = max(1, aircraft_spec.max_seats)
    want = (route_spec.base_demand_per_day * CARRIER_MARKET_SHARE) / (seats * 0.85)
    block = block_hours(route_spec.distance_km, aircraft_spec.cruise_speed_kmh)
    by_airframe = max(1, int(DAILY_UTILIZATION_H / max(0.5, block)))
    return max(1, min(int(round(want)) or 1, by_airframe))


def _layout(spec: AircraftSpec, premium: bool) -> SeatLayout:
    if not premium or spec.max_seats < 100:
        return SeatLayout.all_economy(spec.max_seats)
    biz = max(8, int(spec.max_seats * 0.09))
    return SeatLayout({CabinClass.ECONOMY: spec.max_seats - biz,
                       CabinClass.BUSINESS: biz})


# ------------------------------------------------------------
# world construction
# ------------------------------------------------------------

def build_world_from_data(hub: str = "ORD", n_destinations: int = 4,
                          provider=None, cash: float = 0.0,
                          verbose: bool = True):
    """
    Return (world, engine, report). Picks the busiest routes out of `hub` from
    the corpus, builds both directions of each, and stands up two competing
    carriers — one financing its fleet, one leasing, mirroring build_demo_world's
    contrast so the finance paths stay exercised.

    `report` carries per-route provenance (which tier each spec came from) plus
    the corpus gaps, so a caller can see what is measured and what is not.
    """
    provider = provider or load_provider()
    if provider is None:
        raise RuntimeError(
            "no route-data snapshot found in airlinesim/data — run:\n"
            "  airlinesim ingest --t100-market <export.zip> "
            "--fetch-airport-ref --distill")

    repo = SpecRepository()

    # --- aircraft catalog through the seam ---
    repo.load(AircraftSpec, [{"spec": s} for s in _aircraft_specs()],
              lambda row: row["spec"])
    fleet_catalog = [repo.get(AircraftSpec, r["spec_id"]) for r in FLEET_CATALOG]

    # --- pick the busiest destinations out of the hub, measured pairs only ---
    candidates = []
    for iata in provider.airports:
        if iata == hub:
            continue
        obs = provider.observation(hub, iata)
        back = provider.observation(iata, hub)
        if obs.tier is DataTier.EXACT and back.tier is DataTier.EXACT:
            candidates.append((obs.demand_per_day, iata))
    candidates.sort(reverse=True)
    if not candidates:
        raise RuntimeError(f"no measured routes out of {hub} in the corpus")
    destinations = [iata for _, iata in candidates[:n_destinations]]

    # --- airports through the seam ---
    codes = [hub] + destinations
    repo.load(AirportSpec, [{"iata": c} for c in codes],
              lambda row: provider.airport_spec(row["iata"]))

    # --- routes through the seam (both directions) ---
    pairs = [(hub, d) for d in destinations] + [(d, hub) for d in destinations]
    repo.load(RouteSpec, [{"o": o, "d": d} for o, d in pairs],
              lambda row: provider.route_spec(row["o"], row["d"]))

    world = World(repo)
    for c in codes:
        world.add_airport_resources(repo.get(AirportSpec, c), 0.82)

    # --- equipment selection per route, then demand markets ---
    ops_plan, skipped = [], []
    for o, d in pairs:
        rs = repo.get(RouteSpec, f"{o}-{d}")
        ac = choose_aircraft(rs, fleet_catalog)
        if ac is None:
            skipped.append((f"{o}-{d}", "no catalog aircraft fits the seat window"))
            continue
        world.add_demand_market(rs)
        ops_plan.append((rs, ac))

    if not ops_plan:
        raise RuntimeError("no route in the corpus could be equipped")

    # --- engine + pipeline (same order as build_demo_world) ---
    pricing = PricingModel(elasticity=-1.3, reference_price=200.0)
    arbiter = ResourceArbiter(world, pricing)
    maint = MaintenanceEngine(repo)
    engine = SimulationEngine(world)
    engine.dt = 24.0
    for s in (RouteSuitabilitySubsystem(), DeadheadSubsystem(), RosterSubsystem(),
              BankingSubsystem(), FinanceSubsystem(),
              OperationsSubsystem(arbiter, pricing, maint),
              CrewPositioningSubsystem(), MaintenanceSubsystem(maint),
              CrewLegalitySubsystem(DEFAULT_DUTY_LIMITS)):
        engine.add_subsystem(s)

    # Auto-size starting cash to the down payments the chosen fleet actually
    # needs, plus working capital. A fixed figure silently under-funds a
    # widebody-heavy corpus, and the financing carrier then flies routes on
    # aircraft it never bought.
    if cash <= 0:
        down = sum(ac.list_price * 0.20 for _, ac in ops_plan)
        cash = max(40_000_000, down * 1.35)

    bank = Bank(max_debt_to_cash=6.0)
    loan = FinancingTerms("LOAN", AcquisitionMethod.FINANCE, 0.20, 0.06, 120)
    lease = FinancingTerms("LEASE", AcquisitionMethod.OPERATING_LEASE,
                           lease_rate_frac_per_year=0.11, lease_term_months=84)

    for pid, name, method, terms, premium in (
            ("FIN", "FinanceAir", AcquisitionMethod.FINANCE, loan, True),
            ("LSE", "LeaseLine", AcquisitionMethod.OPERATING_LEASE, lease, False)):
        engine.add_player(_carrier(world, bank, pid, name, method, terms,
                                   ops_plan, codes, premium, cash))

    report = {
        "hub": hub,
        "destinations": destinations,
        "vintage": provider.vintage,
        "starting_cash": cash,
        # Routes a carrier planned but couldn't fund — the bank's leverage cap
        # biting is legitimate simulation behaviour, but it must be visible here
        # rather than buried in a player log.
        "unfunded": {p.name: [l.strip() for l in p.log if "NOT ACQUIRED" in l]
                     for p in engine.players},
        "routes": [{"route": f"{rs.origin_iata}-{rs.dest_iata}",
                    "tier": rs.data_tier,
                    "demand_per_day": rs.base_demand_per_day,
                    "distance_km": round(rs.distance_km),
                    "season_amp": round(rs.seasonality_amplitude, 3),
                    "aircraft": ac.spec_id, "seats": ac.max_seats,
                    "seat_window": (rs.equipment_req.min_viable_seats,
                                    rs.equipment_req.max_viable_seats)}
                   for rs, ac in ops_plan],
        "skipped": skipped,
        "corpus_gaps": provider.manifest.get("known_gaps", []),
        "not_from_data": [
            "ticket price and elasticity (no DB1B fares loaded — engine defaults)",
            "traveler-segment mix (route.py global default, not per-route)",
            "day-of-week profile (T-100 is monthly; no trip-purpose data)",
        ],
    }

    if verbose:
        print(f"Built a world from BTS data: hub {hub}, vintage {provider.vintage}")
        for r in report["routes"]:
            print(f"  {r['route']:8s} {r['tier']:9s} "
                  f"{r['demand_per_day']:>6,}px/day {r['distance_km']:>5,}km "
                  f"amp={r['season_amp']:.3f}  -> {r['aircraft']} "
                  f"({r['seats']} seats, window "
                  f"{r['seat_window'][0]}-{r['seat_window'][1]})")
        for route, why in skipped:
            print(f"  {route:8s} SKIPPED: {why}")
    return world, engine, report


def _carrier(world, bank, pid, name, method, terms, ops_plan, bases,
             premium, cash):
    p = Player(pid, name)
    p.ledger = Ledger(cash=cash)
    owned = method != AcquisitionMethod.OPERATING_LEASE

    types_needed = {ac.spec_id: ac for _, ac in ops_plan}
    acquired = []
    for i, (rs, ac) in enumerate(ops_plan):
        tail = f"{pid}-{i+1}"
        # Only keep what actually funded — see Bank.try_acquire(). Attaching the
        # Airplane unconditionally puts aircraft in the fleet that were never
        # paid for, which silently inflates net worth.
        if not bank.try_acquire(p, ac, tail, method, terms, p.log):
            p.log.append(f"  NOT ACQUIRED {tail} ({ac.display_name}): "
                         f"acquisition failed, route {rs.spec_id} unstaffed")
            continue
        plane = Airplane(spec=ac, tail_number=tail, owner_id=pid, owned=owned,
                         location_iata=rs.origin_iata, acquired_at=world.sim_time)
        p.fleet.append(plane)
        acquired.append((rs, ac, plane))

    # Maintenance staff certified on every type actually operated.
    p.crews.append(CrewUnit(
        CrewSpec("MX", "MX", crew_type=CrewType.MAINTENANCE,
                 cost_per_member_hour=75,
                 certifications=tuple(types_needed)),
        headcount=6 + 2 * len(types_needed), owner_id=pid))

    # Crew pools sized to the flying actually based at each station.
    #
    # A flat two crews per base looked reasonable and silently grounded half a
    # carrier: a hub originating four routes needs more than two cockpit crews,
    # and the roster reported "no legal crew available" while every aircraft sat
    # serviceable. Depth is ops-at-base x CREW_DEPTH so a crew can rest while
    # another flies; the roster stays conservative and may still leave some
    # capacity unflown, which is a known engine limitation rather than this
    # builder under-staffing.
    ops_at = {}
    for rs, _ac, _pl in acquired:
        ops_at[rs.origin_iata] = ops_at.get(rs.origin_iata, 0) + 1
    for base in bases:
        depth = max(2, int(round(ops_at.get(base, 0) * CREW_DEPTH)))
        for i in range(depth):
            p.cockpit_pool.append(CrewUnit(
                CrewSpec("FD", f"FD-{base}{i}", crew_type=CrewType.COCKPIT,
                         cost_per_member_hour=220,
                         certifications=tuple(types_needed)),
                headcount=2, owner_id=pid, home_iata=base))
            p.cabin_pool.append(CrewUnit(
                CrewSpec("CC", f"CC-{base}{i}", crew_type=CrewType.CABIN,
                         cost_per_member_hour=60),
                headcount=4, owner_id=pid, home_iata=base))

    for rs, ac, plane in acquired:
        p.route_ops.append(RouteOp(
            spec=rs, plane=plane, cockpit=None, cabin=None,
            ticket_price=220.0 if premium else 195.0,
            daily_frequency=daily_frequency(rs, ac), owner_id=pid,
            layout=_layout(ac, premium)))
    return p


def run_from_data(days: int = 60, hub: str = "ORD", n_destinations: int = 4,
                  verbose: bool = True):
    """Convenience: build a data-driven world and advance it `days` ticks."""
    from airlinesim.finance_cabin import aircraft_value
    world, engine, report = build_world_from_data(
        hub=hub, n_destinations=n_destinations, verbose=verbose)
    ctx = {"market": MarketConditions()}
    for _ in range(days):
        engine.tick(ctx)
    if verbose:
        print(f"\nSimulated {days} days (t={world.sim_time:.0f}h)\n")
        for p in engine.players:
            debt = sum(l.remaining for l in p.loans)
            assets = sum(aircraft_value(a, world.sim_time)
                         for a in p.fleet if a.owned)
            pax = sum(o.last_pax for o in p.route_ops)
            print(f"  {p.name:11s} cash ${p.ledger.cash/1e6:7.1f}M  "
                  f"debt ${debt/1e6:6.1f}M  "
                  f"net worth ${(p.ledger.cash + assets - debt)/1e6:7.1f}M  "
                  f"{pax:,.0f} px/day")
    return world, engine, report

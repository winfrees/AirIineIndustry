"""
Convenience builders — the easy on-ramp to the engine.

build_demo_world() wires a complete, runnable two-carrier simulation with every
subsystem in the correct pipeline order. run() advances it and prints a summary.
Use these to get started, then copy and customize.
"""
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
    CabinClass, SeatLayout, AcquisitionMethod, FinancingTerms, Bank, aircraft_value,
)
from airlinesim.route import default_segments, EquipmentRequirements, CrewRequirements


def _a320(repo):
    spec = AircraftSpec(
        spec_id="A320", display_name="A320neo", manufacturer="Airbus",
        plane_class=PlaneClass.NARROWBODY, list_price=110_000_000, max_seats=180,
        max_range_km=6300, cruise_speed_kmh=833, fuel_burn_lph=2400,
        # published economy seats per row — the one measured input to cabin
        # geometry. Without it the demo world's cabins are still fitted, just
        # off an estimate (and flagged as one in the UI).
        cabin_abreast=6,
        # A recabin in the sandbox is a real decision too: without a cost and
        # a downtime the "choose well at acquisition" trade-off is free here
        # and expensive on the data world, for no reason a player can see.
        reconfig_cost_per_slot=14_000, reconfig_days=14,
        maint_program=MaintenanceProgram(checks=(
            CheckDefinition(CheckTier.A, 750, 90, 10, 60, 8000, PlaneClass.NARROWBODY),
            CheckDefinition(CheckTier.C, 6000, 730, 240, 6000, 350000, PlaneClass.NARROWBODY),
            CheckDefinition(CheckTier.D, 20000, 2920, 720, 40000, 3000000, PlaneClass.WIDEBODY)),
            b_folded_into_a=True))
    repo._tables[AircraftSpec]["A320"] = spec
    return spec


# Enough for both A320 down payments (20% of $110M each) plus working capital.
# At $40M the second financing was denied for want of $4M, and the aircraft was
# attached anyway — see _carrier.
START_CASH = 60_000_000


def _carrier(world, bank, a320, out_r, ret_r, pid, name, method, terms, layout, price):
    p = Player(pid, name)
    p.ledger = Ledger(cash=START_CASH)
    owned = method != AcquisitionMethod.OPERATING_LEASE

    # Only aircraft that actually funded join the fleet, and each one carries its
    # own route op — see Bank.try_acquire(). Attaching the Airplane regardless of
    # the bank's answer put aircraft in the fleet that were never paid for:
    # FinanceAir flew two A320s against one loan, and the un-financed airframe
    # still counted as an owned asset, overstating net worth.
    p.fleet, ops = [], []
    for idx, (route, base) in enumerate(((out_r, "ORG"), (ret_r, "HUB")), start=1):
        tail = f"{pid}-{idx}"
        if not bank.try_acquire(p, a320, tail, method, terms, p.log):
            p.log.append(f"  NOT ACQUIRED {tail}: route {route.spec_id} not opened")
            continue
        plane = Airplane(spec=a320, tail_number=tail, owner_id=pid, owned=owned,
                         location_iata=base, acquired_at=world.sim_time)
        p.fleet.append(plane)
        ops.append(RouteOp(spec=route, plane=plane, cockpit=None, cabin=None,
                           ticket_price=price, daily_frequency=1, owner_id=pid,
                           layout=layout))
    p.crews.append(CrewUnit(CrewSpec("MX", "MX", crew_type=CrewType.MAINTENANCE,
                   cost_per_member_hour=75, certifications=("A320",)), headcount=8, owner_id=pid))
    for base in ("ORG", "HUB"):
        for i in range(2):
            p.cockpit_pool.append(CrewUnit(CrewSpec("FD", f"FD-{base}{i}", crew_type=CrewType.COCKPIT,
                cost_per_member_hour=220, certifications=("A320",)), headcount=2, owner_id=pid, home_iata=base))
            p.cabin_pool.append(CrewUnit(CrewSpec("CC", f"CC-{base}{i}", crew_type=CrewType.CABIN,
                cost_per_member_hour=60), headcount=4, owner_id=pid, home_iata=base))
    p.route_ops = ops
    return p


def build_demo_world():
    """Return (world, engine) for a ready-to-run two-carrier simulation."""
    repo = SpecRepository()
    a320 = _a320(repo)
    # The sandbox's two airports are fictional, but they need real POSITIONS:
    # weather is geographic, so an airport at (0, 0) gets no climate and no
    # weather at all — which left the demo world unable to demonstrate any of
    # it, and the explorer's weather knobs inert on the tree it roots by
    # default. These sit on the real continental interior, ~1,430 km apart,
    # which agrees with the 1,500 km the route spec declares and gives the
    # sandbox a genuine winter.
    for code, name, rwy, lat, lon in [
            ("ORG", "Origin Intl", 3800, 39.86, -104.67),
            ("HUB", "Hub Intl", 4000, 41.98, -87.90)]:
        repo._tables[AirportSpec][code] = AirportSpec(
            spec_id=code, display_name=name, iata=code, runway_length_m=rwy,
            total_gates=20, has_maintenance_facility=True,
            facility_max_class=PlaneClass.WIDEBODY, fuel_supply_per_day_l=3_000_000,
            lat=lat, lon=lon)
    eqr = EquipmentRequirements(1500, 2500, PlaneClass.NARROWBODY, 120, 240)
    out_r = RouteSpec(spec_id="ORG-HUB", display_name="Out", origin_iata="ORG", dest_iata="HUB",
                      distance_km=1500, base_demand_per_day=900,
                      segments=default_segments(900, 0.35, 0.45),
                      equipment_req=eqr, crew_req=CrewRequirements())
    ret_r = RouteSpec(spec_id="HUB-ORG", display_name="Return", origin_iata="HUB", dest_iata="ORG",
                      distance_km=1500, base_demand_per_day=900,
                      segments=default_segments(900, 0.35, 0.45),
                      equipment_req=eqr, crew_req=CrewRequirements())
    repo._tables[RouteSpec]["ORG-HUB"] = out_r
    repo._tables[RouteSpec]["HUB-ORG"] = ret_r

    world = World(repo)
    for code in ("ORG", "HUB"):
        world.add_airport_resources(repo._tables[AirportSpec][code], 0.82)
    world.add_demand_market(out_r)
    world.add_demand_market(ret_r)

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

    bank = Bank(max_debt_to_cash=6.0)
    loan = FinancingTerms("LOAN", AcquisitionMethod.FINANCE, 0.20, 0.06, 120)
    lease = FinancingTerms("LEASE", AcquisitionMethod.OPERATING_LEASE,
                           lease_rate_frac_per_year=0.11, lease_term_months=84)
    engine.add_player(_carrier(world, bank, a320, out_r, ret_r, "FIN", "FinanceAir",
                               AcquisitionMethod.FINANCE, loan,
                               SeatLayout({CabinClass.ECONOMY: 138, CabinClass.BUSINESS: 16}), 220))
    engine.add_player(_carrier(world, bank, a320, out_r, ret_r, "LSE", "LeaseLine",
                               AcquisitionMethod.OPERATING_LEASE, lease,
                               SeatLayout.all_economy(180), 195))
    return world, engine


def run(engine, days=60, verbose=True):
    """Advance the engine `days` ticks and optionally print a summary."""
    ctx = {"market": MarketConditions()}
    for _ in range(days):
        engine.tick(ctx)
    if verbose:
        w = engine.world
        print(f"Simulated {days} days (t={w.sim_time:.0f}h)\n")
        for p in engine.players:
            debt = sum(l.remaining for l in p.loans)
            assets = sum(aircraft_value(a, w.sim_time) for a in p.fleet if a.owned)
            nw = p.ledger.cash + assets - debt
            pax = sum(o.last_pax for o in p.route_ops)
            print(f"  {p.name:11s} cash ${p.ledger.cash/1e6:6.1f}M  "
                  f"debt ${debt/1e6:5.1f}M  net worth ${nw/1e6:6.1f}M  "
                  f"{pax:.0f} px/day")
    return engine

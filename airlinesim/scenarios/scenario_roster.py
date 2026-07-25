"""
Roster + positioning demonstration.

A carrier holds a POOL of crews rather than hardcoding one per flight. The
RosterSubsystem assigns the best-positioned legal crew to each rotation each
tick. Crews that fly out end up at the destination (out of base) and must be
re-rostered from there. We show:
  1. roster spreading flying across the pool to respect duty limits
  2. crews tracked by location (origin vs out-station)
  3. an under-staffed pool leaving rotations ungrounded
"""
from airlinesim.engine import (
    SpecRepository, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    MaintenanceProgram, CheckDefinition, CheckTier, PlaneClass, CrewType,
    World, Player, Airplane, CrewUnit, RouteOp, Ledger,
    PricingModel, MarketConditions, ResourceArbiter, MaintenanceEngine,
    SimulationEngine, OperationsSubsystem, MaintenanceSubsystem, FinanceSubsystem,
)
from airlinesim.crew import (CrewLegalitySubsystem, RosterSubsystem, CrewPositioningSubsystem,
                  DEFAULT_DUTY_LIMITS)


def setup():
    repo = SpecRepository()
    a320 = AircraftSpec(spec_id="A320", display_name="A320neo", manufacturer="Airbus",
                        plane_class=PlaneClass.NARROWBODY, list_price=110_000_000,
                        max_seats=180, max_range_km=6300, cruise_speed_kmh=833,
                        fuel_burn_lph=2400,
                        maint_program=MaintenanceProgram(checks=(
                            CheckDefinition(CheckTier.A, 750, 90, 10, 60, 8000, PlaneClass.NARROWBODY),)))
    repo._tables[AircraftSpec]["A320"] = a320
    for code, name in [("ORG", "Origin"), ("HUB", "Hub")]:
        repo._tables[AirportSpec][code] = AirportSpec(
            spec_id=code, display_name=name, iata=code, runway_length_m=3800,
            total_gates=40, has_maintenance_facility=True,
            facility_max_class=PlaneClass.WIDEBODY, fuel_supply_per_day_l=5_000_000)
    route = RouteSpec(spec_id="ORG-HUB", display_name="Origin-Hub", origin_iata="ORG",
                      dest_iata="HUB", distance_km=2600, base_demand_per_day=2000,
                      seasonality_amplitude=0.0)
    repo._tables[RouteSpec]["ORG-HUB"] = route
    world = World(repo)
    world.add_airport_resources(repo._tables[AirportSpec]["ORG"], 0.80)
    world.add_airport_resources(repo._tables[AirportSpec]["HUB"], 0.85)
    world.add_demand_market(route)
    return world, repo, a320, route


def cockpit(pid, n, base):
    return CrewUnit(CrewSpec("FD", f"FD-{n}", crew_type=CrewType.COCKPIT,
                             cost_per_member_hour=220, certifications=("A320",)),
                    headcount=2, owner_id=pid, home_iata=base)


def cabin(pid, n, base):
    return CrewUnit(CrewSpec("CC", f"CC-{n}", crew_type=CrewType.CABIN,
                             cost_per_member_hour=60), headcount=4, owner_id=pid,
                    home_iata=base)


def run(label, pool_size):
    world, repo, a320, route = setup()
    pricing = PricingModel(elasticity=-1.3, reference_price=200.0)
    arbiter = ResourceArbiter(world, pricing)
    maint = MaintenanceEngine(repo)
    engine = SimulationEngine(world)
    engine.dt = 24.0
    # pipeline ORDER matters: roster -> finance -> ops -> position -> legality
    engine.add_subsystem(RosterSubsystem())
    engine.add_subsystem(FinanceSubsystem())
    engine.add_subsystem(OperationsSubsystem(arbiter, pricing, maint))
    engine.add_subsystem(CrewPositioningSubsystem())
    engine.add_subsystem(MaintenanceSubsystem(maint))
    engine.add_subsystem(CrewLegalitySubsystem(DEFAULT_DUTY_LIMITS))

    p = Player("AIR", "RosterAir")
    p.ledger = Ledger(cash=20_000_000)
    plane = Airplane(spec=a320, tail_number="AIR-1", owner_id="AIR", location_iata="ORG")
    p.fleet.append(plane)
    p.crews.append(CrewUnit(CrewSpec("MX", "MX", crew_type=CrewType.MAINTENANCE,
                                     cost_per_member_hour=75, certifications=("A320",)),
                            headcount=6, owner_id="AIR"))
    # build the crew POOL, all based at ORG
    p.cockpit_pool = [cockpit("AIR", i, "ORG") for i in range(pool_size)]
    p.cabin_pool = [cabin("AIR", i, "ORG") for i in range(pool_size)]

    # 3 separate single-rotation ops — the natural unit the roster assigns to.
    # Each needs its own crew; the pool must cover them or some stay grounded.
    for i in range(3):
        p.route_ops.append(RouteOp(spec=route, plane=plane, cockpit=None, cabin=None,
                                   ticket_price=260, daily_frequency=1, owner_id="AIR"))

    engine.add_player(p)
    ctx = {"market": MarketConditions()}
    fh = route.distance_km / a320.cruise_speed_kmh
    print(f"\n=== {label} (pool: {pool_size} cockpit / {pool_size} cabin) ===")
    print(f"  route {fh:.1f}h each way, 3 separate rotations/day to crew")
    for day in range(6):
        engine.tick(ctx)
        if day in (0, 2, 5):
            flown = sum(o.last_eff_freq for o in p.route_ops)
            rostered = [o.cockpit.spec.display_name if o.cockpit else "—" for o in p.route_ops]
            locs = ",".join(f"{c.spec.display_name}@{c.location_iata}" for c in p.cockpit_pool)
            print(f"  day {day}: flew {flown:.0f}/3 rotations | rostered {rostered} | pool[{locs}]")
    print(f"  final cash ${p.ledger.cash:,.0f}")


def main():
    print("ROSTER + POSITIONING DEMONSTRATION")
    run("Under-staffed: 1 crew for a 3x schedule", pool_size=1)
    run("Staffed: 3 crews for a 3x schedule", pool_size=3)


if __name__ == "__main__":
    main()

"""
Crew duty/rest demonstration.

One carrier tries to fly an aggressive schedule with a single crew set. The
duty/rest limits (FAR Part 117-shaped) cap how much that crew can legally fly,
so the airline CANNOT operate all its desired rotations without more crew.
Then we add a second crew and show the schedule opens back up.
"""
from airlinesim.engine import (
    SpecRepository, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    MaintenanceProgram, CheckDefinition, CheckTier, PlaneClass, CrewType,
    World, Player, Airplane, CrewUnit, RouteOp, Ledger,
    PricingModel, MarketConditions, ResourceArbiter, MaintenanceEngine,
    SimulationEngine, OperationsSubsystem, MaintenanceSubsystem, FinanceSubsystem,
)
from airlinesim.crew import CrewLegalitySubsystem, DEFAULT_DUTY_LIMITS


def setup():
    repo = SpecRepository()
    a320 = AircraftSpec(spec_id="A320", display_name="A320neo", manufacturer="Airbus",
                        plane_class=PlaneClass.NARROWBODY, list_price=110_000_000,
                        max_seats=180, max_range_km=6300, cruise_speed_kmh=833,
                        fuel_burn_lph=2400,
                        maint_program=MaintenanceProgram(checks=(
                            CheckDefinition(CheckTier.A, 750, 90, 10, 60, 8000, PlaneClass.NARROWBODY),)))
    repo._tables[AircraftSpec]["A320"] = a320
    org = AirportSpec(spec_id="ORG", display_name="Origin", iata="ORG", runway_length_m=3500,
                      total_gates=40, has_maintenance_facility=True,
                      facility_max_class=PlaneClass.WIDEBODY, fuel_supply_per_day_l=5_000_000)
    hub = AirportSpec(spec_id="HUB", display_name="Hub", iata="HUB", runway_length_m=4000,
                      total_gates=40, has_maintenance_facility=True,
                      facility_max_class=PlaneClass.WIDEBODY, fuel_supply_per_day_l=5_000_000)
    repo._tables[AirportSpec]["ORG"] = org
    repo._tables[AirportSpec]["HUB"] = hub
    # long-ish route: ~3.4h each way, so few rotations hit the daily cap fast
    route = RouteSpec(spec_id="ORG-HUB", display_name="Origin-Hub", origin_iata="ORG",
                      dest_iata="HUB", distance_km=2800, base_demand_per_day=2000,
                      seasonality_amplitude=0.0)
    repo._tables[RouteSpec]["ORG-HUB"] = route
    world = World(repo)
    world.add_airport_resources(org, 0.80)
    world.add_airport_resources(hub, 0.85)
    world.add_demand_market(route)
    return world, repo, a320, route


def run(label, two_crews):
    world, repo, a320, route = setup()
    pricing = PricingModel(elasticity=-1.3, reference_price=200.0)
    arbiter = ResourceArbiter(world, pricing)
    maint = MaintenanceEngine(repo)
    engine = SimulationEngine(world)
    engine.dt = 24.0
    engine.add_subsystem(FinanceSubsystem())
    engine.add_subsystem(OperationsSubsystem(arbiter, pricing, maint))
    engine.add_subsystem(MaintenanceSubsystem(maint))
    engine.add_subsystem(CrewLegalitySubsystem(DEFAULT_DUTY_LIMITS))

    p = Player("AIR", "TestAir")
    p.ledger = Ledger(cash=20_000_000)
    plane = Airplane(spec=a320, tail_number="AIR-1", owner_id="AIR", location_iata="ORG")
    p.fleet.append(plane)
    mx = CrewUnit(CrewSpec("MX", "MX", crew_type=CrewType.MAINTENANCE,
                           cost_per_member_hour=75, certifications=("A320",)),
                  headcount=6, owner_id="AIR")
    p.crews.append(mx)

    fd1 = CrewUnit(CrewSpec("FD", "Flight Deck A", crew_type=CrewType.COCKPIT,
                            cost_per_member_hour=220, certifications=("A320",)),
                   headcount=2, owner_id="AIR")
    cc1 = CrewUnit(CrewSpec("CC", "Cabin A", crew_type=CrewType.CABIN,
                            cost_per_member_hour=60), headcount=4, owner_id="AIR")

    # aggressive: want 4 daily rotations of a ~3.4h flight = ~13.4h/day/crew,
    # well over the 9h daily cap -> crew gate must reduce it.
    op = RouteOp(spec=route, plane=plane, cockpit=fd1, cabin=cc1,
                 ticket_price=260, daily_frequency=4, owner_id="AIR")
    p.route_ops.append(op)

    if two_crews:
        # second op on same route with a DIFFERENT crew set, splitting the flying
        fd2 = CrewUnit(CrewSpec("FD", "Flight Deck B", crew_type=CrewType.COCKPIT,
                                cost_per_member_hour=220, certifications=("A320",)),
                       headcount=2, owner_id="AIR")
        cc2 = CrewUnit(CrewSpec("CC", "Cabin B", crew_type=CrewType.CABIN,
                                cost_per_member_hour=60), headcount=4, owner_id="AIR")
        op.daily_frequency = 2   # crew A flies 2
        op2 = RouteOp(spec=route, plane=plane, cockpit=fd2, cabin=cc2,
                      ticket_price=260, daily_frequency=2, owner_id="AIR")  # crew B flies 2
        p.route_ops.append(op2)

    engine.add_player(p)
    ctx = {"market": MarketConditions()}

    print(f"\n=== {label} ===")
    fh_each = route.distance_km / a320.cruise_speed_kmh
    print(f"Route is {fh_each:.1f}h each way; daily cap is {DEFAULT_DUTY_LIMITS.max_daily_flight_hours:.0f}h/crew")
    for day in range(8):
        engine.tick(ctx)
        if day in (0, 2, 5, 7):
            line = f"  day {day}: "
            for o in p.route_ops:
                fdh = o.cockpit.duty.hours_today
                wk = o.cockpit.duty.hours_in_window(world.sim_time, 168.0)
                line += (f"[{o.cockpit.spec.display_name}: want {o.daily_frequency} "
                         f"flew {o.last_eff_freq:.0f} | today {fdh:.1f}h wk {wk:.0f}h "
                         f"{'BLOCK:'+o.last_crew_block if o.last_crew_block else ''}] ")
            print(line)
    print(f"  final cash ${p.ledger.cash:,.0f}")


def main():
    print("CREW DUTY/REST DEMONSTRATION")
    print("FAR Part 117-shaped limits: 9h/day, 60h/7d, 10h min rest")
    run("ONE crew set, aggressive 4x schedule", two_crews=False)
    run("TWO crew sets splitting the same 4x schedule", two_crews=True)


if __name__ == "__main__":
    main()

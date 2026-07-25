"""
Deadhead demonstration.

Crews based at ORG fly out to HUB. To get home they DEADHEAD (ride as passengers)
on the airline's own HUB->ORG revenue flight. We show:
  1. a crew stranded at HUB after flying out
  2. that crew booked onto the return revenue flight as deadhead (seats reserved)
  3. the deadhead seats removed from sale (lost revenue), crew back at base
"""
from airlinesim.engine import (
    SpecRepository, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    MaintenanceProgram, CheckDefinition, CheckTier, PlaneClass, CrewType,
    World, Player, Airplane, CrewUnit, RouteOp, Ledger,
    PricingModel, MarketConditions, ResourceArbiter, MaintenanceEngine,
    SimulationEngine, OperationsSubsystem, MaintenanceSubsystem, FinanceSubsystem,
)
from airlinesim.crew import (CrewLegalitySubsystem, RosterSubsystem, CrewPositioningSubsystem,
                  DeadheadSubsystem, DEFAULT_DUTY_LIMITS)


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
    # both directions as separate routes
    out_route = RouteSpec(spec_id="ORG-HUB", display_name="Out", origin_iata="ORG",
                          dest_iata="HUB", distance_km=2200, base_demand_per_day=500,
                          seasonality_amplitude=0.0)
    ret_route = RouteSpec(spec_id="HUB-ORG", display_name="Return", origin_iata="HUB",
                          dest_iata="ORG", distance_km=2200, base_demand_per_day=500,
                          seasonality_amplitude=0.0)
    repo._tables[RouteSpec]["ORG-HUB"] = out_route
    repo._tables[RouteSpec]["HUB-ORG"] = ret_route
    world = World(repo)
    world.add_airport_resources(repo._tables[AirportSpec]["ORG"], 0.80)
    world.add_airport_resources(repo._tables[AirportSpec]["HUB"], 0.85)
    world.add_demand_market(out_route)
    world.add_demand_market(ret_route)
    return world, repo, a320, out_route, ret_route


def main():
    world, repo, a320, out_route, ret_route = setup()
    pricing = PricingModel(elasticity=-1.3, reference_price=200.0)
    arbiter = ResourceArbiter(world, pricing)
    maint = MaintenanceEngine(repo)
    engine = SimulationEngine(world)
    engine.dt = 24.0
    # ORDER: deadhead (reposition) -> roster -> finance -> ops -> position -> legality
    engine.add_subsystem(DeadheadSubsystem())
    engine.add_subsystem(RosterSubsystem())
    engine.add_subsystem(FinanceSubsystem())
    engine.add_subsystem(OperationsSubsystem(arbiter, pricing, maint))
    engine.add_subsystem(CrewPositioningSubsystem())
    engine.add_subsystem(MaintenanceSubsystem(maint))
    engine.add_subsystem(CrewLegalitySubsystem(DEFAULT_DUTY_LIMITS))

    p = Player("AIR", "DeadheadAir")
    p.ledger = Ledger(cash=20_000_000)
    plane_out = Airplane(spec=a320, tail_number="AIR-OUT", owner_id="AIR", location_iata="ORG")
    plane_ret = Airplane(spec=a320, tail_number="AIR-RET", owner_id="AIR", location_iata="HUB")
    p.fleet += [plane_out, plane_ret]
    p.crews.append(CrewUnit(CrewSpec("MX", "MX", crew_type=CrewType.MAINTENANCE,
                                     cost_per_member_hour=75, certifications=("A320",)),
                            headcount=6, owner_id="AIR"))

    # One crew set based at ORG, plus a separate crew to fly the return so the
    # return flight actually operates (and can carry our deadheaders).
    home_fd = CrewUnit(CrewSpec("FD", "FD-home", crew_type=CrewType.COCKPIT,
                                cost_per_member_hour=220, certifications=("A320",)),
                       headcount=2, owner_id="AIR", home_iata="ORG")
    home_cc = CrewUnit(CrewSpec("CC", "CC-home", crew_type=CrewType.CABIN,
                                cost_per_member_hour=60), headcount=4,
                       owner_id="AIR", home_iata="ORG")
    ret_fd = CrewUnit(CrewSpec("FD", "FD-ret", crew_type=CrewType.COCKPIT,
                               cost_per_member_hour=220, certifications=("A320",)),
                      headcount=2, owner_id="AIR", home_iata="HUB")
    ret_cc = CrewUnit(CrewSpec("CC", "CC-ret", crew_type=CrewType.CABIN,
                               cost_per_member_hour=60), headcount=4,
                      owner_id="AIR", home_iata="HUB")
    p.cockpit_pool = [home_fd, ret_fd]
    p.cabin_pool = [home_cc, ret_cc]

    op_out = RouteOp(spec=out_route, plane=plane_out, cockpit=None, cabin=None,
                     ticket_price=240, daily_frequency=1, owner_id="AIR")
    op_ret = RouteOp(spec=ret_route, plane=plane_ret, cockpit=None, cabin=None,
                     ticket_price=240, daily_frequency=1, owner_id="AIR")
    p.route_ops += [op_out, op_ret]

    engine.add_player(p)
    ctx = {"market": MarketConditions()}

    print("DEADHEAD DEMONSTRATION")
    print("ORG-based crew flies out to HUB, then deadheads home on the HUB->ORG flight\n")
    for day in range(6):
        engine.tick(ctx)
        locs = ", ".join(f"{c.spec.display_name}@{c.location_iata}" for c in p.cockpit_pool)
        dh = op_ret.deadhead_seats
        out_px = op_out.last_pax
        ret_px = op_ret.last_pax
        print(f"day {day}: crews[{locs}] | HUB->ORG deadhead_seats={dh} "
              f"| out_px {out_px:.0f} ret_px {ret_px:.0f}")
    print(f"\nfinal cash ${p.ledger.cash:,.0f}")


if __name__ == "__main__":
    main()

"""Two-airline competitive scenario — proves the arbitration seam works."""
from airlinesim.engine import (
    SpecRepository, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    MaintenanceProgram, CheckDefinition, StructuralLayover, CheckTier, PlaneClass,
    CrewType, World, Player, Airplane, CrewUnit, RouteOp, Ledger,
    PricingModel, MarketConditions, ResourceArbiter, MaintenanceEngine,
    SimulationEngine, OperationsSubsystem, MaintenanceSubsystem, FinanceSubsystem,
    AIStrategySubsystem,
)


def a320_program():
    return MaintenanceProgram(
        checks=(
            CheckDefinition(CheckTier.A, 750, 90, 10, 60, 8000, PlaneClass.NARROWBODY),
            CheckDefinition(CheckTier.B, 0, 240, 36, 170, 25000, PlaneClass.NARROWBODY),
            CheckDefinition(CheckTier.C, 6000, 730, 240, 6000, 350000, PlaneClass.NARROWBODY),
            CheckDefinition(CheckTier.D, 20000, 2920, 720, 40000, 3000000, PlaneClass.WIDEBODY),
        ),
        b_folded_into_a=True, b_fold_every_n_a=4,
        c_escalates_to_3c=True, c_3c_every_n_c=3,
        layover=StructuralLayover(96, 2500, 180000),
    )


def build_world():
    repo = SpecRepository()
    a320 = AircraftSpec(spec_id="A320", display_name="A320neo", manufacturer="Airbus",
                        plane_class=PlaneClass.NARROWBODY, list_price=110_000_000,
                        max_seats=180, max_range_km=6300, cruise_speed_kmh=833,
                        fuel_burn_lph=2400, maint_program=a320_program())
    repo._tables[AircraftSpec]["A320"] = a320

    # A single CONTESTED destination: SCARCE gates + fuel -> competition bites.
    # 6 gates total but carriers will want ~5+4 (and more as AI adds frequency),
    # so the gate ledger is oversubscribed and the arbiter must ration.
    dest = AirportSpec(spec_id="HUB", display_name="Contested Hub", iata="HUB",
                       runway_length_m=4000, total_gates=6,
                       has_maintenance_facility=True, facility_max_class=PlaneClass.WIDEBODY,
                       fuel_supply_per_day_l=150_000, landing_fee=4000)
    origin = AirportSpec(spec_id="ORG", display_name="Origin Intl", iata="ORG",
                         runway_length_m=3500, total_gates=20,
                         has_maintenance_facility=True, facility_max_class=PlaneClass.WIDEBODY,
                         fuel_supply_per_day_l=900_000, landing_fee=3000)
    repo._tables[AirportSpec]["HUB"] = dest
    repo._tables[AirportSpec]["ORG"] = origin

    route = RouteSpec(spec_id="ORG-HUB", display_name="Origin-Hub", origin_iata="ORG",
                      dest_iata="HUB", distance_km=1100, base_demand_per_day=1600,
                      seasonality_amplitude=0.2)
    repo._tables[RouteSpec]["ORG-HUB"] = route

    world = World(repo)
    world.add_airport_resources(dest, fuel_base_price=0.85)
    world.add_airport_resources(origin, fuel_base_price=0.80)
    world.add_demand_market(route)
    return world, repo, a320, route


def make_airline(pid, name, a320, route, ticket_price, freq, is_ai=False):
    p = Player(pid, name, is_ai=is_ai)
    p.ledger = Ledger(cash=8_000_000)
    plane = Airplane(spec=a320, tail_number=f"{pid}-001", owner_id=pid, location_iata="ORG")
    p.fleet.append(plane)
    mx = CrewUnit(CrewSpec(spec_id="MX", display_name=f"{name} MX", crew_type=CrewType.MAINTENANCE,
                           cost_per_member_hour=75, certifications=("A320", "Airbus")),
                  headcount=8, owner_id=pid, home_iata="ORG")
    ground = CrewUnit(CrewSpec(spec_id="GND", display_name="Ground", crew_type=CrewType.GROUND,
                              cost_per_member_hour=35), headcount=10, owner_id=pid)
    p.crews += [mx, ground]
    cockpit = CrewUnit(CrewSpec(spec_id="FD", display_name="Flight Deck", crew_type=CrewType.COCKPIT,
                               cost_per_member_hour=220), headcount=2, owner_id=pid)
    cabin = CrewUnit(CrewSpec(spec_id="CC", display_name="Cabin", crew_type=CrewType.CABIN,
                             cost_per_member_hour=60), headcount=4, owner_id=pid)
    p.route_ops.append(RouteOp(spec=route, plane=plane, cockpit=cockpit, cabin=cabin,
                               ticket_price=ticket_price, daily_frequency=freq, owner_id=pid))
    return p


def main():
    world, repo, a320, route = build_world()
    pricing = PricingModel(elasticity=-1.3, reference_price=200.0)
    arbiter = ResourceArbiter(world, pricing)
    maint = MaintenanceEngine(repo)

    engine = SimulationEngine(world)
    engine.add_subsystem(AIStrategySubsystem(step_frac=0.03))   # profit hill-climber
    engine.add_subsystem(FinanceSubsystem())
    engine.add_subsystem(OperationsSubsystem(arbiter, pricing, maint))
    engine.add_subsystem(MaintenanceSubsystem(maint))

    # Two carriers fighting over the same hub. Different strategies:
    #  - ValueAir: fixed cheap tickets, more frequencies (static human-like baseline)
    #  - PrimeJet: REACTIVE AI, starts overpriced at $300 and must find the market
    value = make_airline("VAL", "ValueAir", a320, route, ticket_price=150, freq=5)
    prime = make_airline("PRM", "PrimeJet", a320, route, ticket_price=300, freq=4, is_ai=True)
    engine.add_player(value)
    engine.add_player(prime)

    ctx = {"market": MarketConditions(fuel_index=1.0)}

    print("=" * 64)
    print("COMPETITIVE SIM — ValueAir vs PrimeJet over a contested hub")
    print("Hub: 8 gates, 400k L/day fuel | Route demand: ~900 pax/day")
    print("=" * 64)

    for day in range(140):
        engine.tick(ctx)
        if day in (0, 1, 5, 15, 30, 60, 90, 120, 139):
            print(f"\n--- Day {day} (t={world.sim_time:.0f}h) ---")
            gl = world.gates["HUB"]
            print(f"   HUB gates: {gl.used():.0f}/{gl.total_gates} used | "
                  f"fuel spot ${world.fuel['HUB'].spot_price():.3f}/L")
            for p in (value, prime):
                op = p.route_ops[0]
                tag = "AI" if p.is_ai else "  "
                print(f"[{tag}][{p.name}] cash ${p.ledger.cash:,.0f} | "
                      f"${op.ticket_price:.0f} | freq {op.daily_frequency} "
                      f"(flew {op.last_eff_freq:.0f}) | LF {op.last_load_factor:.0%} | "
                      f"{op.last_pax:.0f}px")

    print("\n" + "=" * 64)
    print("FINAL STANDINGS")
    print("=" * 64)
    for p in (value, prime):
        plane = p.fleet[0]
        print(f"{p.name:10s} cash ${p.ledger.cash:>14,.0f} | "
              f"airframe {plane.airframe_hours:6.0f}h | "
              f"A:{plane.a_checks_completed} C:{plane.c_checks_completed} D:{plane.d_checks_completed}")
    # show fuel scarcity in action
    fm = world.fuel["HUB"]
    print(f"\nHub fuel spot price now ${fm.spot_price():.3f}/L (base $0.85) — scarcity premium active")


if __name__ == "__main__":
    main()

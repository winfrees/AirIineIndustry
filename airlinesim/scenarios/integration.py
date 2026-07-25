"""
FULL INTEGRATION CHECK
======================
Wires EVERY subsystem into one pipeline and runs a multi-week sim with two
competing carriers, structured routes, financed + leased aircraft, cabin
layouts, crew pools with duty limits, deadheading, and maintenance. Asserts the
whole stack runs and reports a coherent end state.

Pipeline order (matters):
  RouteSuitability -> Deadhead -> Roster -> Banking -> Finance -> Operations
  -> CrewPositioning -> Maintenance -> CrewLegality
"""
from airlinesim.engine import (
    SpecRepository, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    MaintenanceProgram, CheckDefinition, CheckTier, PlaneClass, CrewType,
    World, Player, Airplane, CrewUnit, RouteOp, Ledger,
    PricingModel, MarketConditions, ResourceArbiter, MaintenanceEngine,
    SimulationEngine, OperationsSubsystem, MaintenanceSubsystem,
    FinanceSubsystem, BankingSubsystem, RouteSuitabilitySubsystem,
)
from airlinesim.crew import (CrewLegalitySubsystem, RosterSubsystem, CrewPositioningSubsystem,
                  DeadheadSubsystem, DEFAULT_DUTY_LIMITS)
from airlinesim.finance_cabin import (CabinClass, SeatLayout, cabin_slots_for,
                           AcquisitionMethod, FinancingTerms, Bank, aircraft_value)
from airlinesim.route import default_segments, EquipmentRequirements, CrewRequirements


def build():
    repo = SpecRepository()
    a320 = AircraftSpec(spec_id="A320", display_name="A320neo", manufacturer="Airbus",
                        plane_class=PlaneClass.NARROWBODY, list_price=110_000_000,
                        max_seats=180, max_range_km=6300, cruise_speed_kmh=833,
                        fuel_burn_lph=2400, maint_program=MaintenanceProgram(checks=(
            CheckDefinition(CheckTier.A, 750, 90, 10, 60, 8000, PlaneClass.NARROWBODY),
            CheckDefinition(CheckTier.C, 6000, 730, 240, 6000, 350000, PlaneClass.NARROWBODY),
            CheckDefinition(CheckTier.D, 20000, 2920, 720, 40000, 3000000, PlaneClass.WIDEBODY)),
            b_folded_into_a=True))
    repo._tables[AircraftSpec]["A320"] = a320
    for code, name, rwy in [("ORG", "Origin Intl", 3800), ("HUB", "Hub Intl", 4000)]:
        repo._tables[AirportSpec][code] = AirportSpec(
            spec_id=code, display_name=name, iata=code, runway_length_m=rwy,
            total_gates=20, has_maintenance_facility=True,
            facility_max_class=PlaneClass.WIDEBODY, fuel_supply_per_day_l=3_000_000)
    out_r = RouteSpec(spec_id="ORG-HUB", display_name="Out", origin_iata="ORG",
                      dest_iata="HUB", distance_km=1500, base_demand_per_day=900,
                      segments=default_segments(900, 0.35, 0.45),
                      equipment_req=EquipmentRequirements(1500, 2500, PlaneClass.NARROWBODY, 120, 240),
                      crew_req=CrewRequirements())
    ret_r = RouteSpec(spec_id="HUB-ORG", display_name="Return", origin_iata="HUB",
                      dest_iata="ORG", distance_km=1500, base_demand_per_day=900,
                      segments=default_segments(900, 0.35, 0.45),
                      equipment_req=EquipmentRequirements(1500, 2500, PlaneClass.NARROWBODY, 120, 240),
                      crew_req=CrewRequirements())
    repo._tables[RouteSpec]["ORG-HUB"] = out_r
    repo._tables[RouteSpec]["HUB-ORG"] = ret_r
    world = World(repo)
    for code in ("ORG", "HUB"):
        world.add_airport_resources(repo._tables[AirportSpec][code], 0.82)
    world.add_demand_market(out_r)
    world.add_demand_market(ret_r)
    return world, repo, a320, out_r, ret_r


def make_carrier(world, bank, a320, out_r, ret_r, pid, name, method, terms, layout, price):
    p = Player(pid, name)
    p.ledger = Ledger(cash=40_000_000)
    tail = f"{pid}-1"
    bank.acquire(p, a320, tail, method, terms, p.log)
    plane = Airplane(spec=a320, tail_number=tail, owner_id=pid,
                     owned=(method != AcquisitionMethod.OPERATING_LEASE),
                     location_iata="ORG", acquired_at=world.sim_time)
    plane2 = Airplane(spec=a320, tail_number=tail+"b", owner_id=pid,
                      owned=(method != AcquisitionMethod.OPERATING_LEASE),
                      location_iata="HUB", acquired_at=world.sim_time)
    bank.acquire(p, a320, tail+"b", method, terms, p.log)
    p.fleet += [plane, plane2]
    p.crews.append(CrewUnit(CrewSpec("MX","MX",crew_type=CrewType.MAINTENANCE,
                    cost_per_member_hour=75, certifications=("A320",)), headcount=8, owner_id=pid))
    # crew pools at both bases
    for base in ("ORG", "HUB"):
        for i in range(2):
            p.cockpit_pool.append(CrewUnit(CrewSpec("FD",f"FD-{base}{i}",crew_type=CrewType.COCKPIT,
                cost_per_member_hour=220, certifications=("A320",)), headcount=2, owner_id=pid, home_iata=base))
            p.cabin_pool.append(CrewUnit(CrewSpec("CC",f"CC-{base}{i}",crew_type=CrewType.CABIN,
                cost_per_member_hour=60), headcount=4, owner_id=pid, home_iata=base))
    p.route_ops.append(RouteOp(spec=out_r, plane=plane, cockpit=None, cabin=None,
                               ticket_price=price, daily_frequency=1, owner_id=pid, layout=layout))
    p.route_ops.append(RouteOp(spec=ret_r, plane=plane2, cockpit=None, cabin=None,
                               ticket_price=price, daily_frequency=1, owner_id=pid, layout=layout))
    return p


def build_engine(world, repo):
    pricing = PricingModel(elasticity=-1.3, reference_price=200.0)
    arbiter = ResourceArbiter(world, pricing)
    maint = MaintenanceEngine(repo)
    eng = SimulationEngine(world)
    eng.dt = 24.0
    eng.add_subsystem(RouteSuitabilitySubsystem())
    eng.add_subsystem(DeadheadSubsystem())
    eng.add_subsystem(RosterSubsystem())
    eng.add_subsystem(BankingSubsystem())
    eng.add_subsystem(FinanceSubsystem())
    eng.add_subsystem(OperationsSubsystem(arbiter, pricing, maint))
    eng.add_subsystem(CrewPositioningSubsystem())
    eng.add_subsystem(MaintenanceSubsystem(maint))
    eng.add_subsystem(CrewLegalitySubsystem(DEFAULT_DUTY_LIMITS))
    return eng, pricing


def main():
    world, repo, a320, out_r, ret_r = build()
    bank = Bank(max_debt_to_cash=6.0)
    eng, pricing = build_engine(world, repo)

    mixed = SeatLayout({CabinClass.ECONOMY: 138, CabinClass.BUSINESS: 16})
    econ = SeatLayout.all_economy(180)
    loan = FinancingTerms("LOAN", AcquisitionMethod.FINANCE, 0.20, 0.06, 120)
    lease = FinancingTerms("LEASE", AcquisitionMethod.OPERATING_LEASE,
                           lease_rate_frac_per_year=0.11, lease_term_months=84)

    print("=== ACQUISITION ===")
    c1 = make_carrier(world, bank, a320, out_r, ret_r, "FIN", "FinanceAir",
                      AcquisitionMethod.FINANCE, loan, mixed, 220)
    c2 = make_carrier(world, bank, a320, out_r, ret_r, "LSE", "LeaseLine",
                      AcquisitionMethod.OPERATING_LEASE, lease, econ, 195)
    for p in (c1, c2):
        for l in p.log:
            print(l)
        eng.add_player(p)

    ctx = {"market": MarketConditions()}
    print("\n=== RUN 60 DAYS ===")
    errors = []
    for day in range(60):
        try:
            eng.tick(ctx)
        except Exception as e:
            errors.append((day, repr(e)))
            break

    if errors:
        print(f"!! FAILED at day {errors[0][0]}: {errors[0][1]}")
        return

    print(f"completed 60 ticks, sim_time {world.sim_time:.0f}h\n")
    print("=== END STATE ===")
    for p in (c1, c2):
        debt = sum(l.remaining for l in p.loans)
        assets = sum(aircraft_value(a, world.sim_time) for a in p.fleet if a.owned)
        nw = p.ledger.cash + assets - debt
        flown = sum(o.last_eff_freq for o in p.route_ops)
        pax = sum(o.last_pax for o in p.route_ops)
        crew_locs = {}
        for c in p.cockpit_pool:
            crew_locs[c.location_iata] = crew_locs.get(c.location_iata, 0) + 1
        print(f"{p.name:11s} cash ${p.ledger.cash/1e6:6.1f}M debt ${debt/1e6:5.1f}M "
              f"NW ${nw/1e6:6.1f}M | flew {flown:.0f} legs, {pax:.0f}px/day | "
              f"crew {dict(crew_locs)}")
        # show last revenue line
        rev = [l for l in p.log if "tickets" in l]
        if rev:
            print(f"             last sale: {rev[-1].strip()}")

    # integration assertions
    print("\n=== INTEGRATION CHECKS ===")
    checks = []
    checks.append(("both carriers still solvent",
                   c1.ledger.cash > -1e7 and c2.ledger.cash > -1e7))
    checks.append(("flights operated", sum(o.last_eff_freq for o in c1.route_ops) > 0))
    checks.append(("passengers carried", sum(o.last_pax for o in c1.route_ops) > 0))
    checks.append(("suitability passed (narrowbody on trunk)",
                   all(o.suitable for o in c1.route_ops)))
    checks.append(("depreciation applied",
                   any(aircraft_value(a, world.sim_time) < a.spec.list_price
                       for a in c1.fleet if a.owned)))
    checks.append(("lease carrier holds no owned assets",
                   sum(aircraft_value(a, world.sim_time) for a in c2.fleet if a.owned) == 0))
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    allpass = all(ok for _, ok in checks)
    print(f"\n{'ALL CHECKS PASS' if allpass else 'SOME CHECKS FAILED'}")


if __name__ == "__main__":
    main()

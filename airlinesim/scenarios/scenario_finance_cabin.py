"""
Demonstration: seat-class layout strategy + leasing/banking acquisition methods.

Three carriers, same aircraft type and route, differing on two axes:
  - HOW they acquired the plane (cash / financed / leased)
  - HOW they configured the cabin (all-economy / mixed / premium-heavy)
This isolates the two new systems so their effects are legible.
"""
from airlinesim.engine import (
    SpecRepository, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    MaintenanceProgram, CheckDefinition, CheckTier, PlaneClass, CrewType,
    World, Player, Airplane, CrewUnit, RouteOp, Ledger,
    PricingModel, MarketConditions, ResourceArbiter, MaintenanceEngine,
    SimulationEngine, OperationsSubsystem, MaintenanceSubsystem,
    FinanceSubsystem, BankingSubsystem,
)
from airlinesim.finance_cabin import (
    CabinClass, SeatLayout, DEFAULT_SEAT_CLASSES, cabin_slots_for,
    AcquisitionMethod, FinancingTerms, Bank,
)


def a320_program():
    return MaintenanceProgram(checks=(
        CheckDefinition(CheckTier.A, 750, 90, 10, 60, 8000, PlaneClass.NARROWBODY),
        CheckDefinition(CheckTier.C, 6000, 730, 240, 6000, 350000, PlaneClass.NARROWBODY),
        CheckDefinition(CheckTier.D, 20000, 2920, 720, 40000, 3000000, PlaneClass.WIDEBODY),
    ))


def setup():
    repo = SpecRepository()
    a320 = AircraftSpec(spec_id="A320", display_name="A320neo", manufacturer="Airbus",
                        plane_class=PlaneClass.NARROWBODY, list_price=110_000_000,
                        max_seats=180, max_range_km=6300, cruise_speed_kmh=833,
                        fuel_burn_lph=2400, maint_program=a320_program())
    repo._tables[AircraftSpec]["A320"] = a320
    org = AirportSpec(spec_id="ORG", display_name="Origin", iata="ORG", runway_length_m=3500,
                      total_gates=40, has_maintenance_facility=True,
                      facility_max_class=PlaneClass.WIDEBODY, fuel_supply_per_day_l=2_000_000)
    hub = AirportSpec(spec_id="HUB", display_name="Hub", iata="HUB", runway_length_m=4000,
                      total_gates=40, has_maintenance_facility=True,
                      facility_max_class=PlaneClass.WIDEBODY, fuel_supply_per_day_l=2_000_000)
    repo._tables[AirportSpec]["ORG"] = org
    repo._tables[AirportSpec]["HUB"] = hub
    route = RouteSpec(spec_id="ORG-HUB", display_name="Origin-Hub", origin_iata="ORG",
                      dest_iata="HUB", distance_km=1100, base_demand_per_day=1200,
                      seasonality_amplitude=0.15)
    repo._tables[RouteSpec]["ORG-HUB"] = route
    world = World(repo)
    world.add_airport_resources(org, 0.80)
    world.add_airport_resources(hub, 0.85)
    world.add_demand_market(route)
    return world, repo, a320, route


def crews(pid):
    mx = CrewUnit(CrewSpec("MX", "MX", crew_type=CrewType.MAINTENANCE,
                           cost_per_member_hour=75, certifications=("A320", "Airbus")),
                  headcount=8, owner_id=pid)
    fd = CrewUnit(CrewSpec("FD", "Flight Deck", crew_type=CrewType.COCKPIT,
                           cost_per_member_hour=220), headcount=2, owner_id=pid)
    cc = CrewUnit(CrewSpec("CC", "Cabin", crew_type=CrewType.CABIN,
                           cost_per_member_hour=60), headcount=4, owner_id=pid)
    return mx, fd, cc


def make_carrier(world, bank, a320, route, pid, name, method, terms, layout, price):
    p = Player(pid, name)
    p.ledger = Ledger(cash=30_000_000)
    tail = f"{pid}-001"
    # acquire via the bank using the chosen method
    bank.acquire(p, a320, tail, method, terms, p.log)
    plane = Airplane(spec=a320, tail_number=tail, owner_id=pid,
                     owned=(method != AcquisitionMethod.OPERATING_LEASE),
                     location_iata="ORG", acquired_at=world.sim_time)
    p.fleet.append(plane)
    mx, fd, cc = crews(pid)
    p.crews += [mx]
    p.route_ops.append(RouteOp(spec=route, plane=plane, cockpit=fd, cabin=cc,
                               ticket_price=price, daily_frequency=4, owner_id=pid,
                               layout=layout))
    return p


def demo_class_elasticity():
    """Show that overpricing an inelastic vs elastic class behaves differently."""
    from airlinesim.finance_cabin import DEFAULT_SEAT_CLASSES, CabinClass
    print("=== CLASS ELASTICITY CHECK ===")
    print("How a class's demand responds to pricing at 1.0x, 1.3x, 1.6x its reference fare:")
    for cc in (CabinClass.ECONOMY, CabinClass.BUSINESS, CabinClass.FIRST):
        cspec = DEFAULT_SEAT_CLASSES[cc]
        responses = []
        for mult in (1.0, 1.3, 1.6):
            factor = mult ** cspec.elasticity   # fare/ref = mult
            responses.append(f"{mult:.1f}x->{factor:.0%}")
        print(f"  {cc.name:9s} (elasticity {cspec.elasticity:+.1f}): " + "  ".join(responses))
    print("  (economy demand collapses when overpriced; business/first barely react)\n")


def main():
    demo_class_elasticity()
    world, repo, a320, route = setup()
    pricing = PricingModel(elasticity=-1.3, reference_price=200.0)
    arbiter = ResourceArbiter(world, pricing)
    maint = MaintenanceEngine(repo)
    bank = Bank(max_debt_to_cash=4.0)

    engine = SimulationEngine(world)
    engine.add_subsystem(FinanceSubsystem())
    engine.add_subsystem(BankingSubsystem())
    engine.add_subsystem(OperationsSubsystem(arbiter, pricing, maint))
    engine.add_subsystem(MaintenanceSubsystem(maint))

    slots = cabin_slots_for(a320.max_seats)   # 180 economy-equivalent slots

    # Three cabin strategies (all must fit within 180 slots):
    all_econ = SeatLayout.all_economy(180)                                   # 180 seats
    mixed = SeatLayout({CabinClass.ECONOMY: 138, CabinClass.PREMIUM: 0,
                        CabinClass.BUSINESS: 16, CabinClass.FIRST: 0})        # 138 + 16*2.5 = 178
    premium = SeatLayout({CabinClass.ECONOMY: 80, CabinClass.BUSINESS: 28,
                          CabinClass.FIRST: 7})                               # 80 + 70 + 28 = 178

    for lbl, lay in [("all_econ", all_econ), ("mixed", mixed), ("premium", premium)]:
        ok = lay.is_valid(slots, DEFAULT_SEAT_CLASSES)
        print(f"layout {lbl}: {lay.total_seats()} seats, "
              f"footprint {lay.footprint_used(DEFAULT_SEAT_CLASSES):.0f}/{slots:.0f} slots, valid={ok}")
    print()

    loan_terms = FinancingTerms("LOAN", AcquisitionMethod.FINANCE,
                                down_payment_frac=0.20, annual_rate=0.06, term_months=120)
    lease_terms = FinancingTerms("LEASE", AcquisitionMethod.OPERATING_LEASE,
                                 lease_rate_frac_per_year=0.11, lease_term_months=84)
    cash_terms = FinancingTerms("CASH", AcquisitionMethod.BUY_CASH)

    print("=== ACQUISITION ===")
    cashco = make_carrier(world, bank, a320, route, "CSH", "CashAir",
                          AcquisitionMethod.BUY_CASH, cash_terms, all_econ, 200)
    finco = make_carrier(world, bank, a320, route, "FIN", "FinanceJet",
                         AcquisitionMethod.FINANCE, loan_terms, mixed, 210)
    leaseco = make_carrier(world, bank, a320, route, "LSE", "LeaseLine",
                           AcquisitionMethod.OPERATING_LEASE, lease_terms, premium, 230)
    for p in (cashco, finco, leaseco):
        for l in p.log:
            print(l)
        engine.add_player(p)
    print()

    ctx = {"market": MarketConditions()}
    for p in (cashco, finco, leaseco):
        print(f"{p.name}: starting cash ${p.ledger.cash:,.0f}")

    print("\n=== 1 YEAR OPERATION ===")
    for day in range(365):
        engine.tick(ctx)

    print()
    for p in (cashco, finco, leaseco):
        op = p.route_ops[0] if p.route_ops else None
        debt = sum(l.remaining for l in p.loans)
        cpax = op.last_class_pax if op else {}
        cpax_s = ", ".join(f"{k[:3]}:{v:.0f}" for k, v in cpax.items()) if cpax else "n/a"
        print(f"{p.name:11s} cash ${p.ledger.cash:>13,.0f} | debt ${debt:>12,.0f} | "
              f"daily px [{cpax_s}]")

    print("\nNet worth (cash + depreciated fleet value - debt):")
    from airlinesim.finance_cabin import aircraft_value
    for p in (cashco, finco, leaseco):
        debt = sum(l.remaining for l in p.loans)
        asset_value = sum(aircraft_value(a, world.sim_time) for a in p.fleet if a.owned)
        nw = p.ledger.cash + asset_value - debt
        owned_note = ""
        for a in p.fleet:
            if a.owned:
                v = aircraft_value(a, world.sim_time)
                owned_note = f" [{a.tail_number}: list ${a.spec.list_price/1e6:.0f}M -> ${v/1e6:.1f}M after 1yr/{a.airframe_hours:.0f}h]"
        print(f"  {p.name:11s} cash ${p.ledger.cash/1e6:>6.1f}M + assets ${asset_value/1e6:>6.1f}M "
              f"- debt ${debt/1e6:>5.1f}M = ${nw/1e6:>6.1f}M{owned_note}")


if __name__ == "__main__":
    main()

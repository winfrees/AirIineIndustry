"""
Route entity demonstration.

Shows the three layers:
  1. MARKET STRUCTURE: business vs leisure demand with different day-of-week and
     seasonal profiles -> total route demand varies by weekday and season.
  2. STAGE ECONOMICS: per-seat cost index by distance.
  3. SUITABILITY TIE-IN: equipment + crew validated against route requirements,
     with specific rejection reasons (range, runway, seat economics, augmented crew).
"""
from airlinesim.engine import (
    SpecRepository, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    MaintenanceProgram, CheckDefinition, CheckTier, PlaneClass, CrewType,
    World, Player, Airplane, CrewUnit, RouteOp, Ledger,
    PricingModel, MarketConditions, ResourceArbiter, MaintenanceEngine,
    DemandMarket,
)
from airlinesim.route import (
    default_segments, EquipmentRequirements, CrewRequirements,
    route_can_fly, block_hours, per_seat_cost_index, augmented_crew_required,
    TravelerSegment,
)


def mk_aircraft(repo, sid, name, seats, rng, speed, burn):
    spec = AircraftSpec(spec_id=sid, display_name=name, manufacturer="Airbus",
                        plane_class=(PlaneClass.WIDEBODY if seats > 250 else
                                     PlaneClass.NARROWBODY if seats > 100 else
                                     PlaneClass.REGIONAL),
                        list_price=seats*600_000, max_seats=seats, max_range_km=rng,
                        cruise_speed_kmh=speed, fuel_burn_lph=burn,
                        maint_program=MaintenanceProgram(checks=(
                            CheckDefinition(CheckTier.A,750,90,10,60,8000,PlaneClass.NARROWBODY),)))
    repo._tables[AircraftSpec][sid] = spec
    return spec


def main():
    repo = SpecRepository()
    # three aircraft of different size/range
    rj = mk_aircraft(repo, "RJ", "Regional Jet", 80, 3000, 800, 1100)
    nb = mk_aircraft(repo, "A320", "A320neo", 180, 6300, 833, 2400)
    wb = mk_aircraft(repo, "A350", "A350", 320, 15000, 900, 6500)

    # airports with different runway lengths
    big = AirportSpec(spec_id="BIG", display_name="Big Intl", iata="BIG",
                      runway_length_m=4000, total_gates=40, has_maintenance_facility=True,
                      facility_max_class=PlaneClass.WIDEBODY, fuel_supply_per_day_l=5_000_000)
    small = AirportSpec(spec_id="SML", display_name="Small Regional", iata="SML",
                        runway_length_m=1800, total_gates=10, has_maintenance_facility=True,
                        facility_max_class=PlaneClass.NARROWBODY, fuel_supply_per_day_l=1_000_000)
    repo._tables[AirportSpec]["BIG"] = big
    repo._tables[AirportSpec]["SML"] = small

    # --- THREE ROUTES with different character ---
    # thin short regional route: small market, needs a small plane, short runway
    thin = RouteSpec(spec_id="BIG-SML", display_name="Thin Regional", origin_iata="BIG",
                     dest_iata="SML", distance_km=400, base_demand_per_day=120,
                     segments=default_segments(120, business_frac=0.15, leisure_frac=0.65),
                     equipment_req=EquipmentRequirements(min_range_km=400, min_runway_m=1700,
                         optimal_class=PlaneClass.REGIONAL, min_viable_seats=40, max_viable_seats=120),
                     crew_req=CrewRequirements())
    # dense trunk route: big market, medium haul
    trunk = RouteSpec(spec_id="BIG-MED", display_name="Dense Trunk", origin_iata="BIG",
                      dest_iata="BIG", distance_km=1500, base_demand_per_day=900,
                      segments=default_segments(900, business_frac=0.35, leisure_frac=0.45),
                      equipment_req=EquipmentRequirements(min_range_km=1500, min_runway_m=2500,
                          optimal_class=PlaneClass.NARROWBODY, min_viable_seats=150, max_viable_seats=240),
                      crew_req=CrewRequirements())
    # ultra-long-haul: needs range AND augmented crew
    longhaul = RouteSpec(spec_id="BIG-FAR", display_name="Ultra Long Haul", origin_iata="BIG",
                         dest_iata="BIG", distance_km=12000, base_demand_per_day=400,
                         segments=default_segments(400, business_frac=0.45, leisure_frac=0.40),
                         equipment_req=EquipmentRequirements(min_range_km=12000, min_runway_m=3500,
                             optimal_class=PlaneClass.WIDEBODY, min_viable_seats=250, max_viable_seats=400),
                         crew_req=CrewRequirements(augmented_crew_block_hours=8.0))

    print("=" * 70)
    print("LAYER 3: EQUIPMENT + CREW SUITABILITY")
    print("=" * 70)

    std_crew = CrewUnit(CrewSpec("FD","Std",crew_type=CrewType.COCKPIT,
                                 cost_per_member_hour=220, certifications=("A320","A350","RJ")),
                        headcount=2, owner_id="X")
    aug_crew = CrewUnit(CrewSpec("FD","Aug",crew_type=CrewType.COCKPIT,
                                 cost_per_member_hour=220, certifications=("A320","A350","RJ")),
                        headcount=3, owner_id="X")

    def check(route, ac, crew, crewlabel):
        ok, reasons = route_can_fly(route, ac, big, small if route.dest_iata=="SML" else big, crew)
        verdict = "OK" if ok else "REJECT"
        print(f"  {route.display_name:18s} <- {ac.display_name:13s} ({crewlabel}): {verdict}")
        for r in reasons:
            print(f"       - {r}")

    print("\nThin regional route (wants small plane, 400km):")
    check(thin, rj, std_crew, "2 pilots")          # right-sized -> OK
    check(thin, wb, std_crew, "2 pilots")           # too big -> uneconomic reject

    print("\nDense trunk (wants narrowbody, 1500km):")
    check(trunk, nb, std_crew, "2 pilots")          # right-sized -> OK
    check(trunk, rj, std_crew, "2 pilots")          # too small -> reject

    print("\nUltra long haul (12000km, needs range + augmented crew):")
    check(longhaul, nb, std_crew, "2 pilots")       # range fail
    check(longhaul, wb, std_crew, "2 pilots")       # range ok but crew not augmented
    check(longhaul, wb, aug_crew, "3 pilots")       # augmented -> OK

    print("\n" + "=" * 70)
    print("LAYER 1: MARKET STRUCTURE — demand varies by weekday & season")
    print("=" * 70)
    pricing = PricingModel(reference_price=200.0)
    dm = DemandMarket("BIG-MED", 900, 0.0, segments=trunk.segments)
    print(f"  Dense Trunk total demand by day-of-week (at reference price):")
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    for d in range(7):
        t = d * 24.0 + 120*24  # day d, ~spring
        total = pricing.route_demand(dm, t, price_ratio=1.0)
        # break out segments
        seg = {s.segment.name: s.demand_on(t, 1.0) for s in trunk.segments}
        print(f"    {days[d]}: total {total:5.0f}  "
              f"(bus {seg['BUSINESS']:.0f}, leis {seg['LEISURE']:.0f}, conn {seg['CONNECTING']:.0f})")

    print(f"\n  Leisure seasonality (business route stays flatter):")
    for label, day in [("winter", 15), ("spring", 120), ("summer", 200), ("fall", 290)]:
        t = day * 24.0
        seg = {s.segment.name: s.demand_on(t, 1.0) for s in trunk.segments}
        print(f"    {label:7s}: business {seg['BUSINESS']:.0f}, leisure {seg['LEISURE']:.0f}")

    print("\n" + "=" * 70)
    print("LAYER 2: STAGE-LENGTH ECONOMICS")
    print("=" * 70)
    for dist in (400, 1500, 6000, 12000):
        bh = block_hours(dist, 850)
        idx = per_seat_cost_index(dist)
        aug = "augmented crew" if bh > 8 else "standard crew"
        print(f"  {dist:6d}km: block {bh:4.1f}h, per-seat cost index {idx:.2f}, {aug}")


if __name__ == "__main__":
    main()

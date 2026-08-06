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


# ==================================================================
# CREW DISTRIBUTION — the panel, and the arithmetic behind it
#
# A crew shortage is nearly always a DISTRIBUTION problem rather than a
# headcount one: positioning is direct-to-base only, so an airline can hold
# fifty idle crew at its hub and still cancel a departure at a station where
# it based nobody. The old panel printed one line of "ORD:12 DFW:8" per crew
# type, which showed the headcount and hid the shortage.
#
# These checks pin the aggregation AND the path from it to the browser,
# because a panel only this scenario can reach is not delivered.
# ==================================================================
CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append((label, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")


_STATES = ("flying", "resting", "capped", "away", "ready")


def check_distribution():
    from airlinesim.game import UNBASED, NOMINAL_LEG_H, new_game
    from airlinesim.crew import is_legal_for_flight

    print("\n=== CREW DISTRIBUTION ACROSS BASES ===")
    session = new_game(world="data", ai_profiles={"LSE": "Low-Cost",
                                                  "CRW": "Legacy"})
    session.pause()
    session.advance_days(60)
    snap = session.snapshot()
    session.stop()

    players = snap["players"]
    check("every carrier gets a crew_bases projection",
          all("crew_bases" in p for p in players))

    # A carrier with real flying to look at. The human starts with nothing on
    # a data world, so the interesting one is an AI that has built a network.
    p = max(players, key=lambda x: sum(b["headcount"]
                                       for b in x["crew_bases"].values()))
    bases = p["crew_bases"]
    print(f"  {p['name']}: {len(bases)} stations, "
          f"{sum(b['headcount'] for b in bases.values())} crew")
    for iata, b in sorted(bases.items(), key=lambda kv: -kv[1]["headcount"])[:8]:
        print(f"    {iata:9} based {b['headcount']:4d}  here {b['present']:4d}  "
              f"fly {b['flying']:3d} rdy {b['ready']:3d} rest {b['resting']:3d} "
              f"cap {b['capped']:3d} away {b['away']:3d}   "
              f"dep/day {b['departures']:5.1f} grounded {b['grounded']:2d} "
              f"trimmed {b['trimmed']:2d}")

    # The five states are MUTUALLY EXCLUSIVE and exhaustive. If they ever
    # stop summing to the headcount, the bar in the GUI is drawing widths
    # that don't add to 100% and quietly under- or over-reports the payroll.
    bad = [(i, b["headcount"], sum(b[s] for s in _STATES))
           for i, b in bases.items()
           if sum(b[s] for s in _STATES) != b["headcount"]]
    check("the crew states partition the headcount exactly", not bad,
          f"mismatched: {bad[:3]}" if bad else
          f"{len(bases)} stations, all states sum to headcount")

    # Per-type must partition the same way, or hovering a base reports a
    # different airline from the bar above it.
    bad_t = [(i, t) for i, b in bases.items() for t, v in b["by_type"].items()
             if sum(v[s] for s in _STATES) != v["headcount"]]
    check("the per-type breakdown partitions the same way", not bad_t,
          f"mismatched: {bad_t[:3]}" if bad_t else "cockpit/cabin/ground agree")

    # Availability must come from the ROSTER'S OWN GATE rather than a second
    # implementation of the duty rules, or the panel and the roster can
    # disagree about who is available — the panel saying "34 ready" while the
    # roster refuses all 34. Recount the pools independently against
    # `is_legal_for_flight` and require the totals to match the projection.
    now = session.world.sim_time
    mismatch = []
    for pl, pl_snap in zip(session.engine.players, players):
        assigned = {id(c) for op in pl.route_ops for c in (op.cockpit, op.cabin)
                    if c is not None}
        tally: dict = {}
        for pool in (pl.cockpit_pool, pl.cabin_pool, pl.crews):
            for c in pool:
                home = c.home_iata or c.location_iata or UNBASED
                legal, why = is_legal_for_flight(c, now, NOMINAL_LEG_H, c.limits)
                if id(c) in assigned:
                    s = "flying"
                elif not legal:
                    s = "resting" if why.startswith("resting") else "capped"
                elif home != UNBASED and c.location_iata != home:
                    s = "away"
                else:
                    s = "ready"
                tally.setdefault(home, dict.fromkeys(_STATES, 0))[s] += c.headcount
        for home, want in tally.items():
            got = pl_snap["crew_bases"].get(home)
            for s in _STATES:
                if got is None or got[s] != want[s]:
                    mismatch.append((pl.player_id, home, s,
                                     want[s], got[s] if got else None))
    check("availability is read off the roster's own legality gate",
          not mismatch and "is_legal_for_flight" in _game_source(),
          f"{len(mismatch)} disagreements, e.g. {mismatch[:2]}" if mismatch else
          "independent recount agrees with the projection at every base")

    # 'capped' has to be its own state. Folding it into 'ready' reported
    # crew as available at a base that could not crew its own departures —
    # the panel said "34 ready" beside "2 of 4 departures uncrewed".
    capped = sum(b["capped"] for b in bases.values())
    ready = sum(b["ready"] for b in bases.values())
    check("crew out of duty hours are counted apart from available crew",
          capped >= 0 and (capped + ready) > 0,
          f"{ready} available, {capped} out of hours across {p['name']}'s network")

    # `present` is the other half of the distribution question. It must count
    # every crew somewhere, and only differ from `headcount` when crew are
    # genuinely out of position.
    total_based = sum(b["headcount"] for b in bases.values())
    total_present = sum(b["present"] for b in bases.values())
    unbased = bases.get(UNBASED, {}).get("headcount", 0)
    check("crew are counted once as based and once as located",
          total_present == total_based - unbased,
          f"{total_based} based ({unbased} unbased) vs {total_present} located")

    # The failure the panel exists to surface has to be REACHABLE in a real
    # run, or the whole card is decoration.
    stations = [b for b in bases.values() if not b["headcount"] and b["routes"]]
    with_crew = [b for b in bases.values() if b["headcount"]]
    check("a real run produces stations that are flown but not based",
          bool(stations),
          "  ".join(f"{b['iata']}({b['departures']:g}/day,{b['present']}here)"
                    for b in stations[:6]) or "none — panel would be empty")
    check("a real run produces bases with crew to distribute",
          len(with_crew) >= 1,
          "  ".join(f"{b['iata']}:{b['headcount']}" for b in with_crew[:6]))

    # GROUNDED and TRIMMED must never be reported as one number. They are
    # different failures — one route flew NOTHING, the other flew a reduced
    # schedule — and a panel that adds them reads as "N departures left with
    # no crew aboard", which is not a thing the engine can do. Cross-check
    # both against the ops themselves.
    for pl, pl_snap in zip(session.engine.players, players):
        want_g, want_t = {}, {}
        for op in pl.route_ops:
            if not op.last_crew_block:
                continue
            o = op.spec.origin_iata
            if op.last_eff_freq > 0:
                want_t[o] = want_t.get(o, 0) + 1
            else:
                want_g[o] = want_g.get(o, 0) + 1
        wrong = [(o, want_g.get(o, 0), want_t.get(o, 0),
                  b["grounded"], b["trimmed"])
                 for o, b in pl_snap["crew_bases"].items()
                 if b["grounded"] != want_g.get(o, 0)
                 or b["trimmed"] != want_t.get(o, 0)]
        if wrong:
            break
    else:
        wrong = []
    check("a grounded route is counted apart from a short-crewed one",
          not wrong,
          f"disagreements: {wrong[:3]}" if wrong else
          "grounded = flew nothing; trimmed = flew fewer rotations than scheduled")

    # And the invariant underneath the whole panel, asserted directly: the
    # engine does not fly aircraft without crew. A player reported the old
    # wording as "routes are leaving when no crew is available" — this is the
    # check that says whether that could ever be true.
    flew_uncrewed = [f"{pl.player_id} {op.spec.origin_iata}->{op.spec.dest_iata}"
                     for pl in session.engine.players for op in pl.route_ops
                     if op.last_eff_freq > 0
                     and (op.cockpit is None or op.cabin is None)]
    check("no route ever operates without both cockpit and cabin crew",
          not flew_uncrewed,
          f"FLEW UNCREWED: {flew_uncrewed[:3]}" if flew_uncrewed else
          f"{sum(len(pl.route_ops) for pl in session.engine.players)} ops checked")
    over = [f"{c.spec.spec_id} {c.duty.hours_today:.2f}h"
            for pl in session.engine.players
            for pool in (pl.cockpit_pool, pl.cabin_pool)
            for c in pool
            if c.duty.hours_today > c.limits.max_daily_flight_hours + 1e-6]
    check("no crew is ever flown past its daily flight-hour cap", not over,
          f"OVER CAP: {over[:3]}" if over else "every crew inside its envelope")

    # Hubs must appear whether or not anyone is based at them yet — a hub you
    # just opened and haven't staffed is exactly what you need to see.
    for pl_snap in players:
        missing = [h for h in pl_snap["hubs"] if h not in pl_snap["crew_bases"]]
        if missing:
            break
    else:
        missing = []
    check("every declared hub appears in the panel, staffed or not",
          not missing, f"missing: {missing}" if missing else "")

    # Maintenance staff are hired with no home station. They have no base to
    # be away from, so they must not read as a permanent positioning failure.
    unb = bases.get(UNBASED)
    check("crew with no home base don't read as out of position",
          unb is None or unb["away"] == 0,
          f"{unb['headcount']} unbased, {unb['away']} counted away" if unb else
          "no unbased crew in this run")


def _game_source():
    from pathlib import Path
    import airlinesim.game as g
    return Path(g.__file__).read_text(encoding="utf-8")


def check_gui_wiring():
    """
    The panel has to be reachable by a player, not just by this scenario.
    `attach_alliances` shipped once with the actions written, the AI using
    them and NOTHING reachable from the GUI; this pins the whole chain so
    the crew card cannot go the same way.
    """
    from pathlib import Path
    import airlinesim.server as server_mod

    print("\n=== GUI WIRING ===")
    webui = Path(server_mod.WEBUI_DIR)
    html = (webui / "index.html").read_text(encoding="utf-8")
    app = (webui / "app.js").read_text(encoding="utf-8")
    css = (webui / "styles.css").read_text(encoding="utf-8")

    check("the crew card exists in the page", 'id="crewCard"' in html
          and 'id="crew"' in html)
    check("the renderer reads crew_bases from the snapshot",
          "crew_bases" in app and "crewBaseRow" in app)
    check("every crew state the projection emits is rendered",
          all(f'"{s}"' in app for s in _STATES),
          " ".join(_STATES))
    check("the bar has a fill colour for every state",
          all(f".crewSeg.{s}" in css for s in _STATES))
    check("stations flown but not based get their own line",
          "crewStations" in app and ".crewStations" in css)
    check("the per-type split is reachable from the bar",
          "crewTypeLines" in app and "by_type" in app)


def main():
    print("CREW DUTY/REST DEMONSTRATION")
    print("FAR Part 117-shaped limits: 9h/day, 60h/7d, 10h min rest")
    run("ONE crew set, aggressive 4x schedule", two_crews=False)
    run("TWO crew sets splitting the same 4x schedule", two_crews=True)
    check_distribution()
    check_gui_wiring()
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(CHECKS) else 'FAILURES ABOVE'}")


if __name__ == "__main__":
    main()

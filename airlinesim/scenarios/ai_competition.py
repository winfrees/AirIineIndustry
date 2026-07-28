"""
AI COMPETITION — do rival carriers actually run an airline?
===========================================================

The AI used to only move price and frequency on routes handed to it. This
scenario checks that AI carriers now plan networks, manage a fleet, staff
themselves, and differentiate by archetype — and, just as importantly, that
they do it through the SAME action layer the human plays through, so they
can't cheat.

Run: airlinesim run ai_competition
"""
from airlinesim.databuilder import build_world_from_data
from airlinesim.engine import MarketConditions, AirportSpec
from airlinesim.finance_cabin import CabinClass, aircraft_value
from airlinesim.ai import AICarrierSubsystem, ARCHETYPES, route_fit

DAYS = 700


def build(profiles, hub="ORD", n=5):
    """
    A BTS-data world on the game's real start conditions: the human carrier
    holds cash and nothing else, each AI holds ONE archetype-appropriate route
    and grows a network from it.
    """
    world, engine, _rep = build_world_from_data(
        hub=hub, n_destinations=n, verbose=False, ai_profiles=profiles)
    for p in engine.players:
        p.is_ai = p.player_id in profiles
    return world, engine


def net_worth(world, p):
    debt = sum(l.remaining for l in p.loans)
    assets = sum(aircraft_value(a, world.sim_time) for a in p.fleet if a.owned)
    return p.ledger.cash + assets - debt


def main():
    print("=" * 70)
    print("AI COMPETITION — rivals that build networks, not just reprice")
    print("=" * 70)

    world, engine = build({"LSE": "Low-Cost", "CRW": "Legacy", "RGN": "Regional"})
    ai_sub = next(s for s in engine.subsystems if isinstance(s, AICarrierSubsystem))
    humans = [p for p in engine.players if not p.is_ai]
    human = humans[0]
    ais = [p for p in engine.players if p.is_ai]

    start = {p.player_id: {"routes": len(p.route_ops), "fleet": len(p.fleet),
                           "nw": net_worth(world, p)} for p in engine.players}

    ctx = {"market": MarketConditions()}
    for _ in range(DAYS):
        engine.tick(ctx)

    print(f"\nAfter {DAYS} days:\n")
    for p in engine.players:
        tag = "AI" if p.is_ai else "  "
        prof = ai_sub.profile_of(p.player_id) if p.is_ai else None
        style = f" [{prof['archetype']}]" if prof else ""
        print(f"[{tag}] {p.name}{style}")
        print(f"       fleet {len(p.fleet):2d}  routes {len(p.route_ops):2d}  "
              f"cash ${p.ledger.cash/1e6:7.1f}M  net worth ${net_worth(world, p)/1e6:7.1f}M")
        types = {}
        for a in p.fleet:
            types[a.spec.display_name] = types.get(a.spec.display_name, 0) + 1
        if types:
            print(f"       fleet mix: {types}")
        tiers = {}
        for o in p.route_ops:
            tiers[o.service_tier] = tiers.get(o.service_tier, 0) + 1
        if tiers:
            print(f"       service tiers: {tiers}")

    print("\n" + "=" * 70)
    print("CHECKS")
    print("=" * 70)
    checks = []

    def check(label, ok, detail=""):
        checks.append((label, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if detail:
            print(f"         {detail}")

    # --- network planning ---
    grew = [p for p in ais if len(p.route_ops) > start[p.player_id]["routes"]]
    check("AI opens routes", len(grew) == len(ais),
          "; ".join(f"{p.name}: {start[p.player_id]['routes']}->{len(p.route_ops)}"
                    for p in ais))

    opened_pairs = {(o.spec.origin_iata, o.spec.dest_iata) for p in ais for o in p.route_ops}
    check("AI routes are real airport pairs with distance", len(opened_pairs) > 3,
          f"{len(opened_pairs)} distinct pairs")

    # --- fleet planning ---
    bought = [p for p in ais if len(p.fleet) > start[p.player_id]["fleet"]]
    check("AI acquires aircraft", len(bought) > 0,
          "; ".join(f"{p.name}: {start[p.player_id]['fleet']}->{len(p.fleet)}" for p in ais))

    # --- every opened route must be legal equipment: the AI went through the
    # same validation the human does, so nothing it flies may be unsuitable ---
    unsuitable = [(p.name, o.spec.spec_id, o.suitability_reasons)
                  for p in ais for o in p.route_ops if not o.suitable]
    check("no AI route flies unsuitable equipment", not unsuitable,
          str(unsuitable[:3]) if unsuitable else "all AI ops pass route_can_fly")

    # --- staffing: an AI that expands without hiring just strands schedules ---
    crewed = [p for p in ais if len(p.cockpit_pool) >= len(p.route_ops) * 0.5]
    check("AI staffs its network", len(crewed) == len(ais),
          "; ".join(f"{p.name}: {len(p.cockpit_pool)} cockpit / {len(p.route_ops)} routes"
                    for p in ais))

    flying = [p for p in ais if sum(o.last_pax for o in p.route_ops) > 0]
    check("AI routes actually carry passengers", len(flying) == len(ais),
          "; ".join(f"{p.name}: {sum(o.last_pax for o in p.route_ops):.0f} px/day" for p in ais))

    # --- solvency: expansion must be paid for, not wished for ---
    check("AI carriers remain solvent", all(net_worth(world, p) > 0 for p in ais),
          "; ".join(f"{p.name}: ${net_worth(world, p)/1e6:.1f}M" for p in ais))

    # --- archetype differentiation ---
    # All three styles are in THIS world, competing over the same corpus from
    # the same hub — differentiation has to survive contact with rivals, not
    # just show up in isolation.
    def by_style(name):
        return next((p for p in ais
                     if ai_sub.profile_of(p.player_id)["archetype"] == name), None)
    lowcost, legacy, regional = by_style("Low-Cost"), by_style("Legacy"), by_style("Regional")

    check("all three archetypes are running",
          all(x is not None for x in (lowcost, legacy, regional)),
          ", ".join(f"{p.name}={ai_sub.profile_of(p.player_id)['archetype']}" for p in ais))

    # --- price discipline: a cost-plus floor must never outrun the ceiling ---
    breaches = [(p.name, round(max((o.ticket_price for o in p.route_ops), default=0)))
                for p in ais
                if max((o.ticket_price for o in p.route_ops), default=0)
                > ARCHETYPES[ai_sub.profile_of(p.player_id)["archetype"]].price_ceiling + 0.01]
    check("no carrier prices above its ceiling", not breaches,
          str(breaches) if breaches else
          "; ".join(f"{p.name} max ${max((o.ticket_price for o in p.route_ops), default=0):.0f}"
                    for p in ais))

    # --- start conditions ---
    check("each AI started from exactly one route",
          all(start[p.player_id]["routes"] == 1 for p in ais),
          "; ".join(f"{p.name}: {start[p.player_id]['routes']}" for p in ais))
    check("the human starts with cash and nothing else",
          start[human.player_id]["routes"] == 0
          and start[human.player_id]["fleet"] == 0
          and human.ledger.cash > 0,
          f"{human.name}: {start[human.player_id]['fleet']} aircraft, "
          f"{start[human.player_id]['routes']} routes, "
          f"${human.ledger.cash/1e6:.0f}M cash")

    check("every AI carrier has somewhere to do maintenance",
          all(p.hub_iatas for p in ais),
          "; ".join(f"{p.name}: {p.hub_iatas}" for p in ais))

    # --- business-model fit: the premium carrier flies premium airports ---
    # Measured from the corpus (traffic rank + runway length), so this is the
    # SFO->JFK vs OAK->LGA distinction rather than a hand-authored opinion.
    def avg_fit(p):
        arch = ARCHETYPES[ai_sub.profile_of(p.player_id)["archetype"]]
        fits = [route_fit(arch, world.repo.get(AirportSpec, o.spec.origin_iata),
                          world.repo.get(AirportSpec, o.spec.dest_iata))
                for o in p.route_ops]
        return sum(fits) / max(1, len(fits))

    check("every carrier's network suits its own business model",
          all(avg_fit(p) >= 0.85 for p in ais),
          "; ".join(f"{p.name} fit {avg_fit(p):.2f}" for p in ais))

    if legacy:
        legacy_rwy = [world.repo.get(AirportSpec, i).runway_length_m
                      for o in legacy.route_ops
                      for i in (o.spec.origin_iata, o.spec.dest_iata)]
        arch = ARCHETYPES["Legacy"]
        check("premium carrier flies airports that can take its aircraft",
              legacy_rwy and min(legacy_rwy) >= arch.min_runway_pref_m * 0.8,
              f"shortest runway on the Legacy network: {min(legacy_rwy):.0f}m "
              f"(prefers >= {arch.min_runway_pref_m:.0f}m)")

    # --- financial discipline: these are public companies ---
    flows = {p.name: ai_sub.profile_of(p.player_id)["cash_flow_per_day"] for p in ais}
    check("every AI carrier reaches positive operating cash flow",
          all(v > 0 for v in flows.values()),
          "; ".join(f"{n}: ${v:,.0f}/day" for n, v in flows.items()))
    check("no AI carrier is left in permanent retrenchment",
          all(ai_sub.profile_of(p.player_id)["stage"] == "healthy" for p in ais),
          "; ".join(f"{p.name}: {ai_sub.profile_of(p.player_id)['stage']}" for p in ais))

    if lowcost and legacy:
        lc_tier = (sum(o.service_tier for o in lowcost.route_ops)
                   / max(1, len(lowcost.route_ops)))
        lg_tier = (sum(o.service_tier for o in legacy.route_ops)
                   / max(1, len(legacy.route_ops)))
        check("Low-Cost buys cheaper service than Legacy", lc_tier < lg_tier,
              f"avg service tier {lc_tier:.1f} vs {lg_tier:.1f}")

        def premium_seats(p):
            return sum(a.layout.seats_of(CabinClass.BUSINESS) for a in p.fleet if a.layout)
        check("Legacy configures premium cabins, Low-Cost doesn't",
              premium_seats(legacy) > 0 and premium_seats(lowcost) == 0,
              f"business seats — Legacy {premium_seats(legacy)}, "
              f"Low-Cost {premium_seats(lowcost)}")

        lc_fare = (sum(o.ticket_price for o in lowcost.route_ops)
                   / max(1, len(lowcost.route_ops)))
        lg_fare = (sum(o.ticket_price for o in legacy.route_ops)
                   / max(1, len(legacy.route_ops)))
        check("Low-Cost prices below Legacy", lc_fare < lg_fare,
              f"avg fare ${lc_fare:.0f} vs ${lg_fare:.0f}")

    # --- the human is untouched by the AI: no AI may mutate another carrier ---
    check("AI leaves the human's network alone",
          len(human.route_ops) == start[human.player_id]["routes"]
          and len(human.fleet) == start[human.player_id]["fleet"],
          f"{human.name}: {len(human.route_ops)} routes, {len(human.fleet)} aircraft")

    # --- no AI configured => world is untouched by ai.py ---
    from airlinesim.databuilder import build_world_from_data as _b
    w3, e3, _ = _b(hub="ORD", n_destinations=4, verbose=False)
    before3 = {p.player_id: len(p.route_ops) for p in e3.players}
    ctx3 = {"market": MarketConditions()}
    for _ in range(60):
        e3.tick(ctx3)
    check("no AI profiles => nobody plans a network",
          all(len(p.route_ops) == before3[p.player_id] for p in e3.players),
          "route counts unchanged without ai_profiles")

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(checks)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(checks) else 'SOME CHECKS FAILED'}")


if __name__ == "__main__":
    main()

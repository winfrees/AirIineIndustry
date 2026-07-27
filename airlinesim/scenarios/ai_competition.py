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
from airlinesim.engine import MarketConditions
from airlinesim.finance_cabin import CabinClass, aircraft_value
from airlinesim.ai import AICarrierSubsystem, ARCHETYPES

DAYS = 200


def build(profiles, hub="ORD", n=5):
    """A BTS-data world where the leasing carrier is AI-run."""
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

    world, engine = build({"LSE": "Low-Cost"})
    ai_sub = next(s for s in engine.subsystems if isinstance(s, AICarrierSubsystem))
    humans = [p for p in engine.players if not p.is_ai]
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
    # Archetype contrast needs the two styles side by side: same corpus, same
    # hub, same starting position — only the playbook differs.
    w2, e2 = build({"FIN": "Legacy", "LSE": "Low-Cost"})
    ctx2 = {"market": MarketConditions()}
    for _ in range(DAYS):
        e2.tick(ctx2)
    sub2 = next(s for s in e2.subsystems if isinstance(s, AICarrierSubsystem))
    lowcost = next((p for p in e2.players
                    if sub2.profile_of(p.player_id)["archetype"] == "Low-Cost"), None)
    legacy = next((p for p in e2.players
                   if sub2.profile_of(p.player_id)["archetype"] == "Legacy"), None)

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
    human = humans[0]
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

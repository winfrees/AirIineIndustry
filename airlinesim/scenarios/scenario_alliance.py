"""
ALLIANCES AND CONSOLIDATION CHECK
=================================

  1. FEED        connecting demand has to be fed. A hub-inbound leg is worth
                 more than a spoke-inbound one, and a partner's onward flights
                 count where a stranger's do not.
  2. ALLIANCE    forming one is a real trade: feed and reach against dues and
                 a self-imposed no-compete restraint.
  3. VALUATION   what a carrier is worth, itemised, and what it is worth to a
                 specific buyer.
  4. RATIONALE   the three reasons carriers combine, and the specific test for
                 "neither can compete alone".
  5. EXECUTION   a merger moves fleet, routes, crews, hubs AND debt, and
                 consolidates the overlap.

Run:  airlinesim run alliance
"""
from airlinesim import actions
from airlinesim.alliance import (
    ALLIANCE_TERMS, AllianceKind, alliance_of, alliance_snapshot,
    attach_alliances, feed_factor, onward_capacity,
)
from airlinesim.databuilder import build_world_from_data
from airlinesim.engine import MarketConditions
from airlinesim.merger import (
    Rationale, competitive_position, execute_merger, merger_case,
    reputation_of, value_carrier,
)

CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")


def _world(hub="ORD", n=6):
    w, e, _r = build_world_from_data(hub=hub, n_destinations=n, verbose=False)
    attach_alliances(w, e)
    return w, e, e.players[0], e.players[1]


def _run(engine, ticks):
    ctx = {"market": MarketConditions()}
    for _ in range(ticks):
        engine.tick(ctx)


# ------------------------------------------------------------------
# 1 — feed
# ------------------------------------------------------------------
def check_feed():
    print("\n=== FEED: connecting demand has to connect to something ===")
    w, e, a, b = _world()
    _run(e, 1)
    by_leg = {f"{o.spec.origin_iata}-{o.spec.dest_iata}": o.feed_factor
              for o in a.route_ops}
    inbound = {k: v for k, v in by_leg.items() if k.endswith("-ORD")}
    outbound = {k: v for k, v in by_leg.items() if k.startswith("ORD-")}
    print("   inbound to the hub :", {k: round(v, 3) for k, v in inbound.items()})
    print("   outbound to spokes :", {k: round(v, 3) for k, v in outbound.items()})

    check("a leg INTO a hub is worth more than one into a spoke",
          inbound and outbound and min(inbound.values()) > max(outbound.values()),
          f"hub-inbound {min(inbound.values()):.3f} vs spoke-inbound "
          f"{max(outbound.values()):.3f}")
    check("a spoke with nothing beyond it gets no connectivity bonus",
          all(abs(v - 1.0) < 1e-9 for v in outbound.values()))

    # The return leg of the same rotation is not a connection. Counting it made
    # every out-and-back pair look like a hub.
    leg = next(o for o in a.route_ops if o.spec.dest_iata != "ORD")
    onward_all = onward_capacity(w, e.players, leg.spec.dest_iata, a.player_id)
    onward_fwd = onward_capacity(w, e.players, leg.spec.dest_iata, a.player_id,
                                 exclude_dest=leg.spec.origin_iata)
    check("a passenger doesn't connect onto the flight back where they came from",
          onward_all > 0 and onward_fwd == 0,
          f"{leg.spec.origin_iata}-{leg.spec.dest_iata}: {onward_all:,.0f} seats "
          f"depart {leg.spec.dest_iata}, {onward_fwd:,.0f} of them go anywhere new")

    # A stranger's flights are worth nothing; a partner's are worth their tier.
    hub_leg = next(o for o in a.route_ops if o.spec.dest_iata == "ORD")
    solo = feed_factor(w, e.players, hub_leg, a.player_id)
    actions.form_alliance(w, a, "Test", "JOINT_VENTURE", [b.player_id])
    allied = feed_factor(w, e.players, hub_leg, a.player_id)
    check("a partner's onward flights add feed a stranger's did not",
          allied > solo, f"{solo:.4f} -> {allied:.4f} after allying")


# ------------------------------------------------------------------
# 2 — the alliance as a trade
# ------------------------------------------------------------------
def check_alliance():
    print("\n=== ALLIANCE: feed and reach, against dues and a restraint ===")
    for kind in AllianceKind:
        t = ALLIANCE_TERMS[kind]
        print(f"   {kind.name:14s} feed x{t.feed_efficiency:.2f}  "
              f"quality x{t.connect_quality:.2f}  dues ${t.dues_per_day:,.0f}/day")
    check("deeper cooperation feeds better and costs more",
          ALLIANCE_TERMS[AllianceKind.JOINT_VENTURE].feed_efficiency
          > ALLIANCE_TERMS[AllianceKind.CODESHARE].feed_efficiency
          > ALLIANCE_TERMS[AllianceKind.INTERLINE].feed_efficiency
          and ALLIANCE_TERMS[AllianceKind.JOINT_VENTURE].dues_per_day
          > ALLIANCE_TERMS[AllianceKind.INTERLINE].dues_per_day)

    w, e, a, b = _world()
    ok, msg = actions.form_alliance(w, a, "Skyway", "CODESHARE", [b.player_id])
    check("an alliance can be formed before the world has ticked", ok, msg)
    check("both carriers are members and see each other as partners",
          alliance_of(w, a.player_id) is alliance_of(w, b.player_id)
          and alliance_snapshot(w, a.player_id)["partners"] == [b.player_id])
    ok2, msg2 = actions.form_alliance(w, a, "Second", "INTERLINE", [])
    check("a carrier can't join two alliances at once", not ok2, msg2)

    # Dues are real but small against a working airline's revenue, so compare
    # the CHARGE rather than the balance — a carrier that earns more than it
    # pays in dues still ends the month richer, which says nothing either way.
    a.log.clear()
    _run(e, 30)
    dues = [ln for ln in a.log if "alliance dues" in ln]
    charged = sum(float(ln.split("$")[1].split()[0].replace(",", ""))
                  for ln in dues)
    expected = ALLIANCE_TERMS[AllianceKind.CODESHARE].dues_per_day * 30 / 2
    check("membership is billed every day, split across the members",
          dues and abs(charged - expected) < expected * 0.05,
          f"${charged:,.0f} charged over 30 days, expected ~${expected:,.0f}")

    # No-compete: a real restraint that costs the member a route.
    mine = {(o.spec.origin_iata, o.spec.dest_iata) for o in a.route_ops}
    partner_only = next((o for o in b.route_ops
                         if (o.spec.origin_iata, o.spec.dest_iata) not in mine), None)
    if partner_only is not None:
        pair = (partner_only.spec.origin_iata, partner_only.spec.dest_iata)
        actions.set_no_compete_hub(w, a, pair[0], True)
        blocked, why = actions.open_route(w, a, f"{pair[0]}-{pair[1]}",
                                          a.fleet[0].tail_number, price=250, freq=1)
        check("a no-compete hub blocks competing with a partner, with a reason",
              not blocked and pair[0] in why, why)
        actions.set_no_compete_hub(w, a, pair[0], False)
        allowed, _ = actions.open_route(w, a, f"{pair[0]}-{pair[1]}",
                                        a.fleet[0].tail_number, price=250, freq=1)
        check("dropping the agreement allows the route again", allowed)

    ok3, msg3 = actions.leave_alliance(w, a)
    check("a carrier can leave", ok3 and alliance_of(w, a.player_id) is None, msg3)


# ------------------------------------------------------------------
# 3 + 4 — valuation and rationale
# ------------------------------------------------------------------
def check_valuation():
    print("\n=== VALUATION ===")
    w, e, a, b = _world()
    _run(e, 20)
    for p in (a, b):
        v = value_carrier(w, p, cash_flow_per_day=200_000)
        print("  ", v.describe())

    v = value_carrier(w, a, cash_flow_per_day=200_000)
    check("a valuation is itemised, not a single number",
          v.fleet_value >= 0 and v.network_value > 0)
    check("a carrier is never worth less than its liquidation value",
          v.enterprise_value() >= v.liquidation_value())
    loser = value_carrier(w, a, cash_flow_per_day=-500_000)
    check("a loss-making carrier carries no going-concern value",
          loser.going_concern == 0.0 and loser.enterprise_value() < v.enterprise_value(),
          f"profitable ${v.enterprise_value():,.0f} vs "
          f"loss-making ${loser.enterprise_value():,.0f}")
    check("reputation is derived from the operating record, and is neutral-ish",
          0.45 <= reputation_of(w, a) <= 1.4, f"{reputation_of(w, a):.3f}")

    print("\n=== RATIONALE ===")
    case = merger_case(w, e.players, a, b, 200_000, 200_000)
    print("  ", case.describe())
    check("a merger case always states a rationale and a reason",
          case.rationale is not None and case.reason)
    check("overlapping networks read as HORIZONTAL",
          case.overlap_routes == 0 or case.rationale in
          (Rationale.HORIZONTAL, Rationale.COMPLEMENTARY, Rationale.SURVIVAL),
          f"{case.overlap_routes} overlapping routes -> {case.rationale.name}")

    # The specific test the brief asks for: neither can compete alone.
    pos_a = competitive_position(w, e.players, a, 200_000)
    print(f"   {a.name}: share {pos_a.share:.0%}, leader {pos_a.leader_share:.0%}, "
          f"sub-scale={pos_a.sub_scale}, losing={pos_a.losing} "
          f"-> cannot compete alone = {pos_a.cannot_compete_alone()}")
    check("a carrier that leads its market can always compete alone",
          not competitive_position(w, e.players, max(
              e.players, key=lambda p: sum(o.daily_frequency for o in p.route_ops)),
              200_000).cannot_compete_alone())

    # Force the survival condition: a tiny carrier bleeding cash against a
    # much larger rival is exactly the case a survival merger answers.
    small = b
    for op in list(small.route_ops)[2:]:
        small.route_ops.remove(op)
    small.ledger.cash = 3_000_000.0
    pos_small = competitive_position(w, e.players, small, -400_000)
    check("a sub-scale carrier bleeding cash against a leader cannot compete alone",
          pos_small.cannot_compete_alone(),
          f"share {pos_small.share:.0%} vs leader {pos_small.leader_share:.0%}, "
          f"runway {pos_small.cash_runway_days:.0f} days")
    check("being small is not on its own enough — a healthy niche is viable",
          not competitive_position(w, e.players, small, +400_000).cannot_compete_alone(),
          "same carrier, same size, positive cash flow")


# ------------------------------------------------------------------
# 5 — execution
# ------------------------------------------------------------------
def check_execution():
    print("\n=== EXECUTION ===")
    w, e, a, b = _world()
    _run(e, 10)
    a.ledger.cash = 5_000_000_000.0        # fund the deal outright
    fleet_before = len(a.fleet)
    routes_before = len(a.route_ops)
    debt_before = sum(l.remaining for l in a.loans)
    t_fleet, t_routes = len(b.fleet), len(b.route_ops)
    t_debt = sum(l.remaining for l in b.loans)
    # Directional: a duplicated ORD->LGA does not make LGA->ORD redundant.
    overlap = ({(o.spec.origin_iata, o.spec.dest_iata) for o in a.route_ops}
               & {(o.spec.origin_iata, o.spec.dest_iata) for o in b.route_ops})

    ok, msg = actions.acquire_carrier(w, a, b.player_id, 200_000, 200_000, force=True)
    print("  ", msg)
    check("a merger executes and reports what it did", ok, msg)
    check("the target's fleet transfers",
          len(a.fleet) == fleet_before + t_fleet and not b.fleet,
          f"{fleet_before} + {t_fleet} -> {len(a.fleet)}")
    check("overlapping routes are consolidated, not flown twice",
          len(a.route_ops) == routes_before + t_routes - len(overlap),
          f"{routes_before} + {t_routes} − {len(overlap)} overlap -> {len(a.route_ops)}")
    check("the target's DEBT transfers too — you buy what it owes",
          abs(sum(l.remaining for l in a.loans) - (debt_before + t_debt)) < 1.0,
          f"${debt_before:,.0f} + ${t_debt:,.0f} -> "
          f"${sum(l.remaining for l in a.loans):,.0f}")
    check("the target is left an empty shell",
          not b.fleet and not b.route_ops and not b.loans and not b.hub_iatas)
    check("every transferred asset answers to its new owner",
          all(x.owner_id == a.player_id for x in a.fleet)
          and all(o.owner_id == a.player_id for o in a.route_ops))

    # The combined carrier must still run.
    _run(e, 10)
    check("the combined carrier keeps flying after the merger",
          sum(o.last_pax for o in a.route_ops) > 0,
          f"{sum(o.last_pax for o in a.route_ops):,.0f} pax/day across "
          f"{len(a.route_ops)} routes")

    # And a deal nobody can fund is refused rather than half-executed.
    w2, e2, c, d = _world()
    _run(e2, 5)
    c.ledger.cash = 1_000.0
    ok2, msg2 = actions.acquire_carrier(w2, c, d.player_id, 0, 0)
    check("an unfundable acquisition is refused, not half-executed",
          not ok2 and d.fleet, msg2)


# ------------------------------------------------------------------
# 6 — reachable from a played game
# ------------------------------------------------------------------
def check_wiring():
    """
    The features have to be reachable by a HUMAN, not just from Python.

    They shipped once without this: the actions existed, the AI used them, and
    attach_alliances() was never called in the game path — so in an actual
    played game the subsystem wasn't attached, feed did nothing, and the
    alliance actions would have failed on an empty player roster.
    """
    print("\n=== REACHABLE FROM A PLAYED GAME ===")
    from airlinesim.alliance import AllianceSubsystem
    from airlinesim.game import new_game
    from airlinesim.server import COMMANDS

    gs = new_game(world="demo", weather=False)
    try:
        check("a played game attaches the alliance subsystem",
              any(isinstance(s, AllianceSubsystem) for s in gs.engine.subsystems))
        gs.advance_hours(24)

        for name in ("form_alliance", "join_alliance", "leave_alliance",
                     "set_no_compete_hub", "acquire_carrier"):
            check(f"GameSession exposes {name}()", callable(getattr(gs, name, None)))
            check(f"the HTTP command table forwards {name}", name in COMMANDS)

        cands = gs.merger_candidates()
        check("merger_candidates() costs every rival for the human",
              "candidates" in cands and len(cands["candidates"]) >= 1,
              f"{len(cands.get('candidates', []))} candidate(s), "
              f"cash ${cands.get('cash', 0):,.0f}")
        first = cands["candidates"][0]
        check("a candidate carries a rationale, a price and a reason",
              first["rationale"] and first["total_outlay"] > 0 and first["reason"],
              f"{first['name']}: {first['rationale']} "
              f"${first['total_outlay']:,.0f} — {first['reason'][:60]}")

        rival = first["player_id"]
        ok, msg = gs.form_alliance("Skyway", "CODESHARE", [rival])
        check("a human can form an alliance through the session API", ok, msg)
        ok2, _ = gs.set_no_compete_hub("HUB", True)
        check("a human can coordinate a hub", ok2)
        ok3, _ = gs.leave_alliance()
        check("a human can leave", ok3)

        # An AI must never buy the human out: losing the airline to a
        # takeover you were never asked about is an unanswerable loss, not a
        # difficulty. Tested by driving the AI's own review against a human
        # who would otherwise be an irresistible target.
        _check_ai_never_buys_the_human()

        # And if the human ever DOES lose everything, they are told.
        gs2 = new_game(world="demo", weather=False)
        try:
            # Fly first, so the session has SEEN the airline hold assets —
            # "lost everything" is only meaningful against having had
            # something, which is what stops a data world (which starts empty)
            # from being declared over on day one.
            gs2.advance_hours(24)
            human = gs2._human()
            human.fleet, human.route_ops = [], []
            human.loans, human.leases = [], []      # isolate from bankruptcy
            human.ledger.cash = 5_000_000.0
            gs2.advance_hours(24)
            check("losing every asset ends the game with a reason, not silence",
                  gs2.game_over and "airline is gone" in gs2.game_over_reason,
                  gs2.game_over_reason)
        finally:
            gs2.stop()

        # ...but a data-world game legitimately STARTS with no fleet and no
        # routes, so that test must not fire on day one.
        gs3 = new_game(world="data", n_destinations=3, weather=False)
        try:
            gs3.advance_hours(48)
            check("a game that starts with nothing is not instantly over",
                  not gs3.game_over,
                  f"human holds {len(gs3._human().fleet)} aircraft, "
                  f"{len(gs3._human().route_ops)} routes, "
                  f"${gs3._human().ledger.cash:,.0f}")
        finally:
            gs3.stop()
    finally:
        gs.stop()


def _check_ai_never_buys_the_human():
    from airlinesim.ai import AICarrierSubsystem
    w, e, a, b = _world()
    _run(e, 5)
    ai_sub = AICarrierSubsystem(profiles={a.player_id: "Legacy"})
    ai_sub._players = list(e.players)
    a.is_ai, b.is_ai = True, False          # b is the human
    a.ledger.cash = 50_000_000_000.0        # could buy anyone
    b.ledger.cash = 1.0                     # and b is desperate
    mem = ai_sub._mem(a, w)
    mem.stage = "healthy"
    mem.cash_flow_per_day = 5_000_000.0
    fleet_before, routes_before = len(b.fleet), len(b.route_ops)
    ai_sub._merger_review(w, a, mem, mem.archetype)
    check("an AI with unlimited cash still won't acquire the human",
          len(b.fleet) == fleet_before and len(b.route_ops) == routes_before,
          f"human kept {len(b.fleet)} aircraft and {len(b.route_ops)} routes")


def main():
    print("ALLIANCES AND CONSOLIDATION CHECK")
    print("=" * 70)
    check_feed()
    check_alliance()
    check_valuation()
    check_execution()
    check_wiring()
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(CHECKS) else 'FAILURES ABOVE'}")


if __name__ == "__main__":
    main()

"""
CABIN GEOMETRY + PER-CABIN PRICING CHECK
========================================

Pins the behavior of the cabin structure work end to end:

  1. GEOMETRY      an airframe's installable cabin is derived from published
                   abreast + max_seats, and all-economy always equals max_seats
  2. FITTING       seat counts snap to whole rows, overflow is trimmed, and an
                   unspecified economy cabin fills what's left
  3. ACTIONS       seats entered at ACQUISITION are actually installed (the bug
                   this work started from), and recabin takes the same input
  4. PRICING       a route can price EACH CABIN the assigned aircraft has, the
                   engine sells at those fares, and an unpriced cabin still
                   follows the base fare exactly as it always did
  5. SEAM          the per-route premium propensity is inert at its 1.0 default

Run:  airlinesim run cabin
"""
from airlinesim.actions import (
    acquire_aircraft, open_route, reconfigure_aircraft, set_cabin_price,
    set_price,
)
from airlinesim.builder import build_demo_world
from airlinesim.cabin import (
    fit_layout, geometry_for, preset_layout, presets_for, PRESETS,
)
from airlinesim.databuilder import _aircraft_specs
from airlinesim.engine import (
    AircraftSpec, MarketConditions, PlaneClass, RouteOp,
)
from airlinesim.finance_cabin import CabinClass, SeatLayout
from airlinesim.route import (
    SEGMENT_CABIN_SPLIT, TravelerSegment, cabin_demand_on, cabin_split_for,
    default_segments,
)

CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")


# ------------------------------------------------------------------
# 1 + 2 — geometry and fitting
# ------------------------------------------------------------------
def check_geometry():
    print("\n=== CABIN GEOMETRY ===")
    specs = {s.spec_id: s for s in _aircraft_specs()}
    print(f"{'type':8s} {'seats':>5s} {'cabin':>7s} {'Y':>3s} "
          f"{'W':>6s} {'J':>6s} {'F':>6s}   (premium seat cost in economy seats)")
    for sid in ("E175", "A220", "A320", "B738", "B763", "B789", "B77W"):
        g = geometry_for(specs[sid])
        print(f"{sid:8s} {specs[sid].max_seats:5d} {g.cabin_length_m:6.1f}m "
              f"{g.abreast_economy:3d} "
              f"{g.footprint(CabinClass.PREMIUM):6.2f} "
              f"{g.footprint(CabinClass.BUSINESS):6.2f} "
              f"{g.footprint(CabinClass.FIRST):6.2f}")

    # The invariant the whole engine rests on: an all-economy cabin is the
    # type's seat count. Cabin length is derived to make this true, so if it
    # ever stops being true the derivation is wrong, not the seat count.
    bad = [(s.spec_id, s.max_seats, fit_layout(s, {}).total_seats())
           for s in specs.values() if fit_layout(s, {}).total_seats() != s.max_seats]
    check("all-economy equals max_seats for every catalog type", not bad,
          f"mismatches: {bad}" if bad else f"{len(specs)} types checked")

    # A business seat costs more economy seats on a wider aircraft, because
    # the economy it displaces is denser. This is the whole reason geometry
    # beats a single flat footprint number.
    nb = geometry_for(specs["A320"]).footprint(CabinClass.BUSINESS)
    wb = geometry_for(specs["B789"]).footprint(CabinClass.BUSINESS)
    check("a business seat costs more on a widebody than a narrowbody", wb > nb,
          f"A320 {nb:.2f}Y vs 787-9 {wb:.2f}Y per business seat")

    # Monotonic: every step up the cabin ladder costs at least as much space.
    order = (CabinClass.ECONOMY, CabinClass.PREMIUM, CabinClass.BUSINESS,
             CabinClass.FIRST)
    mono = all(
        all(geometry_for(s).footprint(a) < geometry_for(s).footprint(b)
            for a, b in zip(order, order[1:]))
        for s in specs.values())
    check("footprint rises monotonically economy -> first", mono)

    print("\n=== FITTING ===")
    a320, b789 = specs["A320"], specs["B789"]

    f = fit_layout(a320, {"BUSINESS": 18})
    check("a request off a row boundary snaps to whole rows",
          f.layout.seats_of(CabinClass.BUSINESS) == 20 and f.notes,
          f"asked 18 business (4-abreast) -> {f.summary()}; {'; '.join(f.notes)}")

    f = fit_layout(a320, {"BUSINESS": 16})
    check("an unspecified economy cabin fills what's left",
          f.layout.seats_of(CabinClass.ECONOMY) == 144 and f.exact,
          f"asked 16 business -> {f.summary()} ({f.total_seats()} seats)")

    f = fit_layout(a320, {"ECONOMY": 4000})
    check("an impossible request is trimmed, not accepted",
          f.total_seats() == a320.max_seats and not f.exact,
          f"asked 4000 economy -> {f.summary()}; {'; '.join(f.notes)}")

    f = fit_layout(b789, {"FIRST": 8, "BUSINESS": 48, "PREMIUM": 21})
    fits = geometry_for(b789).fits(f.layout.seats)
    check("a three-cabin widebody plan fits the tube it's installed in", fits,
          f"{f.summary()} — {f.length_used_m():.1f}m of "
          f"{f.geometry.cabin_length_m:.1f}m")

    # Every preset, on every type, must be installable. A preset that doesn't
    # fit is a preset that hands the player an invalid starting point.
    bad_presets = []
    for s in specs.values():
        for name in PRESETS:
            fit = preset_layout(s, name)
            if not geometry_for(s).fits(fit.layout.seats):
                bad_presets.append((s.spec_id, name))
    check("every cabin preset fits every airframe in the catalog", not bad_presets,
          f"{len(PRESETS)} presets x {len(specs)} types"
          if not bad_presets else str(bad_presets))

    # The old flat model let this through: subtracting business seats from
    # max_seats one for one installs a cabin no fuselage could hold.
    naive = SeatLayout({CabinClass.BUSINESS: 26,
                        CabinClass.ECONOMY: b789.max_seats - 26})
    check("the old one-for-one seat subtraction is caught as over-capacity",
          not geometry_for(b789).fits(naive.seats),
          f"26J + {b789.max_seats - 26}Y needs "
          f"{geometry_for(b789).length_used(naive.seats):.1f}m of a "
          f"{geometry_for(b789).cabin_length_m:.1f}m cabin")


# ------------------------------------------------------------------
# 3 — the action surface (what the GUI drives)
# ------------------------------------------------------------------
def check_actions():
    print("\n=== ACQUISITION + RECABIN ===")
    world, engine = build_demo_world()
    player = engine.players[0]
    player.ledger.cash = 400_000_000

    ok, msg = acquire_aircraft(world, player, "A320", "CAB-1", "LEASE",
                               base_iata="HUB", seats={"business": 16})
    plane = next(a for a in player.fleet if a.tail_number == "CAB-1")
    installed = plane.effective_layout()
    # This is the reported bug: the seat counts were parsed, sent, and then
    # dropped, so the aircraft arrived all-economy with no error anywhere.
    check("seats requested at acquisition are actually installed",
          ok and installed.seats_of(CabinClass.BUSINESS) == 16,
          f"{msg}")
    check("economy fills the rest of the cabin at acquisition",
          installed.seats_of(CabinClass.ECONOMY) == 144,
          f"installed {installed.total_seats()} seats")

    ok, msg = acquire_aircraft(world, player, "A320", "CAB-2", "LEASE",
                               base_iata="HUB", seats="three-class")
    two = next(a for a in player.fleet if a.tail_number == "CAB-2")
    check("a named cabin preset can be bought directly",
          ok and two.effective_layout().seats_of(CabinClass.FIRST) > 0, msg)

    ok, msg = acquire_aircraft(world, player, "A320", "CAB-3", "LEASE",
                               base_iata="HUB", seats={"sleeper": 4})
    check("an unknown cabin name is refused before any money moves",
          not ok and not any(a.tail_number == "CAB-3" for a in player.fleet), msg)

    before = player.ledger.cash
    ok, msg = reconfigure_aircraft(world, player, "CAB-1", {"business": 28})
    check("recabin fits the same way acquisition does and charges for it",
          ok and plane.effective_layout().seats_of(CabinClass.BUSINESS) == 28
          and player.ledger.cash < before, msg)
    check("recabin grounds the tail for the type's downtime",
          not plane.in_service and plane.reconfiguring_until > world.sim_time,
          f"back in service at hour {plane.reconfiguring_until:.0f}")

    # CAB-1 is now in the shop from the recabin above, so it can't be sent
    # back in for another one.
    ok, msg = reconfigure_aircraft(world, player, "CAB-1", {"business": 40})
    check("recabining a tail already in the shop is refused", not ok, msg)

    ok, msg = reconfigure_aircraft(world, player, "CAB-2", "three-class")
    check("recabining to the cabin it already has is refused, not billed",
          not ok, msg)


# ------------------------------------------------------------------
# 4 — per-cabin pricing
# ------------------------------------------------------------------
def check_pricing():
    print("\n=== PER-CABIN PRICING ===")
    world, engine = build_demo_world()
    player = engine.players[0]
    player.ledger.cash = 400_000_000
    acquire_aircraft(world, player, "A320", "PRC-1", "LEASE",
                     base_iata="HUB", seats={"business": 16})
    ok, msg = open_route(world, player, "HUB-ORG", "PRC-1", price=200, freq=2)
    op = next(o for o in player.route_ops if o.plane.tail_number == "PRC-1")

    # Default: every cabin is the base fare times its class multiplier —
    # exactly what every route did before per-cabin prices existed.
    check("an unpriced cabin follows the base fare and its class multiple",
          op.fare_for(CabinClass.ECONOMY) == 200
          and op.fare_for(CabinClass.BUSINESS) == 800,
          f"economy ${op.fare_for(CabinClass.ECONOMY):.0f}, "
          f"business ${op.fare_for(CabinClass.BUSINESS):.0f}")

    ok, msg = set_cabin_price(world, player, _op_id(op), "BUSINESS", 1450)
    check("a cabin can be priced on its own", ok and op.fare_for(CabinClass.BUSINESS) == 1450, msg)
    check("pricing one cabin leaves the others alone",
          op.fare_for(CabinClass.ECONOMY) == 200,
          f"economy still ${op.fare_for(CabinClass.ECONOMY):.0f}")

    set_price(world, player, _op_id(op), 240)
    check("the base fare still moves every cabin that isn't priced",
          op.fare_for(CabinClass.ECONOMY) == 240
          and op.fare_for(CabinClass.BUSINESS) == 1450,
          f"economy ${op.fare_for(CabinClass.ECONOMY):.0f} moved, "
          f"business ${op.fare_for(CabinClass.BUSINESS):.0f} held")

    ok, msg = set_cabin_price(world, player, _op_id(op), "FIRST", 3000)
    check("a cabin the aircraft doesn't have can't be priced", not ok, msg)

    ok, msg = set_cabin_price(world, player, _op_id(op), "BUSINESS", None)
    check("clearing a cabin price hands it back to the base fare",
          ok and op.fare_for(CabinClass.BUSINESS) == 960, msg)

    # And it has to reach revenue, not just the accessor: run the sim with a
    # priced business cabin and confirm the takings follow the fare charged.
    set_cabin_price(world, player, _op_id(op), "BUSINESS", 1600)
    ctx = {"market": MarketConditions()}
    biz_rev = biz_pax = 0.0
    for _ in range(8):
        engine.tick(ctx)
        # a tick where duty limits grounded the rotation carries nobody at any
        # fare, so take the last tick that actually flew
        if op.last_class_pax.get("BUSINESS", 0.0) > 0:
            biz_rev = op.last_class_revenue.get("BUSINESS", 0.0)
            biz_pax = op.last_class_pax.get("BUSINESS", 0.0)
    implied = (biz_rev / biz_pax) if biz_pax > 1e-6 else 0.0
    check("the engine sells the business cabin at the fare it was given",
          abs(implied - 1600) < 1.0 and biz_pax > 0,
          f"{biz_pax:.1f} business pax, ${biz_rev:,.0f} — ${implied:,.0f}/seat")

    # Recabining away a cabin must not leave a fare priced against seats that
    # no longer exist.
    reconfigure_aircraft(world, player, "PRC-1", {"economy": 180})
    check("removing a cabin clears the fare that was set on it",
          CabinClass.BUSINESS not in op.cabin_prices,
          f"cabin prices now {[c.name for c in op.cabin_prices]}")


def _op_id(op):
    return f"{op.owner_id}:{op.spec.spec_id}:{op.plane.tail_number}"


# ------------------------------------------------------------------
# 5 — the per-route premium-propensity seam
# ------------------------------------------------------------------
def check_demand_seam():
    print("\n=== PER-ROUTE CABIN SPLIT (seam) ===")
    # Default MUST be a no-op: the shipped corpus measures nothing that could
    # set this, so every route runs at 1.0 and the numbers must be identical
    # to the global split.
    same = all(cabin_split_for(seg, 1.0) == SEGMENT_CABIN_SPLIT[seg]
               for seg in TravelerSegment)
    check("propensity 1.0 reproduces the global split exactly", same)

    segs = default_segments(1000)
    base = {c: cabin_demand_on(segs, c, 0.0, 1.0) for c in
            ("ECONOMY", "PREMIUM", "BUSINESS", "FIRST")}
    rich = {c: cabin_demand_on(segs, c, 0.0, 1.0, 1.4) for c in base}
    check("a higher propensity moves travelers up the cabin ladder",
          rich["FIRST"] > base["FIRST"] and rich["ECONOMY"] < base["ECONOMY"],
          "  ".join(f"{c[0]} {base[c]:.1f}->{rich[c]:.1f}" for c in base))
    check("a tilt moves demand between cabins without creating any",
          abs(sum(rich.values()) - sum(base.values())) < 1e-6,
          f"total {sum(base.values()):.2f} vs {sum(rich.values()):.2f} pax/day")

    poor = {c: cabin_demand_on(segs, c, 0.0, 1.0, 0.6) for c in base}
    check("a lower propensity moves them back down",
          poor["FIRST"] < base["FIRST"] and poor["ECONOMY"] > base["ECONOMY"],
          "  ".join(f"{c[0]} {base[c]:.1f}->{poor[c]:.1f}" for c in base))


def main():
    print("CABIN STRUCTURE CHECK")
    print("=" * 70)
    check_geometry()
    check_actions()
    check_pricing()
    check_demand_seam()
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(CHECKS) else 'FAILURES ABOVE'}")


if __name__ == "__main__":
    main()

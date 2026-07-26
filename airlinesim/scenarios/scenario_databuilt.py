"""
DATA-BUILT WORLD — the engine actually running on real BTS route data.
=====================================================================

Phase 3. build_demo_world() proves the pipeline on hand-authored constants; this
proves the SAME pipeline on the distilled BTS corpus, with specs flowing through
SpecRepository.load() — the import seam CLAUDE.md reserved for real-world data
and which nothing used before now.

The point of the checks below is that a data-driven world must be more than
"it didn't crash":

  * every op must be SUITABLE — real seat windows and runway requirements are
    stricter than the demo's hand-picked ones, so this is where a bad
    interpretive choice in distillation shows up
  * fleet, ops and financing must AGREE — an aircraft that failed acquisition
    must not appear in the fleet, which would inflate net worth
  * load factors must be plausible — a single token rotation against a 3,400
    px/day market reads as a capacity failure and makes real data look wrong
  * seasonality must actually bite — the fitted per-route curve should move
    carriage across the year
  * provenance must survive into the running sim

Run: airlinesim run databuilt
"""
from airlinesim.databuilder import build_world_from_data
from airlinesim.engine import MarketConditions
from airlinesim.finance_cabin import aircraft_value


def main():
    try:
        world, engine, rep = build_world_from_data(hub="ORD", n_destinations=4)
    except RuntimeError as exc:
        print(f"cannot build: {exc}")
        return False

    ctx = {"market": MarketConditions()}
    for _ in range(30):
        engine.tick(ctx)

    print("\n=== AFTER 30 DAYS ===")
    for p in engine.players:
        debt = sum(l.remaining for l in p.loans)
        assets = sum(aircraft_value(a, world.sim_time) for a in p.fleet if a.owned)
        pax = sum(o.last_pax for o in p.route_ops)
        seats = sum(o.plane.spec.max_seats * o.last_eff_freq for o in p.route_ops)
        lf = pax / seats if seats else 0.0
        print(f"  {p.name:11s} cash ${p.ledger.cash/1e6:7.1f}M "
              f"debt ${debt/1e6:7.1f}M NW ${(p.ledger.cash+assets-debt)/1e6:7.1f}M "
              f"| {pax:6,.0f} px/day over {len(p.route_ops)} routes, LF {lf:.1%}")
        unfunded = rep["unfunded"].get(p.name, [])
        if unfunded:
            print(f"    {len(unfunded)} route(s) unfunded (bank leverage cap)")

    print("\n=== PER-OP DETAIL (FinanceAir) ===")
    fin = engine.players[0]
    for o in fin.route_ops:
        print(f"  {o.spec.spec_id:8s} tier={o.spec.data_tier:6s} "
              f"{o.plane.spec.spec_id:5s} freq={o.daily_frequency:2d}->"
              f"{o.last_eff_freq:4.1f} LF={o.last_load_factor:6.1%} "
              f"pax={o.last_pax:6,.0f} profit=${o.last_profit:>10,.0f}"
              + ("" if o.suitable else f"  UNSUITABLE: {o.suitability_reasons}"))

    # Snapshot the steady-state operating picture BEFORE advancing: at day 210
    # some airframes are legitimately in a heavy check, so judging "did every
    # route fly" on a single later tick tests maintenance downtime, not the data.
    # `last_crew_block` covers two very different things: a total failure to
    # roster ("no legal crew available"), and duty limits trimming frequency
    # ("CABIN daily cap"). The second is the duty system working as designed and
    # must not read as a failure — only the first is.
    day30 = [{"id": o.spec.spec_id, "pax": o.last_pax,
              "lf": o.last_load_factor, "suitable": o.suitable,
              "unrostered": "no legal crew" in (o.last_crew_block or "")
                            or "no crew rostered" in (o.last_crew_block or ""),
              "duty_capped": o.last_eff_freq < o.daily_frequency,
              "eff_freq": o.last_eff_freq,
              "grounded": o.plane.grounded_until > world.sim_time}
             for p in engine.players for o in p.route_ops]
    capped = sum(1 for d in day30 if d["duty_capped"])
    print(f"\n  crew duty limits trimmed frequency on {capped}/{len(day30)} ops "
          f"— the data-implied frequency meets the real duty envelope")

    # Seasonality: the fitted per-route curve should shift carriage across the
    # year. Advance to the opposite side of the calendar and compare.
    winter = sum(o.last_pax for o in fin.route_ops)
    for _ in range(180):
        engine.tick(ctx)
    summer = sum(o.last_pax for o in fin.route_ops)
    print(f"\n=== SEASONALITY BITES ===")
    print(f"  carriage at day 30: {winter:,.0f} px/day")
    print(f"  carriage at day 210: {summer:,.0f} px/day  "
          f"({(summer/winter - 1) * 100:+.1f}%)")

    print("\n=== WHAT IS NOT FROM DATA ===")
    for note in rep["not_from_data"]:
        print(f"  - {note}")
    for gap in rep["corpus_gaps"]:
        print(f"  GAP: {gap}")

    all_ops = [o for p in engine.players for o in p.route_ops]
    print("\n=== CHECKS ===")
    checks = [
        ("world built from the corpus, not constants",
         len(rep["routes"]) >= 6 and rep["vintage"].startswith("t100")),
        ("every route spec came from a measured pair",
         all(r["tier"] == "exact" for r in rep["routes"])),
        ("provenance survives into the running sim",
         all(o.spec.data_tier == "exact" and o.spec.data_vintage
             for o in all_ops)),
        ("every op is suitable under real seat/runway requirements",
         all(o.suitable for o in all_ops)),
        ("equipment was right-sized, not uniform",
         len({r["aircraft"] for r in rep["routes"]}) > 1),
        ("aircraft in each seat window",
         all(r["seat_window"][0] <= r["seats"] <= r["seat_window"][1]
             for r in rep["routes"])),
        ("fleet matches ops for every carrier (nothing unpaid-for flying)",
         all(len(p.fleet) == len(p.route_ops) for p in engine.players)),
        ("financing carrier holds one loan per owned aircraft",
         len(engine.players[0].loans) == len(engine.players[0].fleet)),
        ("lease carrier owns no assets",
         sum(aircraft_value(a, world.sim_time)
             for a in engine.players[1].fleet if a.owned) == 0),
        ("every route carried passengers in steady state",
         all(d["pax"] > 0 for d in day30)),
        ("no route failed to roster a crew at all",
         not any(d["unrostered"] for d in day30)),
        ("every route actually operated flights",
         all(d["eff_freq"] > 0 for d in day30)),
        ("load factors are plausible (30-100%) in steady state",
         all(0.30 <= d["lf"] <= 1.001 for d in day30)),
        ("any later idle route is explained by maintenance or crew limits",
         all(o.last_pax > 0 or o.plane.grounded_until > world.sim_time
             or o.last_crew_block or not o.suitable for o in all_ops)),
        ("both carriers solvent after 210 days",
         all(p.ledger.cash > 0 for p in engine.players)),
        ("fitted seasonality moves carriage across the year",
         abs(summer / winter - 1) > 0.01),
        ("corpus gaps are reported, not hidden",
         len(rep["corpus_gaps"]) >= 1 and len(rep["not_from_data"]) >= 1),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    allpass = all(ok for _, ok in checks)
    print(f"\n{'ALL CHECKS PASS' if allpass else 'SOME CHECKS FAILED'}")
    return allpass


if __name__ == "__main__":
    main()

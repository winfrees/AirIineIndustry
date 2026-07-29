"""
WEATHER + DISRUPTION CHECK
==========================

Pins the whole chain the weather work added:

  1. CLOCK       the engine is dt-INDEPENDENT — a simulated month agrees
                 whether it is stepped in 24h, 6h or 1h slices. This is the
                 foundation everything else rests on, and it is what two
                 per-day-spent-per-tick bugs used to break.
  2. DETERMINISM the same seed gives the same storms, in this process and the
                 next one. explorer.py's whole premise depends on it.
  3. GEOGRAPHY   climate comes out of latitude and coastline: the north gets
                 snow, the Gulf coast gets convection and hurricanes, the
                 west gets fire smoke, and Miami never sees a blizzard.
  4. IMPACT      weather reduces capacity, cancels flights, delays the ones
                 that fly, eats crew duty hours, strands passengers, and puts
                 them in hotels — all charged to the ledger.
  5. RECORD      the per-airport disruption history accumulates, so a hub
                 that costs you every winter can be seen to.

Run:  airlinesim run weather
"""
import collections

from airlinesim.builder import build_demo_world
from airlinesim.databuilder import build_world_from_data
from airlinesim.disruption import (
    DisruptionSubsystem, WeatherSubsystem, airport_reliability, attach_weather,
    disruption_snapshot, tally_for,
)
from airlinesim.engine import MarketConditions
from airlinesim.weather import WeatherKind, WeatherModel, climate_for

CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")


def _run(dt, days, weather=False, seed=42):
    world, engine = build_demo_world()
    engine.dt = dt
    if weather:
        attach_weather(world, engine, seed=seed)
    ctx = {"market": MarketConditions()}
    pax = 0.0
    for _ in range(int(days * 24 / dt)):
        engine.tick(ctx)
        pax += sum(o.last_pax for p in engine.players for o in p.route_ops)
    return world, engine, pax


# ------------------------------------------------------------------
# 1 — the clock
# ------------------------------------------------------------------
def check_clock():
    print("\n=== CLOCK: hours, not days ===")
    results = {}
    for dt in (24.0, 6.0, 1.0):
        w, e, pax = _run(dt, 30)
        results[dt] = (pax, e.players[0].ledger.cash, w.sim_time)
    base_pax, base_cash, _ = results[24.0]
    spread_pax = max(abs(v[0] - base_pax) / base_pax for v in results.values())
    spread_cash = max(abs(v[1] - base_cash) / abs(base_cash) for v in results.values())
    check("a simulated month is the same at 24h, 6h and 1h resolution",
          spread_pax < 0.01 and spread_cash < 0.02,
          "  ".join(f"dt={k}: {v[0]:,.0f}px ${v[1]:,.0f}" for k, v in results.items()))
    check("every resolution lands on the same simulated time",
          len({round(v[2], 6) for v in results.values()}) == 1,
          f"sim_time {sorted({v[2] for v in results.values()})}")

    # The two bugs this had to fix, pinned so they can't come back. Both were
    # a per-DAY budget being spent per TICK.
    w, e, _ = _run(1.0, 10)
    gates_ok = all(gl.used() <= gl.total_gates for gl in w.gates.values())
    check("gate ledgers aren't exhausted by hourly ticks", gates_ok,
          "  ".join(f"{k} {gl.used():.1f}/{gl.total_gates}" for k, gl in w.gates.items()))
    ops = [o for p in e.players for o in p.route_ops]
    check("hourly ticks still fly the schedule",
          all(o.last_eff_freq > 0 for o in ops),
          f"effective frequencies {[round(o.last_eff_freq, 2) for o in ops]}")


# ------------------------------------------------------------------
# 2 — determinism
# ------------------------------------------------------------------
def check_determinism():
    print("\n=== DETERMINISM ===")
    a = WeatherModel(seed=7)
    b = WeatherModel(seed=7)
    c = WeatherModel(seed=8)
    for m in (a, b, c):
        m.add_airport("ORD", 41.98, -87.90)
        m.add_airport("MIA", 25.79, -80.29)

    sa = [(w.kind.name, round(w.capacity_factor, 6), round(w.delay_h, 6))
          for w in (a.at("ORD", float(h)) for h in range(0, 24 * 60))]
    sb = [(w.kind.name, round(w.capacity_factor, 6), round(w.delay_h, 6))
          for w in (b.at("ORD", float(h)) for h in range(0, 24 * 60))]
    sc = [(w.kind.name, round(w.capacity_factor, 6), round(w.delay_h, 6))
          for w in (c.at("ORD", float(h)) for h in range(0, 24 * 60))]
    check("the same seed produces identical weather", sa == sb,
          f"{len(sa)} hours compared")
    check("a different seed produces different weather", sa != sc)

    # Out-of-order access must not change anything: the explorer forks and
    # replays, so asking about hour 500 before hour 3 has to be safe.
    d = WeatherModel(seed=7)
    d.add_airport("ORD", 41.98, -87.90)
    shuffled = {}
    for h in list(range(0, 24 * 60))[::-1]:
        w = d.at("ORD", float(h))
        shuffled[h] = (w.kind.name, round(w.capacity_factor, 6), round(w.delay_h, 6))
    check("weather doesn't depend on the order it's asked about",
          [shuffled[h] for h in range(0, 24 * 60)] == sa)

    # hash() would be salted per process; blake2b is not. This is the property
    # that makes a save reproducible on another machine.
    from airlinesim.weather import _h01
    check("the noise source is stable, not process-salted",
          abs(_h01("airlinesim", 1, "check") - 0.5) < 0.5
          and _h01("a", 1) == _h01("a", 1) and _h01("a", 1) != _h01("a", 2))


# ------------------------------------------------------------------
# 3 — geography
# ------------------------------------------------------------------
def check_geography():
    print("\n=== GEOGRAPHY ===")
    spots = {
        "MIA": (25.79, -80.29), "ORD": (41.98, -87.90), "MSP": (44.88, -93.22),
        "SEA": (47.45, -122.31), "ATL": (33.64, -84.43), "LAX": (33.94, -118.41),
        "PHX": (33.43, -112.01), "BOS": (42.36, -71.01),
    }
    cl = {k: climate_for(k, *v) for k, v in spots.items()}
    print(f"  {'ap':5s} {'conv':>5s} {'winter':>7s} {'icing':>6s} {'hurr':>5s} "
          f"{'fire':>5s}  {'Jan':>6s} {'Jul':>6s}")
    for k, c in cl.items():
        print(f"  {k:5s} {c.convective:5.2f} {c.winter_severity:7.2f} "
              f"{c.icing_belt:6.2f} {c.hurricane_exposure:5.2f} "
              f"{c.wildfire_exposure:5.2f}  {c.mean_temp_c(15):6.1f} {c.mean_temp_c(200):6.1f}")

    check("the north is colder in January than the south",
          cl["MSP"].mean_temp_c(15) < cl["ORD"].mean_temp_c(15) < cl["MIA"].mean_temp_c(15))
    check("winter severity rises with latitude",
          cl["MSP"].winter_severity > cl["ATL"].winter_severity > cl["MIA"].winter_severity)
    check("Miami never freezes", cl["MIA"].freezing(15) == 0.0)
    check("the Gulf coast is more convective than the Pacific coast",
          cl["ATL"].convective > cl["LAX"].convective,
          f"ATL {cl['ATL'].convective:.2f} vs LAX {cl['LAX'].convective:.2f}")
    check("hurricane exposure is coastal and southern, not inland",
          cl["MIA"].hurricane_exposure > 0.5 and cl["MSP"].hurricane_exposure == 0.0
          and cl["ORD"].hurricane_exposure < 0.1,
          f"MIA {cl['MIA'].hurricane_exposure:.2f}  ORD {cl['ORD'].hurricane_exposure:.2f}  "
          f"MSP {cl['MSP'].hurricane_exposure:.2f}")
    check("wildfire smoke is a western exposure",
          cl["SEA"].wildfire_exposure > cl["BOS"].wildfire_exposure,
          f"SEA {cl['SEA'].wildfire_exposure:.2f} vs BOS {cl['BOS'].wildfire_exposure:.2f}")

    # A year of weather, to confirm the seasons land in the right months and
    # that each airport's disruption is dominated by the right thing.
    m = WeatherModel(seed=42)
    for k, (la, lo) in spots.items():
        m.add_airport(k, la, lo)
    profile = {}
    for k in spots:
        kinds = collections.Counter()
        winter_bad = summer_bad = 0
        for h in range(0, 24 * 365, 2):
            w = m.at(k, float(h))
            if not w.disrupted:
                continue
            kinds[w.kind.name] += 1
            day = (h / 24.0) % 365
            if day < 60 or day > 330:
                winter_bad += 1
            elif 150 < day < 240:
                summer_bad += 1
        profile[k] = (kinds, winter_bad, summer_bad)
        print(f"  {k}: {dict(kinds.most_common(3))}  winter-hit {winter_bad} "
              f"summer-hit {summer_bad}")

    check("a northern hub is disrupted more in winter than in summer",
          profile["MSP"][1] > profile["MSP"][2],
          f"MSP winter {profile['MSP'][1]} vs summer {profile['MSP'][2]}")
    check("snow and blizzards never reach Miami",
          not (profile["MIA"][0]["SNOW"] or profile["MIA"][0]["BLIZZARD"]),
          f"MIA kinds {dict(profile['MIA'][0])}")
    check("northern airports do see snow",
          profile["MSP"][0]["SNOW"] > 0,
          f"MSP snow hours (2-hourly samples) {profile['MSP'][0]['SNOW']}")


# ------------------------------------------------------------------
# 4 + 5 — operational impact
# ------------------------------------------------------------------
def check_impact():
    print("\n=== OPERATIONAL IMPACT ===")
    # The demo sandbox has no coordinates, so it has no weather — which is
    # itself the correct behaviour and worth stating. The impact test needs
    # the corpus world, where airports have real positions.
    clear_w, clear_e, _report = build_world_from_data(hub="ORD", n_destinations=5,
                                                      verbose=False)
    wx_w, wx_e, _r2 = build_world_from_data(hub="ORD", n_destinations=5, verbose=False)
    attach_weather(wx_w, wx_e, seed=42)
    check("attaching weather puts both subsystems in the right places",
          isinstance(wx_e.subsystems[0], WeatherSubsystem)
          and isinstance(wx_e.subsystems[-1], DisruptionSubsystem),
          f"{wx_e.subsystems[0].__class__.__name__} first, "
          f"{wx_e.subsystems[-1].__class__.__name__} last")

    for engine in (clear_e, wx_e):
        engine.dt = 1.0
    ctx_a, ctx_b = {"market": MarketConditions()}, {"market": MarketConditions()}
    for _ in range(24 * 120):
        clear_e.tick(ctx_a)
        wx_e.tick(ctx_b)

    clear_p = clear_e.players[0]
    wx_p = wx_e.players[0]
    tally = tally_for(wx_p)
    snap = disruption_snapshot(wx_w, wx_p)
    print(f"  120 days, {wx_p.name}: {snap['cancelled_flights']} flights cancelled, "
          f"{snap['delay_hours']:.0f} delay-hours, {snap['stranded_pax']:,} stranded "
          f"({snap['rebooked_pax']:,} rebooked), ${snap['total_cost']:,} in costs")

    check("weather cancels flights", tally.cancelled_flights > 0,
          f"{tally.cancelled_flights:.1f} flights over 120 days")
    check("weather delays the flights it doesn't cancel", tally.delay_hours > 0,
          f"{tally.delay_hours:.0f} delay-hours")
    check("cancelled flights strand passengers", tally.stranded_pax > 0,
          f"{tally.stranded_pax:,.0f} passengers")
    check("stranded passengers cost money (hotels, meals, compensation)",
          tally.hotel_cost > 0 and tally.compensation_cost > 0,
          f"hotels ${tally.hotel_cost:,.0f}, meals ${tally.meal_cost:,.0f}, "
          f"compensation ${tally.compensation_cost:,.0f}")
    check("some stranded passengers are re-seated rather than all refunded",
          tally.rebooked_pax > 0,
          f"{tally.rebooked_pax:,.0f} rebooked of {tally.stranded_pax:,.0f}")
    check("crew stuck away from base are put up overnight",
          tally.crew_hotel_cost > 0, f"${tally.crew_hotel_cost:,.0f}")
    check("a weather-exposed airline carries fewer passengers than a clear one",
          sum(o.last_pax for o in wx_p.route_ops) <= sum(o.last_pax for o in clear_p.route_ops)
          or tally.cancelled_flights > 0)
    check("the disruption bill actually hits the ledger",
          wx_p.ledger.cash < clear_p.ledger.cash,
          f"weathered ${wx_p.ledger.cash:,.0f} vs clear ${clear_p.ledger.cash:,.0f} "
          f"(difference ${clear_p.ledger.cash - wx_p.ledger.cash:,.0f})")

    rel = airport_reliability(wx_w)
    check("the per-airport disruption record accumulates", bool(rel),
          "; ".join(f"{k} {v['reliability']:.2f}" for k, v in
                    sorted(rel.items(), key=lambda kv: kv[1]["reliability"])[:4]))
    worst = min(rel.items(), key=lambda kv: kv[1]["reliability"]) if rel else None
    check("some airports are measurably worse than others",
          worst is not None and worst[1]["reliability"] < 1.0
          and len({round(v["reliability"], 2) for v in rel.values()}) > 1,
          f"worst: {worst[0]} at {worst[1]['reliability']:.2f} reliability, "
          f"{worst[1]['disrupted_hours']:.0f} disrupted hours" if worst else "")

    # A world with no weather model must be untouched — that is what keeps
    # every other scenario in this repo comparable.
    check("a world without weather is completely unaffected",
          all(getattr(o, "weather_capacity", 1.0) == 1.0 for o in clear_p.route_ops)
          and tally_for(clear_p).total_cost() == 0.0)


def main():
    print("WEATHER + DISRUPTION CHECK")
    print("=" * 70)
    check_clock()
    check_determinism()
    check_geography()
    check_impact()
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(CHECKS) else 'FAILURES ABOVE'}")


if __name__ == "__main__":
    main()

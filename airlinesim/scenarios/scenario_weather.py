"""
WEATHER + DISRUPTION CHECK
==========================

Pins the whole chain the weather work added:

  1. CLOCK       the engine is dt-INDEPENDENT — a simulated month agrees
                 whether it is stepped in 24h, 6h or 1h slices. This is the
                 foundation everything else rests on, and it is what two
                 per-day-spent-per-tick bugs used to break.
  2. PROBABILISTIC weather is a stochastic process, so a new game gets a new
                 season — but it is REPRODUCIBLE: same seed replays, and a
                 fork carries the generator state so two explorer branches
                 face the same weather and differ only by their decisions.
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
import pickle

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


def _run(dt, days, weather=False, seed=42, unlimited_duty=False):
    world, engine = build_demo_world()
    engine.dt = dt
    if weather:
        attach_weather(world, engine, seed=seed)
    if unlimited_duty:
        # Used only to separate crew-rest quantisation from a real dt leak.
        from airlinesim.crew import GROUND_DUTY_LIMITS
        for p in engine.players:
            for c in list(p.cockpit_pool) + list(p.cabin_pool) + list(p.crews):
                c.limits = GROUND_DUTY_LIMITS
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
    # CREW REST IS THE ONE THING A COARSE TICK GENUINELY CANNOT REPRESENT, and
    # the tolerance here says so rather than pretending otherwise. Rest is
    # `min_rest_hours` of CONSECUTIVE wall-clock time; a 24-hour tick has no
    # way to grant ten hours, so it grants twenty-four and the daily run is
    # slightly optimistic about how much crew is available. Everything else in
    # the engine is resolution-independent to well under a percent, which is
    # what the next check demonstrates by taking duty limits away.
    check("a simulated month is the same at 24h, 6h and 1h resolution",
          spread_pax < 0.05 and spread_cash < 0.02,
          "  ".join(f"dt={k}: {v[0]:,.0f}px ${v[1]:,.0f}" for k, v in results.items())
          + f"   (spread {spread_pax * 100:.1f}% pax / {spread_cash * 100:.1f}% cash)")

    # ...and prove the residual is ONLY rest quantisation. With a permissive
    # duty envelope the three resolutions converge to a fraction of a percent,
    # so any future drift in this number is a real dt leak somewhere else and
    # not the crew model — which is exactly the distinction that made the
    # deadhead duty bug (a whole leg's duty logged per TICK) hard to see.
    free = {}
    for dt in (24.0, 1.0):
        _w, e, pax = _run(dt, 30, unlimited_duty=True)
        free[dt] = (pax, e.players[0].ledger.cash)
    dev = abs(free[1.0][0] - free[24.0][0]) / free[24.0][0]
    check("with duty limits removed, the resolutions agree to <0.5%", dev < 0.005,
          f"dt=24: {free[24.0][0]:,.0f}px   dt=1: {free[1.0][0]:,.0f}px "
          f"({dev * 100:.2f}%) — so the spread above is crew rest, nothing else")

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
def _season_of(model, hours=24 * 60):
    """Run a model's process forward and record what the sky did."""
    out = []
    for h in range(hours):
        model.advance(float(h), 1.0)
        w = model.at("ORD", float(h))
        out.append((w.kind.name, round(w.capacity_factor, 6), round(w.delay_h, 6)))
    return out


def _model(seed):
    m = WeatherModel(seed=seed)
    m.add_airport("ORD", 41.98, -87.90)
    m.add_airport("MIA", 25.79, -80.29)
    return m


def check_probabilistic():
    print("\n=== PROBABILISTIC, AND REPRODUCIBLE ===")
    # Probabilistic: an unseeded model is a fresh season every time, so a
    # player cannot learn next week's weather from last playthrough's.
    seasons = [_season_of(WeatherModel(climates=_model(1).climates)) for _ in range(3)]
    check("an unseeded model draws a different season each time",
          len({tuple(s) for s in seasons}) == 3,
          f"{len({tuple(s) for s in seasons})} distinct seasons from 3 models")

    # Reproducible: the same seed replays exactly. This is what makes a SAVE
    # resume into the weather it would have had.
    a, b, c = _season_of(_model(7)), _season_of(_model(7)), _season_of(_model(8))
    check("the same seed replays the same season", a == b, f"{len(a)} hours compared")
    check("a different seed gives a different season", a != c)

    # The explorer's actual requirement: fork mid-season and both halves
    # continue identically. The generator state travels in the pickle.
    m = _model(11)
    for h in range(24 * 30):
        m.advance(float(h), 1.0)
    forked_a = pickle.loads(pickle.dumps(m))
    forked_b = pickle.loads(pickle.dumps(m))

    def continue_from(model):
        out = []
        for h in range(24 * 30, 24 * 60):
            model.advance(float(h), 1.0)
            w = model.at("ORD", float(h))
            out.append((w.kind.name, round(w.capacity_factor, 6)))
        return out

    check("forking mid-season and replaying gives identical weather",
          continue_from(forked_a) == continue_from(forked_b),
          "720 hours after a 30-day fork")
    check("a fork carries the generator state, not just the seed",
          forked_a.rng.getstate() != WeatherModel(seed=11).rng.getstate())

    # Switching off has to actually clear the sky, not freeze it.
    off = _model(7)
    for h in range(24 * 10):
        off.advance(float(h), 1.0)
    off.enabled = False
    w = off.at("ORD", 24 * 10.0)
    check("disabling weather reports a clear sky",
          w.kind is WeatherKind.CLEAR and w.capacity_factor == 1.0
          and not w.disrupted and off.active(24 * 10.0) == [])

    # Staged events: geography is respected, the calendar is not.
    jul = _model(3)
    jul.advance(0.0, 1.0)
    summer = 24 * 200.0
    check("a staged blizzard works out of season at a northern airport",
          jul.inject("BLIZZARD", "ORD", summer, 0.9) is not None)
    check("a staged hurricane is refused where the geography can't produce one",
          jul.inject("HURRICANE", "ORD", summer, 0.9) is None)
    check("a staged hurricane IS allowed on the hurricane coast",
          jul.inject("HURRICANE", "MIA", summer, 0.9) is not None)
    staged = jul.at("ORD", summer + 6.0)
    check("a staged event delivers roughly the intensity it was asked for",
          staged.intensity > 0.5,
          f"asked 0.9 out of season, delivered {staged.intensity:.2f}")


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
    # The model is a PROCESS now, so it has to be driven forward rather than
    # queried at arbitrary times: one pass over the year, advancing as it goes,
    # sampling every airport at each step.
    m = WeatherModel(seed=42)
    for k, (la, lo) in spots.items():
        m.add_airport(k, la, lo)
    kinds = {k: collections.Counter() for k in spots}
    winter = {k: 0 for k in spots}
    summer = {k: 0 for k in spots}
    step = 2.0
    for h in range(0, 24 * 365, int(step)):
        m.advance(float(h), step)
        day = (h / 24.0) % 365
        for k in spots:
            w = m.at(k, float(h))
            if not w.disrupted:
                continue
            kinds[k][w.kind.name] += 1
            if day < 60 or day > 330:
                winter[k] += 1
            elif 150 < day < 240:
                summer[k] += 1
    profile = {k: (kinds[k], winter[k], summer[k]) for k in spots}
    for k in spots:
        print(f"  {k}: {dict(kinds[k].most_common(3))}  winter-hit {winter[k]} "
              f"summer-hit {summer[k]}")

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
# 3b — the two REGIONAL kinds: nor'easters and lake effect
#
# These are the only kinds whose whole point is that they hit a NAMED
# handful of airports and nowhere else, so a check that they merely occur
# proves nothing. Both halves are asserted: the belt gets them, and the
# places that must never see them don't.
# ------------------------------------------------------------------
_NE_SPOTS = {
    # the Seaboard
    "BOS": (42.36, -71.01), "EWR": (40.69, -74.17), "LGA": (40.78, -73.87),
    "PWM": (43.65, -70.31), "PHL": (39.87, -75.24), "ORF": (36.89, -76.20),
    # the snow belt
    "BUF": (42.94, -78.73), "ROC": (43.12, -77.67), "SYR": (43.11, -76.10),
    "ERI": (42.08, -80.18), "MQT": (46.35, -87.40), "GRR": (42.88, -85.52),
    # controls: upwind of a lake, inland, and far away
    "MKE": (42.95, -87.90), "ORD": (41.98, -87.90), "MSP": (44.88, -93.22),
    "MIA": (25.79, -80.29), "SEA": (47.45, -122.31), "DEN": (39.86, -104.67),
}


def check_regional_kinds():
    print("\n=== NOR'EASTERS AND LAKE EFFECT ===")
    cl = {k: climate_for(k, *v) for k, v in _NE_SPOTS.items()}

    # The climate gate comes first. Every cold kind is multiplied by
    # `freezing()`, so a coast the model thinks is mild is a coast no winter
    # storm can reach — this was a real bug: continentality measured to the
    # NEAREST ocean gave Boston Seattle's February and silently gated snow,
    # ice, blizzards AND nor'easters off the entire Northeast.
    print(f"  {'ap':5s} {'Feb C':>6s} {'Jul C':>6s} {'frz':>5s} "
          f"{'noreast':>8s} {'lake':>5s}")
    for k, c in cl.items():
        print(f"  {k:5s} {c.mean_temp_c(46):6.1f} {c.mean_temp_c(200):6.1f} "
              f"{c.freezing(46):5.2f} {c.noreaster_exposure:8.2f} "
              f"{c.lake_effect_exposure:5.2f}")

    check("the Northeast coast actually freezes in winter",
          cl["BOS"].freezing(46) > 0.25 and cl["PWM"].freezing(46) > 0.25,
          f"BOS {cl['BOS'].freezing(46):.2f}, PWM {cl['PWM'].freezing(46):.2f}")
    check("nor'easter exposure rises up the Seaboard and stops at the coast",
          cl["BOS"].noreaster_exposure > cl["PHL"].noreaster_exposure
          > cl["ORF"].noreaster_exposure > cl["ORD"].noreaster_exposure,
          f"BOS {cl['BOS'].noreaster_exposure:.2f} > PHL "
          f"{cl['PHL'].noreaster_exposure:.2f} > ORF "
          f"{cl['ORF'].noreaster_exposure:.2f} > ORD "
          f"{cl['ORD'].noreaster_exposure:.2f}")
    check("nowhere west of the Appalachians has nor'easter exposure",
          max(cl[k].noreaster_exposure for k in ("ORD", "MSP", "DEN", "SEA")) == 0.0)
    check("lake-effect exposure is highest at the Erie/Ontario snow belt",
          min(cl[k].lake_effect_exposure for k in ("BUF", "ROC", "ERI")) > 0.5,
          f"BUF {cl['BUF'].lake_effect_exposure:.2f}, "
          f"ROC {cl['ROC'].lake_effect_exposure:.2f}, "
          f"ERI {cl['ERI'].lake_effect_exposure:.2f}")
    # The directionality IS the phenomenon. Milwaukee and Chicago sit on the
    # UPWIND shore of the same lake that buries Grand Rapids; a model that
    # scored them on distance alone would rank them together.
    check("a field upwind of a lake is not in the snow belt",
          cl["MKE"].lake_effect_exposure < 0.1
          and cl["ORD"].lake_effect_exposure < 0.1
          and cl["GRR"].lake_effect_exposure > 0.4,
          f"MKE {cl['MKE'].lake_effect_exposure:.2f} and ORD "
          f"{cl['ORD'].lake_effect_exposure:.2f} upwind, GRR "
          f"{cl['GRR'].lake_effect_exposure:.2f} downwind of Lake Michigan")
    check("airports with no Great Lake near them get no lake effect",
          max(cl[k].lake_effect_exposure for k in ("MSP", "BOS", "MIA", "DEN")) == 0.0)

    # Now DELIVERY. Exposure is only half of it — a kind also has to spawn
    # somewhere it can reach the belt from. Nor'easters need the Hatteras
    # genesis box; lake-effect bands have to be born over open water, and
    # spawning them uniformly across the Midwest basin put one on the snow
    # belt about once a year, some forty times rarer than the real thing.
    years = 6
    m = WeatherModel(seed=1877)
    for k, (la, lo) in _NE_SPOTS.items():
        m.add_airport(k, la, lo)
    days = {k: collections.Counter() for k in _NE_SPOTS}
    step = 6.0
    for h in range(0, int(24 * 365 * years), int(step)):
        m.advance(float(h), step)
        for k in _NE_SPOTS:
            w = m.over(k, float(h), step, samples=1)
            if w.intensity > 0.15:
                days[k][w.kind.name] += step / 24.0

    def per_year(k, kind):
        return days[k][kind] / years

    print(f"  {'ap':5s} {'noreast d/yr':>13s} {'lake d/yr':>10s}   ({years} sim-years)")
    for k in _NE_SPOTS:
        n, l = per_year(k, "NOREASTER"), per_year(k, "LAKE_EFFECT")
        if n or l:
            print(f"  {k:5s} {n:13.1f} {l:10.1f}")

    check("nor'easters reach the major Seaboard airports",
          min(per_year(k, "NOREASTER") for k in ("BOS", "EWR", "LGA")) > 0.5,
          "  ".join(f"{k} {per_year(k, 'NOREASTER'):.1f}/yr"
                    for k in ("BOS", "EWR", "LGA", "PWM")))
    check("nor'easters never reach the interior or the West",
          max(per_year(k, "NOREASTER")
              for k in ("ORD", "MSP", "DEN", "SEA", "MIA")) == 0.0)
    check("lake-effect bands actually land on the snow belt",
          min(per_year(k, "LAKE_EFFECT") for k in ("BUF", "ROC")) > 2.0,
          "  ".join(f"{k} {per_year(k, 'LAKE_EFFECT'):.1f}/yr"
                    for k in ("BUF", "ROC", "SYR", "ERI", "MQT")))
    check("lake-effect bands never land away from the lakes",
          max(per_year(k, "LAKE_EFFECT")
              for k in ("BOS", "MSP", "DEN", "SEA", "MIA", "PHL")) == 0.0)
    # Seasonality. The gate is a single harmonic, so midsummer is a small
    # number rather than exactly zero — the assertion is that it is a
    # negligible FRACTION of the seasonal peak, which is what a cosine can
    # actually promise. Lake effect peaks in December, earlier than the deep
    # winter kinds: it needs cold air over water that has not yet frozen.
    seasons = {}
    for kind, basin, peak in ((WeatherKind.NOREASTER, "atlantic", 35.0),
                              (WeatherKind.LAKE_EFFECT, "northeast", 349.0)):
        hi = m._seasonal_gate(kind, basin, peak, 38.0, 47.0, -82.0)
        lo = m._seasonal_gate(kind, basin, (peak + 182.5) % 365.0,
                              38.0, 47.0, -82.0)
        seasons[kind.name] = (hi, lo)
    check("both kinds are winter phenomena",
          all(hi > 0.5 and lo < 0.02 * hi for hi, lo in seasons.values()),
          "  ".join(f"{n} peak {hi:.2f} vs opposite season {lo:.4f}"
                    for n, (hi, lo) in seasons.items()))
    check("neither kind spawns outside its own genesis region",
          m._seasonal_gate(WeatherKind.NOREASTER, "northeast", 35.0,
                           38.0, 47.0, -82.0) == 0.0
          and m._seasonal_gate(WeatherKind.LAKE_EFFECT, "splains", 349.0,
                               29.0, 41.0, -108.0) == 0.0,
          "nor'easters only from the Hatteras box, lake effect only from the "
          "two basins that contain lakes")


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
    check_probabilistic()
    check_geography()
    check_regional_kinds()
    check_impact()
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(CHECKS) else 'FAILURES ABOVE'}")


if __name__ == "__main__":
    main()

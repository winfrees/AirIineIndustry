"""
ROUTE DATA CHECK — the three-tier historic/comparable lookup.
============================================================

Verifies the Phase-2 provider against the committed snapshot (real BTS T-100
Market 2023-2025). Runs offline: the snapshot is in the repo, the warehouse is
not needed.

What it asserts, and why each one earns its place:

  1. TIER RESOLUTION      a measured pair resolves EXACT, a pair between two
                          known airports that BTS never recorded resolves
                          COMPARABLE, an unknown airport resolves SYNTHETIC
  2. MONOTONICITY         the gravity model must not say a route gets thinner as
                          its endpoints grow. It fits BETTER with a size
                          interaction, and that version is wrong over half the
                          corpus — this check is what keeps it out.
  3. HONEST ACCURACY      the cross-validated median/within-2x travel with the
                          artifact, and Tier-2 demand is unbiased in the median
  4. SPEC CONSTRUCTION    route_spec() produces a RouteSpec the engine accepts,
                          with segment demand summing to the route total and
                          provenance attached
  5. NO SILENT FABRICATION  a Market-only corpus reports demand as CENSORED and
                          declares its gaps, rather than implying capacity it
                          doesn't have
"""
import math

from airlinesim.routedata import (RouteDataProvider, DataTier, gravity_features,
                                  load_provider)


def _find_absent_pair(p, limit=4000):
    """Two corpus airports with no BTS route between them — a Tier-2 case."""
    aps = p.airports
    for i, o in enumerate(aps):
        for d in aps[max(0, len(aps) - 60):]:
            if o == d:
                continue
            if p.observation(o, d).tier is DataTier.COMPARABLE:
                return o, d
        if i > limit:
            break
    return None, None


def main():
    p = load_provider()
    if p is None:
        print("No snapshot in airlinesim/data — run:")
        print("  airlinesim ingest --t100-market <export.zip> "
              "--fetch-airport-ref --distill")
        return False

    print("=== CORPUS ===")
    print("  " + p.summary().replace("\n", "\n  "))

    print("\n=== TIER RESOLUTION ===")
    exact = [("ATL", "MCO"), ("LAX", "JFK"), ("ORD", "DEN")]
    for o, d in exact:
        obs = p.observation(o, d)
        print(f"  {o}-{d}  {obs.tier.value:11s} demand={obs.demand_per_day:9,.0f}/day "
              f"dist={obs.distance_km:6,.0f}km amp={obs.season_amp:.3f} "
              f"peak_day={obs.season_peak_day}")
    co, cd = _find_absent_pair(p)
    comparable = None
    if co:
        comparable = p.observation(co, cd)
        print(f"  {co}-{cd}  {comparable.tier.value:11s} "
              f"demand={comparable.demand_per_day:9,.0f}/day "
              f"dist={comparable.distance_km:6,.0f}km  (gravity estimate)")
    syn = p.observation("ATL", "ZZZ")
    print(f"  ATL-ZZZ  {syn.tier.value:11s} (unknown airport -> engine defaults)")

    print("\n=== GRAVITY MONOTONICITY ===")
    g = p._gravity
    coef, cal = g.get("coefficients", []), g.get("calibration", 1.0)

    def pred(op, ip, dk):
        f = gravity_features(op, ip, dk, 50, 50)
        return math.exp(sum(c * x for c, x in zip(coef, f))) * cal

    sizes = (150, 500, 1096, 3000, 20000, 100000)
    size_bad = 0
    for ip in sizes:
        prev = None
        for op in sizes:
            v = pred(op, ip, 1500)
            if prev is not None and v < prev - 1e-9:
                size_bad += 1
            prev = v
    dist_bad, prev = 0, None
    for dk in (200, 500, 1000, 2000, 3500, 5000):
        v = pred(5000, 5000, dk)
        if prev is not None and v > prev + 1e-9:
            dist_bad += 1
        prev = v
    print(f"  size grid {len(sizes)}x{len(sizes)}: {size_bad} decreasing steps "
          f"(must be 0)")
    print(f"  distance sweep: {dist_bad} increasing steps (must be 0)")
    print(f"  elasticities: origin {coef[1]:+.3f}, dest {coef[2]:+.3f} "
          f"(textbook gravity is ~+0.5 each)")

    cv = g.get("cross_validation", {})
    print(f"\n=== HELD-OUT ACCURACY ({cv.get('folds')} folds, "
          f"{cv.get('held_out_routes', 0):,} routes) ===")
    print(f"  median predicted/actual : {cv.get('median_ratio')}  (1.0 = unbiased)")
    print(f"  within 2x               : {cv.get('within_2x', 0):.1%}")
    print(f"  within 3x               : {cv.get('within_3x', 0):.1%}")

    print("\n=== SPEC CONSTRUCTION ===")
    rs = p.route_spec("ORD", "DEN")
    seg_total = sum(s.base_per_day for s in rs.segments)
    print(f"  ORD-DEN  demand={rs.base_demand_per_day:,}  "
          f"segments sum={seg_total:,.0f}  amp={rs.seasonality_amplitude:.3f}")
    print(f"           tier={rs.data_tier!r} vintage={rs.data_vintage!r}")
    print(f"           seats window={rs.equipment_req.min_viable_seats}-"
          f"{rs.equipment_req.max_viable_seats}  "
          f"runway>={rs.equipment_req.min_runway_m:.0f}m  "
          f"range>={rs.equipment_req.min_range_km:.0f}km")
    ap = p.airport_spec("ORD")
    print(f"  ORD spec: runway={ap.runway_length_m:.0f}m gates={ap.total_gates} "
          f"fuel={ap.fuel_supply_per_day_l:,}L/day fee=${ap.landing_fee:,.0f}")
    syn_spec = p.route_spec("ATL", "ZZZ")

    print("\n=== SEASONAL SHAPE (fitted, not assumed) ===")
    for o, d in (("ATL", "MCO"), ("SFO", "SEA")):
        obs = p.observation(o, d)
        peak = max(range(12), key=lambda i: obs.monthly[i]) + 1
        trough = min(range(12), key=lambda i: obs.monthly[i]) + 1
        print(f"  {o}-{d}: monthly peak month {peak}, trough {trough}, "
              f"amp={obs.season_amp:.3f}, fitted peak_day={obs.season_peak_day}")

    print("\n=== CHECKS ===")
    checks = [
        ("snapshot loads with routes and airports",
         len(p) > 1000 and len(p.airports) >= 100),
        ("measured pairs resolve EXACT",
         all(p.observation(o, d).tier is DataTier.EXACT for o, d in exact)),
        ("an unrecorded pair between known airports resolves COMPARABLE",
         comparable is not None and comparable.tier is DataTier.COMPARABLE),
        ("an unknown airport resolves SYNTHETIC",
         syn.tier is DataTier.SYNTHETIC),
        ("gravity is monotone non-decreasing in both endpoint sizes",
         size_bad == 0),
        ("gravity is monotone decreasing in distance", dist_bad == 0),
        ("endpoint-size elasticities are positive",
         coef[1] > 0 and coef[2] > 0),
        ("Tier-2 is unbiased in the median (cross-validated)",
         0.9 <= (cv.get("median_ratio") or 0) <= 1.1),
        ("Tier-2 beats a coin flip: >50% of held-out routes within 2x",
         (cv.get("within_2x") or 0) > 0.5),
        ("segment demand sums to route demand",
         abs(seg_total - rs.base_demand_per_day) < 2.0),
        ("provenance is attached to every generated spec",
         rs.data_tier == "exact" and bool(rs.data_vintage)
         and syn_spec.data_tier == "synthetic"),
        ("seat window is ordered and plausible",
         0 < rs.equipment_req.min_viable_seats < rs.equipment_req.max_viable_seats),
        ("airport specs carry real runway lengths",
         ap is not None and ap.runway_length_m > 2000),
        ("Market-only corpus reports demand as CENSORED, not de-censored",
         "censored" in (p.manifest.get("demand_basis") or [])),
        ("corpus declares its known gaps",
         len(p.manifest.get("known_gaps") or []) >= 1),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    allpass = all(ok for _, ok in checks)
    print(f"\n{'ALL CHECKS PASS' if allpass else 'SOME CHECKS FAILED'}")
    return allpass


if __name__ == "__main__":
    main()

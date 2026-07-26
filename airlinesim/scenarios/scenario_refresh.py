"""
REFRESH CHECK — the corpus-maintenance logic, offline.
=====================================================

Phase 4. The refresh workflow's whole job is deciding when to update the corpus
and when to refuse, so the decisions themselves need testing without a network:

  1. STALENESS       against a FIXED date, so the assertions don't rot as the
                     real calendar moves, and so the DB1B end-of-collection cap
                     is exercised deliberately rather than by luck
  2. DIFFING         a reviewer must see what moved, not an opaque binary change
  3. REGRESSION GUARD a partial ingest still distills successfully, so a blind
                     cron would cheerfully replace a corpus that has capacity and
                     fares with one that has neither. This is the check that
                     stops it.
  4. GRAVITY WITHHOLD a 7-parameter fit on a handful of routes interpolates
                     noise. Below the row floor the coefficients are withheld so
                     unknown pairs resolve SYNTHETIC instead of being served a
                     fabricated "comparable route".

Run: airlinesim run refresh
"""
import datetime as _dt
import os
import shutil
import tempfile

from airlinesim.btsdata import (distill, ingest, refresh, schema, warehouse)

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "btsdata", "fixtures")


def _fixture_warehouse(path, with_db1b=True):
    conn = warehouse.connect(path)
    warehouse.create_all(conn)
    jobs = [(schema.T100_MARKET, "t100_market_sample.csv"),
            (schema.AIRPORT_REF, "airports_sample.csv"),
            (schema.RUNWAY_REF, "runways_sample.csv")]
    if with_db1b:
        jobs += [(schema.DB1B_MARKET, "db1b_market_sample.csv"),
                 (schema.DB1B_COUPON, "db1b_coupon_sample.csv")]
    for table, name in jobs:
        ingest.ingest_file(conn, table, os.path.join(FIXTURES, name), verbose=False)
    warehouse.backfill_longest_runway(conn)
    return conn


def main():
    tmp = tempfile.mkdtemp(prefix="refresh-scenario-")

    # --- 1. staleness, against a fixed date -------------------------------
    print("=== STALENESS (evaluated as of a fixed 2026-07-15) ===")
    fixed = _dt.date(2026, 7, 15)
    exp_t100 = refresh.expected_latest_t100(fixed)
    exp_db1b = refresh.expected_latest_db1b(fixed)
    print(f"  expected latest T-100: {exp_t100[0]}-{exp_t100[1]:02d} "
          f"({refresh.T100_LAG_MONTHS}-month publication lag)")
    print(f"  expected latest DB1B : {exp_db1b[0]}Q{exp_db1b[1]} "
          f"({refresh.DB1B_LAG_DAYS}-day lag)")

    conn = _fixture_warehouse(os.path.join(tmp, "fix.sqlite"))
    rows = refresh.staleness(conn)
    for s in rows:
        verdict = "MISSING" if s["missing"] else "STALE" if s["stale"] else "current"
        print(f"  {s['label']:16s} held {s['held']:8s} exp {s['expected']:8s} "
              f"{verdict:8s} {'auto' if s['automatable'] else 'MANUAL'}"
              + (f"  {s['note']}" if s["note"] else ""))
    by_key = {s["source"]: s for s in rows}

    # --- 2. distill a small corpus, then diff ------------------------------
    print("\n=== DIFF ===")
    old_dir, new_dir = os.path.join(tmp, "old"), os.path.join(tmp, "new")
    distill.distill(conn, old_dir, corpus_airports=20, min_pax_per_day=1.0,
                    verbose=False)
    first = refresh.diff_snapshots(os.path.join(tmp, "absent"), old_dir)
    print(f"  against no previous snapshot: first_snapshot="
          f"{first['first_snapshot']}, routes={first['routes']}")

    # Same warehouse distilled again must be a no-op diff — otherwise every
    # scheduled run would propose a PR full of noise.
    distill.distill(conn, new_dir, corpus_airports=20, min_pax_per_day=1.0,
                    verbose=False)
    same = refresh.diff_snapshots(old_dir, new_dir)
    print(f"  same warehouse twice: +{same['added']} / -{same['removed']}, "
          f"demand moved on {same['demand_moved_gt_10pct']} routes")

    # --- 3. regression guard ----------------------------------------------
    print("\n=== REGRESSION GUARD ===")
    # A corpus that loses its fares: distil the same volumes with DB1B absent.
    conn_nofare = _fixture_warehouse(os.path.join(tmp, "nofare.sqlite"),
                                     with_db1b=False)
    worse_dir = os.path.join(tmp, "worse")
    distill.distill(conn_nofare, worse_dir, corpus_airports=20,
                    min_pax_per_day=1.0, verbose=False)
    lost_fares = refresh.diff_snapshots(old_dir, worse_dir)
    bad_fares = refresh.regressions(lost_fares)
    print(f"  fares {lost_fares['fare_coverage_old']:.0%} -> "
          f"{lost_fares['fare_coverage_new']:.0%} => refuse: {bool(bad_fares)}")
    for b in bad_fares:
        print(f"    {b}")

    # A corpus that loses most of its routes.
    shrunk = dict(lost_fares, routes_old=6720, routes_new=20,
                  fare_coverage_old=1.0, fare_coverage_new=1.0,
                  connecting_coverage_old=1.0, connecting_coverage_new=1.0)
    bad_shrink = refresh.regressions(shrunk)
    print(f"  routes 6,720 -> 20 => refuse: {bool(bad_shrink)}")
    for b in bad_shrink:
        print(f"    {b}")

    # Capacity loss: Segment -> Market is the worst regression available.
    downgraded = dict(same, volume_table_old="t100_segment",
                      volume_table_new="t100_market")
    bad_cap = refresh.regressions(downgraded)
    print(f"  volume table Segment -> Market => refuse: {bool(bad_cap)}")
    for b in bad_cap:
        print(f"    {b}")

    # And an honest improvement must NOT be refused.
    improved = dict(same, fare_coverage_old=0.0, fare_coverage_new=1.0)
    print(f"  fares 0% -> 100% => refuse: {bool(refresh.regressions(improved))} "
          f"(must be False)")

    # --- 4. gravity withheld on a tiny corpus -----------------------------
    print("\n=== GRAVITY WITHHOLD ===")
    import json
    with open(os.path.join(old_dir, "gravity.json")) as fh:
        g = json.load(fh)
    print(f"  fixture corpus n={g['n']}, R²={g['r_squared']}, "
          f"floor={distill.MIN_GRAVITY_ROWS}")
    print(f"  withheld: {'withheld' in g}")
    if "withheld" in g:
        print(f"    {g['withheld']}")

    from airlinesim.routedata import RouteDataProvider, DataTier
    tiny = RouteDataProvider.from_dir(old_dir)
    # ATL and LAS are both in the fixture corpus but the pair is not a route,
    # so it would be a Tier-2 candidate if the model were usable.
    tier = tiny.observation("ATL", "LAS").tier
    print(f"  unknown pair between known airports resolves: {tier.value}")

    conn.close()
    conn_nofare.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== CHECKS ===")
    checks = [
        # expected_latest_*() report the RAW publication expectation; the
        # end-of-collection cap for DB1B is applied by staleness() and is
        # asserted separately below.
        ("staleness uses the publication lag, not today's date",
         exp_t100 == (2026, 4) and exp_db1b == (2026, 2)),
        ("DB1B expectation is capped at its end of collection (2025 Q2)",
         by_key["db1b_market"]["expected"] == "2025-02"
         and "OD40" in by_key["db1b_market"]["note"]),
        ("T-100 is reported as needing a manual export",
         not by_key["t100_market"]["automatable"]
         and by_key["db1b_market"]["automatable"]),
        ("missing T-100 Segment is flagged", by_key["t100_segment"]["missing"]),
        ("no previous snapshot is reported as the first, not as a loss",
         first["first_snapshot"] and first["routes"] > 0),
        ("re-distilling the same warehouse produces no route churn",
         same["added"] == 0 and same["removed"] == 0
         and same["demand_moved_gt_10pct"] == 0),
        ("losing fare coverage is refused", bool(bad_fares)),
        ("losing most routes is refused", bool(bad_shrink)),
        ("downgrading Segment -> Market is refused", bool(bad_cap)),
        ("an improvement is NOT refused", not refresh.regressions(improved)),
        ("gravity is withheld on a corpus too small to fit",
         "withheld" in g and g["coefficients"] == []),
        ("with gravity withheld, unknown pairs resolve SYNTHETIC not COMPARABLE",
         tier is DataTier.SYNTHETIC),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    allpass = all(ok for _, ok in checks)
    print(f"\n{'ALL CHECKS PASS' if allpass else 'SOME CHECKS FAILED'}")
    return allpass


if __name__ == "__main__":
    main()

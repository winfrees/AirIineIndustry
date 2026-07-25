"""
BTS INGEST CHECK — the offline half of the data-access verification.
==================================================================

The GitHub Actions job (.github/workflows/bts-probe.yml) runs the SAME probe
against the live BTS endpoints. This scenario runs it against committed
fixtures, so the parse -> normalize -> warehouse -> join chain is verified
everywhere, including sandboxes where bts.gov is unreachable and in CI with no
external dependency.

Splitting it this way means a red build tells you WHICH half broke: this
scenario failing means our code regressed; the Actions job failing while this
stays green means BTS changed something upstream.

Asserts:
  1. the full offline pipeline passes every probe check
  2. re-loading a slice is IDEMPOTENT — the failure mode that would silently
     double every passenger count in the warehouse
  3. the closed-runway exclusion holds in the longest-runway backfill
  4. a header mismatch is DIAGNOSED rather than crashing — this is the expected
     outcome when BTS renames a column, and the probe's whole value is reporting
     it usefully
"""
import argparse
import io
import os
import tempfile

from airlinesim.btsdata import probe, readers, schema, warehouse


def _args(db, **over):
    ns = argparse.Namespace(
        sources=["t100", "db1b_market", "db1b_coupon", "airports", "runways"],
        year=2024, month=6, quarter=2, limit=None, max_mb=400,
        db=db, offline=True, fixture_dir=probe.FIXTURE_DIR)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def main():
    tmp = tempfile.mkdtemp(prefix="btsdata-scenario-")
    db = os.path.join(tmp, "probe.sqlite")

    print("=== OFFLINE PROBE (fixtures) ===")
    first = probe.run(_args(db))
    for c in first["checks"]:
        print(f"  [{c['mark']}] {c['name']}: {c['detail']}")

    print("\n=== IDEMPOTENCE (reload the same slices twice) ===")
    again = probe.run(_args(db))
    print(f"  first pass:  {first['table_counts']}")
    print(f"  second pass: {again['table_counts']}")

    print("\n=== HEADER-MISMATCH DIAGNOSIS ===")
    # BTS renaming PASSENGERS is the realistic break. The reader must report it,
    # not raise, so the probe can print an actionable header diff.
    bogus = io.StringIO("YEAR,MONTH,UNIQUE_CARRIER,ORIGIN,DEST,PAX_TOTAL,SEATS,"
                        "DEPARTURES_PERFORMED,DISTANCE\n"
                        "2024,6,AA,ATL,ORD,100,120,2,607\n")
    rows, rep = readers.read_rows(schema.T100_SEGMENT, bogus)
    print(f"  rows parsed: {len(rows)} (expected 0)")
    print(f"  unmatched required: {rep.header.unmatched_required}")
    print(f"  mapped anyway: {sorted(rep.header.mapped)}")

    # Case/separator tolerance: the same table served through a different
    # channel with different header spellings must still resolve.
    variant = io.StringIO("Year,Month,Unique Carrier,Origin,Dest,Passengers,Seats,"
                          "Departures Performed,Distance\n"
                          "2024,6,AA,ATL,ORD,100,120,2,607\n")
    vrows, vrep = readers.read_rows(schema.T100_SEGMENT, variant,
                                    validator=readers.validate_t100)
    print(f"  spelling-variant headers resolved: {vrep.header.ok} "
          f"({len(vrows)} row parsed)")

    conn = warehouse.connect(db)
    atl = conn.execute("SELECT longest_runway_m FROM airport_ref "
                       "WHERE iata='ATL'").fetchone()[0]
    conn.close()

    print("\n=== CHECKS ===")
    checks = [
        ("offline probe passes every check", first["ok"]),
        ("all five sources loaded rows",
         all(s["rows_loaded"] > 0 for s in first["sources"].values())),
        ("reload is idempotent (no double-counting)",
         first["table_counts"] == again["table_counts"]),
        ("closed runway excluded from longest-runway backfill",
         atl is not None and 3700 < atl < 3800),
        ("renamed required column is diagnosed, not crashed",
         rows == [] and "passengers" in rep.header.unmatched_required),
        ("header spelling variants still resolve", vrep.header.ok and len(vrows) == 1),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    allpass = all(ok for _, ok in checks)
    print(f"\n{'ALL CHECKS PASS' if allpass else 'SOME CHECKS FAILED'}")
    return allpass


if __name__ == "__main__":
    main()

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
from dataclasses import asdict

from airlinesim.btsdata import discover, probe, readers, schema, warehouse


def _args(db, **over):
    ns = argparse.Namespace(
        sources=["t100", "db1b_market", "db1b_coupon", "airports", "runways"],
        year=2024, month=6, quarter=2, limit=None, max_mb=400,
        db=db, offline=True, discover=False, discover_only=False,
        fixture_dir=probe.FIXTURE_DIR)
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

    # --- T-100 channel discovery ---------------------------------------
    # Discovery only executes against the live network, so its parsing and
    # report rendering are exercised here against synthetic input. A crash in
    # this path would waste a whole Actions run and lose the diagnosis, which is
    # the one thing the live job exists to produce.
    print("\n=== DISCOVERY (parsing + rendering, synthetic input) ===")
    names = discover.candidate_names(2024, 6)
    print(f"  PREZIP name sweep would try {len(names)} distinct URLs")

    page = ('<html><title>Transtats</title><body>'
            '<a href="/PREZIP/T_100_Domestic_Segment_All_Carriers_2024_6.zip">dl</a>'
            '<form><input name="UserTableName" value="T_100_Domestic_Segment"/></form>'
            '<select name="t"><option value="T-100 Domestic Segment (All Carriers)">x'
            '</option></select></body></html>')
    ex = discover._Extractor()
    ex.feed(page)
    mentions = discover._PREZIP_RE.findall(page)
    print(f"  zip hrefs: {ex.zip_links}")
    print(f"  PREZIP mentions: {sorted(set(mentions))}")
    print(f"  form fields: {ex.form_fields}")

    rendered = []
    for hits in ([discover.UrlResult("https://x/found.zip", 200, 1234, "application/zip")],
                 []):
        drep = discover.DiscoveryReport(
            swept=[discover.UrlResult("https://x/miss.zip", 404, 0, "", "HTTP 404")] + hits,
            hits=list(hits),
            pages=[discover.PageResult("https://www.transtats.bts.gov/DL_SelectFields.aspx",
                                       200, "", "Download", ["/PREZIP/x.zip"],
                                       ["PREZIP/x.zip"],
                                       ["input:UserTableName=T_100_Domestic_Segment"],
                                       ["T-100 Domestic Segment"])],
            arcgis={"items": [], "fields": [], "query_url": "", "error": "unavailable"},
            notes=[f"swept {len(names)} PREZIP names, {len(hits)} answered 200"])
        rendered.append(probe.to_markdown({
            "generated_at": "t", "mode": "network",
            "period": {"year": 2024, "month": 6, "quarter": 2},
            "row_limit": None, "database": "", "table_counts": {},
            "sources": {}, "checks": [], "discovery": asdict(drep),
            "ok": bool(hits)}))
    print(f"  rendered hit/no-hit summaries: "
          f"{[len(r) for r in rendered]} chars")

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
        ("discovery sweeps a non-trivial name matrix", len(names) >= 40),
        ("discovery extracts zip hrefs, PREZIP names and form fields",
         ex.zip_links and mentions and any("UserTableName" in f for f in ex.form_fields)),
        ("discovery report renders for both hit and no-hit outcomes",
         all("T-100 channel discovery" in r for r in rendered)),
        ("per-table reject ceilings differ (reference vs traffic tables)",
         schema.AIRPORT_REF.max_reject_rate > schema.T100_SEGMENT.max_reject_rate),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    allpass = all(ok for _, ok in checks)
    print(f"\n{'ALL CHECKS PASS' if allpass else 'SOME CHECKS FAILED'}")
    return allpass


if __name__ == "__main__":
    main()

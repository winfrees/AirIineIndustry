"""
INGEST — load real BTS exports into the warehouse.
=================================================

The probe verifies a source is reachable and shaped right by sampling a bounded
slice. This loads the whole thing.

Written because T-100 has no stable URL: it comes out of the TranStats
field-picker as a session export (see download.py), so the practical workflow is
"export by hand once, then ingest the file". That makes a LOCAL PATH the primary
input here, not a fallback.

Three differences from the probe's load path, all forced by real files:

  * MULTI-MEMBER ZIPS. A T-100 export is one zip holding one CSV per year. The
    probe reads only the largest member; ingest walks every matching member.
  * STREAMING. A full-year T-100 export is ~250k rows and a DB1B coupon quarter
    is tens of millions. Rows are consumed through readers.iter_rows and flushed
    in batches rather than materialized.
  * WHOLE-YEAR IDEMPOTENCE. Each (year, month) partition is cleared the first
    time a row for it is seen, so re-ingesting a file replaces rather than
    doubling — the failure mode that would silently corrupt every downstream
    demand figure.

Loading is deliberately dumb: every row that parses goes in, including all-cargo
and charter service classes. Interpretation — which CLASS counts as passenger
demand, de-censoring, the gravity fit — belongs to distillation, so those choices
stay in one reviewable place instead of being baked into the loader.
"""
from __future__ import annotations
from collections import defaultdict
import argparse
import datetime as _dt
import io
import os
import sys
import zipfile

from airlinesim.btsdata import readers, schema, warehouse

BATCH = 20_000

# CLASS codes in T-100. Kept here as documentation, not as a filter: the loader
# stores every class and the distiller decides.
#   F  scheduled passenger/cargo service   <- the demand signal
#   G  scheduled all-cargo
#   L  non-scheduled civilian passenger (charter)
#   P  non-scheduled civilian passenger, other
SERVICE_CLASS_NOTES = {"F": "scheduled passenger", "G": "scheduled all-cargo",
                       "L": "charter passenger", "P": "charter passenger (other)"}


def _members(path: str, pattern: str = ".csv"):
    """
    Yield (name, text_stream) for every CSV in a zip, a directory, or a lone
    file. Streams from inside the zip — no extraction, no temp copies.
    """
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith(pattern):
                with open(os.path.join(path, name), encoding="latin-1") as fh:
                    yield name, fh
        return

    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        for info in sorted(zf.infolist(), key=lambda i: i.filename):
            if info.is_dir() or pattern not in info.filename.lower():
                continue
            with zf.open(info) as raw:
                yield info.filename, io.TextIOWrapper(raw, encoding="latin-1")
        return

    with open(path, encoding="latin-1") as fh:
        yield os.path.basename(path), fh


def _period_of(table, row) -> tuple:
    """Partition key for a row: (year, month) for T-100, (year, quarter) for DB1B."""
    year = int(row.get("year") or 0)
    if table.key.startswith("db1b"):
        return year, int(row.get("quarter") or 0)
    return year, int(row.get("month") or row.get("quarter") or 0)


def ingest_file(conn, table, path: str, limit=None, verbose=True) -> dict:
    """Load one path (zip / dir / csv) into `table`. Returns a report dict."""
    validator = readers.VALIDATORS.get(table.key)
    period_col = ("quarter" if table.key.startswith("db1b")
                  else "month" if table.key.startswith("t100") else None)

    summary = {"path": path, "table": table.key, "members": [],
               "rows_read": 0, "rows_kept": 0, "rejects": {},
               "partitions": {}, "class_rows": {}, "class_pax": {},
               "pairs": 0, "unmatched_required": []}
    pairs = set()

    for name, stream in _members(path):
        gen = readers.iter_rows(table, stream, limit=limit, validator=validator)
        prep = next(gen)
        if not prep.header.ok:
            summary["unmatched_required"] = list(prep.header.unmatched_required)
            summary["members"].append({"name": name, "error":
                                       f"unmatched required {prep.header.unmatched_required}",
                                       "headers": list(prep.header.headers)[:40]})
            if verbose:
                print(f"  {name}: UNMATCHED REQUIRED "
                      f"{prep.header.unmatched_required}")
            continue

        cleared, buffers, counts = set(), defaultdict(list), defaultdict(int)
        for row in gen:
            key = _period_of(table, row)
            if key not in cleared:
                # First row for this partition: drop any previous load of it.
                if period_col:
                    conn.execute(f"DELETE FROM {table.key} WHERE year=? AND "
                                 f"{period_col}=?", key)
                cleared.add(key)
            buffers[key].append(row)
            counts[key] += 1

            cls = row.get("service_class")
            if cls is not None:
                summary["class_rows"][cls] = summary["class_rows"].get(cls, 0) + 1
                summary["class_pax"][cls] = (summary["class_pax"].get(cls, 0.0)
                                             + (row.get("passengers") or 0.0))
            o, d = row.get("origin"), row.get("dest")
            if o and d:
                pairs.add((o, d))

            if len(buffers[key]) >= BATCH:
                warehouse.insert_rows(conn, table, buffers[key])
                buffers[key].clear()

        for key, rows in buffers.items():
            if rows:
                warehouse.insert_rows(conn, table, rows)
        conn.commit()

        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for (year, period), n in sorted(counts.items()):
            conn.execute(
                "INSERT OR REPLACE INTO partitions (source, year, period, rows, "
                "sha256, channel, url, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                (table.key, year, period, n, "", "local-file",
                 f"{path}#{name}", now))
        conn.commit()

        summary["rows_read"] += prep.rows_read
        summary["rows_kept"] += prep.rows_kept
        for why, cnt in prep.rejects.items():
            summary["rejects"][why] = summary["rejects"].get(why, 0) + cnt
        for (year, period), n in counts.items():
            summary["partitions"][f"{year}-{period:02d}"] = n
        summary["members"].append({"name": name, "rows_read": prep.rows_read,
                                   "rows_kept": prep.rows_kept,
                                   "truncated": prep.truncated})
        if verbose:
            rate = (1 - prep.rows_kept / prep.rows_read) if prep.rows_read else 0
            print(f"  {name}: {prep.rows_kept:,} kept of {prep.rows_read:,} "
                  f"({rate:.1%} rejected)")

    summary["pairs"] = len(pairs)
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="airlinesim ingest",
        description="Load BTS exports (zip / dir / csv) into the SQLite warehouse.")
    p.add_argument("--db", default="warehouse.sqlite", help="sqlite path")
    p.add_argument("--t100-market", default="", help="T-100 Market export path")
    p.add_argument("--t100-segment", default="", help="T-100 Segment export path")
    p.add_argument("--db1b-market", default="", help="DB1B Market export path")
    p.add_argument("--db1b-coupon", default="", help="DB1B Coupon export path")
    p.add_argument("--airports", default="", help="OurAirports airports.csv path")
    p.add_argument("--runways", default="", help="OurAirports runways.csv path")
    p.add_argument("--fetch-airport-ref", action="store_true",
                   help="download OurAirports airports+runways (GitHub-hosted)")
    p.add_argument("--limit", type=int, default=0, help="cap rows per member (0=all)")
    p.add_argument("--distill", nargs="?", const="airlinesim/data", default="",
                   metavar="OUT_DIR",
                   help="after loading, write the snapshot the simulation reads "
                        "(default airlinesim/data)")
    p.add_argument("--corpus-airports", type=int, default=0,
                   help="top N airports in the snapshot (default 300)")
    p.add_argument("--min-pax-per-day", type=float, default=-1.0,
                   help="drop directional pairs below this (default 10)")
    args = p.parse_args(argv)

    jobs = [(schema.T100_MARKET, args.t100_market),
            (schema.T100_SEGMENT, args.t100_segment),
            (schema.DB1B_MARKET, args.db1b_market),
            (schema.DB1B_COUPON, args.db1b_coupon),
            (schema.AIRPORT_REF, args.airports),
            (schema.RUNWAY_REF, args.runways)]
    jobs = [(t, path) for t, path in jobs if path]

    if not jobs and not args.fetch_airport_ref:
        p.print_help()
        return 2

    conn = warehouse.connect(args.db)
    warehouse.create_all(conn)
    limit = args.limit or None
    summaries = []

    for table, path in jobs:
        print(f"\n{table.label}\n  <- {path}")
        summaries.append(ingest_file(conn, table, path, limit=limit))

    if args.fetch_airport_ref:
        from airlinesim.btsdata import download
        for table, cands in ((schema.AIRPORT_REF, download.airport_candidates()),
                             (schema.RUNWAY_REF, download.runway_candidates())):
            print(f"\n{table.label}\n  <- {cands[0].url}")
            cand, payload, _ = download.resolve(cands)
            name, stream = download.open_payload(payload, cand.member_hint)
            rows, prep = readers.read_rows(table, stream,
                                           validator=readers.VALIDATORS.get(table.key))
            n = warehouse.replace_partition(
                conn, table, 0, 0, rows, warehouse.sha256(payload), cand.channel,
                cand.url, _dt.datetime.now(_dt.timezone.utc).isoformat())
            rate = (1 - prep.rows_kept / prep.rows_read) if prep.rows_read else 0
            print(f"  {name}: {n:,} kept of {prep.rows_read:,} ({rate:.1%} rejected)")
        warehouse.backfill_longest_runway(conn)

    print("\n=== WAREHOUSE ===")
    for key, n in warehouse.table_counts(conn).items():
        if n:
            print(f"  {key:14s} {n:>10,} rows")
    parts = conn.execute("SELECT source, COUNT(*) AS n, MIN(year) AS lo, "
                         "MAX(year) AS hi FROM partitions GROUP BY source").fetchall()
    for r in parts:
        print(f"  {r['source']:14s} {r['n']:>3} partitions, {r['lo']}-{r['hi']}")

    for s in summaries:
        if s["class_pax"]:
            print(f"\n  {s['table']} passengers by CLASS "
                  f"({', '.join(SERVICE_CLASS_NOTES.get(c, c) for c in s['class_pax'])}):")
            for cls, pax in sorted(s["class_pax"].items(), key=lambda kv: -kv[1]):
                print(f"    {cls} {SERVICE_CLASS_NOTES.get(cls, ''):26s} "
                      f"{pax:>15,.0f} pax  ({s['class_rows'].get(cls, 0):,} rows)")
        if s["rejects"]:
            print(f"  {s['table']} rejects: {s['rejects']}")
        print(f"  {s['table']} distinct directional pairs: {s['pairs']:,}")

    if args.distill:
        from airlinesim.btsdata import distill as _distill
        print(f"\n=== DISTILL -> {args.distill} ===")
        _distill.distill(
            conn, args.distill,
            corpus_airports=args.corpus_airports or _distill.CORPUS_AIRPORTS,
            min_pax_per_day=(args.min_pax_per_day if args.min_pax_per_day >= 0
                             else _distill.MIN_PAX_PER_DAY))

    conn.close()
    print(f"\nwarehouse: {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

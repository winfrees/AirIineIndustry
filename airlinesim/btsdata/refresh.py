"""
REFRESH — keep the corpus current, and say plainly when it can't.
===============================================================

Phase 4. The original plan was "monthly cron fetches everything and opens a PR".
Reality, established by the probe runs, splits the sources in two:

  AUTOMATABLE   DB1B Market/Coupon (confirmed live at /PREZIP/, per-quarter
                URLs) and OurAirports (GitHub-hosted). These refresh unattended.

  MANUAL ONLY   T-100. It is not in /PREZIP/ at all; it comes out of the
                TranStats field-picker as a per-request session export whose URL
                is a receipt, not a channel. No cron can fetch it.

So "self-updating" is honest for fares and airport reference data, and for T-100
the best a machine can do is NOTICE that the corpus has gone stale and say
exactly what to re-export. Pretending otherwise would mean a workflow that
silently ships a corpus a year out of date.

`airlinesim refresh` therefore:
  1. reports staleness per source against what BTS should have published
  2. fetches what it can
  3. re-distills
  4. diffs the new snapshot against the committed one, so a reviewer sees what
     moved rather than an opaque binary change

It refuses to write a snapshot that would LOSE data — dropping from a corpus
with capacity or fares to one without is the failure mode a blind cron would
commit cheerfully.
"""
from __future__ import annotations
import argparse
import csv
import datetime as _dt
import gzip
import json
import os
import sys

from airlinesim.btsdata import distill, download, ingest, readers, schema, warehouse

# T-100 domestic publishes roughly two months in arrears; DB1B roughly 45 days
# after quarter end. Used only to decide whether a re-export is worth asking for.
T100_LAG_MONTHS = 3
DB1B_LAG_DAYS = 60


def _today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def expected_latest_t100(today=None) -> tuple:
    d = today or _today()
    m = d.month - T100_LAG_MONTHS
    y = d.year
    while m <= 0:
        m += 12
        y -= 1
    return y, m


def expected_latest_db1b(today=None) -> tuple:
    d = (today or _today()) - _dt.timedelta(days=DB1B_LAG_DAYS)
    return d.year, (d.month - 1) // 3 + 1


def staleness(conn) -> list:
    """One row per source: what we hold, what should exist, and the verdict."""
    out = []
    for key, label, expect in (
            ("t100_market", "T-100 Market", expected_latest_t100()),
            ("t100_segment", "T-100 Segment", expected_latest_t100()),
            ("db1b_market", "DB1B Market", expected_latest_db1b()),
            ("db1b_coupon", "DB1B Coupon", expected_latest_db1b())):
        row = conn.execute(
            "SELECT MAX(year * 100 + period) AS latest, COUNT(*) AS parts "
            "FROM partitions WHERE source = ?", (key,)).fetchone()
        held = row["latest"] if row and row["latest"] else 0
        want = expect[0] * 100 + expect[1]
        # DB1B collection ENDED Q2 2025 — asking for anything later is asking
        # for something that does not exist. OD40/DB1C is a different product
        # with a different schema and needs its own reader.
        if key.startswith("db1b") and want > 202502:
            want, note = 202502, "DB1B ended Q2 2025; later needs an OD40 reader"
        else:
            note = ""
        out.append({
            "source": key, "label": label,
            "held": f"{held // 100}-{held % 100:02d}" if held else "none",
            "expected": f"{want // 100}-{want % 100:02d}",
            "partitions": (row["parts"] if row else 0) or 0,
            "stale": bool(held) and held < want,
            "missing": not held,
            "automatable": not key.startswith("t100"),
            "note": note,
        })
    return out


def fetch_automatable(conn, years, quarters, limit=None, verbose=True) -> list:
    """
    Refresh the sources that have stable URLs. Skips (source, year, quarter)
    partitions already loaded, which is what makes this cheap to run monthly —
    a DB1B quarter is ~370 MB and there is no reason to re-download it.
    """
    done, loaded = warehouse.loaded_partitions(conn), []
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()

    for table, name in ((schema.DB1B_MARKET, "Market"),
                        (schema.DB1B_COUPON, "Coupon")):
        for y in years:
            for q in quarters:
                if (y * 100 + q) > 202502:
                    continue          # past the end of DB1B collection
                if (table.key, y, q) in done:
                    if verbose:
                        print(f"  {table.key} {y}Q{q}: already loaded, skipped")
                    continue
                try:
                    cand, payload, _ = download.resolve(
                        download.db1b_candidates(name, y, q))
                except download.FetchError as exc:
                    print(f"  {table.key} {y}Q{q}: UNAVAILABLE — {exc}")
                    continue
                member, stream = download.open_payload(payload, cand.member_hint)
                rows, prep = readers.read_rows(
                    table, stream, limit=limit,
                    validator=readers.VALIDATORS.get(table.key))
                if not prep.header.ok:
                    print(f"  {table.key} {y}Q{q}: HEADER MISMATCH "
                          f"{prep.header.unmatched_required}")
                    continue
                n = warehouse.replace_partition(
                    conn, table, y, q, rows, warehouse.sha256(payload),
                    cand.channel, cand.url, now)
                loaded.append({"source": table.key, "year": y, "period": q,
                               "rows": n, "truncated": prep.truncated})
                if verbose:
                    print(f"  {table.key} {y}Q{q}: +{n:,} rows"
                          + (" (truncated)" if prep.truncated else ""))

    for table, cands in ((schema.AIRPORT_REF, download.airport_candidates()),
                         (schema.RUNWAY_REF, download.runway_candidates())):
        try:
            cand, payload, _ = download.resolve(cands)
        except download.FetchError as exc:
            print(f"  {table.key}: UNAVAILABLE — {exc}")
            continue
        member, stream = download.open_payload(payload, cand.member_hint)
        rows, prep = readers.read_rows(
            table, stream, validator=readers.VALIDATORS.get(table.key))
        n = warehouse.replace_partition(conn, table, 0, 0, rows,
                                        warehouse.sha256(payload), cand.channel,
                                        cand.url, now)
        loaded.append({"source": table.key, "year": 0, "period": 0, "rows": n})
        if verbose:
            print(f"  {table.key}: {n:,} rows")
    warehouse.backfill_longest_runway(conn)
    return loaded


# ------------------------------------------------------------
# snapshot diffing — so a reviewer sees what moved
# ------------------------------------------------------------

def _read_snapshot(directory: str) -> tuple:
    rp = os.path.join(directory, "routes.csv.gz")
    mp = os.path.join(directory, "MANIFEST.json")
    if not os.path.exists(rp):
        return {}, {}
    with gzip.open(rp, "rt", newline="") as fh:
        routes = {(r["origin"], r["dest"]): r for r in csv.DictReader(fh)}
    manifest = {}
    if os.path.exists(mp):
        with open(mp) as fh:
            manifest = json.load(fh)
    return routes, manifest


def diff_snapshots(old_dir: str, new_dir: str) -> dict:
    old, old_m = _read_snapshot(old_dir)
    new, new_m = _read_snapshot(new_dir)
    if not old:
        return {"first_snapshot": True, "routes": len(new)}

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    moved = []
    for key in set(old) & set(new):
        try:
            a, b = float(old[key]["demand_per_day"]), float(new[key]["demand_per_day"])
        except (KeyError, ValueError):
            continue
        if a > 0 and abs(b - a) / a > 0.10:
            moved.append({"route": f"{key[0]}-{key[1]}",
                          "from": round(a, 1), "to": round(b, 1),
                          "pct": round((b / a - 1) * 100, 1)})
    moved.sort(key=lambda m: -abs(m["pct"]))

    def cov(m, block):
        return (m.get(block) or {}).get("coverage", 0.0)

    return {
        "first_snapshot": False,
        "routes_old": len(old), "routes_new": len(new),
        "added": len(added), "removed": len(removed),
        "added_sample": [f"{o}-{d}" for o, d in added[:10]],
        "removed_sample": [f"{o}-{d}" for o, d in removed[:10]],
        "demand_moved_gt_10pct": len(moved),
        "biggest_moves": moved[:10],
        "vintage_old": old_m.get("years"), "vintage_new": new_m.get("years"),
        "volume_table_old": old_m.get("volume_table"),
        "volume_table_new": new_m.get("volume_table"),
        "fare_coverage_old": cov(old_m, "fares"),
        "fare_coverage_new": cov(new_m, "fares"),
        "connecting_coverage_old": cov(old_m, "connecting"),
        "connecting_coverage_new": cov(new_m, "connecting"),
    }


def regressions(diff: dict) -> list:
    """
    Changes that mean the new snapshot is WORSE than the committed one. A cron
    that blindly commits whatever it produced will happily replace a corpus that
    has capacity and fares with one that has neither, because a partial ingest
    still distills successfully.
    """
    if diff.get("first_snapshot"):
        return []
    bad = []
    if diff["volume_table_old"] == "t100_segment" and \
            diff["volume_table_new"] == "t100_market":
        bad.append("volume table regressed from T-100 Segment to Market — "
                   "capacity, load factor and de-censoring would be lost")
    if diff["routes_new"] < diff["routes_old"] * 0.9:
        bad.append(f"route count fell {diff['routes_old']:,} -> "
                   f"{diff['routes_new']:,} (>10% loss)")
    for label, o, n in (("fare", diff["fare_coverage_old"], diff["fare_coverage_new"]),
                        ("connecting", diff["connecting_coverage_old"],
                         diff["connecting_coverage_new"])):
        if n < o - 0.05:
            bad.append(f"{label} coverage fell {o:.0%} -> {n:.0%}")
    return bad


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="airlinesim refresh",
        description="Refresh the BTS corpus: report staleness, fetch what has "
                    "stable URLs, re-distill, and diff against the committed "
                    "snapshot.")
    p.add_argument("--db", default="warehouse.sqlite")
    p.add_argument("--out", default="airlinesim/data",
                   help="snapshot directory to write")
    p.add_argument("--t100-market", default="",
                   help="path/URL of a T-100 Market export (no stable URL exists)")
    p.add_argument("--t100-segment", default="",
                   help="path/URL of a T-100 Segment export (preferred: has capacity)")
    p.add_argument("--years", default="",
                   help="comma-separated years for DB1B (default: corpus years)")
    p.add_argument("--quarters", default="1,2,3,4")
    p.add_argument("--limit", type=int, default=0,
                   help="cap rows per DB1B member (0 = all; DB1B quarters are huge)")
    p.add_argument("--no-fetch", action="store_true",
                   help="don't touch the network; only re-distill and diff")
    p.add_argument("--check-only", action="store_true",
                   help="report staleness and exit without writing anything")
    p.add_argument("--allow-regression", action="store_true",
                   help="write the snapshot even if it loses data (not advised)")
    p.add_argument("--report", default="", help="write a JSON report here")
    p.add_argument("--summary", default="", help="append markdown here")
    p.add_argument("--pr-body", default="",
                   help="write a pull-request body here (for the refresh workflow)")
    args = p.parse_args(argv)

    conn = warehouse.connect(args.db)
    warehouse.create_all(conn)

    print("=== STALENESS ===")
    st = staleness(conn)
    for s in st:
        verdict = ("MISSING" if s["missing"] else
                   "STALE" if s["stale"] else "current")
        how = "auto" if s["automatable"] else "MANUAL EXPORT"
        print(f"  {s['label']:16s} held {s['held']:8s} expected {s['expected']:8s} "
              f"{verdict:8s} [{how}]" + (f"  {s['note']}" if s["note"] else ""))

    if args.check_only:
        conn.close()
        need = [s for s in st if (s["stale"] or s["missing"]) and not s["automatable"]]
        return 1 if need else 0

    # T-100 first: it is the corpus's spine and only a human can supply it.
    for path, table in ((args.t100_segment, schema.T100_SEGMENT),
                        (args.t100_market, schema.T100_MARKET)):
        if path:
            print(f"\n=== INGEST {table.label} ===\n  <- {path}")
            ingest.ingest_file(conn, table, path,
                               limit=args.limit or None)

    fetched = []
    if not args.no_fetch:
        years = ([int(y) for y in args.years.split(",") if y.strip()]
                 or _corpus_years(conn))
        quarters = [int(q) for q in args.quarters.split(",") if q.strip()]
        print(f"\n=== FETCH AUTOMATABLE SOURCES (years {years}, "
              f"quarters {quarters}) ===")
        fetched = fetch_automatable(conn, years, quarters,
                                    limit=args.limit or None)

    print("\n=== DISTILL ===")
    tmp = args.out + ".new"
    manifest = distill.distill(conn, tmp)

    print("\n=== DIFF vs COMMITTED SNAPSHOT ===")
    diff = diff_snapshots(args.out, tmp)
    if diff.get("first_snapshot"):
        print(f"  first snapshot: {diff['routes']:,} routes")
    else:
        print(f"  routes {diff['routes_old']:,} -> {diff['routes_new']:,} "
              f"(+{diff['added']} / -{diff['removed']})")
        print(f"  vintage {diff['vintage_old']} -> {diff['vintage_new']}, "
              f"volume table {diff['volume_table_old']} -> "
              f"{diff['volume_table_new']}")
        print(f"  fare coverage {diff['fare_coverage_old']:.0%} -> "
              f"{diff['fare_coverage_new']:.0%}; connecting "
              f"{diff['connecting_coverage_old']:.0%} -> "
              f"{diff['connecting_coverage_new']:.0%}")
        print(f"  demand moved >10% on {diff['demand_moved_gt_10pct']:,} routes")
        for m in diff["biggest_moves"][:5]:
            print(f"    {m['route']:9s} {m['from']:>8,.0f} -> {m['to']:>8,.0f} "
                  f"({m['pct']:+.1f}%)")

    bad = regressions(diff)
    written = False
    if bad and not args.allow_regression:
        print("\n=== REFUSING TO WRITE — the new snapshot loses data ===")
        for b in bad:
            print(f"  {b}")
        print(f"  new snapshot left at {tmp} for inspection; pass "
              f"--allow-regression to override")
    else:
        for b in bad:
            print(f"  OVERRIDDEN REGRESSION: {b}")
        os.makedirs(args.out, exist_ok=True)
        for fn in os.listdir(tmp):
            os.replace(os.path.join(tmp, fn), os.path.join(args.out, fn))
        os.rmdir(tmp)
        written = True
        print(f"\n  snapshot written to {args.out}")

    report = {"generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
              "staleness": st, "fetched": fetched, "diff": diff,
              "regressions": bad, "written": written,
              "manifest": manifest}
    if args.report:
        with open(args.report, "w") as fh:
            json.dump(report, fh, indent=2)
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write(to_markdown(report) + "\n")
    if args.pr_body:
        with open(args.pr_body, "w") as fh:
            fh.write(to_markdown(report) + "\n\n---\n"
                     "_Generated by [Claude Code](https://claude.ai/code)_\n")
    conn.close()
    return 0 if (written or not bad) else 1


def _corpus_years(conn) -> list:
    r = conn.execute("SELECT MIN(year) lo, MAX(year) hi FROM partitions "
                     "WHERE source LIKE 't100%'").fetchone()
    if not r or not r["lo"]:
        y = _today().year - 1
        return [y]
    return list(range(r["lo"], r["hi"] + 1))


def to_markdown(report: dict) -> str:
    d = report["diff"]
    out = [f"## BTS corpus refresh — "
           f"{'written' if report['written'] else 'NOT written'}", "",
           "### Staleness", "",
           "| Source | Held | Expected | Verdict | Refresh |", "|---|---|---|---|---|"]
    for s in report["staleness"]:
        verdict = ("**MISSING**" if s["missing"] else
                   "**STALE**" if s["stale"] else "current")
        out.append(f"| {s['label']} | {s['held']} | {s['expected']} | {verdict} | "
                   f"{'auto' if s['automatable'] else '**manual export**'} |")

    manual = [s for s in report["staleness"]
              if (s["stale"] or s["missing"]) and not s["automatable"]]
    if manual:
        out += ["", "### Manual action needed", "",
                "T-100 has no stable URL — it is a TranStats field-picker session "
                "export. To bring the corpus current:", "",
                "1. Export **T-100 Domestic Segment** (preferred — it carries "
                "SEATS/departures) or Market from TranStats",
                "2. `airlinesim refresh --t100-segment <export.zip>`", ""]

    if not d.get("first_snapshot"):
        out += ["### Snapshot diff", "",
                f"- routes {d['routes_old']:,} → {d['routes_new']:,} "
                f"(+{d['added']} / −{d['removed']})",
                f"- vintage {d['vintage_old']} → {d['vintage_new']}",
                f"- volume table `{d['volume_table_old']}` → "
                f"`{d['volume_table_new']}`",
                f"- fare coverage {d['fare_coverage_old']:.0%} → "
                f"{d['fare_coverage_new']:.0%}",
                f"- connecting coverage {d['connecting_coverage_old']:.0%} → "
                f"{d['connecting_coverage_new']:.0%}",
                f"- demand moved >10% on {d['demand_moved_gt_10pct']:,} routes"]
        if d["biggest_moves"]:
            out += ["", "| Route | Was | Now | Change |", "|---|---|---|---|"]
            out += [f"| {m['route']} | {m['from']:,.0f} | {m['to']:,.0f} | "
                    f"{m['pct']:+.1f}% |" for m in d["biggest_moves"][:10]]
    if report["regressions"]:
        out += ["", "### ⚠️ Refused — the new snapshot loses data", ""]
        out += [f"- {b}" for b in report["regressions"]]
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())

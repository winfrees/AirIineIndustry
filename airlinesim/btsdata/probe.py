"""
END-TO-END PROBE — "can we actually get this data, and does it join up?"
======================================================================

This is the Phase-0 verification job (docs/route-data-plan.md), run on a GitHub
Actions runner because runners have the open network egress that development
sandboxes often don't.

It deliberately does MORE than a reachability check. A 200 OK proves nothing
about whether the bytes are usable, so the probe walks the whole chain and
reports at every stage:

  1. ACCESS       which download channel answered, and what the others said
  2. HEADERS      which CSV header supplied each warehouse column; what was
                  unmatched (this is how we discover schema.py's guesses are
                  wrong, which is expected)
  3. PARSE        rows kept vs rejected, with reject reasons counted
  4. LOAD         rows into SQLite through the real warehouse code path
  5. PLAUSIBILITY are the numbers in a sane band, or did we parse cents as
                  dollars / miles into the fare column
  6. INTEGRATION  do the sources actually JOIN — do the busiest T-100 airports
                  have runway data, do the busiest T-100 pairs have fares, does
                  a de-censored demand figure come out coherent

Stage 6 is the one that answers the real question. T-100, DB1B and OurAirports
are three unrelated publications with three different keying conventions, and
the way this project fails is not "the download 404'd" — it's "everything
downloaded fine and the join produced 11% coverage".

Runs against the network, or against committed fixtures with --offline, so the
same logic is exercised in CI with no external dependency.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import argparse
import datetime as _dt
import json
import os
import sys
import tempfile

from airlinesim.btsdata import download, readers, warehouse, schema

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

# Bumped whenever the report's shape or the checks change. Stamped into every
# report and printed in the summary header, because a re-run of an old Actions
# run replays its ORIGINAL commit — twice now, an unchanged summary was read as
# "the fix didn't work" when the fix simply wasn't in the code being run. If the
# version in a summary isn't what you expect, you're looking at stale code.
PROBE_FORMAT_VERSION = 2

# Coverage floors for the integration stage. These are targets for the corpus
# we intend to build (top ~300 airports), not laws of nature — a probe run with
# --limit truncation will legitimately fall short, so truncation downgrades
# these to informational rather than failing the job.
MIN_AIRPORT_COVERAGE = 0.95
MIN_FARE_COVERAGE = 0.80
# Reject-rate ceilings are per table — see SourceTable.max_reject_rate.

# De-censoring assumption, restated here so the probe checks the same rule the
# ingest will use. T-100 passengers are FLOWN, not demanded; see the plan doc.
TARGET_LOAD_FACTOR = 0.85


def code_version() -> dict:
    """
    Identify the code that produced a report: probe format version plus the
    commit, from the Actions environment when present and git otherwise.
    """
    info = {"probe_format": PROBE_FORMAT_VERSION,
            "sha": (os.environ.get("GITHUB_SHA") or "")[:12],
            "ref": os.environ.get("GITHUB_REF_NAME") or "",
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT") or ""}
    if not info["sha"]:
        try:
            import subprocess
            info["sha"] = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"],
                capture_output=True, text=True, timeout=10,
                cwd=os.path.dirname(__file__)).stdout.strip()
        except Exception:  # noqa: BLE001 — identification is best-effort
            pass
    return info


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    informational: bool = False   # reported, but never fails the job

    @property
    def mark(self) -> str:
        if self.ok:
            return "PASS"
        return "WARN" if self.informational else "FAIL"


@dataclass
class SourceResult:
    key: str
    label: str
    channel: str = ""
    url: str = ""
    bytes: int = 0
    member: str = ""
    failed_candidates: list = field(default_factory=list)
    headers: list = field(default_factory=list)
    mapped: dict = field(default_factory=dict)
    unmatched_required: list = field(default_factory=list)
    unmatched_optional: list = field(default_factory=list)
    ignored: list = field(default_factory=list)
    rows_read: int = 0
    rows_kept: int = 0
    rows_loaded: int = 0
    rejects: dict = field(default_factory=dict)
    truncated: bool = False
    error: str = ""


@dataclass
class SourcePlan:
    table: object
    year: int
    period: int
    candidates: list
    fixture: str


# ------------------------------------------------------------
# Fetchers
# ------------------------------------------------------------

class NetworkFetcher:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes

    def get(self, plan: SourcePlan):
        cand, payload, attempts = download.resolve(plan.candidates, self.max_bytes)
        return cand, payload, [(c.url, e) for c, e in attempts]


class FixtureFetcher:
    """Reads committed fixtures so the probe runs with no network at all."""
    def __init__(self, directory: str = FIXTURE_DIR):
        self.dir = directory

    def get(self, plan: SourcePlan):
        path = os.path.join(self.dir, plan.fixture)
        try:
            with open(path, "rb") as fh:
                payload = fh.read()
        except OSError as exc:
            # Surfaced as a normal failed check, not a traceback: a probe that
            # crashes tells you nothing, which defeats the point of the job.
            raise download.FetchError(f"fixture unreadable: {exc}") from exc
        cand = download.Candidate("fixture", f"file://{path}",
                                  member_hint=plan.fixture, verified=True)
        return cand, payload, []


# ------------------------------------------------------------
# Plans
# ------------------------------------------------------------

def build_plans(year: int, month: int, quarter: int, sources) -> list:
    all_plans = {
        "t100": SourcePlan(schema.T100_SEGMENT, year, month,
                           download.t100_candidates(year, month),
                           "t100_segment_sample.csv"),
        "db1b_market": SourcePlan(schema.DB1B_MARKET, year, quarter,
                                  download.db1b_candidates("Market", year, quarter),
                                  "db1b_market_sample.csv"),
        "db1b_coupon": SourcePlan(schema.DB1B_COUPON, year, quarter,
                                  download.db1b_candidates("Coupon", year, quarter),
                                  "db1b_coupon_sample.csv"),
        "airports": SourcePlan(schema.AIRPORT_REF, 0, 0,
                               download.airport_candidates(),
                               "airports_sample.csv"),
        "runways": SourcePlan(schema.RUNWAY_REF, 0, 0,
                              download.runway_candidates(),
                              "runways_sample.csv"),
    }
    return [(k, all_plans[k]) for k in sources if k in all_plans]


# ------------------------------------------------------------
# The probe
# ------------------------------------------------------------

def probe_source(plan: SourcePlan, fetcher, conn, limit) -> tuple:
    """Run stages 1-5 for one source. Returns (SourceResult, [Check])."""
    res = SourceResult(key=plan.table.key, label=plan.table.label)
    checks = []

    try:
        cand, payload, failed = fetcher.get(plan)
    except (download.FetchError, OSError) as exc:
        res.error = str(exc)
        checks.append(Check(f"{res.key}: access", False, str(exc)))
        return res, checks

    res.channel, res.url, res.bytes = cand.channel, cand.url, len(payload)
    res.failed_candidates = [{"url": u, "error": e} for u, e in failed]
    checks.append(Check(f"{res.key}: access", True,
                        f"{res.bytes:,} bytes via {cand.channel} ({cand.url})"))

    # A 200 carrying an HTML page is a REAL failure mode here: TranStats answers
    # some bad requests with an interstitial or error page rather than a status
    # code, and parsed as CSV that yields nonsense headers instead of an
    # obvious fault. Name it explicitly.
    head = payload[:512].lstrip()[:64].lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        checks.append(Check(f"{res.key}: payload is data, not HTML", False,
                            "server returned an HTML page with a success status "
                            "— likely a rejected query or an interstitial"))
        return res, checks

    try:
        member, stream = download.open_payload(payload, cand.member_hint)
        res.member = member
        validator = readers.VALIDATORS.get(plan.table.key)
        rows, prep = readers.read_rows(plan.table, stream, limit=limit,
                                       validator=validator)
    except Exception as exc:  # noqa: BLE001 — diagnosing unknown upstream bytes
        res.error = f"{type(exc).__name__}: {exc}"
        checks.append(Check(f"{res.key}: decode/parse", False, res.error))
        return res, checks
    h = prep.header
    res.headers = list(h.headers)
    res.mapped = dict(h.mapped)
    res.unmatched_required = list(h.unmatched_required)
    res.unmatched_optional = list(h.unmatched_optional)
    res.ignored = list(h.ignored)
    res.rows_read, res.rows_kept = prep.rows_read, prep.rows_kept
    res.rejects = dict(prep.rejects)
    res.truncated = prep.truncated

    checks.append(Check(
        f"{res.key}: required headers", h.ok,
        "all present" if h.ok else
        f"UNMATCHED {h.unmatched_required} — actual headers: {list(h.headers)[:25]}"))
    if not h.ok:
        return res, checks

    checks.append(Check(f"{res.key}: rows parsed", res.rows_kept > 0,
                        f"{res.rows_kept:,} kept of {res.rows_read:,} read"
                        + (" (truncated)" if res.truncated else "")))

    if res.rows_read:
        rate = 1.0 - (res.rows_kept / res.rows_read)
        ceiling = plan.table.max_reject_rate
        checks.append(Check(f"{res.key}: reject rate", rate <= ceiling,
                            f"{rate:.1%} rejected (ceiling {ceiling:.0%}) "
                            f"{res.rejects or ''}"))

    # --- stage 5: plausibility of the numbers we parsed ---
    for col, (low, high) in plan.table.sane_means.items():
        vals = [r[col] for r in rows if r.get(col) is not None]
        if not vals:
            checks.append(Check(f"{res.key}: {col} present", False, "no values parsed"))
            continue
        mean = sum(vals) / len(vals)
        checks.append(Check(f"{res.key}: {col} mean in band",
                            low <= mean <= high,
                            f"mean {mean:,.2f} vs expected [{low:,.0f}, {high:,.0f}]"))

    # --- stage 4: load through the real warehouse path ---
    res.rows_loaded = warehouse.replace_partition(
        conn, plan.table, plan.year, plan.period, rows,
        warehouse.sha256(payload), cand.channel, cand.url,
        _dt.datetime.now(_dt.timezone.utc).isoformat())
    checks.append(Check(f"{res.key}: warehouse load", res.rows_loaded == res.rows_kept,
                        f"{res.rows_loaded:,} rows in partition "
                        f"({plan.year}, {plan.period})"))
    return res, checks


def integration_checks(conn, loaded: dict, requested=None) -> list:
    """
    Stage 6 — do the sources JOIN? This is the question the whole job exists to
    answer, and the one a reachability check can't.
    """
    checks = []
    have = {k for k, v in loaded.items() if v.rows_loaded > 0}
    truncated = any(v.truncated for v in loaded.values())

    if "t100_segment" not in have:
        # Distinguish "T-100 was asked for and failed" (a real failure — nothing
        # joins without it) from "this run deliberately probed a subset", which
        # must not be reported as a broken join.
        asked = requested is None or "t100" in requested
        return [Check("integration", not asked,
                      "no T-100 rows loaded; nothing to join" if asked else
                      "skipped — T-100 not among the requested sources",
                      informational=not asked)]

    # Busiest airports and pairs, as the distiller will compute them.
    top_airports = [r["iata"] for r in conn.execute("""
        SELECT origin AS iata, SUM(passengers) AS pax FROM t100_segment
        GROUP BY origin ORDER BY pax DESC LIMIT 100""").fetchall()]
    top_pairs = [(r["origin"], r["dest"]) for r in conn.execute("""
        SELECT origin, dest, SUM(passengers) AS pax FROM t100_segment
        GROUP BY origin, dest ORDER BY pax DESC LIMIT 200""").fetchall()]

    # --- runway coverage: suitability already enforces min_runway_m, so a pair
    #     whose airports have no runway data cannot be validated at all.
    if "airport_ref" in have:
        warehouse.backfill_longest_runway(conn)
        placeholders = ",".join("?" * len(top_airports))
        n = conn.execute(f"""
            SELECT COUNT(*) AS n FROM airport_ref
            WHERE iata IN ({placeholders}) AND longest_runway_m IS NOT NULL
            """, top_airports).fetchone()["n"] if top_airports else 0
        cov = n / len(top_airports) if top_airports else 0.0
        checks.append(Check("integration: runway coverage of top airports",
                            cov >= MIN_AIRPORT_COVERAGE,
                            f"{cov:.1%} ({n}/{len(top_airports)}) have a runway length",
                            informational=truncated))

    # --- fare coverage: the fare join is the fragile one, because DB1B keys on
    #     the MARKET (journey) while T-100 keys on the SEGMENT (leg).
    if "db1b_market" in have and top_pairs:
        found = 0
        for o, d in top_pairs:
            hit = conn.execute("SELECT 1 FROM db1b_market WHERE origin=? AND dest=? "
                               "LIMIT 1", (o, d)).fetchone()
            found += 1 if hit else 0
        cov = found / len(top_pairs)
        checks.append(Check("integration: fare coverage of top pairs",
                            cov >= MIN_FARE_COVERAGE,
                            f"{cov:.1%} ({found}/{len(top_pairs)}) of busiest T-100 "
                            f"pairs have a DB1B market fare",
                            informational=truncated))

    # --- connecting share, derived the way the plan says (coupon-level) ---
    if "db1b_coupon" in have:
        row = conn.execute("""
            SELECT SUM(CASE WHEN multi > 1 THEN passengers ELSE 0 END) AS conn_pax,
                   SUM(passengers) AS all_pax
            FROM (SELECT c.origin, c.dest, c.passengers,
                         (SELECT COUNT(*) FROM db1b_coupon c2
                          WHERE c2.itin_id = c.itin_id) AS multi
                  FROM db1b_coupon c)""").fetchone()
        all_pax = row["all_pax"] or 0
        share = (row["conn_pax"] or 0) / all_pax if all_pax else -1.0
        checks.append(Check("integration: connecting share computable",
                            0.0 <= share <= 1.0,
                            f"{share:.1%} of sampled coupon pax are on multi-coupon "
                            f"itineraries"))

    # --- de-censoring produces a coherent demand figure ---
    row = conn.execute("""
        SELECT SUM(passengers) AS pax, SUM(seats) AS seats
        FROM t100_segment WHERE seats > 0""").fetchone()
    pax, seats = row["pax"] or 0, row["seats"] or 0
    lf = pax / seats if seats else 0.0
    demand = pax / min(lf, TARGET_LOAD_FACTOR) if lf > 0 else 0.0
    checks.append(Check("integration: load factor sane", 0.0 < lf <= 1.05,
                        f"aggregate LF {lf:.1%}"))
    checks.append(Check("integration: de-censored demand >= flown pax",
                        demand >= pax > 0,
                        f"{pax:,.0f} flown -> {demand:,.0f} implied demand "
                        f"(target LF {TARGET_LOAD_FACTOR:.0%})"))
    return checks


def run(args) -> dict:
    plans = build_plans(args.year, args.month, args.quarter, args.sources)
    fetcher = FixtureFetcher(args.fixture_dir) if args.offline else NetworkFetcher(
        args.max_mb * 1024 * 1024)

    db_path = args.db or os.path.join(tempfile.mkdtemp(prefix="btsprobe-"), "probe.sqlite")
    conn = warehouse.connect(db_path)
    warehouse.create_all(conn)

    results, checks = {}, []
    discovery = None
    for name, plan in plans:
        res, ck = probe_source(plan, fetcher, conn, args.limit)

        # T-100 is not optional: it is the only source of SEATS and departures,
        # so without it there is no load factor, no de-censoring, no seat window
        # and no monthly seasonality. If its URL guesses failed, go and FIND the
        # real one rather than reporting a dead end — and if the sweep turns up a
        # working URL, use it immediately so one run both discovers and validates.
        if name == "t100" and res.error and not (args.offline or args.discover):
            # Never let a T-100 failure pass without saying whether the hunt for
            # a working channel was even attempted.
            checks.append(Check("t100_segment: channel discovery", False,
                                "SKIPPED — re-run without --no-discover to sweep "
                                "PREZIP names, scrape TranStats for real filenames "
                                "and resolve the ArcGIS mirror",
                                informational=True))

        if name == "t100" and res.error and not args.offline and args.discover:
            from airlinesim.btsdata import discover
            discovery = discover.discover_t100(args.year, args.month)
            checks.append(Check("t100_segment: channel discovery",
                                bool(discovery.hits),
                                "; ".join(discovery.notes) or "no candidates answered",
                                informational=not discovery.hits))
            if discovery.hits:
                found = discovery.hits[0]
                plan = SourcePlan(plan.table, plan.year, plan.period,
                                  [download.Candidate("prezip-discovered", found.url,
                                                      note="found by name sweep")],
                                  plan.fixture)
                res, ck = probe_source(plan, fetcher, conn, args.limit)
                checks.append(Check("t100_segment: retry with discovered URL",
                                    not res.error, found.url))

        results[plan.table.key] = res
        checks.extend(ck)

    checks.extend(integration_checks(conn, results, requested=args.sources))

    failures = [c for c in checks if not c.ok and not c.informational]
    report = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "code_version": code_version(),
        "mode": "offline-fixtures" if args.offline else "network",
        "period": {"year": args.year, "month": args.month, "quarter": args.quarter},
        "row_limit": args.limit,
        "database": db_path,
        "table_counts": warehouse.table_counts(conn),
        "sources": {k: asdict(v) for k, v in results.items()},
        "checks": [asdict(c) | {"mark": c.mark} for c in checks],
        "discovery": asdict(discovery) if discovery else None,
        "ok": not failures,
    }
    conn.close()
    return report


# ------------------------------------------------------------
# Reporting
# ------------------------------------------------------------

def to_markdown(report: dict) -> str:
    cv = report.get("code_version") or {}
    stamp = (f"probe format **v{cv.get('probe_format', '?')}** · commit "
             f"`{cv.get('sha') or 'unknown'}`"
             + (f" · ref `{cv['ref']}`" if cv.get("ref") else "")
             + (f" · attempt {cv['run_attempt']}" if cv.get("run_attempt") else ""))
    out = [f"## BTS data probe — {'PASS' if report['ok'] else 'FAIL'}", "",
           stamp, "",
           f"Mode **{report['mode']}** · period "
           f"{report['period']['year']}-{report['period']['month']:02d} "
           f"(Q{report['period']['quarter']}) · row limit "
           f"{report['row_limit'] or 'none'}", "",
           "### Checks", "", "| | Check | Detail |", "|---|---|---|"]
    icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
    for c in report["checks"]:
        # Generous cap: the first live run truncated the T-100 candidate errors
        # at 300 chars and cut off exactly the part worth reading.
        detail = str(c["detail"]).replace("|", "\\|")[:1200]
        out.append(f"| {icon[c['mark']]} | {c['name']} | {detail} |")

    out += ["", "### Sources", "",
            "| Source | Channel | Bytes | Rows kept | Unmatched required |",
            "|---|---|---|---|---|"]
    for key, s in report["sources"].items():
        out.append(f"| {key} | {s['channel'] or '—'} | {s['bytes']:,} | "
                   f"{s['rows_kept']:,} | {s['unmatched_required'] or '—'} |")

    # The actionable part when a guess in schema.py was wrong.
    bad = {k: s for k, s in report["sources"].items() if s["unmatched_required"]}
    if bad:
        out += ["", "### Header mismatches — update `schema.py` aliases", ""]
        for k, s in bad.items():
            out += [f"**{k}** missing `{s['unmatched_required']}`", "",
                    "Actual headers:", "", "```",
                    ", ".join(s["headers"][:60]) or "(none)", "```", ""]

    d = report.get("discovery")
    if d:
        out += ["", "### T-100 channel discovery", ""]
        for note in d["notes"]:
            out.append(f"- {note}")
        hits = d["hits"]
        out += ["", f"**PREZIP name sweep — {len(hits)} hit(s):**", ""]
        if hits:
            out += ["```"] + [f"{h['url']}  ({h['length']:,} bytes, "
                              f"{h['content_type']})" for h in hits] + ["```"]
        else:
            out += ["_No generated name answered 200._ Statuses returned:", ""]
            tally = {}
            for r in d["swept"]:
                key = r["status"] or r["error"][:40]
                tally[key] = tally.get(key, 0) + 1
            out.append(", ".join(f"`{k}` ×{v}" for k, v in sorted(
                tally.items(), key=lambda kv: str(kv[0]))))

        # Page scrape: real filenames and form fields beat any guess of mine.
        pages = [p for p in d["pages"] if p["prezip_mentions"] or p["t100_options"]
                 or p["zip_links"]]
        if pages:
            out += ["", "**Referenced on TranStats pages:**", ""]
            for p in pages:
                out.append(f"- `{p['url']}` (HTTP {p['status']}) — "
                           f"{p['title'] or 'untitled'}")
                for m in p["prezip_mentions"][:12]:
                    out.append(f"    - PREZIP: `{m}`")
                for o in p["t100_options"][:12]:
                    out.append(f"    - option: `{o}`")
                for z in p["zip_links"][:8]:
                    out.append(f"    - zip href: `{z}`")

        fields = [f for p in d["pages"] for f in p["form_fields"]
                  if "DL_SelectFields" in p["url"]]
        if fields:
            out += ["", "**Download-form fields (for a correct DownLoad_Table POST):**",
                    "", "```", ", ".join(fields[:60]), "```"]

        arc = d["arcgis"]
        if arc:
            out += ["", "**ArcGIS mirror** (documented REST API; curated subset):", ""]
            if arc.get("error"):
                out.append(f"- error: {arc['error']}")
            for it in arc.get("items", [])[:6]:
                out.append(f"- {it['title']} ({it['type']}) `{it['url'] or '—'}`")
            if arc.get("query_url"):
                out += ["", f"query endpoint: `{arc['query_url']}`", "",
                        "fields: " + ", ".join(f"`{f}`" for f in arc.get("fields", [])[:40])]

    working = {k: (s["channel"], s["url"]) for k, s in report["sources"].items()
               if s["channel"] and s["channel"] != "fixture"}
    if working:
        out += ["", "### Confirmed channels — mark `verified=True` in `download.py`", "",
                "```"]
        out += [f"{k}: {ch} {url}" for k, (ch, url) in working.items()]
        out += ["```"]
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="airlinesim-bts-probe",
        description="Verify BTS sources are downloadable, parseable and joinable.")
    p.add_argument("--sources", default="t100,db1b_market,db1b_coupon,airports,runways",
                   help="comma-separated: t100,db1b_market,db1b_coupon,airports,runways")
    p.add_argument("--year", type=int, default=2024)
    p.add_argument("--month", type=int, default=6)
    p.add_argument("--quarter", type=int, default=2)
    p.add_argument("--limit", type=int, default=200_000,
                   help="max rows parsed per source (0 = no limit)")
    p.add_argument("--max-mb", type=int, default=400, help="per-download size cap")
    p.add_argument("--db", default="", help="sqlite path (default: temp dir)")
    p.add_argument("--offline", action="store_true",
                   help="use committed fixtures instead of the network")
    p.add_argument("--no-discover", dest="discover", action="store_false",
                   help="don't sweep/scrape for a working T-100 channel on failure")
    p.add_argument("--discover-only", action="store_true",
                   help="run T-100 channel discovery and report, nothing else")
    p.add_argument("--fixture-dir", default=FIXTURE_DIR)
    p.add_argument("--report", default="", help="write JSON report here")
    p.add_argument("--summary", default="", help="append markdown here "
                                                "(e.g. $GITHUB_STEP_SUMMARY)")
    args = p.parse_args(argv)
    args.sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    args.limit = args.limit or None

    if args.discover_only:
        from airlinesim.btsdata import discover
        d = discover.discover_t100(args.year, args.month)
        report = {"generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                  "mode": "discover-only",
                  "period": {"year": args.year, "month": args.month,
                             "quarter": args.quarter},
                  "row_limit": args.limit, "database": "", "table_counts": {},
                  "sources": {}, "checks": [], "discovery": asdict(d),
                  "ok": bool(d.hits)}
    else:
        report = run(args)
    md = to_markdown(report)
    print(md)

    if args.report:
        with open(args.report, "w") as fh:
            json.dump(report, fh, indent=2)
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write(md + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

"""
SOURCE ACCESS — the only network-touching module in the project.
==============================================================

BTS publishes no REST/JSON API for T-100 or the O&D Survey, so there are three
practical channels and none of them is contractually stable:

  1. /PREZIP/ direct zips      undocumented but what most public tooling uses
  2. TranStats field-picker    POST to DL_SelectFields.aspx; parameter names
                               have changed historically
  3. ArcGIS mirror             real REST API, but a curated SUBSET of the table

So each source declares an ORDERED LIST of candidates, cheapest and most
specific first, and `resolve()` reports which one actually answered. That makes
the access layer self-diagnosing: when BTS changes something, the probe tells us
which channel died rather than the ingest failing opaquely.

None of these URLs could be verified from the environment this was written in
(bts.gov is blocked by its network policy), which is exactly what probe.py run
on a GitHub Actions runner is for. Every candidate is annotated with whether it
has been confirmed live.
"""
from __future__ import annotations
from dataclasses import dataclass
import io
import time
import urllib.error
import urllib.request
import zipfile

USER_AGENT = ("Mozilla/5.0 (compatible; airlinesim-ingest/0.1; "
              "+https://github.com/winfrees/AirIineIndustry)")

# Cap on a single download. T-100 for one month is small; a DB1B Coupon quarter
# is large enough to be worth an explicit opt-in rather than silently pulling
# hundreds of MB onto a runner.
DEFAULT_MAX_BYTES = 400 * 1024 * 1024


@dataclass(frozen=True)
class Candidate:
    """One way to obtain a source table for one period."""
    channel: str                  # 'prezip' | 'form' | 'arcgis' | 'github'
    url: str
    method: str = "GET"
    data: bytes | None = None     # form POST body
    member_hint: str = ".csv"     # which file inside a zip we want
    verified: bool = False        # has this been confirmed live by a probe run?
    note: str = ""


# ------------------------------------------------------------
# Candidate builders, per source table
# ------------------------------------------------------------

def t100_candidates(year: int, month: int) -> list:
    """
    T-100 Domestic Segment for one (year, month).

    ALL THREE PREZIP GUESSES BELOW RETURNED 404 on 2026-07-25. They are kept so
    the sweep doesn't re-report them as unknowns, but the real channel is found
    by discover.py, which probe.py invokes automatically on failure and then
    retries with — see docs/route-data-plan.md. T-100 is not droppable: it is
    the only source of SEATS and departures, hence of load factor, de-censored
    demand, the economic seat window, and monthly seasonality.
    """
    return [
        # Confirmed 404 (2026-07-25). PREZIP evidently doesn't use the internal
        # RawDataTable name for this table, if it carries it at all.
        Candidate("prezip", f"https://transtats.bts.gov/PREZIP/T_T100D_SEGMENT_ALL_CARRIER_{year}.zip",
                  note="404 on 2026-07-25 — per-year, all carriers"),
        Candidate("prezip", "https://transtats.bts.gov/PREZIP/T_T100D_SEGMENT_ALL_CARRIER.zip",
                  note="404 on 2026-07-25 — all years, all carriers"),
        Candidate("prezip", f"https://transtats.bts.gov/PREZIP/T_T100D_SEGMENT_US_CARRIER_ONLY_{year}.zip",
                  note="404 on 2026-07-25 — per-year, US carriers only"),
        # The field-picker form. Table_ID 311 is T-100 Segment (All Carriers) in
        # published references; the body below is a best-effort reconstruction.
        Candidate("form", "https://www.transtats.bts.gov/DownLoad_Table.asp?Table_ID=311",
                  method="POST",
                  data=(f"UserTableName=T_100_Domestic_Segment&DBShortName=Air_Carriers"
                        f"&RawDataTable=T_T100D_SEGMENT_ALL_CARRIER&sqlstr="
                        f"&varlist=YEAR,MONTH,UNIQUE_CARRIER,ORIGIN,DEST,AIRCRAFT_TYPE,"
                        f"PASSENGERS,SEATS,DEPARTURES_PERFORMED,DISTANCE"
                        f"&grouplist=&suml=&sumRegion=&filter1=title%3D&filter2=title%3D"
                        f"&geo=All%3E&time={month}&timeName=Month&GEOGRAPHY=All"
                        f"&XYEAR={year}&FREQUENCY={month}&VarDesc=&VarType=Num"
                        ).encode(),
                  note="TranStats field-picker; parameter names drift"),
    ]


def db1b_candidates(table: str, year: int, quarter: int) -> list:
    """DB1B Market or Coupon for one (year, quarter). `table` in {Market, Coupon}."""
    return [
        Candidate("prezip",
                  f"https://transtats.bts.gov/PREZIP/Origin_and_Destination_Survey_DB1B{table}_{year}_{quarter}.zip",
                  verified=True,
                  note="CONFIRMED LIVE 2026-07-25 for 2024 Q2: Market 110 MB, "
                       "Coupon 258 MB, all required headers matched, mean market "
                       "fare $323.77. This is also the naming clue for T-100 — "
                       "PREZIP uses the DOWNLOAD UI's table name plus period, "
                       "not the internal RawDataTable name."),
    ]


def airport_candidates() -> list:
    """OurAirports reference data — GitHub-hosted, public domain."""
    return [
        Candidate("github",
                  "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv",
                  member_hint="airports.csv", verified=True,
                  note="confirmed reachable 2026-07-25 (HTTP 200)"),
    ]


def runway_candidates() -> list:
    return [
        Candidate("github",
                  "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/runways.csv",
                  member_hint="runways.csv", verified=True,
                  note="confirmed reachable 2026-07-25 (HTTP 200)"),
    ]


# ------------------------------------------------------------
# Fetching
# ------------------------------------------------------------

class FetchError(RuntimeError):
    pass


def fetch(cand: Candidate, max_bytes: int = DEFAULT_MAX_BYTES,
          retries: int = 3, timeout: float = 120.0) -> bytes:
    """
    Fetch one candidate with exponential backoff. Raises FetchError with a
    diagnosable message rather than letting urllib's exception escape, because
    the probe reports these strings verbatim.
    """
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(cand.url, data=cand.data, method=cand.method,
                                     headers={"User-Agent": USER_AGENT})
        if cand.data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise FetchError(f"declared {int(declared):,} bytes exceeds cap "
                                     f"{max_bytes:,} (re-run with a larger --max-mb)")
                buf = io.BytesIO()
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    buf.write(chunk)
                    if buf.tell() > max_bytes:
                        raise FetchError(f"stream exceeded cap {max_bytes:,} bytes")
                return buf.getvalue()
        except FetchError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise FetchError(last or "unknown failure")


def resolve(candidates, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple:
    """
    Try candidates in order. Returns (candidate, payload_bytes, attempts) where
    `attempts` is a list of (candidate, error_string) for everything that failed
    first — the probe reports these so a dead channel is visible, not silent.
    """
    attempts = []
    for cand in candidates:
        try:
            return cand, fetch(cand, max_bytes=max_bytes), attempts
        except FetchError as exc:
            attempts.append((cand, str(exc)))
    raise FetchError("all candidates failed: " +
                     "; ".join(f"{c.channel} {c.url} -> {e}" for c, e in attempts))


def open_payload(payload: bytes, member_hint: str = ".csv"):
    """
    Return (name, text_stream) for a payload that may be a zip or a bare CSV.

    BTS zips contain one CSV plus a readme; we pick the largest member whose
    name matches the hint so a bundled readme.html can't be mistaken for data.
    """
    if payload[:2] == b"PK":
        zf = zipfile.ZipFile(io.BytesIO(payload))
        members = [m for m in zf.infolist() if not m.is_dir()]
        matching = [m for m in members
                    if member_hint.lower() in m.filename.lower()] or members
        best = max(matching, key=lambda m: m.file_size)
        raw = zf.read(best)
        return best.filename, io.StringIO(raw.decode("latin-1"))
    return member_hint, io.StringIO(payload.decode("latin-1"))

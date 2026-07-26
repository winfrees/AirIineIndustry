"""
CSV -> NORMALIZED ROWS. Pure functions over file objects; no network.
====================================================================

Deliberately separated from download.py so the whole parse/normalize path is
testable offline against committed fixtures — including in environments where
bts.gov is unreachable. This is what lets Phases 1-2 of the plan be developed
and verified without ever hitting BTS.

Header resolution is tolerant (see schema.Column.match) and REPORTED: read_rows
returns a HeaderReport alongside the rows so the caller can show exactly which
CSV header supplied each warehouse column, which required columns went
unmatched, and what was ignored. That report is the probe's main output.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import csv


@dataclass
class HeaderReport:
    table: str
    headers: tuple = ()                       # actual CSV headers as found
    mapped: dict = field(default_factory=dict)   # warehouse col -> CSV header
    unmatched_required: list = field(default_factory=list)
    unmatched_optional: list = field(default_factory=list)
    ignored: list = field(default_factory=list)  # CSV headers we don't use

    @property
    def ok(self) -> bool:
        return not self.unmatched_required


@dataclass
class ParseReport:
    header: HeaderReport
    rows_read: int = 0
    rows_kept: int = 0
    rejects: dict = field(default_factory=dict)  # reason -> count
    truncated: bool = False

    def reject(self, reason: str):
        self.rejects[reason] = self.rejects.get(reason, 0) + 1


def _coerce(raw: str, kind: str):
    """Coerce one CSV cell. Blank -> None so we never invent a zero."""
    if raw is None:
        return None
    s = raw.strip().strip('"')
    if s == "":
        return None
    if kind == "int":
        try:
            return int(float(s))
        except ValueError:
            return None
    if kind == "float":
        try:
            return float(s)
        except ValueError:
            return None
    return s


def resolve_headers(table, headers) -> HeaderReport:
    rep = HeaderReport(table=table.key, headers=tuple(headers))
    used = set()
    for col in table.columns:
        found = col.match(headers)
        if found is None:
            (rep.unmatched_required if col.required
             else rep.unmatched_optional).append(col.name)
        else:
            rep.mapped[col.name] = found
            used.add(found)
    rep.ignored = [h for h in headers if h not in used]
    return rep


def iter_rows(table, stream, limit: int | None = None, validator=None):
    """
    Stream `stream` as CSV, yielding normalized dicts keyed by warehouse column
    name. Yields (report, row) pairs is avoided deliberately: instead the caller
    passes in a ParseReport-carrying generator protocol — see read_rows for the
    buffered variant and ingest.py for the streaming one.

    Generator form exists because a full-year T-100 export is ~250k rows and a
    DB1B coupon quarter is tens of millions; materializing those as a list of
    dicts is gratuitous memory pressure.

    Usage:
        gen = iter_rows(table, stream, validator=v)
        prep = next(gen)          # the ParseReport, yielded FIRST
        for row in gen: ...       # rows; prep updates as you consume
    """
    reader = csv.reader(stream)
    try:
        headers = next(reader)
    except StopIteration:
        yield ParseReport(header=HeaderReport(table=table.key))
        return

    hrep = resolve_headers(table, headers)
    prep = ParseReport(header=hrep)
    yield prep
    if not hrep.ok:
        # Without the required columns there is nothing meaningful to parse; the
        # caller has the header diagnosis from the report already yielded.
        return

    index = {col.name: headers.index(hrep.mapped[col.name])
             for col in table.columns if col.name in hrep.mapped}
    kinds = {col.name: col.kind for col in table.columns}
    required = [c.name for c in table.required_columns]

    for raw in reader:
        prep.rows_read += 1
        if limit is not None and prep.rows_kept >= limit:
            prep.truncated = True
            return
        if len(raw) < len(headers):
            prep.reject("short row")
            continue
        row = {name: _coerce(raw[i], kinds[name]) for name, i in index.items()}
        missing = [n for n in required if row.get(n) is None]
        if missing:
            prep.reject(f"null required: {','.join(sorted(missing))}")
            continue
        if validator is not None:
            why = validator(row)
            if why:
                prep.reject(why)
                continue
        prep.rows_kept += 1
        yield row


def read_rows(table, stream, limit: int | None = None, validator=None):
    """
    Buffered form of iter_rows: returns (rows, ParseReport). Convenient for the
    probe, which samples a bounded slice; use iter_rows for a full ingest.
    """
    gen = iter_rows(table, stream, limit=limit, validator=validator)
    prep = next(gen)
    return list(gen), prep


# ------------------------------------------------------------
# Per-table validators — cheap sanity, not economics
# ------------------------------------------------------------

def _is_station(code) -> bool:
    return isinstance(code, str) and len(code) == 3 and code.isalpha()


def validate_t100(row) -> str | None:
    if not _is_station(row.get("origin")) or not _is_station(row.get("dest")):
        return "non-station origin/dest"
    if row["origin"] == row["dest"]:
        return "origin == dest"
    if (row.get("passengers") or 0) < 0 or (row.get("seats") or 0) < 0:
        return "negative pax/seats"
    # Pax above seats on a segment is a reporting artifact, not a full flight.
    seats = row.get("seats") or 0
    if seats > 0 and (row.get("passengers") or 0) > seats * 1.05:
        return "passengers exceed seats"
    if (row.get("distance_mi") or 0) <= 0:
        return "non-positive distance"
    return None


def validate_t100_market(row) -> str | None:
    """
    Like validate_t100 but WITHOUT the seats checks — T-100 Market has no SEATS
    column, so there is nothing to compare passengers against.
    """
    if not _is_station(row.get("origin")) or not _is_station(row.get("dest")):
        return "non-station origin/dest"
    if row["origin"] == row["dest"]:
        return "origin == dest"
    if (row.get("passengers") or 0) < 0:
        return "negative pax"
    if (row.get("distance_mi") or 0) <= 0:
        return "non-positive distance"
    return None


def validate_db1b_market(row) -> str | None:
    if not _is_station(row.get("origin")) or not _is_station(row.get("dest")):
        return "non-station origin/dest"
    fare = row.get("market_fare")
    if fare is None or fare < 0:
        return "missing/negative fare"
    # DB1B carries a small number of $0 and absurd-value tickets (bulk fares,
    # employee travel, data errors). Excluding them is standard practice and is
    # an approximation we should state, not hide.
    if fare == 0 or fare > 20000:
        return "implausible fare (bulk/employee/error)"
    return None


def validate_db1b_coupon(row) -> str | None:
    if not _is_station(row.get("origin")) or not _is_station(row.get("dest")):
        return "non-station origin/dest"
    return None


def validate_airport(row) -> str | None:
    # We only care about airports with an IATA code; heliports and closed
    # fields are dropped because no route can be authored against them.
    if not row.get("iata"):
        return "no IATA code"
    kind = (row.get("airport_type") or "").lower()
    if kind and "airport" not in kind:
        return f"not an airport ({kind})"
    return None


def validate_runway(row) -> str | None:
    if (row.get("closed") or "0").strip() in ("1", "yes", "true"):
        return "closed runway"
    if not row.get("length_ft"):
        return "no length"
    return None


VALIDATORS = {
    "t100_segment": validate_t100,
    "t100_market": validate_t100_market,
    "db1b_market": validate_db1b_market,
    "db1b_coupon": validate_db1b_coupon,
    "airport_ref": validate_airport,
    "runway_ref": validate_runway,
}

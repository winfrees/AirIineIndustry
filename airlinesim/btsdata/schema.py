"""
SOURCE TABLE SHAPES — what each BTS/reference table looks like.
==============================================================

Declares, per source table: the columns we depend on, the CSV header spellings
we'll accept for each, how to coerce them, and the warehouse DDL.

WHY ALIASES: BTS serves the same logical table through several channels
(TranStats' field-picker form, the undocumented /PREZIP/ zips, an ArcGIS
mirror) and the header spellings differ between them and have changed over
time — ORIGIN vs Origin, DEPARTURES_PERFORMED vs Departures, MARKET_FARE vs
MktFare. Rather than hardcode one guess, every column carries a set of accepted
spellings, matched case- and separator-insensitively.

IMPORTANT — these alias sets are INFORMED GUESSES, not verified against a live
download. bts.gov is unreachable from the sandbox this was written in, so the
whole point of probe.py is to report the ACTUAL headers and tell us which of
these expectations were wrong. Treat a probe "unmatched required column" result
as the source of truth and fix the aliases here, not as a bug in the probe.
"""
from __future__ import annotations
from dataclasses import dataclass, field


def _norm(header: str) -> str:
    """Fold a CSV header for tolerant matching: lowercase, alphanumeric only."""
    return "".join(ch for ch in header.lower() if ch.isalnum())


@dataclass(frozen=True)
class Column:
    """One warehouse column and the CSV headers that may carry it."""
    name: str                      # warehouse column name
    aliases: tuple                 # accepted CSV header spellings
    kind: str = "str"              # 'int' | 'float' | 'str'
    required: bool = True

    def match(self, headers) -> str | None:
        """Return the actual header in `headers` that supplies this column."""
        wanted = {_norm(a) for a in (self.name,) + self.aliases}
        for h in headers:
            if _norm(h) in wanted:
                return h
        return None


@dataclass(frozen=True)
class SourceTable:
    key: str                       # warehouse table name
    label: str                     # human description
    columns: tuple
    ddl: str
    # a plausibility band per numeric column, checked by the probe:
    #   name -> (low, high) for the column's MEAN over sampled rows
    sane_means: dict = field(default_factory=dict)
    # Tolerated share of rows dropped by the validator. This is PER TABLE
    # because a high reject rate means opposite things for different sources:
    # on a BTS traffic table it signals a parse problem, but on a reference
    # table like OurAirports — which covers every airfield on earth, including
    # tens of thousands of IATA-less private strips — filtering most rows out is
    # the entire point. A single global threshold flagged that as a failure and
    # would have masked real problems.
    max_reject_rate: float = 0.10

    @property
    def required_columns(self) -> tuple:
        return tuple(c for c in self.columns if c.required)


# ============================================================
# T-100 DOMESTIC SEGMENT — the volume/capacity core
# ============================================================
# One row per carrier x origin x dest x aircraft type x service class x month.
# Reported under 14 CFR 291.45. Monthly, free, 1990-present.

T100_SEGMENT = SourceTable(
    key="t100_segment",
    label="T-100 Domestic Segment (Data Bank 28DS)",
    columns=(
        Column("year", ("YEAR",), "int"),
        Column("month", ("MONTH",), "int"),
        Column("carrier", ("UNIQUE_CARRIER", "CARRIER", "OP_UNIQUE_CARRIER")),
        Column("origin", ("ORIGIN",)),
        Column("dest", ("DEST", "DESTINATION")),
        Column("aircraft_type", ("AIRCRAFT_TYPE",), "str", required=False),
        Column("service_class", ("CLASS", "SERVICE_CLASS"), "str", required=False),
        Column("passengers", ("PASSENGERS",), "float"),
        Column("seats", ("SEATS",), "float"),
        Column("departures_performed", ("DEPARTURES_PERFORMED",), "float"),
        Column("departures_scheduled", ("DEPARTURES_SCHEDULED",), "float", required=False),
        Column("distance_mi", ("DISTANCE",), "float"),
        Column("ramp_to_ramp_hrs", ("RAMP_TO_RAMP",), "float", required=False),
        Column("air_time_hrs", ("AIR_TIME",), "float", required=False),
    ),
    ddl="""
        CREATE TABLE IF NOT EXISTS t100_segment (
            year INTEGER, month INTEGER, carrier TEXT,
            origin TEXT, dest TEXT, aircraft_type TEXT, service_class TEXT,
            passengers REAL, seats REAL,
            departures_performed REAL, departures_scheduled REAL,
            distance_mi REAL, ramp_to_ramp_hrs REAL, air_time_hrs REAL
        )""",
    # A domestic segment row averages a few thousand pax/month across a mix of
    # mainline and commuter operations; distances span ~50-3000 statute miles.
    sane_means={"distance_mi": (50.0, 3000.0)},
)


# ============================================================
# DB1B MARKET — fares
# ============================================================
# 10% ticket sample, quarterly. Directional O&D with a MILE-PRORATED fare.
# NOTE: collection ended July 2025; superseded by OD40/DB1C. This reader
# targets the frozen 1993-2025Q2 archive.

DB1B_MARKET = SourceTable(
    key="db1b_market",
    label="DB1B Market (O&D Survey, 10% ticket sample)",
    columns=(
        Column("year", ("YEAR",), "int"),
        Column("quarter", ("QUARTER",), "int"),
        Column("origin", ("ORIGIN",)),
        Column("dest", ("DEST", "DESTINATION")),
        Column("ticket_carrier", ("TICKET_CARRIER", "TKCARRIER", "RPCARRIER",
                                  "REPORTING_CARRIER"), "str", required=False),
        Column("passengers", ("PASSENGERS",), "float"),
        Column("market_fare", ("MARKET_FARE", "MKTFARE"), "float"),
        Column("market_miles", ("MARKET_MILES_FLOWN", "MKTMILESFLOWN",
                                "NONSTOP_MILES"), "float", required=False),
        Column("market_coupons", ("MARKET_COUPONS", "MKTCOUPONS"), "float", required=False),
    ),
    ddl="""
        CREATE TABLE IF NOT EXISTS db1b_market (
            year INTEGER, quarter INTEGER, origin TEXT, dest TEXT,
            ticket_carrier TEXT, passengers REAL,
            market_fare REAL, market_miles REAL, market_coupons REAL
        )""",
    # One-way prorated domestic market fares. Wide band on purpose: this is a
    # smell test for "did we parse dollars or cents / miles into the fare
    # column", not an economic assertion.
    sane_means={"market_fare": (40.0, 1500.0)},
)


# ============================================================
# DB1B COUPON — connecting share, per SEGMENT
# ============================================================
# A coupon IS a segment, which is why connecting share must come from here and
# not from the Market table: Market only knows the whole journey and cannot
# attribute a connection to a specific leg. Segment connecting share =
# share of that segment's coupons belonging to a multi-coupon itinerary.

DB1B_COUPON = SourceTable(
    key="db1b_coupon",
    label="DB1B Coupon (O&D Survey, segment-level)",
    columns=(
        Column("year", ("YEAR",), "int"),
        Column("quarter", ("QUARTER",), "int"),
        Column("itin_id", ("ITIN_ID", "ITINID")),
        Column("seq_num", ("SEQ_NUM", "SEQNUM"), "int", required=False),
        Column("origin", ("ORIGIN",)),
        Column("dest", ("DEST", "DESTINATION")),
        Column("op_carrier", ("OPERATING_CARRIER", "OPCARRIER"), "str", required=False),
        Column("passengers", ("PASSENGERS",), "float"),
        Column("distance", ("DISTANCE",), "float", required=False),
        Column("trip_break", ("TRIP_BREAK", "TRIPBREAK"), "str", required=False),
    ),
    ddl="""
        CREATE TABLE IF NOT EXISTS db1b_coupon (
            year INTEGER, quarter INTEGER, itin_id TEXT, seq_num INTEGER,
            origin TEXT, dest TEXT, op_carrier TEXT,
            passengers REAL, distance REAL, trip_break TEXT
        )""",
)


# ============================================================
# OURAIRPORTS — airport reference (runway lengths, coordinates)
# ============================================================
# Public domain, GitHub-hosted, and reachable where bts.gov often isn't.
# Supplies AirportSpec.runway_length_m, which route suitability already
# enforces. `total_gates` and `fuel_supply_per_day_l` have NO public source
# and stay heuristics — see the known-limitations list.

AIRPORT_REF = SourceTable(
    key="airport_ref",
    label="OurAirports airports.csv",
    columns=(
        Column("ident", ("ident",)),
        Column("iata", ("iata_code",), "str", required=False),
        Column("name", ("name",), "str", required=False),
        Column("lat", ("latitude_deg",), "float", required=False),
        Column("lon", ("longitude_deg",), "float", required=False),
        Column("elevation_ft", ("elevation_ft",), "float", required=False),
        Column("iso_country", ("iso_country",), "str", required=False),
        Column("airport_type", ("type",), "str", required=False),
    ),
    ddl="""
        CREATE TABLE IF NOT EXISTS airport_ref (
            ident TEXT, iata TEXT, name TEXT, lat REAL, lon REAL,
            elevation_ft REAL, iso_country TEXT, airport_type TEXT,
            longest_runway_m REAL
        )""",
    sane_means={"lat": (-90.0, 90.0), "lon": (-180.0, 180.0)},
    # Live run: 76,752 of 85,807 rows have no IATA code and a further ~250 are
    # heliports/seaplane bases. Dropping ~90% is correct behavior, not a fault —
    # what matters for this table is the runway coverage of the airports we
    # actually author routes against, which the probe checks separately.
    max_reject_rate=0.95,
)

RUNWAY_REF = SourceTable(
    key="runway_ref",
    label="OurAirports runways.csv",
    columns=(
        Column("airport_ident", ("airport_ident",)),
        Column("length_ft", ("length_ft",), "float", required=False),
        Column("surface", ("surface",), "str", required=False),
        Column("closed", ("closed",), "str", required=False),
    ),
    ddl="""
        CREATE TABLE IF NOT EXISTS runway_ref (
            airport_ident TEXT, length_ft REAL, surface TEXT, closed TEXT
        )""",
)


ALL_TABLES = (T100_SEGMENT, DB1B_MARKET, DB1B_COUPON, AIRPORT_REF, RUNWAY_REF)
TABLES_BY_KEY = {t.key: t for t in ALL_TABLES}

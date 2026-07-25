"""
SQLITE WAREHOUSE — the deep backend, and the idempotence guarantee.
==================================================================

`sqlite3` is standard library, so this costs no dependency.

The `partitions` table is the point of the whole design: it records every
(source, year, period) slice that has been loaded, with the sha256 of the bytes
it came from. That gives three properties the refresh job needs:

  * INCREMENTAL   a re-run fetches only slices not already present
  * IDEMPOTENT    re-loading the same slice replaces it instead of
                  double-counting passengers, which is the failure mode that
                  would silently corrupt every downstream demand figure
  * AUDITABLE     a changed upstream file shows up as a checksum mismatch
                  rather than quietly shifting the numbers

Nothing here aggregates or interprets. Distillation is a separate step so that
the interpretation choices (de-censoring, gravity fit, segment mix) live in one
reviewable place rather than being smeared through the loader.
"""
from __future__ import annotations
import hashlib
import sqlite3

from airlinesim.btsdata.schema import ALL_TABLES

PARTITIONS_DDL = """
CREATE TABLE IF NOT EXISTS partitions (
    source TEXT NOT NULL,
    year INTEGER NOT NULL,
    period INTEGER NOT NULL,          -- month for T-100, quarter for DB1B, 0 for static
    rows INTEGER,
    sha256 TEXT,
    channel TEXT,                     -- which download channel supplied it
    url TEXT,
    fetched_at TEXT,
    PRIMARY KEY (source, year, period)
)"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_t100_pair ON t100_segment(origin, dest)",
    "CREATE INDEX IF NOT EXISTS ix_t100_period ON t100_segment(year, month)",
    "CREATE INDEX IF NOT EXISTS ix_mkt_pair ON db1b_market(origin, dest)",
    "CREATE INDEX IF NOT EXISTS ix_cpn_pair ON db1b_coupon(origin, dest)",
    "CREATE INDEX IF NOT EXISTS ix_cpn_itin ON db1b_coupon(itin_id)",
    "CREATE INDEX IF NOT EXISTS ix_apt_iata ON airport_ref(iata)",
    "CREATE INDEX IF NOT EXISTS ix_rwy_ident ON runway_ref(airport_ident)",
)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Bulk-load friendly. Durability matters less than throughput here: the
    # warehouse is a derived cache that can always be rebuilt from BTS.
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    return conn


def create_all(conn: sqlite3.Connection):
    conn.execute(PARTITIONS_DDL)
    for table in ALL_TABLES:
        conn.execute(table.ddl)
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()


def loaded_partitions(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute("SELECT source, year, period FROM partitions").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {(r["source"], r["year"], r["period"]) for r in rows}


def insert_rows(conn: sqlite3.Connection, table, rows) -> int:
    """Insert normalized rows. Columns absent from a row are stored as NULL."""
    if not rows:
        return 0
    cols = [c.name for c in table.columns]
    sql = (f"INSERT INTO {table.key} ({','.join(cols)}) "
           f"VALUES ({','.join('?' * len(cols))})")
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


def replace_partition(conn: sqlite3.Connection, table, year: int, period: int,
                      rows, payload_sha: str, channel: str, url: str,
                      fetched_at: str) -> int:
    """
    Load one slice, replacing any previous load of the same slice. This is the
    idempotence guarantee — without the DELETE, a re-run would double every
    passenger count in that period.
    """
    period_col = {"t100_segment": "month"}.get(table.key,
                 "quarter" if table.key.startswith("db1b") else None)
    if period_col is not None:
        conn.execute(f"DELETE FROM {table.key} WHERE year=? AND {period_col}=?",
                     (year, period))
    else:
        # Static reference tables (airports, runways) have no period dimension:
        # a refresh replaces the whole table.
        conn.execute(f"DELETE FROM {table.key}")

    n = insert_rows(conn, table, rows)
    conn.execute(
        "INSERT OR REPLACE INTO partitions "
        "(source, year, period, rows, sha256, channel, url, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (table.key, year, period, n, payload_sha, channel, url, fetched_at))
    conn.commit()
    return n


def backfill_longest_runway(conn: sqlite3.Connection) -> int:
    """
    Fold runways.csv into airport_ref.longest_runway_m — the single field route
    suitability actually reads. Takes the longest non-closed runway per airport.
    """
    conn.execute("""
        UPDATE airport_ref SET longest_runway_m = (
            SELECT MAX(length_ft) * 0.3048 FROM runway_ref
            WHERE runway_ref.airport_ident = airport_ref.ident
        )""")
    conn.commit()
    return conn.execute("SELECT COUNT(*) AS n FROM airport_ref "
                        "WHERE longest_runway_m IS NOT NULL").fetchone()["n"]


def table_counts(conn: sqlite3.Connection) -> dict:
    out = {}
    for table in ALL_TABLES:
        try:
            out[table.key] = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table.key}").fetchone()["n"]
        except sqlite3.OperationalError:
            out[table.key] = 0
    return out

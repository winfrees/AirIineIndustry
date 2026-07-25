"""
BTS INGEST — dev-time only. NOT imported by the simulation at runtime.
=====================================================================

This subpackage downloads and normalizes public Bureau of Transportation
Statistics data into a local SQLite warehouse, from which a small committed
snapshot is distilled (see docs/route-data-plan.md).

Hard rule: `airlinesim.routedata` (runtime) must NEVER import this subpackage.
The simulation reads distilled artifacts; only the ingest touches the network.

Pure standard library, like the rest of the project — urllib, zipfile, csv,
sqlite3, gzip, json.

Module map:
    schema.py     source table SHAPES: column aliases, coercion, DDL
    download.py   source ACCESS: the only network-touching module
    readers.py    CSV -> normalized rows (pure; takes file objects)
    warehouse.py  SQLite schema + partition manifest (idempotent refresh)
    probe.py      end-to-end verification run (the GitHub Actions job)
"""

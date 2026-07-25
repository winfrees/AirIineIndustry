# Historic route data — implementation plan (Option C)

Companion to `route-data-design.md`, which has the source investigation. This is
the agreed build. Status: **plan, awaiting go-ahead.**

## Decisions locked

| Decision | Choice |
|---|---|
| Architecture | **Option C** — SQLite warehouse for depth + committed distilled snapshot as the default runtime backend, both behind one `RouteDataProvider` |
| Fares | **In scope now**, from the frozen DB1B archive |
| Corpus | Top **~300** US airports by enplanements, all directional pairs among them clearing a minimum-departures floor |
| Vintage | **3 most recent complete years**, 2020–21 excluded as unusable |

On vintage, the two sources don't line up and the manifest must record both
separately rather than implying one number:

- **Volumes (T-100):** 2023, 2024, 2025 — all complete and post-COVID as of now.
- **Fares (DB1B):** collection ended Q2 2025, so the same window yields 2023,
  2024 and only Q1–Q2 of 2025. Encode the rule as "most recent N complete
  periods per source, minus `EXCLUDE_YEARS = (2020, 2021)`" and let each source
  resolve its own window.

---

## Module layout

Everything is stdlib (`urllib`, `zipfile`, `csv`, `sqlite3`, `gzip`, `json`), so
the no-third-party-deps rule holds for the ingest too.

    airlinesim/
      routedata.py            # RouteDataProvider + 3-tier resolution + gravity + provenance
      btsdata/                # dev-time ingest. NEVER imported by runtime code.
        download.py           #   the ONLY network-touching module
        schema.py             #   versioned per-source column maps
        t100.py               #   T-100 Segment reader
        db1b.py               #   DB1B Coupon + Market readers
        od40.py               #   stub, defined interface only
        airports.py           #   OurAirports -> airport_ref
        warehouse.py          #   SQLite schema + partition manifest
        distill.py            #   warehouse -> committed snapshot
        fixtures/             #   tiny real-schema CSVs for offline tests
      data/                   # the committed snapshot (added by the first refresh)
        routes.csv.gz
        airports.csv.gz
        gravity.json
        MANIFEST.json
      scenarios/scenario_routedata.py

The one hard rule: `airlinesim.routedata` must never import `airlinesim.btsdata`.
Runtime reads artifacts; only the ingest touches the network.

## Warehouse schema

    partitions(source, year, period, rows, sha256, fetched_at)   -- idempotent refresh
    t100_segment(year, month, carrier, origin, dest, aircraft_type, service_class,
                 passengers, seats, departures_performed, departures_scheduled,
                 distance_mi, ramp_to_ramp_hrs, air_time_hrs)
    db1b_coupon(year, quarter, itin_id, seq_num, origin, dest, op_carrier,
                passengers, distance, trip_break)
    db1b_market(year, quarter, origin, dest, ticket_carrier, passengers,
                market_fare, market_miles, market_coupons)
    airport_ref(iata, name, lat, lon, longest_runway_m, elevation_ft)

`partitions` is what makes refresh incremental: a re-run fetches only the
(source, year, period) rows it doesn't already have, and the sha256 makes a
changed upstream file visible instead of silently double-counting.

**Table choice matters for the two fare-derived quantities**, and they need
different DB1B tables:

- **Fares → `db1b_market`.** Prorated market fare is already mile-prorated by BTS
  per directional O&D.
- **Connecting share → `db1b_coupon`.** A coupon *is* a segment, so segment-level
  connecting share = share of coupons on that segment belonging to a multi-coupon
  itinerary. The Market table can't do this; it only knows the whole journey.

## Snapshot contents

`routes.csv.gz`, one row per directional pair: `origin, dest, distance_km,
pax_per_day, seats_per_day, deps_per_day, obs_load_factor, month_mult_1..12,
mean_fare, fare_p25, fare_p75, connecting_share, seats_p10, seats_p90,
dominant_plane_class, n_carriers`.

`airports.csv.gz`: `iata, name, runway_m, out_pax_per_day, in_pax_per_day,
deps_per_day, hub_rank, est_gates`.

`gravity.json`: fitted coefficients + diagnostics (n, R²) so Tier 2 needs only
the airport marginals at runtime, not the full corpus.

`MANIFEST.json`: per-source vintages, row counts, ingest date, config, code
version. This is the provenance record.

Distilled to ~300 airports with monthly shape, this should land in the
low-hundreds of KB gzipped — reviewable in a PR diff.

## The provider

```python
class DataTier(Enum): EXACT; COMPARABLE; SYNTHETIC

@dataclass(frozen=True)
class RouteObservation:      # neutral record both backends return
    origin, dest, distance_km, pax_per_day, seats_per_day, ...
    tier: DataTier
    vintage: str

class RouteDataProvider:     # SnapshotProvider | WarehouseProvider
    def airport(iata) -> AirportSpec | None
    def observation(o, d) -> RouteObservation
    def route_spec(o, d, **overrides) -> RouteSpec
```

Tier 2's gravity model, fitted by OLS on the Tier-1 pairs:

    log(pax_per_day) ~ a + b·log(origin_out_pax) + c·log(dest_in_pax)
                         + d·log(distance_km) + e·hub_flag

Five parameters over a few thousand rows — normal equations plus Gaussian
elimination, no numpy. Fit happens at ingest; the runtime just evaluates it.

### Data → RouteSpec mapping

| RouteSpec field | Derivation |
|---|---|
| `distance_km` | `distance_mi` × 1.609 |
| `base_demand_per_day` | de-censored pax (below) |
| `seasonality_amplitude`, `seasonal_peak_day` | sine fit to the 12 month multipliers |
| `segments` | connecting share from DB1B coupons; business/leisure from fare dispersion; `dow_profile` stays today's heuristic constants |
| `equipment_req.min_viable_seats` / `max_viable_seats` | observed seats-per-departure p10/p90 — replaces today's hand-picked window with evidence |
| `equipment_req.min_runway_m` | max runway need across aircraft types observed serving the pair |
| `crew_req` | unchanged, stage-length driven |

De-censoring, as a named tunable on the provider rather than a magic number:

    TARGET_LOAD_FACTOR = 0.85
    demand ≈ passengers / min(observed_lf, TARGET_LOAD_FACTOR)

Two additive fields on `RouteSpec` carry provenance: `data_tier: str = ""` and
`data_vintage: str = ""`. Defaults keep every existing construction site working.

## Phases

**Phase 0 — verify access. BUILT (not yet run against live BTS).**
`.github/workflows/bts-probe.yml` + `airlinesim/btsdata/probe.py`. Two jobs: an
offline one that proves our own pipeline on fixtures, then a live one that walks
access → headers → parse → warehouse load → plausibility → cross-source join
against real BTS and writes an actionable report to the run summary.

Run it with the **Actions → BTS data probe → Run workflow** button. Expect the
first live run to fail on URL or header guesses — that is the job working, and
the summary prints the actual headers plus the channel that answered, which is
what gets pasted back into `schema.py` / `download.py`. Only `verified=True`
candidates in `download.py` have been confirmed reachable (OurAirports only, so
far).

**Phase 1 — warehouse + readers (offline-testable).** `schema.py`, `t100.py`,
`db1b.py`, `airports.py`, `warehouse.py`, plus committed fixtures. Parsing and
aggregation are pure functions over file objects, kept strictly separate from
`download.py`, so the whole phase is testable with no network — including here.

**Phase 2 — distill + provider.** `distill.py`, `routedata.py`, the gravity fit,
both backends behind the one interface.

**Phase 3 — engine integration.** `SpecRepository.load(RouteSpec, ...)` — the seam
CLAUDE.md reserved for this and which is currently unused — plus a
`build_world_from_data()` alongside `build_demo_world()`. No engine or subsystem
changes beyond the two additive `RouteSpec` fields.

**Phase 4 — self-updating refresh.** `.github/workflows/bts-refresh.yml`, monthly
cron: refresh → distill → open a PR with the regenerated artifact and manifest. It
must fail loudly rather than commit a partial corpus when BTS is unavailable. The
"database that updates itself" is therefore a reviewed data commit, which keeps
every sim reproducible from a git SHA.

**Phase 5 — docs.** Update CLAUDE.md's known-limitations with the caveats below.

## Testing, given this sandbox has no BTS access

New scenario `airlinesim run routedata` drives the whole pipeline on committed
fixtures: fixtures → temp SQLite → distill → provider → build a World → tick it.
Asserts tier resolution (a fixture pair resolves EXACT, a held-out pair resolves
COMPARABLE, an unknown airport resolves SYNTHETIC), gravity sanity, and that a
data-built world runs. Offline, so it works here and in CI. This follows the
existing convention of scenarios-as-tests rather than adding a test framework.

**No snapshot ships in phase 1–3.** With `airlinesim/data/` absent the provider
resolves Tier 3 and behaves exactly as today. The real artifact arrives via the
first Phase 4 refresh PR, produced where the network exists — rather than me
committing a corpus I could not actually download.

## Caveats to carry into CLAUDE.md's known-limitations

1. **T-100 passengers are flown, not demanded** — the de-censoring divisor is a
   game-balance assumption, not a measurement.
2. **`dow_profile` is not from data.** T-100 is monthly; nothing in it informs
   day-of-week.
3. **Business vs leisure is a fare-dispersion proxy**, not observed trip purpose.
4. **DB1B market fares are journey fares.** Attributing them to one leg is a
   proration, and DB1B's 10% sample is noisy on thin routes.
5. **Fare vintage lags volume vintage** (DB1B stops mid-2025) until an OD40 reader
   exists.
6. **`total_gates` and `fuel_supply_per_day_l` remain heuristics** — no public
   dataset covers them; gates would be scaled from peak-hour departures.
7. **`/PREZIP/` is undocumented** and can break without notice; OD40 will need a
   new reader.

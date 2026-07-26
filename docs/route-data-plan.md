# Historic route data — implementation plan (Option C)

Companion to `route-data-design.md`, which has the source investigation. This is
the agreed build. Status: **plan, awaiting go-ahead.**

## Decisions locked

| Decision | Choice |
|---|---|
| Architecture | **Option C** — SQLite warehouse for depth + committed distilled snapshot as the default runtime backend, both behind one `RouteDataProvider` |
| Fares | **In scope now**, from the frozen DB1B archive |
| Corpus | Top **~300** US airports by enplanements, all directional pairs among them clearing a minimum-departures floor |
| Vintage | **3 most recent complete years**, 2020–21 excluded — under revision, see the 2015 note below |

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

Run it with the **Actions → BTS data probe → Run workflow** button.

### First live run — 2026-07-25 ([run 30173843598](https://github.com/winfrees/AirIineIndustry/actions/runs/30173843598))

| Source | Result |
|---|---|
| DB1B Market | ✅ 110 MB, headers matched, mean fare **$323.77**, 0.1% rejects |
| DB1B Coupon | ✅ 258 MB, headers matched |
| OurAirports airports / runways | ✅ 8,801 airports, 46,849 runways |
| **T-100 Segment** | ❌ 404 on all three guessed `/PREZIP/` filenames |

So the fare and reference channels are confirmed and pinned `verified=True`. The
DB1B success is also the naming clue: the working URL is
`Origin_and_Destination_Survey_DB1BMarket_2024_2.zip`, i.e. PREZIP uses the
**download UI's** table name plus period, not the internal `RawDataTable` name
that the T-100 guesses used.

Two fixes came out of that run:

- **`discover.py`** — rather than guess a fourth time, discovery HEAD-sweeps a
  64-name matrix, scrapes the TranStats index/table/download pages for real
  `.zip` hrefs, literal `PREZIP/...` strings and form field names, and resolves
  the ArcGIS mirror through the public ArcGIS search API. `probe.py` runs it
  automatically when T-100 access fails and, if the sweep finds a working URL,
  **retries with it in the same run** — so one run can both discover and
  validate. `--discover-only` (or the `discover_only` workflow input) does just
  the hunt in ~1 minute instead of re-downloading 370 MB of DB1B.
- **Per-table reject ceilings.** The run flagged `airport_ref` at 89.7% rejected,
  which was a false alarm: 76,752 of 85,807 OurAirports rows have no IATA code
  because the dataset covers every airfield on earth. Filtering them is the
  point, so the ceiling is now a `SourceTable` field (95% for reference tables,
  10% for traffic tables) instead of one global threshold that would have masked
  real problems.

The summary also truncated the T-100 error detail at 300 characters, cutting off
the useful part; the cap is now 1,200.

### T-100 is not in /PREZIP/ at all

Confirmed by inspecting the directory: no T-100 file is published there, so the
name sweep was never going to succeed. The real mechanism is a **TranStats
session export** — the field-picker writes results to

    https://transtats.bts.gov/ftproot/TranStatsData/<request-id>_<TABLE>.zip

e.g. `896816367_T_T100_MARKET_ALL_CARRIER.zip`. The numeric prefix is generated
per form submission, so **such a URL is a receipt, not a channel**: it will 404
once TranStats reaps it, and it cannot be regenerated by guessing. Consequences:

- Fine for a one-off ingest. Unusable as the monthly refresh channel, so the
  self-updating half of Phase 4 needs either the form POST reproduced (the field
  names `discover.py` scrapes are exactly what that requires) or a manual
  re-export.
- The ingest therefore accepts an operator-supplied URL **or local path**
  (`--t100-url`, `--t100-market-url`, `--t100-request-id`, and matching workflow
  inputs). Download by hand once, point the probe at the file, validate the whole
  chain. `explicit_candidate()` covers both.

### T-100 Market is not a substitute for T-100 Segment

`T_T100_MARKET_ALL_CARRIER` is the **on-flight market** table. It carries
PASSENGERS, FREIGHT, MAIL and DISTANCE — and **no SEATS, no DEPARTURES, no
AIRCRAFT_TYPE**. So adopting Market in place of Segment removes:

| Lost with Market-only | Why it mattered |
|---|---|
| load factor | the de-censoring rule becomes impossible; `base_demand_per_day` has no basis |
| seats-per-departure p10/p90 | no evidence-based `min/max_viable_seats`; back to hand-picked constants |
| aircraft type per route | nothing for the "AI right-sizes equipment" next step |
| departures | no frequency, so no peak-hour proxy for the `total_gates` heuristic |

What Market *does* give, and DB1B cannot, is a **complete census** of O&D
passenger volumes — strictly better than scaling a 10% ticket sample by ten. It
is a good demand source and not a capacity source at all.

Both tables are exported through the same field-picker, so choosing Segment
instead of Market costs one more export and keeps the supply side of the model.
`t100_market` is wired up as a genuine additional source either way, and the
probe emits a loud WARN plus skips the load-factor checks when only Market is
present, rather than reporting a clean green on half a model.

### Vintage: capping at 2015

Direction is to use historical data up to 2015. Recorded implications:

- A 2015-vintage corpus models the **2015 network** — before the Alaska/Virgin
  America merger, with different LCC footprints and hub structures. Route
  presence and relative airport size will not match today's.
- Fares are in 2015 dollars; roughly 30% cumulative inflation to 2026 means fare
  calibration needs an explicit deflator or an acknowledged level shift.
- It is cleanly pre-COVID, so the 2020–21 exclusion becomes moot and the corpus
  is internally consistent.
- If the export covers 1990–2015 rather than 2015 alone, that is ~25 years for a
  stable multi-year average and a well-determined seasonal shape — a real
  strength, just one that ends a decade ago.
- The refresh cadence question changes character: a fixed historical corpus does
  not need monthly refresh, which sidesteps the transient-URL problem entirely
  but also drops the "updates itself" goal down to "re-export when we choose to
  move the vintage".

**Phase 1 — warehouse + readers (offline-testable).** `schema.py`, `t100.py`,
`db1b.py`, `airports.py`, `warehouse.py`, plus committed fixtures. Parsing and
aggregation are pure functions over file objects, kept strictly separate from
`download.py`, so the whole phase is testable with no network — including here.

**Phase 2 — distill + provider.** `distill.py`, `routedata.py`, the gravity fit,
both backends behind the one interface.

**Phase 3 — engine integration. DONE.** `airlinesim/databuilder.py` with
`build_world_from_data()` / `run_from_data()`, specs flowing through
`SpecRepository.load()`, and `airlinesim run databuilt` asserting 17 invariants.
No engine or subsystem changes beyond the two additive `RouteSpec` fields, as
planned.

Results on the real corpus (hub ORD, 4 destinations, both directions):

| Route | Demand | Distance | Aircraft chosen | Seat window |
|---|---|---|---|---|
| ORD-LGA | 3,468/day | 1,180 km | 787-9 (290) | 204-600 |
| ORD-LAX | 3,173/day | 2,807 km | 787-9 (290) | 186-600 |
| ORD-DEN | 2,817/day | 1,429 km | A320 (180) | 165-600 |
| ORD-SFO | 2,525/day | 2,971 km | A320 (180) | 148-600 |

Three things the integration surfaced that pure unit checks would not:

- **Frequency has to come from the data too.** One rotation a day against a
  3,400 px/day market flies ~50% full and makes the corpus look wrong when it
  isn't. Frequency is now derived from measured demand and capped by airframe
  hours — after which cabin duty limits trim it further on most trunk ops, so
  the data-implied schedule genuinely meets the real duty envelope.
- **Crew depth must scale with the flying based at a station.** A flat two crews
  per base left a hub originating four routes reporting "no legal crew
  available" while every aircraft sat serviceable.
- **Failed acquisitions must not join the fleet.** `Bank.acquire()` returns None
  when credit is denied, and attaching the Airplane anyway put aircraft in the
  fleet that were never paid for, inflating net worth to $1.3B. Fixed here;
  `builder.py` still has the same latent bug and is flagged in CLAUDE.md.

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

# Historic route data — investigation & options

Status: **investigation / not yet implemented.** Written to agree an approach before coding.

Goal (as requested): model routes from historic real-world data when the O&D pair
exists in the record, and fall back to a *comparable* route synthesized from the
size of the origin and destination airports when it doesn't. BTS is the source.

---

## 1. What BTS actually gives us

### T-100 Domestic Segment (Data Bank 28DS) — the core table

Non-stop segment traffic, reported under 14 CFR 291.45. One row per
carrier × origin × destination × aircraft type × service class × month.

Relevant columns: `PASSENGERS`, `SEATS`, `DEPARTURES_PERFORMED`,
`DEPARTURES_SCHEDULED`, `RAMP_TO_RAMP`, `AIR_TIME`, `DISTANCE`,
`AIRCRAFT_TYPE`, `UNIQUE_CARRIER`, `ORIGIN`, `DEST`, `YEAR`, `MONTH`.

- Frequency: monthly, free. Public availability runs roughly two months behind
  month-end for domestic (longer for revised/international tables).
- Coverage: 1990–present. Domestic ~30k directional O&D pairs/year, of which a
  few thousand carry meaningful scheduled traffic.
- This maps almost 1:1 onto what `RouteSpec` needs: `distance_km`,
  `base_demand_per_day`, and the monthly shape behind `seasonality_amplitude`.
  It also gives us real `AIRCRAFT_TYPE`-per-route evidence, which is exactly what
  the "AI doesn't right-size equipment" limitation needs later.

**What T-100 does *not* give us, and must not be faked silently:**

| We need | T-100 has it? | Honest source |
|---|---|---|
| `distance_km` | yes (`DISTANCE`, statute miles) | direct |
| seasonality amplitude + peak month | yes (monthly totals) | direct, fit a sine or keep 12 multipliers |
| `dow_profile` (day-of-week) | **no** — monthly aggregates only | keep today's heuristic, or derive *departure* DOW from the On-Time Performance DB (that's flights, not pax) |
| fares / `ticket_price`, elasticity | **no** | DB1B / OD40 (below) |
| business vs leisure vs connecting split | **no** | connecting share is derivable from DB1B itineraries; business/leisure is only a fare-dispersion *proxy* |
| true demand | **no** — see censoring below | needs an explicit de-censoring assumption |

### The demand-censoring problem (the biggest modeling honesty issue)

T-100 `PASSENGERS` is passengers *flown*, i.e. `min(demand, capacity)` after the
carrier already optimized price and frequency. It is not demand. Feeding it
straight into `base_demand_per_day` systematically **understates** demand on
high-load-factor routes, and the sim would then never reward adding capacity on
exactly the routes where real airlines do.

Any implementation needs a stated de-censoring rule, e.g.

    observed_lf = PASSENGERS / SEATS
    demand ≈ PASSENGERS / min(observed_lf_cap, target_lf)   # target_lf ~0.85

so a route running at 92% load implies latent demand above what was flown. This
is an approximation and should be labelled as one in code and in the known-
limitations list — it is a game-balance choice, not a measurement.

### DB1B → OD40/DB1C — fares, and a live migration to design around

Fares live in the Origin & Destination Survey, not T-100.

- **DB1B** (Coupon / Market / Ticket tables): 10% random ticket sample, quarterly,
  ~45 days after quarter end. `Market` carries prorated market fare, market miles,
  passengers, carrier-change flags — enough to calibrate `reference_price` per
  stage length and to compute connecting share per segment.
- **Important:** DB1B collection **ended July 2025**. It is superseded by
  **OD40 / DB1C** — a 40% sample collected *monthly*. First release covered
  Jul–Sep 2025. OD40 is published first on bts.gov and was only expected to reach
  TranStats in the legacy format later.

Consequence for design: DB1B is a frozen historical archive (1993 – Q2 2025) with
one schema, and OD40 is the going-forward feed with another. Whatever we build
must treat "fare source" as a pluggable, versioned reader, not a single parser.
Anything that hardcodes DB1B column names will break on the first OD40 refresh.

### Airport reference data

`AirportSpec` needs `runway_length_m` (suitability already enforces it),
plus coordinates if we ever want great-circle distance independent of T-100.

- BTS `T_MASTER_CORD` has coordinates and city markets.
- **OurAirports** (`davidmegginson/ourairports-data` on GitHub) has
  `airports.csv` + `runways.csv` with per-runway `length_ft` — a direct feed for
  `runway_length_m` (take the longest usable runway). Public-domain, and it is
  reachable from this sandbox (verified) where bts.gov is not.
- `total_gates` and `fuel_supply_per_day_l` have **no** public dataset. They stay
  derived heuristics (e.g. gates scaled from peak-hour departures in T-100). Say so.

### Access mechanics — no official API

There is no REST/JSON API for T-100. Three practical paths:

1. **TranStats web form** (`DL_SelectFields.aspx`) — POSTs a field selection,
   returns a zipped CSV. Fiddly, has historically changed its parameter names.
2. **PREZIP direct zips** — `https://transtats.bts.gov/PREZIP/<TABLE>_<year>_<q>.zip`.
   Widely used by third-party scrapers (this is how most published tooling does it).
   Undocumented and unsupported, so it can vanish without notice.
3. **ArcGIS mirror** — geodata.bts.gov publishes a "T-100 Domestic Market and
   Segment Data" feature service with a real REST/GeoJSON API. Stable interface,
   but it is a curated subset, not the full table.

**Verified constraint:** this remote sandbox's network policy blocks
`bts.gov`, `transtats.bts.gov`, `catalog.data.gov`, `data.transportation.gov`
and `geodata.bts.gov` at the proxy (403 on CONNECT). GitHub and the package
registries are reachable. So the exact download URLs above are **unverified from
here** and step 1 of any implementation is confirming them from a networked
machine or a GitHub Actions runner (runners do have open egress).

---

## 2. The lookup the sim needs (common to every option)

This part is the same regardless of where the bytes live — a provider with a
three-tier resolution, which is precisely the "historic, else comparable" ask:

    RouteDataProvider.route_spec(origin, dest) ->

    Tier 1  EXACT       O&D pair present in the historic record
                        -> real pax, seats, frequency, monthly shape, fleet mix,
                           observed fare. Highest confidence.

    Tier 2  COMPARABLE  pair absent (or below a traffic threshold)
                        -> synthesize with a gravity model fitted on the Tier-1
                           pairs: demand ~ f(origin outbound pax,
                           dest inbound pax, distance, hub flags). Both airport
                           "sizes" come straight from T-100 marginals, so this
                           is calibrated on real data rather than invented.

    Tier 3  SYNTHETIC   airport unknown entirely
                        -> today's default_segments() behavior, unchanged.

Every generated `RouteSpec` should carry provenance — which tier produced it, and
the vintage of the data behind it — so scenarios can print it and so nobody
mistakes a Tier-2 gravity estimate for a measurement.

A gravity fit on log-transformed T-100 marginals is ordinary least squares on a
handful of terms; it is straightforward in pure stdlib (no numpy needed) at this
size.

---

## 3. Options

All three keep the existing architecture intact: they feed
`SpecRepository.load(RouteSpec, rows, builder)` — the import seam that CLAUDE.md
already reserves for exactly this ("hand-authored dicts today, real-world data
later, no engine changes"). `SpecRepository.load()` exists and is currently unused;
this is what it was for. No engine or subsystem changes are required to get
Tier 1 + Tier 2 working.

### Option A — Committed distilled snapshot + scheduled refresh (recommended)

An out-of-band ingest tool downloads BTS, aggregates hard, and writes a small
**committed** artifact; the runtime only ever reads that artifact.

    tools/ingest_bts.py         # network-facing, dev-time only, not imported at runtime
    airlinesim/data/
      routes.csv.gz            # O&D pair -> pax/day, seats, freq, 12 monthly
                               #   multipliers, mean fare, connecting share
      airports.csv.gz          # iata -> runway_m, outbound/inbound pax, hub flag
      MANIFEST.json            # source tables, vintages, row counts, ingest date
    airlinesim/routedata.py    # RouteDataProvider: the 3-tier lookup above

Refresh is a monthly GitHub Actions cron that runs the ingest and opens a PR, so
the "database that updates itself" is a reviewed data commit. Diffs are visible,
every sim is reproducible from a git SHA, and CI stays offline.

- Distilled to the top few hundred airports and annual+monthly-shape aggregates,
  this is a few hundred KB — comfortably committable.
- Pure stdlib at runtime (`csv`, `gzip`, `json`). Zero new deps.
- Works offline, works in this sandbox, works in CI.
- Cost: loses carrier-level and aircraft-type-level granularity, so questions
  like "what did competitors actually fly here" need a re-ingest.

### Option B — Local SQLite warehouse

Ingest raw monthly files into a real database (`sqlite3` is stdlib) at
`~/.airlinesim/bts.sqlite`, with a `partitions` table recording each loaded
(table, year, month) so refresh is incremental and idempotent — re-running only
fetches what's missing. Runtime queries it behind the same provider interface.

- Keeps full fidelity: carrier × aircraft type × class, all years. Directly
  enables the "AI reads suitability + per-route P&L to choose aircraft" and
  used-aircraft-market next steps, and real competitor modeling.
- Real SQL for the gravity fit and comparables search.
- Cost: multi-GB local store; every developer and CI job needs a network-bound
  setup step before the sim runs; nothing is reproducible from the repo alone.
  This sandbox cannot build it at all.

### Option C — Both, behind one interface

Ship Option A's snapshot as the default backend and Option B's warehouse as an
optional deep backend, selected by config. Same `RouteDataProvider` API; the
snapshot is *generated from* the warehouse, so there's one aggregation code path.

- Best end state, and the two backends are genuinely complementary.
- Cost: two backends to maintain from day one, and the second one is unusable in
  this environment. Sequencing it as A → (B later, if the AI/fleet work needs it)
  gets the same place with less speculative work.

### Recommendation

**Option A first, structured so Option B can be added without touching callers.**
It delivers the actual request — historic-when-available, gravity fallback
otherwise — with no new dependencies, keeps CI and this sandbox green, and makes
the data auditable in review. Promote to Option C only when a feature genuinely
needs carrier/aircraft-type detail.

---

## 4. Open questions for the coding plan

1. **Scope of the corpus** — all US domestic, or top N airports? N drives artifact
   size and the quality of the gravity fit.
2. **Vintage** — one recent representative year (clean, small, reproducible) or a
   multi-year average (robust to COVID-era distortion; 2020–21 is unusable as-is)?
3. **Fares** — calibrate against DB1B's frozen archive now and add an OD40 reader
   when its format settles, or wait and ship Tier 1/2 on T-100 volumes only?
4. **Refresh cadence & mechanism** — monthly Action opening a PR (auditable) vs
   committing directly to a data branch (less noise)?
5. **`dow_profile`** — leave as the current heuristic (honest, T-100 can't inform
   it) or derive departure-DOW from the On-Time Performance DB and document that
   it's a capacity proxy rather than a demand profile?
6. **Segment mix** — does per-route business/leisure/connecting from a fare proxy
   replace `SEGMENT_CABIN_SPLIT`'s global default, or feed the already-noted
   "make it tunable per route" next step?

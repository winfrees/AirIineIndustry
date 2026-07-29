# airlinesim

An airline asset & resource management simulation engine — continuous-time,
multi-player, and built to be extended. It models the operational and financial
loop of running competing airlines: acquiring aircraft, configuring cabins,
staffing crews under duty limits, scheduling maintenance, pricing into a
structured market, and competing over scarce gates, fuel, and passengers.

Pure standard library — no third-party runtime dependencies.

## Install

From the package directory:

```bash
pip install .
```

This installs the `airlinesim` package and an `airlinesim` command.

### Windows, without installing Python

Every release ships a portable build — `AirlineSim-<version>-win-amd64.zip` on
the [Releases page](../../releases). Unzip it (Explorer's zip preview is not
enough — extract the folder) and run:

| launcher | what it does |
|-------------------------|---------------------------------------------|
| `AirlineSim-GUI.bat`    | play in a browser; starts the local server  |
| `AirlineSim-Demo.bat`   | the 60-day two-carrier demo in a console    |
| `Run-Checks.bat`        | run the bundled scenarios and print PASS/FAIL |
| `airlinesim.bat ...`    | the full CLI, e.g. `airlinesim.bat run integration` |

The bundle carries its own CPython and the route corpus, so it needs no Python,
no pip and no network. It is not code-signed: SmartScreen warns on first launch.

To build one yourself (or from a branch), run the **Windows release** workflow
from the Actions tab — it builds and smoke-tests the bundle and attaches it to
the run. Locally, on Windows:

```bash
python -m build                                          # wheel + sdist
python tools/build_windows_bundle.py --version 0.2.0 --wheel dist/airlinesim-0.2.0-py3-none-any.whl
python tools/smoke_windows_bundle.py --bundle dist/AirlineSim-0.2.0-win-amd64
```

Tagging `v<version>` (matching `pyproject.toml`) publishes a GitHub Release with
the bundle, the wheel, the sdist and `SHA256SUMS.txt`.

## Quick start

```python
from airlinesim import build_demo_world, run

world, engine = build_demo_world()   # two carriers, full subsystem pipeline
run(engine, days=60)                  # advance and print a summary
```

Or from the command line:

```bash
airlinesim demo --days 60        # run the built-in two-carrier demo
airlinesim list                  # list bundled scenarios
airlinesim run integration       # run a named scenario
```

(If you haven't installed, the same works via `python -m airlinesim.cli ...`.)

## What it models

- **Spec-driven entities.** Aircraft, airports, crews, and routes are built from
  data specs through a `SpecRepository`, so hand-authored data today can be
  swapped for imported real-world data with no engine changes.
- **Tiered maintenance.** A/B/C/D checks driven by per-aircraft programs, with
  the modern A+B fold, the 3C/IL structural escalation, and value-aware
  retirement (scrap when an overhaul costs more than the depreciated airframe).
- **Structured route demand.** Business / leisure / connecting traveler segments,
  each with its own elasticity, seasonality, and day-of-week profile.
- **Cabin-class revenue.** Seat layouts trade total seats against revenue per
  seat; each class fills from its own demand pool at its own price elasticity,
  and each route prices every cabin its aircraft carries.
- **Cabin geometry.** A configuration is fitted to the airframe it goes into:
  seats snap to installable rows at that class's pitch and seats-across, so a
  lie-flat business seat costs ~2.2 economy seats on a narrowbody and ~4.2 on
  a widebody — and an over-large request comes back trimmed with the reason.
- **Crew as a real constraint.** Duty/rest limits (FAR Part 117-shaped),
  type-ratings, a pool-based roster, out-of-base positioning, and deadheading
  on revenue seats.
- **Financing & banking.** Buy / finance / operating-lease acquisition, amortizing
  loans, lease rent and expiry, creditworthiness gating, and declining-balance
  depreciation feeding the balance sheet.
- **Competition.** A `ResourceArbiter` resolves contention over finite gates,
  fuel, and passenger demand between carriers each tick.
- **Weather and disruption.** A probabilistic, geographic weather model —
  climate derived from each airport's real coordinates, with fronts,
  convection, snow, ice, fog, hurricanes, wildfire smoke and volcanic ash as
  systems that move across the map. It cuts airport capacity, cancels and
  delays flights, eats into crew duty limits so the *next* rotation is the one
  that fails, strands passengers into rebookings and hotel bills, and keeps a
  per-airport reliability record so an exposed hub costs you over a season.
  Each new game draws its own season; the outcome explorer can switch weather
  off or on at any node and stage named events — a blizzard at your hub in
  week three — to compare against a sibling branch that didn't get one.
- **Alliances and consolidation.** Connecting demand has to be *fed*: a leg is
  worth more when something departs its destination, your own metal counts in
  full, a partner's counts at the alliance tier's efficiency, and a stranger's
  counts for nothing. Mergers run off an itemised valuation with three stated
  rationales — horizontal, complementary, or survival when neither carrier can
  compete alone.
- **Hourly clock.** The engine's tick length is a resolution knob and the
  simulation is independent of it: a month of flying comes out the same
  stepped in 24-hour, 6-hour or 1-hour slices.

## Architecture

The simulation advances in ticks. Each tick runs an ordered pipeline of
subsystems over a shared `World` (contested resources) and many `Player`s
(owned assets):

```
RouteSuitability -> Deadhead -> Roster -> Banking -> Finance
   -> Operations -> CrewPositioning -> Maintenance -> CrewLegality
```

`World` holds the contested commons (gates, fuel, passenger markets, the clock).
Each `Player` holds owned assets (fleet, crews, routes, ledger). Competition is
modeled as contention over world resources resolved by the arbiter — so adding a
competitor is adding a `Player`, not rewriting allocation logic.

## Bundled scenarios

| name          | shows                                                        |
|---------------|-------------------------------------------------------------|
| `competitive` | two carriers competing over a contested hub                 |
| `integration` | every subsystem in one pipeline, with pass/fail checks      |
| `crew`        | duty/rest limits capping how much a crew can fly            |
| `roster`      | pool-based rostering and out-of-base positioning            |
| `deadhead`    | crews repositioning home on revenue seats                   |
| `route`       | market structure + equipment/crew suitability validation   |
| `finance`     | buy vs finance vs lease, with depreciation                  |
| `cabin`       | cabin geometry, seat fitting, and per-cabin fares            |
| `weather`     | clock resolution, geographic weather, and the disruption chain |
| `alliance`    | connecting feed, alliances, valuation, and mergers          |
| `btsdata`     | BTS ingest pipeline against committed fixtures (offline)    |
| `routedata`   | the three-tier historic/comparable route lookup             |
| `databuilt`   | the engine running on real BTS route data                   |
| `refresh_cx`  | corpus staleness, diffing, and the data-loss guard          |

## Historic route data

Routes can be modeled from real US Bureau of Transportation Statistics data
instead of hand-authored constants, with a calibrated fallback when a market
isn't in the record:

```bash
airlinesim demo --data --hub ORD    # a world built from the BTS corpus
airlinesim run routedata            # inspect the three-tier lookup
airlinesim refresh --check-only      # is the corpus stale? what needs re-export?
```

A committed snapshot (~364 KB, 300 airports, 6,720 directional routes) ships in
`airlinesim/data/`, so this works offline with no database to build. Lookups
resolve in three tiers, and every generated `RouteSpec` records which one it came
from:

| tier | when | source |
|---|---|---|
| `exact` | BTS recorded this directional pair | measured passengers, distance, 12-month seasonal shape |
| `comparable` | pair absent, both airports known | gravity model fitted on the measured pairs, using each endpoint's real traffic as its size |
| `synthetic` | an airport is unknown | the engine's own defaults, unchanged |

The comparable-route model is cross-validated rather than asserted, and the
numbers ship with the data in `data/gravity.json`: **median predicted/actual
1.004, 60.6% of held-out routes within 2×, 78.2% within 3×**.

Rebuilding the corpus from a fresh BTS export:

```bash
airlinesim ingest --t100-market <export.zip> --fetch-airport-ref --distill
```

See `docs/route-data-design.md` for which BTS tables cover what, and
`docs/route-data-plan.md` for the build.

## Extending

The intended extension points:

- **New entity data** — add specs and load them through `SpecRepository`.
- **New constraints** — equipment/crew rules live in `route.py` as data-driven
  checks; add a rule without touching the engine.
- **New behavior** — implement a `Subsystem` and insert it into the pipeline.

## Honest limitations

This is a prototype engine, not a production airline model. Notable
simplifications:

- Maintenance intervals, depreciation rates, and duty limits are
  industry-*shaped* defaults for game balance, not certified figures. They are
  data and trivially swappable.
- Route demand *is* wired through to cabin revenue — each segment is its own
  priced, capacity-bound pool — but the segment-to-cabin split fractions are a
  single global default. A per-route tilt is implemented and sits neutral,
  because nothing in the corpus measures how premium a market is;
  `docs/cabin-demand-design.md` sets out the options for measuring it.
- Cabin geometry is calibrated to each type's seat count, not to fuselage
  drawings: seats-across is the published figure, but cabin length is derived
  so that an all-economy layout comes to exactly `max_seats`. Pitch tables are
  industry-shaped, not certified.
- Crew positioning deadheads direct-to-base only; multi-hop routing and ferry
  (positioning) flights are not yet implemented.
- Weather is a probabilistic model whose climate comes from each airport's real
  coordinates, but every frequency, size and impact figure in it is a
  climate-*shaped* heuristic — no weather record is committed to this repo.
  `docs/weather-design.md` sets out how NOAA Climate Normals and BTS On-Time
  Performance would replace them. The disruption chain has real gaps too: a
  cancelled flight leaves its aircraft in the right place anyway, rebooking
  only searches the same market in the same tick, and nobody pre-cancels ahead
  of a forecast.
- The bundled AI adjusts price/frequency but does not yet use route suitability
  to right-size equipment, and prices only the economy base fare — the premium
  cabins it installs sell at the default class multiple.
- On the historic data specifically: the shipped corpus is built from T-100
  **Market**, which carries no seat counts, so demand equals passengers *flown*
  and is understated on full routes. Capacity, load factor and a measured seat
  window need a T-100 **Segment** export. Day-of-week profiles and the
  business-vs-leisure split are not from data at all — no BTS source carries trip
  purpose. Gate counts and fuel throughput have no public dataset and remain
  heuristics. Each field's footing is tagged MEASURED / DERIVED / HEURISTIC in
  `airlinesim/data/MANIFEST.json`.

## License

MIT.

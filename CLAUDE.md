# CLAUDE.md — airlinesim

Context for Claude Code sessions on this project. Read this first.

## What this is

A continuous-time, multi-player airline asset & resource management simulation
engine, written in pure-stdlib Python (3.10+, no third-party runtime deps). It
models the operational and financial loop of running competing airlines.

Built entity-by-entity to a consistent bar ("the Aircraft standard"): every
entity has a real data spec, a live mutable instance, integrated behavior, and
constraint enforcement — not partial stubs.

## Layout

    airlinesim/
      engine.py         # core: specs, World, Player, arbiter, pricing, maintenance,
                        #       Operations/Finance/Banking/RouteSuitability subsystems,
                        #       SimulationEngine tick loop
      crew.py           # duty/rest limits, rostering, positioning, deadheading
      route.py          # market segments, stage economics, equipment/crew suitability
      finance_cabin.py  # cabin classes + seat layout; financing/banking; depreciation
      builder.py        # build_demo_world() / run() convenience entry points
      cli.py            # `airlinesim` command (list / run / demo / probe)
      routedata.py      # RUNTIME provider: 3-tier historic/comparable lookup
      databuilder.py    # build_world_from_data(): a world from the BTS corpus
      data/             # committed distilled snapshot (routes/airports/gravity)
      btsdata/          # DEV-TIME BTS ingest (schema/download/readers/warehouse/
                        #   ingest/distill/discover/probe + fixtures). Never
                        #   imported at runtime.
      scenarios/        # runnable demos, each with a main()
    tools/              # DEV-TIME build tooling (never imported by the package):
                        #   build_windows_bundle.py  portable Windows build
                        #   smoke_windows_bundle.py  scenario/CLI/GUI smoke test
    pyproject.toml      # pip-installable; console entry point `airlinesim`

## Core architecture (do not break these)

- Spec vs instance. Frozen *Spec dataclasses are immutable reference data loaded
  through SpecRepository; mutable instances hold live state. The repository is
  the import seam — hand-authored dicts today, real-world data later, no engine
  changes.
- World vs Player. World holds the contested commons (gates, fuel, demand
  markets, clock). Each Player holds owned assets. Competition = contention over
  World resources.
- ResourceArbiter. Every claim on a finite World resource goes through the
  arbiter, even single-player. Adding a competitor is adding a Player, never
  rewriting allocation logic.
- Subsystem pipeline. The tick runs an ordered list of Subsystems. Order matters:
  RouteSuitability -> Deadhead -> Roster -> Banking -> Finance -> Operations
   -> CrewPositioning -> Maintenance -> CrewLegality
  New behavior should be a new Subsystem slotted into this pipeline.

## Conventions

- Pure standard library. Flag explicitly before adding any third-party runtime dep.
- Tunable real-world constants (maintenance intervals, duty limits, depreciation)
  live as data on specs/models, not hardcoded in logic. Keep it that way.
- Prefer honesty over green tests: if a change introduces an approximation or a
  known-wrong edge, say so in comments and in the response.
- Cross-module imports inside the package are absolute (from airlinesim.crew
  import ...). Several are intentionally inline to avoid import cycles.

## How to run

    airlinesim demo --days 60        # built-in two-carrier sim
    airlinesim list                  # list scenarios
    airlinesim run integration       # full-stack pass/fail check
    airlinesim run btsdata           # BTS ingest check (offline, fixtures)
    airlinesim probe --offline       # the same ingest probe, raw report
    airlinesim run routedata         # 3-tier route data check (offline, snapshot)
    airlinesim run databuilt         # engine running on real BTS routes
    airlinesim run refresh_cx        # corpus-refresh logic (offline)
    airlinesim refresh --check-only  # is the corpus stale? what needs re-export?
    airlinesim demo --data --hub ORD # data-driven demo instead of constants
    airlinesim ingest --t100-market T_T100D_MARKET_ALL_CARRIER.zip \
        --fetch-airport-ref --distill   # warehouse + regenerate the snapshot

The `integration` scenario is the closest thing to a test suite — it wires every
subsystem and asserts six invariants. Run it after any engine change.

`python tools/smoke_windows_bundle.py` is the wider net: every self-checking
scenario (grepping for `ALL CHECKS PASS`, since scenarios signal failure in
their *output*, not their exit code), the report-only scenarios, the CLI, and a
live GUI server fetch. Runs on any OS despite the name. It requires the package
to be **installed** and runs every subprocess in a temp directory, deliberately:
`python -m airlinesim.cli` puts the working directory on `sys.path`, so a run
from the repo root imports the checkout and the install is never tested — which
is how `btsdata/fixtures/*.csv` shipped missing from the wheel. Any new non-`.py`
file under `airlinesim/` needs a matching `[tool.setuptools.package-data]` entry.

## Windows releases

`.github/workflows/windows-release.yml` builds a portable Windows bundle on tag
push (`v*`), on manual dispatch, and on PRs touching the engine or build tooling.

- The bundle is the official **embeddable CPython** zip + the package installed
  into `python\Lib\site-packages` + `.bat` launchers. Not PyInstaller, not a
  zipapp: `routedata.DATA_DIR` and `server.WEBUI_DIR` are `__file__`-relative
  and need real files on disk. Keep it that way or rewrite both to
  `importlib.resources` first.
- The launchers set `PYTHONUTF8=1` and `chcp 65001`. The engine prints em
  dashes, arrows and `R²`; under Windows' legacy code pages those raise
  `UnicodeEncodeError` as soon as output is piped or redirected.
- A tag must match `pyproject.toml` *and* `airlinesim/__init__.__version__` —
  the workflow refuses to publish an artifact whose name disagrees with the
  version inside it. Bump both when releasing.
- The embeddable zip is fetched from python.org and its sha256 is pinned in
  `KNOWN_SHA256`. Bumping the bundled CPython means adding the new digest
  (cross-checked against python.org's published sums) — a mismatch is meant to
  stop the build, not to be worked around by dropping the pin. A version with no
  entry still builds and prints its digest so it can be added.

## Historic route data (in progress)

Route modeling is being extended to use real BTS data with a comparable-route
fallback. Design and phased plan: `docs/route-data-design.md` and
`docs/route-data-plan.md`. Read those before touching `btsdata/` or `route.py`
demand code.

- `airlinesim/btsdata/` is the dev-time ingest and is **never** imported by
  runtime code — the simulation will read distilled artifacts instead.
- **T-100 has no stable URL.** It is not in `/PREZIP/` at all; it comes out of the
  TranStats field-picker as a per-request session export. So the working pattern
  is export by hand, then `airlinesim ingest --t100-market <zip>`. DB1B and
  OurAirports URLs *are* confirmed live and pinned in `download.py`.
- **T-100 Market ≠ Segment.** Market (what's loaded today) has passengers but no
  SEATS/departures/aircraft type, so load factor, de-censored demand and the
  seat window are unavailable until a Segment export lands.
- Warehouse state: T-100 Market 2023–2025 (749,662 rows, 36 monthly partitions,
  43,170 directional pairs) + OurAirports. Fares (DB1B) not yet loaded.
- The warehouse is derived and gitignored — rebuild with `airlinesim ingest`.
  The **snapshot** in `airlinesim/data/` IS committed: 300 airports, 6,720
  routes, ~364 KB.
- `routedata.RouteDataProvider` serves three tiers: EXACT (measured pair),
  COMPARABLE (gravity estimate from endpoint sizes), SYNTHETIC (engine
  defaults). Every generated RouteSpec carries `data_tier` + `data_vintage`.
- Tier-2 accuracy is cross-validated and travels in `data/gravity.json`:
  median predicted/actual 1.004, 60.6% within 2x, 78.2% within 3x.
- `routedata.py` must NEVER import `btsdata`. Shared logic
  (`gravity_features`, `seat_window`) lives in `routedata` and `btsdata`
  imports it, so the fit and the evaluation can't drift apart.
- **Refresh is split by what can actually be automated.** DB1B and OurAirports
  have stable URLs and refresh unattended via
  `.github/workflows/bts-refresh.yml` (monthly). T-100 cannot — so the workflow
  reports staleness and says what to re-export instead of shipping a stale
  corpus. `airlinesim refresh` diffs the new snapshot against the committed one
  and **refuses to write** one that loses data (Segment→Market, >10% of routes,
  or a drop in fare/connecting coverage); pass `--allow-regression` to override.
- Gravity coefficients are **withheld** below 200 routes or non-positive R², so
  a corpus too small to fit resolves unknown pairs SYNTHETIC rather than serving
  a fabricated comparable.
- Fares come from DB1B **nonstop markets only** (`market_coupons = 1`): a market
  fare covers a whole journey, so attributing a one-stop fare to one leg would
  overstate it. Connecting share comes from **coupons** (a coupon is a segment).
  `-1.0` in `connecting_share` means *unknown*, not zero.
- `databuilder.build_world_from_data(hub, n_destinations)` stands up a full
  two-carrier world from the corpus through `SpecRepository.load()`. Equipment is
  chosen per route from the data-derived seat window, so a thin pair gets an E175
  and a transcon a widebody. Verified by `airlinesim run databuilt`.
- Crew pools there are sized `ops_at_base * CREW_DEPTH`; a flat two-per-base
  silently grounded half a carrier with "no legal crew available".
- Frequency is derived from measured demand and capped by airframe hours. Cabin
  duty limits then trim it further on most trunk ops — the data-implied frequency
  genuinely meets the duty envelope.

## Known limitations (accurate — don't "fix" silently)

### Engine

- Maintenance intervals, depreciation rates, duty limits are industry-*shaped*
  defaults for game balance, NOT certified figures.
- Route demand splits into business/leisure/connecting segments, each its own
  priced, capacity-bound pool resolved independently by the arbiter. Each
  segment's demand is partitioned across its cabin(s) by a fixed fraction
  (business -> 12.5% FIRST / 87.5% BUSINESS; leisure -> 15.2% PREMIUM / 84.8%
  ECONOMY; connecting -> 100% ECONOMY). Those split fractions are global
  game-balance defaults matched to `DEFAULT_SEAT_CLASSES`, not per-route or
  certified — see `route.py`'s `SEGMENT_CABIN_SPLIT`.
- Crew deadheading is direct-to-base only; no multi-hop routing or ferry flights.
- The bundled AI adjusts price/frequency but doesn't use route suitability to
  right-size equipment.
- Roster is conservative — can leave capacity unflown.
- **Use `Bank.try_acquire()`, not `Bank.acquire()`**, unless you need the
  Loan/Lease object. `acquire()` returns None both for a denial AND for a
  successful `BUY_CASH`, and it leaves attaching the Airplane to the caller;
  three call sites got that wrong and flew aircraft that were never paid for,
  overstating net worth by whole airframes. Every call site in the package now
  uses `try_acquire()`. Attach the Airplane only when it returns True.

### Historic route data — what is measured, derived, and guessed

`airlinesim/data/MANIFEST.json` tags every field. Summary:

**MEASURED** (straight from BTS): passenger volumes, distance, the 12 monthly
multipliers, runway lengths, per-airport inbound/outbound traffic. Plus, when
DB1B is loaded: nonstop-market fares and per-segment connecting share.

**DERIVED** (a stated transformation): the single-harmonic seasonal fit, the
gravity coefficients, and de-censoring where capacity exists.

**HEURISTIC** (no public dataset — game balance):
- `total_gates` and `fuel_supply_per_day_l` — scaled off passenger volume.
- `min_runway_m` as a route *requirement* — banded by stage length. The
  airports' actual runway lengths are measured; what a route *requires* is not
  in any dataset and can't be inferred without aircraft types.
- The economic seat window — derived from a plausible daily-frequency band
  (0.7–20 departures at 85% load factor), standing in for the
  seats-per-departure distribution a Segment export would give us.
- `databuilder`'s `CARRIER_MARKET_SHARE`, `DAILY_UTILIZATION_H`, `CREW_DEPTH`.

**Specific caveats that must not be "fixed" silently:**

1. **Demand is CENSORED in the shipped corpus.** T-100 passengers are those
   *flown* — `min(demand, capacity)` after the carrier already optimized. The
   shipped corpus is T-100 **Market**, which has no `SEATS`, so there is nothing
   to de-censor against and demand is understated on full routes. A T-100
   **Segment** export fixes this and is the single highest-value missing input.
2. **`dow_profile` is not from data.** T-100 is monthly; nothing in it informs
   day-of-week. It stays `route.py`'s constants.
3. **Business-vs-leisure is not measured.** With DB1B coupons loaded, the
   *connecting* share is real; business vs leisure is then just a split of the
   remainder in the caller's ratio. No BTS source carries trip purpose.
4. **Fares are journey fares, restricted to nonstop markets.** DB1B market fare
   covers a whole itinerary, so only `market_coupons = 1` rows are used. It is a
   10% sample, so thin pairs are noisy, and fares are in their own year's dollars
   with **no deflator**.
5. **Fare vintage lags volume vintage.** DB1B collection ended Q2 2025; the
   manifest records the two windows separately rather than implying one.
6. **Tier-2 is a fitted estimate, not a measurement.** Cross-validated at median
   predicted/actual 1.004, 60.6% within 2×, 78.2% within 3× — so roughly a third
   of comparable routes are off by more than 2×. A more accurate variant with a
   size-interaction term was **rejected**: it inverts the origin-size elasticity
   for destinations below ~1,100 inbound pax/day (51% of corpus airports), and a
   model where a route thins out as its origin grows is qualitatively wrong.
   `scenario_routedata` asserts monotonicity so it can't creep back.
7. **Gravity is withheld below 200 routes or non-positive R².** A 7-parameter fit
   on 20 routes returns R² = −3044; unknown pairs then resolve SYNTHETIC rather
   than being served a fabricated comparable.
8. **`/PREZIP/` is undocumented** and can vanish. T-100 is not there at all — it
   is a TranStats field-picker session export whose URL is a receipt, not a
   channel. OD40/DB1C (which replaced DB1B) needs its own reader.
9. **The 2015 vintage question is moot** — the loaded export turned out to be
   2023–2025, so the corpus is the intended recent post-COVID window.

## Good next steps (from prior design discussion)

- Make the segment/cabin split fractions (`SEGMENT_CABIN_SPLIT`) tunable per
  route instead of a single global default, if different markets should have
  different premium-cabin propensities.
- Multi-hop / ferry crew positioning.
- Make the AI read suitability + per-route P&L to choose aircraft and cabins.
- Resale / used-aircraft market feeding the existing depreciation + retirement.

## Suggested first task for a new session

Run `airlinesim run integration` to confirm the stack is green, then read
engine.py's SimulationEngine.tick and the Subsystem classes to see the pipeline
before making changes.

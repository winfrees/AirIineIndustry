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
      explorer.py       # outcome-space exploration: fork a state, branch it,
                        #   run N cycles, test a derivation, repeat
      gamelog.py        # rotating file log for a live session (debug aid)
      crew.py           # duty/rest limits, rostering, positioning, deadheading
      route.py          # market segments, stage economics, equipment/crew suitability
      finance_cabin.py  # cabin classes + seat layout; financing/banking; depreciation
      cabin.py          # cabin GEOMETRY: pitch/abreast per class, row-snapping
                        #   seat fitter, named cabin presets
      weather.py        # probabilistic geographic weather: climate from lat/lon,
                        #   a stochastic process of moving systems, per-airport sky
      disruption.py     # what weather COSTS: cancellations, delays, crew
                        #   timeouts, stranded pax, hotels, airport reliability
      alliance.py       # co-ops/unions: connecting FEED, alliance tiers,
                        #   no-compete hubs
      merger.py         # valuation, synergy, the three merger rationales, and
                        #   the "neither can compete alone" test
      builder.py        # build_demo_world() / run() convenience entry points
      cli.py            # `airlinesim` command (list / run / demo / probe)
      routedata.py      # RUNTIME provider: 3-tier historic/comparable lookup
      databuilder.py    # build_world_from_data(): a world from the BTS corpus
      geomag.py         # magnetic declination from the committed World
                        #   Magnetic Model; orients the map, nothing else
      data/             # committed distilled snapshot (routes/airports/gravity)
                        #   + basemap.json, the Natural Earth US base map
                        #   + wmm2020.cof, the World Magnetic Model
      webui/            # the browser front ends: index.html/app.js (the game),
                        #   explore.html/explore.js (the outcome explorer),
                        #   map.js (the network map)
      btsdata/          # DEV-TIME BTS ingest (schema/download/readers/warehouse/
                        #   ingest/distill/discover/probe + fixtures). Never
                        #   imported at runtime.
      scenarios/        # runnable demos, each with a main()
    tools/              # DEV-TIME build tooling (never imported by the package):
                        #   build_windows_bundle.py  portable Windows build
                        #   smoke_windows_bundle.py  scenario/CLI/GUI smoke test
                        #   build_basemap.py         Natural Earth -> basemap.json
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
    airlinesim run explorer          # outcome-explorer + engine-determinism check
    airlinesim run cabin             # cabin geometry, seat fitting, per-cabin fares
    airlinesim run weather           # clock resolution, weather, disruption chain
    airlinesim run alliance          # feed, alliances, valuation, mergers
    airlinesim run map               # base map, clipping, and the map's data seam
    airlinesim run session           # real-time clock guard + log rotation
    airlinesim gui                   # play it in a browser; defaults to --world data
    airlinesim gui --world demo      # the two-airport sandbox instead
    airlinesim explore               # the outcome-explorer GUI (same server as `gui`)
    airlinesim refresh --check-only  # is the corpus stale? what needs re-export?
    airlinesim demo --data --hub ORD # data-driven demo instead of constants
    airlinesim ingest --t100-market T_T100D_MARKET_ALL_CARRIER.zip \
        --fetch-airport-ref --distill   # warehouse + regenerate the snapshot

The `integration` scenario is the closest thing to a test suite — it wires every
subsystem and asserts six invariants. Run it after any engine change. Run
`explorer` too: it is what pins the engine's determinism, which nothing else
checks.

`python tools/smoke_windows_bundle.py` is the wider net: every self-checking
scenario (grepping for `ALL CHECKS PASS`, since scenarios signal failure in
their *output*, not their exit code), the report-only scenarios, the CLI, and a
live GUI server fetch. Runs on any OS despite the name. It requires the package
to be **installed** and runs every subprocess in a temp directory, deliberately:
`python -m airlinesim.cli` puts the working directory on `sys.path`, so a run
from the repo root imports the checkout and the install is never tested — which
is how `btsdata/fixtures/*.csv` shipped missing from the wheel. Any new non-`.py`
file under `airlinesim/` needs a matching `[tool.setuptools.package-data]` entry.

## The real-time clock, and why it refuses to catch up

`GameSession._loop` converts real seconds into sim-days. That conversion has
to be guarded, because the wall clock is not monotone with *attention*: if the
machine sleeps, the process is frozen and the gap is real time nobody played.

Replaying it was a real bug. Three hours with the lid shut is ~5,400 sim-days
at the default 0.5 days/s — about fifteen years, delivered to the engine in one
55-second locked burst. Everything downstream was CORRECT: 84-month leases
expired on schedule, and `BankingSubsystem` did what it should, handing the
metal back and closing the routes those tails flew. The player simply wasn't
there to re-lease. AI carriers looked immune only because they re-acquire on
their own review cycle. **The lease teardown is not the bug and must not be
softened** — the clock is.

So: a gap over `SUSPEND_GAP_S` (5 s, ~25 poll intervals) is DISCARDED, the
session pauses itself, and `clock_notice` says how much was skipped — cleared
by `resume()`, so the banner survives a page reload. Smaller gaps are still
clamped by `MAX_CATCHUP_S` and `MAX_TICKS_PER_WAKE`, because the session lock
is held for the whole catch-up burst and an unbounded one freezes the command
API and the SSE stream with it. A raise inside the loop now pauses and logs
instead of killing the thread, which used to leave a frozen-but-healthy GUI
with no error anywhere. `airlinesim run session` pins all of this.

## Session logging

`gamelog.py` is a `RotatingFileHandler` on the `airlinesim` logger, defaulting
to `~/.airlinesim/logs/airlinesim.log` at 4 MB x 6 files. Off unless a caller
configures it, so scenarios and the explorer are unaffected; `airlinesim gui`
turns it on and prints the path (`--log-file/--log-level/--log-max-mb/
--log-backups/--no-log`).

It logs DECISIONS and EVENTS, never ticks: human commands with their outcome
(including refusals), AI moves through `ai._note`, lease expiry in
`engine.py` — the one place the engine takes assets away unasked — clock
anomalies, and swallowed exceptions. Measured volume is ~8.2 KB per 1,000
sim-days with three AI carriers (2,000 days -> 16,411 bytes, 104 lines), so a
24-hour session at default speed is ~0.4 MB and the 24 MB cap is roughly
sixty days of continuous play. If you add logging to a per-tick path, that
arithmetic stops holding — summarise instead.

## Outcome explorer (second GUI)

`explorer.py` + `webui/explore.html` answer "what is the shape of everything
that could happen?", where `game.py` answers "what happens in the run I'm
playing?". Both front ends are served by the same `server.py` — the game at `/`,
the explorer at `/explore.html`.

- **It rests on the engine being deterministic.** There is not one `random` call
  in `engine.py`, so a forked state re-run with the same edits gives a
  byte-identical result. That is what makes a tree of branches a *map* rather
  than noise. `airlinesim run explorer` asserts it, and is the only scenario
  that does — adding nondeterminism to a subsystem turns that check red, which
  is the intended alarm, not a flaky test.
- A **node** is a forked `(world, engine, ctx)` triple pickled to a blob, plus
  the metrics projected off it. An **edge** is "apply these mutations, then run
  N cycles". One cycle is one `engine.tick`, i.e. `engine.dt` = 24h = one day.
- Forking is `pickle`, the same mechanism `GameSession.save/load` uses. It costs
  ~11 KB per node on the demo world (~60 KB once a run has accrued state), so
  the tree is capped at `MAX_NODES` (400) rather than growing until the process
  dies. `sweep()` and `expand()` check the cap up front instead of leaving a
  half-built tree behind.
- `game.build_game_world()` is the shared seam: the explorer roots its tree from
  the exact world `new_game()` plays, minus the background thread. Don't add a
  second world constructor for the explorer — it will drift.
- **Derivations are an AST whitelist, not a sandbox around `eval`.** Every node
  is checked against `_ALLOWED_NODES` before anything compiles, so an expression
  can't reach an import, a dunder, or any call but `abs/min/max/round`. This
  matters because the server binds `0.0.0.0`: a derivation box wired to a bare
  `eval()` would be remote code execution for anyone on the LAN. Extend the
  whitelist deliberately, never by widening it to "whatever the user typed".
- Adding a knob means one entry in `MUTATION_KINDS` — the HTTP layer and the
  GUI's target pickers are both driven off that table. Add a probe for it in
  `scenario_explorer`'s sensitivity check at the same time (see below).

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

## Time resolution, weather and disruption

Design and the road to real data: `docs/weather-design.md`. Read it before
touching `weather.py`, `disruption.py`, or anything that scales with `dt`.

- **`engine.dt` is HOURS PER TICK and is a real knob.** The engine is
  dt-independent: a simulated month gives the same carriage and cash at 24 h,
  6 h or 1 h resolution, and `airlinesim run weather` asserts it. Scenarios
  keep the 24 h default; a played game runs hourly.
- **Anything that consumes a DAILY budget must scale by `dt / 24`.** Two
  things didn't, and both broke the moment ticks were sub-day: the gate claim
  asked for a whole day's frequency every tick (an airport's gates were gone
  before noon, every carrier's frequency collapsed to zero), and a deadheading
  crew's seat reservation was subtracted whole from a tick-sized cabin (6 of
  the 7.5 seats an hourly tick offers). Both are fixed and pinned.
- **`GameSession.speed` is sim HOURS per real second**, not days. Rate and
  resolution are independent knobs — the GUI exposes them as *speed* and
  *detail*. Saves written with the old day-rate are converted on load
  (`_LEGACY_MAX_DAYS_PER_S`); without that a resumed game runs 24x slow and
  reads as frozen.
- **Weather is PROBABILISTIC but REPRODUCIBLE, and the distinction is the
  whole design.** `WeatherModel.advance()` is a stochastic process: each tick
  it retires dead systems and rolls for new ones, so a player cannot learn
  next week's storms and two playthroughs of the same opening diverge. The
  draws come from `WeatherModel.rng` and the live systems live on the model —
  BOTH pickle with the world, so a save resumes into the weather it would have
  had and an explorer fork replays its parent's season exactly. That is what
  `scenario_explorer`'s "identical branches produce identical outcomes" check
  actually needs: reproducibility on fork, not predictability. `engine.py`
  still contains no `random` call; the randomness lives in state the world
  owns. Never reach for `hash()` here — it is salted per process.
- **A new game draws a fresh seed** (`DEFAULT_WEATHER_SEED = None`), so each
  playthrough gets its own season. The explorer attaches at a FIXED seed
  (`EXPLORER_WEATHER_SEED`) instead, because two sibling branches that both
  switch weather on have to face the same season or the comparison between
  them is noise rather than a result.
- **The explorer can switch weather on or off at ANY node** (`weather` = 0/1,
  which attaches a model to a clear world if there isn't one) and can STAGE a
  named event at an airport (`weather_<kind>`, one knob per `WeatherKind`,
  generated from the enum so the two can't drift). A staged event obeys
  GEOGRAPHY but not the calendar — a blizzard at ORD in July is a legitimate
  what-if, a hurricane at ORD is refused with a reason rather than staged as a
  silent no-op. Its `intensity` is what gets DELIVERED at the target, so the
  system is sized to overcome the local susceptibility gate.
- **The sky is averaged ACROSS a tick, not sampled at its first instant**
  (`WeatherModel.over()`). A thunderstorm lives ~6h: sampled once per 24-hour
  tick it was usually born and dead between two looks, so a coarse run saw
  almost no weather and the explorer's weather knob looked inert.
- **Spawn draws scale with `dt`, and a long tick gets MULTIPLE draws** rather
  than one draw at a scaled-up probability — folding the scale into a single
  Bernoulli saturates at `p > 1` and quietly produced *less* weather at coarse
  resolution than at fine.
- **Weather is opt-in** (`disruption.attach_weather(world, engine)`), ON for a
  played game and OFF for every existing scenario, which is what keeps them
  comparable. It needs geography: airports with no lat/lon get no weather, so
  the demo sandbox has none and the corpus world does.
- **Ordering matters.** `WeatherSubsystem` runs FIRST and only annotates ops
  with the capacity/delay they face; `DisruptionSubsystem` runs LAST, after
  Operations has recorded what actually flew. Operations stays the single
  authority on how much flying happens — a version that cancelled flights in
  the weather subsystem would have two authorities on one number.
- **The indirect path is the point.** Weather delay is added to
  `fh_per_rotation` inside the crew-legality gate, so a delay eats the duty
  day and the *next* rotation is the one that cancels. The delay is cheap; the
  crew it strands is not.
- **Climate is derived from MEASURED lat/lon; everything else is HEURISTIC.**
  No weather record is committed to this repo. Five calibration errors were
  fixed and are documented in the design doc — the two worth remembering are
  that continentality and hurricane exposure must be measured to the OCEAN
  (measuring to any water made Chicago maritime and gave it hurricanes), and
  that convection needs a Gulf-moisture term or Los Angeles storms like
  Atlanta.
- **Rebooking is same-tick, same-market only** — about 7% of stranded
  passengers on a corpus world, the rest refunded. That OVERSTATES the cost;
  a real recovery model carries passengers forward over days and searches
  alternate routings. Listed with the other honest limits in the design doc,
  along with the big one: a cancelled flight leaves its aircraft in the right
  place anyway.
- **All of it is now VISIBLE, which it wasn't for two releases.** Every figure
  above was in `/api/state` and rendered nowhere: the weather work was
  complete in the engine and invisible in the product. The Routes table has a
  `Wx` column (sky, capacity lost, delay added, frequencies cancelled), the
  Airports card shows the live sky and the cumulative RELIABILITY record with
  what it has cost, each Carriers card shows its disruption tally, and the
  network map draws the systems themselves. The reliability column is the
  "this hub costs you every winter" number the whole feature exists to
  produce — on a corpus world ORD lands around 68% after forty days. Don't
  add a subsystem that computes something and stop at the snapshot.

## Alliances and consolidation

Design, limits and the historic-data question: `docs/consolidation-design.md`.

- **Connecting demand has to be FED.** `CONNECTING` existed as a segment but
  was carried as if it were local, so a hub was worth no more than a spoke and
  an alliance was worth nothing. `alliance.feed_factor()` scores what departs a
  leg's DESTINATION: own metal in full, a partner's at the alliance tier's
  efficiency, a **stranger's at nothing** — which is the entire commercial case
  for allying. It reaches the arbiter through the existing `desirability` seam,
  so no allocation logic changed.
- **Feed is a connectivity INDEX, not an itinerary ledger.** No passenger is
  traced to a final destination and connections are one stop only. It is a
  deliberate stand-in for an O&D itinerary model the engine doesn't have; the
  design doc says what that would take and what it would replace.
- **A partner's return leg is not feed.** Nobody connects onto the flight back
  where they came from — counting it made every out-and-back pair look like a
  hub. `onward_capacity(..., exclude_dest=)`.
- **Alliance actions need the player roster, which World doesn't hold.**
  `register_players()` is called at attach time AND every tick; when it was
  only set during a tick, `form_alliance` before the first tick silently
  refused and `blocks_route` silently allowed.
- **Allying costs something** — dues per day, a connecting passenger worth
  less than a local one, and `no_compete_hubs` that genuinely block a member
  from a route a partner flies. Two carriers at the SAME hub gain almost
  nothing and still pay: complementary networks are what pay off.
- **Valuation is itemised** and floored at liquidation value; a loss-making
  carrier carries NO going-concern value. It reads the AI's smoothed operating
  cash flow, never `RouteOp.last_profit` — that's a contribution margin and
  excludes lease rent, loan service, payroll and hub overhead.
- **Three merger rationales** (HORIZONTAL / COMPLEMENTARY / SURVIVAL), and
  SURVIVAL is the only one that can approve weak synergies. It is gated on
  `Position.cannot_compete_alone()`: outmatched by a leader with 2x your share
  AND sub-scale or short of runway. **Being small is not enough** — a healthy
  niche carrier is small on purpose, and the scenario pins that the same
  carrier flips viable purely on the sign of its cash flow.
- **A merger transfers the DEBT too.** Overlap is DIRECTIONAL: a duplicated
  ORD->LGA does not make LGA->ORD redundant, and treating routes as unordered
  pairs consolidated twelve legs where six markets overlapped.
- **There is no regulator**, and that is the biggest gap: real horizontal
  mergers get blocked or conditioned on divestitures, here they only get
  expensive.
- **AI carriers consolidate among THEMSELVES and never buy the human out.**
  Losing your airline to a takeover you were never asked about is an
  unanswerable loss, not a difficulty — the human is always the initiator. A
  bid/accept flow is the natural extension. `scenario_alliance` drives the
  AI's own review against a flush AI and a desperate human to prove it holds.
- **The GUI path is the feature.** It shipped once with the actions written,
  the AI using them, and NOTHING reachable by a player: `attach_alliances()`
  was never called in the game path, so the subsystem wasn't attached, feed
  did nothing, and the actions would have failed on an empty player roster.
  `scenario_alliance`'s wiring section now asserts the whole chain —
  subsystem attached, `GameSession` method present, `server.COMMANDS` entry
  present — for every one of them. **A feature only the scenario can reach is
  not delivered.**

## Crew rest and rotations (the bug that looked like AI collapse)

A player reported that AI carriers "contract to one airplane". They weren't
downsizing — they were **crew-starved**, and four separate faults stacked into
one failure mode. All four are fixed; none of them should be re-broken.

- **`crew._all_crews()` omitted the ROSTERING POOLS.** Rest is banked by
  `CrewLegalitySubsystem` for crews that didn't fly, but it walked only
  `player.crews` plus crews attached to a route op. A pool crew that wasn't
  rostered this tick was attached to nothing, so it banked nothing. That is a
  one-way ratchet: a crew flies (log_flight zeroes its rest), the roster skips
  it next tick because it is resting, and from then on it is invisible to the
  only code that could ever clear the rest — stuck at "resting (1.0/10h)"
  FOREVER. Pools drained to one or two usable crews and whole networks went to
  zero load factor. `engine.tick` already walked the pools for the daily
  counter roll; this one place didn't.
- **Rest was owed per TICK, not per DUTY PERIOD.** `duty_before_rest_hours`
  was declared on `DutyLimits` and never read — "did not fly this tick" stood
  in for it. That makes the rule depend on tick SIZE: at dt=24 a crew flies a
  whole day's rotations then owes one rest; at dt=1 the same schedule bills it
  ten hours of rest for four minutes of flying, so it works one hour in
  eleven. Wiring the field in is what restores dt-independence.
- **The deadhead logged a whole leg's duty every tick** (`dh_hours =
  dist/speed`) while flight hours were dt-scaled — the same per-day-budget-
  spent-per-tick class as the old gate bug. Now `* (dt/24)`.
- **The AI opened one-way legs.** Combined with direct-to-base deadheading,
  every crew it rostered flew out once and was stranded at a spoke. What that
  looked like from outside was a churn loop: acquire, open a route, fly
  nothing, get declared idle after `idle_days_before_shedding`, hand the lease
  back at an early-termination penalty, repeat — several million dollars a
  cycle, ending at one aeroplane. `_open_rotation` / `_close_rotation` now
  open and close the leg AND its return on the same tail, network caps count
  ROTATIONS not legs, and `_with_return_legs` pairs the seeded route too.
- **`_track_health` called an aircraft idle when it merely hadn't flown.** An
  aircraft with a schedule it couldn't crew is not spare capacity; the answer
  is to hire, not to hand the metal back. Idle now means UNASSIGNED.
- **`_staff_up` sized hiring off `len(route_ops)`.** A route is not a unit of
  crew demand — seven daily rotations of a 1.4-hour leg is more than one crew
  can legally fly. `_crew_target` derives the pool from scheduled BLOCK HOURS
  over `max_daily_flight_hours × CREW_DEPTH`, and hiring closes the gap in
  real steps instead of two per review.

`airlinesim run weather` pins the dt-independence, and now also pins that the
residual spread is ONLY rest quantisation: with a permissive duty envelope the
three resolutions agree to under 0.5%. A 24-hour tick genuinely cannot
represent "ten consecutive hours of rest" — it grants twenty-four — so the
daily run is slightly optimistic about crew availability. That is a stated
resolution limit, not a leak, and the check says which is which.

## Cabins: geometry, fitting and per-cabin fares

`cabin.py` answers "what physically fits in this airframe?"; `finance_cabin.py`
still answers "what is a seat in this class worth?". Keep that split.

- **The model is one-dimensional on purpose.** A cabin is a box of fixed width
  and length; width is already captured by ABREAST (seats per row), so the
  constraint collapses to `Σ rows × pitch ≤ cabin_length_m`. Floor area is
  `length × width` — this IS the area model, width just cancels. Seats come in
  whole rows because that is how they are installed.
- **Footprints are derived, not asserted.** `(pitch_c/pitch_Y) ×
  (abreast_Y/abreast_c)` — so a business seat costs ~2.2 economy seats on a
  6-abreast narrowbody and ~4.2 on a 9-abreast widebody, because the economy it
  displaces is denser. The flat `DEFAULT_SEAT_CLASSES.footprint` table can't
  express that and remains only as the legacy fallback for callers with no
  aircraft spec in hand.
- **`cabin_abreast` on `AircraftSpec` is the one MEASURED input** (published
  economy seats per row). `cabin_length_m` is DERIVED, back-computed from
  `max_seats` at economy pitch so that all-economy == `max_seats` exactly —
  it is not a fuselage dimension and must not be presented as one. Pitch tables
  and premium-abreast fractions are HEURISTIC game balance. A spec with no
  `cabin_abreast` gets a banded estimate, flagged `abreast_source="estimated"`
  all the way out to the UI.
- **`max_seats` doubles as the certified occupancy limit.** The derived cabin
  rounds up to a whole row, so without that cap an all-economy 787-9 would come
  out at 297 seats on a 290-seat type. It only ever binds on a single-class
  cabin — a premium seat eats more length per seat, so a mixed cabin is under
  it by construction. `airlinesim run cabin` asserts this for every type.
- **The fitter never rejects for size.** `fit_layout` snaps to whole rows,
  trims overflow cheapest-cabin-first (economy yields, first class doesn't),
  and fills unspecified economy with whatever is left — then reports every
  adjustment in `CabinFit.notes`. Nothing is changed silently. Blank economy
  means "fill it", which is the auto-calculation the whole feature exists for.
- **One fitter, three entry points.** Acquisition, recabin and the per-op
  layout override all go through `actions.build_layout` -> `cabin.fit_layout`,
  and the GUI previews through `GET /api/cabin` -> the *same* function. The
  browser deliberately owns no geometry: a preview that disagreed with the
  installed cabin would be worse than no preview.
- **Do not set an HTML `max` on the seat inputs.** It makes the browser refuse
  to submit an over-large number, replacing the fitter's "here is what fits and
  why" with a bare tooltip. The maxima are displayed instead.
- **Per-cabin fares live on the route** (`RouteOp.cabin_prices`), not on the
  fleet: what a business seat is worth is a property of the market. A cabin
  with no entry falls back to `ticket_price × price_multiplier`, so a route
  nobody has priced by cabin behaves exactly as it did before — and
  `set_cabin_price` refuses a cabin the assigned aircraft doesn't have, rather
  than storing a fare against seats that don't exist. Recabining a tail out of
  a cabin clears that cabin's fares on its routes and says so.
- **`databuilder._layout` used to subtract business seats from `max_seats`
  one-for-one**, which installed cabins no fuselage could hold (26 lie-flat
  seats displace far more than 26 economy seats) — those aircraft flew with
  capacity that didn't exist. It goes through the fitter now; `airlinesim run
  cabin` pins the old arithmetic as over-capacity so it can't come back.

## The network map (third front end)

`webui/map.js` draws the game on a US map: `tools/build_basemap.py` distils
Natural Earth into `airlinesim/data/basemap.json`, `server.py` serves it at
`/api/basemap`, and `map.js` overlays routes, aircraft, airports and weather.
`airlinesim run map` pins the corpus, the clipping and the data seam.

- **The map is NOT radar, and the GUI says so in the panel.** The engine
  models a daily FREQUENCY smeared across the tick — there is no aircraft
  object with a departure time and a position. So the icon count, its
  direction and its ground speed are real (one per operating route, phase =
  sim clock modulo the leg's `block_h`, hence pixels-per-hour ∝ cruise
  speed), but the aeroplane at a given point is a rendering of the schedule.
  Presenting derived positions as tracked flights would be the most
  misleading thing in the GUI; don't quietly drop the note.
- **`eff_freq` in the op snapshot is what decides whether anything is drawn.**
  It was missing at first, so `o.last_eff_freq === 0` compared against
  `undefined`, never fired, and a crew-short carrier kept flying ghosts. A
  route that operated nothing is now drawn DASHED and faint with the reason
  on its tooltip — losing the icons silently just looked like a rendering
  bug. `scenario_map` asserts both branches are reachable in a real run.
- **Natural Earth is PUBLIC DOMAIN**, which is the only reason a vector base
  map can be committed here at all. The attribution travels in the JSON and
  the scenario checks it is still there.
- **Rings must be CLIPPED to the window, not filtered by it.** Natural Earth
  carries North America as one ring, so "keep the ring if any point is
  inside" kept Canada and Mexico in full and drew them straight across the
  frame. `clip_ring` (Sutherland-Hodgman) and `clip_line` fix it; simplify
  runs BEFORE the clip so the frame edge stays exact and no sliver of
  background shows along it. `scenario_map` asserts every layer is inside the
  bbox — that check is what catches a rebuild that loses the clip.
- **It is geography, NOT terrain relief.** Land, coast, lakes, rivers, state
  lines and Interstates are all vectors. Shaded relief needs an elevation
  raster (ETOPO/SRTM), tens of megabytes before it is an image, and none is
  committed. The docstring, the README and the GUI note all say this; calling
  a flat vector map "terrain" is exactly the overclaim this project's docs
  exist to prevent.
- **The window is the lower 48** (`BBOX`). The corpus has 29 airports outside
  it — Alaska, Hawaii, Guam, Saipan, Puerto Rico. They are NAMED in the
  legend ("off window: ANC HNL …") rather than projected somewhere wrong.
  Note GUM and SPN are EAST longitude, so any "is this a US coordinate?"
  test that assumes `lon < 0` is wrong.
- **NORTH IS UP ONLY BECAUSE THE NORTHING IS NEGATED.** The textbook Albers
  formula is `y = rho0 - rho*cos(theta)`, written for a maths frame where +y
  points north; SVG's +y points DOWN, so using it unchanged draws the map
  mirrored top-to-bottom. A flipped US still reads as a plausible landmass at
  a glance, which is exactly why `scenario_map` asserts the sign instead of
  relying on an eyeball. Don't "simplify" `project()` back to the textbook
  form.
- **The map can orient to MAGNETIC north** (`geomag.py` + the committed
  public-domain `data/wmm2020.cof`), which is the convention aviation uses.
  The synthesis reproduces the WMM's three published test values to 0.04 nT,
  and `scenario_map` pins that — a double-normalised Legendre recursion gives
  plausible magnitudes with wrong signs, so magnitude alone proves nothing.
  Two limits travel with it: WMM-2020 expired at 2025.0 so anything later is
  EXTRAPOLATED (a few tenths of a degree over the US — fine for a map, not for
  navigation), and **declination is not constant across a continent**. It runs
  +16°E in Washington to −17°W in Maine, so no single rotation puts magnetic
  north up everywhere; the map orients at the projection's reference meridian
  (96°W, where variation is ~1.9°E) and the panel states the spread rather
  than implying a precision it hasn't got.
- **SVG, not canvas**, and the base map is drawn ONCE into a `<g>` that never
  changes while only the live layer re-renders per snapshot. Click handling
  then comes free, which is what makes aircraft and routes selectable without
  hit-testing geometry by hand.
- **The map is a FULL-WIDTH row and leads the page.** It is the one block
  that reads better the more width it gets. Its height is capped at 78vh — a
  map taller than the viewport buries every panel under it — but the cap is
  applied to `max-width` DERIVED from that height, not to `max-height`.
  Capping height alone leaves the box wider than the projection and the SVG
  letterboxes with dead bands down both sides: the lower 48 in Albers is about
  1.6:1 and no box shape changes that. Width follows height, aspect never
  changes, nothing is cropped or stretched.
- **The prose lives in an About dialog, not under the map.** Five lines of
  explanation under every render is what the button exists to absorb. But the
  note is NOT optional: "aircraft positions are DERIVED" is the one thing a
  viewer could reasonably get wrong. `scenario_map` therefore pins the whole
  reachability path — the button, the dialog containing the note, and the
  click handler in app.js — because "the string is in index.html" stopped
  being evidence that a player can read it.
- **`#mapSel` is live state only.** Selecting shows what is selected; nothing
  selected shows nothing. The "click an aircraft to highlight it" instruction
  is in About.
- **Alliances & Consolidation is full width too.** Its content is prose, not a
  table — a merger case carries a rationale, a price, an integration cost and
  the reason it would or wouldn't be approved. In one column those wrap into
  an unreadable ribbon.
- **Selection reaches the panels through `data-rowop` / `data-rowtail`** on
  the Routes and Fleet rows. If you re-render those tables, keep the
  attributes — the map is a control surface, and losing them turns it back
  into a poster.
- **Carrier colours are assigned by order of appearance** in `snap.players`,
  and the same `carrierColor()` drives the legend AND the swatch on each
  Carriers card, so the two views can't disagree.
- Adding a column to the Routes table means updating `cabinFareRow`'s
  `colspan` and `emptyRow`'s count in the same edit — the per-cabin fare row
  spans the whole table and silently short-runs otherwise.

## AI carriers and the action layer

`actions.py` holds every decision an airline can make as a plain function over
`(world, player, ...) -> (ok, message)`: open/close a route, acquire/sell/
break-lease/recabin an aircraft, declare a hub, hire crew, set price,
frequency and service tier. `GameSession`'s commands are thin lock-held
wrappers over it and `ai.py` calls the identical functions, so an AI carrier
faces the same equipment validation, the same `Bank.try_acquire()` credit
gate, the same fees and the same teardown a player does. It cannot cheat by
construction, and a rule change lands on everyone at once. Build-time route
opening (`builder._open_us_route`) goes through it too, so there is no
separate path that can drift.

**Start conditions.** On a data world with `ai_profiles` set, the human begins
with cash and nothing else — no fleet, no routes — so the opening decisions
are which aircraft to lease, where to base, and where to fly. Each AI begins
with exactly ONE route, chosen by `route_fit` so it suits that archetype, and
builds a network from it (`build_world_from_data(human_routes=, ai_routes=)`).

**Airport character** (`ai.airport_fit`) is inferred from two MEASURED corpus
fields, because the committed corpus has no fares to read premium-ness off:
traffic rank separates a metro's primary field from its reliever (ORD 4 vs
MDW 29), and RUNWAY LENGTH separates fields that can host a widebody premium
operation from ones that can't. The runway half is load-bearing — on traffic
alone LGA (rank 16) outranks JFK (rank 19), which would base a premium
carrier at the one New York airport that can't take a long-haul aircraft.
The result is that Legacy flies SFO->JFK and Low-Cost flies the cheaper
secondary fields, as a consequence of measurements rather than a prestige
table. Fit scales a candidate's expected share, so it is a preference, not a
prohibition: a big enough market still tempts a carrier out of its niche.

**Financial discipline.** `RouteOp.last_profit` is a CONTRIBUTION MARGIN
(revenue less that flight's fuel, crew and fees) and excludes lease rent,
loan service, payroll and hub overhead — a carrier can show every route
"profitable" while the company burns cash. So the AI manages to OPERATING
CASH FLOW, sampled off the ledger itself (`_update_cash_flow`), which by
construction includes every cost the engine charges. Stages: `healthy` ->
`freeze` (stop expanding) -> `cut` (close the worst route, trim frequency)
-> `shed` (hand back metal). Two rules keep that from becoming a death
spiral, and both were needed: a carrier below `min_viable_routes` is in
RAMP-UP and keeps investing (its overhead is sized for a network it doesn't
have yet) subject to `ramp_up_grace_days`; and a sub-scale carrier REBUILDS
rather than cuts, because you cannot cost-cut below minimum efficient scale.
Overhead sheds before capacity does — spare hubs go first, and a distressed
carrier will rebase to a cheaper field (`_rebase_if_overpriced`), which is
what lets a Regional rival stuck at ORD move to AUS/DEN and recover.

`ai.py`'s `AICarrierSubsystem` gives rivals network planning, fleet planning
(equipment chosen on cost per seat-km over the mission it actually flies),
cabin configuration at acquisition, service tiers, crew hiring and hub
selection, in three archetypes (Low-Cost / Legacy / Regional) that are one
policy engine with different weights. Any number can run at once — naming a
carrier in `ai_profiles` that isn't in the base roster adds it as a new
leasing entrant, so a three-way market is three names.

**Equipment is chosen for the MISSION, not in the abstract.** `_rank_aircraft`
orders eligible types by cost per available seat-km over the carrier's stage,
and `_pick_for_mission` then runs a route search against the top `SHORTLIST`
and buys the type whose best available route is worth the most. That is what
right-sizes: `_evaluate` caps passengers at what the market offers while still
charging the whole trip cost of a bigger aeroplane, so a widebody aimed at a
90-passenger market loses to a regional jet with no seat-count table to
maintain. It used to take the global argmin, which meant one type won for an
archetype and stage and the carrier bought nothing else all game. A modest
`COMMONALITY_BONUS` favours a type already in the fleet, because rated crews
and an existing maintenance program are worth something a trip-cost table
can't show.

**`list_price` MUST BE ONE MEASURE ACROSS THE FLEET CATALOG.** It drives
purchase, financing, lease rent, depreciation, book value AND the AI's
ownership charge, so a type priced on a different basis than its neighbours
wins or loses every comparison for a reason that isn't about the aeroplane.
The 757-200 and 767-300ER are out of production and carried their HISTORIC
list prices ($48M, $72M) against in-production types at current list
($106–317M). At 2.5–3.5x too little capital for their seat count they
dominated cost per seat-km at EVERY stage length, and all three archetypes
converged on the 757 — a fleet monoculture produced entirely by a units
mismatch. Both now carry an EQUIVALENT CAPITAL VALUE on the same basis, and
`databuilder`'s catalog header says so.

**Archetype names resolve through `ai.archetype()`, and an unknown one
raises.** `ARCHETYPES` is keyed by DISPLAY name ("Low-Cost", "Legacy",
"Regional") while the constants are `LOW_COST`/`LEGACY`/`REGIONAL`, so the
obvious `ai_profiles={"crw": "LEGACY"}` missed the dict and fell through to
the Low-Cost default — silently. Three carriers asked for three personalities
all flew the same one, at the same service tier, with the same cabins, and the
only evidence was that their numbers matched to the dollar. Both spellings now
resolve and a name matching neither is an error, not a default.

**The route search ROTATES its scan window** (`mem.scan_cursor`). A review can
only afford `candidates_per_review` evaluations, and without a cursor it
scored the same opening slice of the airport list every time: a carrier whose
first candidates didn't clear its profit bar never saw the rest of the map and
sat at its opening network for the whole game, slowly bleeding. Sorted first,
so the engine stays deterministic for the explorer.

Two balance figures in there are load-bearing rather than cosmetic.
`max_fleet` must leave headroom above the fleet a carrier STARTS with: set at
the starting size, the carrier can never acquire, so never has an idle
aircraft to deploy, so never opens a route, and reads as broken rather than
disciplined. And the price ceiling is clamped OUTSIDE the cost-plus floor —
clamping the floor last lets a thin route bid its own fare upward without
limit (costs over few passengers raise the floor, the higher fare sheds more
passengers), which reached nine-figure fares before it was caught. A cost
floor above what the market bears means the route is unviable, which
`_close_bad_routes` answers. Enable per world with
`build_world_from_data(ai_profiles={player_id: archetype})` or
`new_game(world="data", ai_profiles=...)`; with no profiles nothing in ai.py
runs. Honest limits: route evaluation is a STATIC forecast that does not
model how incumbents respond to entry (a perfectly predictive AI would be
unbeatable, and that error is the seam a player out-plans it through); it
only considers routes from hubs and current aircraft locations, so it never
opens a second base from scratch; and it never recabins after acquisition.

The GUI plays the **data world by default** (`airlinesim gui`), because that
is where the corpus, the 16-type fleet and the three AI archetypes actually
live — the demo sandbox is still there behind `--world demo`. Every action in
`actions.py` is reachable from it: route opening is a free-text origin/dest
pair over a datalist of all 300 corpus airports (a 300-row `<select>` is
unusable, and the point is that any pair is legal), cabins are planned live
against the airframe's geometry at acquisition and again in the recabin
dialog, each cabin on a route carries its own fare input, and Sell / Return /
Recabin / Close / service tier / hubs are per-row controls. `Hub.world_kind`
remembers which world the server was started with so **New Game** rebuilds
*that* world rather than silently dropping back to the demo one.

The HTTP command table in `server.py` is a hand-written argument mapping, and
that is exactly where a field goes missing: `acquire_aircraft` did not forward
`seats`, and `open_route` did not forward `service_tier`, so both were typed
into the form, sent over the wire, and dropped in the lambda — silently, since
a dropped kwarg just takes its default. **When you add a field to a form, check
its entry in `COMMANDS`.**

One deliberate asymmetry: `/api/catalog` serves all 300 airports (the route
picker needs them), but `snapshot()["airports"]` is filtered to airports the
game is actually touching — route endpoints, hubs and fleet locations. Pushing
300 gate ledgers down the SSE stream every tick and rendering a 300-row card
buries the handful that matter.

A HUB costs `hub_fee_per_day` and buys two things: it is the only place its
carrier can do maintenance (`MaintenanceEngine._find_facility`), and it gives
preferential gates there — `HUB_GATE_PRIORITY` multiplies the gate claim's
priority, so it decides only when gates are actually oversubscribed. Without
that second half a hub was pure cost and one cheap hub was always optimal,
with no reason to ever open a second. Closing your last hub while you still
have a fleet is refused: it would leave every check with nowhere to go and
quietly turn into "flying on risk" weeks later.

Airport fee schedules (gate/amenities/baggage/hub) are HEURISTIC but scaled
off measured traffic in the corpus, so a secondary field is a genuine cost
alternative to its primary. Desirability is driven by service tier only:
`AirportSpec.access_index` is the seam for real catchment data and stays 1.0
because the committed corpus has none — the LGA-vs-JFK access story is NOT
modeled, deliberately, rather than faked.

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
  certified — see `route.py`'s `SEGMENT_CABIN_SPLIT`. A per-route tilt exists
  (`RouteSpec.premium_propensity` -> `route.cabin_split_for`) but every route
  in the corpus runs at its neutral 1.0: nothing measures premium propensity.
  `docs/cabin-demand-design.md` is the plan for driving it off catchment
  income, with the options costed.
- **`MarketConditions.fuel_index` is a dead field.** It is declared on the
  dataclass and threaded through `ctx["market"]`, but no subsystem ever reads it
  — grep `engine.py` and the only hit is its own declaration. Setting it changes
  nothing. Fuel is priced off `FuelMarket.spot_price()`, which derives from
  `base_price_per_l`, so that is the knob with a real effect (and the one the
  explorer's `fuel_price` mutation drives). Either wire `fuel_index` into
  `OperationsSubsystem`'s fuel costing or delete it; leaving it as-is invites
  the next caller to "adjust the fuel market" and measure nothing.
- Crew deadheading is direct-to-base only; no multi-hop routing or ferry
  flights. **This is why a one-way route is a trap**: a crew ends its tick at
  the DESTINATION and can only get home on a leg pointing at its base, so a
  leg with no return strands its crew there permanently. `build_world_from_data`
  and `ai.py` both open routes as ROTATIONS for exactly this reason — see
  "Crew rest and rotations" below.
- The bundled AI sets only the ECONOMY base fare — the premium cabins it
  configures sell at the default class multiple, never repriced. It does now
  right-size equipment to the mission (`_pick_for_mission`), but it still
  never recabins after acquisition.
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

- Load airport-catchment income and switch the per-route cabin split on. The
  mechanism is already in (`RouteSpec.premium_propensity` ->
  `route.cabin_split_for`), sitting inert at 1.0; what's missing is the data.
  `docs/cabin-demand-design.md` costs four ways to get it and recommends one.
- Multi-hop / ferry crew positioning.
- Make the AI read suitability + per-route P&L to choose aircraft and cabins,
  and set per-cabin fares — it still prices only the economy base fare, so
  every premium cabin it configures sells at the default multiple.
- Resale / used-aircraft market feeding the existing depreciation + retirement.
- Explorer: persist a tree to disk (it is in-memory and dies with the server),
  and let a derivation *drive* expansion — branch only where it holds, so the
  search follows the interesting frontier instead of the full cross product.
- **A standalone iOS build.** `docs/ios-port-design.md` measures the port
  surface and costs four paths. The short version: ~9,300 LOC of the package
  is platform-free, there are ZERO non-stdlib imports, and `actions.py` +
  `snapshot()` already form the model layer an app needs — so this is a port,
  not a rewrite. Two things must be fixed first regardless of path: **saves
  are 5.5 MB pickles** bound to the Python version and the class layout (an
  app update would orphan them), and the **desktop-shaped UI** — a 12-column
  Routes table and a 300-airport datalist reflow narrow but were not designed
  for touch.

## Suggested first task for a new session

Run `airlinesim run integration` to confirm the stack is green, then read
engine.py's SimulationEngine.tick and the Subsystem classes to see the pipeline
before making changes.

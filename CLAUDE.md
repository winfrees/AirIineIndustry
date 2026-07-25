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
      cli.py            # `airlinesim` command (list / run / demo)
      scenarios/        # runnable demos, each with a main()
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

The `integration` scenario is the closest thing to a test suite — it wires every
subsystem and asserts six invariants. Run it after any engine change.

## Known limitations (accurate — don't "fix" silently)

- Maintenance intervals, depreciation rates, duty limits are industry-*shaped*
  defaults for game balance, NOT certified figures.
- Route demand splits into business/leisure/connecting segments, each segment
  is now its own priced, capacity-bound demand pool resolved independently
  by the arbiter (business segment -> BUSINESS cabin; leisure + connecting
  -> ECONOMY, summed into one pool). Pool size responds to a
  capacity-weighted average price signal, so segment elasticity actually
  bites. FIRST and PREMIUM cabins still have no dedicated segment source
  (only 3 segments exist) — carriers configuring those cabins on a segmented
  route see legitimate zero demand for them, not silently dropped revenue.
- Crew deadheading is direct-to-base only; no multi-hop routing or ferry flights.
- The bundled AI adjusts price/frequency but doesn't use route suitability to
  right-size equipment.
- Roster is conservative — can leave capacity unflown.

## Good next steps (from prior design discussion)

- Give FIRST and PREMIUM cabins a dedicated demand source (a 4th segment, or
  a documented split rule off an existing one) — see the segment/cabin gap
  noted above.
- Multi-hop / ferry crew positioning.
- Make the AI read suitability + per-route P&L to choose aircraft and cabins.
- Resale / used-aircraft market feeding the existing depreciation + retirement.

## Suggested first task for a new session

Run `airlinesim run integration` to confirm the stack is green, then read
engine.py's SimulationEngine.tick and the Subsystem classes to see the pipeline
before making changes.

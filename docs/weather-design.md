# Weather and disruption — design, and the road to real data

Status: **implemented and running on a HEURISTIC climatology.** The data seam
is built and empty. This document says what is modelled, what is invented,
and exactly what would replace each invented part with a measurement.

---

## 1. Why the clock had to change first

Weather is an hourly phenomenon. A convective complex closes an airport for
ninety minutes; a crew times out at four in the afternoon; a de-icing queue
adds forty minutes to a departure. None of that is expressible when the
smallest interval the simulation can resolve is a whole day.

So the clock moved first:

- `SimulationEngine.dt` is **hours per tick**, a real resolution knob, and
  defaults to 24 h for scenarios where a day is the smallest interesting unit.
- `GameSession.speed` is **sim hours per real second** (was sim *days*), and
  the loop accumulates a debt in hours and spends `engine.dt` per tick. Rate
  and resolution are now independent: how fast time passes and how finely it
  is simulated are separate decisions, exposed in the GUI as *speed* and
  *detail*.
- A played game runs at **1-hour resolution, 24 h/s** by default.

**Two bugs had to be fixed to make sub-day ticks mean anything**, and both are
the same mistake — a per-DAY budget spent per TICK:

| Bug | Symptom at 1-hour ticks |
|---|---|
| Gate claim asked for the full `daily_frequency` every tick | A 20-gate airport was exhausted before noon; every carrier's effective frequency collapsed to zero |
| A deadheading crew's seat reservation was subtracted whole from a tick-sized cabin | 6 seats removed from the 7.5 an hourly tick offers — 80% of the aircraft, for one crew |

Both are fixed and pinned by `airlinesim run weather`, which asserts that a
simulated month produces the same carriage and the same cash at 24 h, 6 h and
1 h resolution. **Anything added to the engine that consumes a daily budget
must scale by `dt / 24` the same way.**

The honest limitation of sub-day resolution: a day's departures are **smeared
uniformly** across it rather than scheduled at real departure times. Weather
therefore bites in proportion to the hours it covers, which is right on
average and wrong for any particular flight. Real departure banks would need
a schedule model the engine does not have.

---

## 2. The weather model (`weather.py`)

Three layers.

**Climate** — each airport's climatology is derived from its *measured*
latitude and longitude: seasonal temperature, continentality, and propensity
for convection, snow, freezing rain, fog, hurricanes, wildfire smoke and
volcanic ash. This decides what kind of weather is even possible where.

**Systems** — weather is not per-airport dice. A `WeatherSystem` has a
position, radius, intensity and velocity, and it *moves*. A front sweeps west
to east, so it closes ORD and then, six hours later, DTW. A hurricane runs
west and recurves northeast. Ash drifts downwind. This is what makes a
network built along one corridor riskier than a scattered one — the whole
reason to model geography rather than roll dice per airport.

**Local** — the systems overhead, gated by that airport's susceptibility,
give a capacity multiplier, a delay, and possibly a closure. A blizzard
passing over Miami does nothing, because Miami's winter severity is zero.

### Probabilistic, and still reproducible

Weather is a **stochastic process**. `WeatherModel.advance()` runs each tick:
retire the systems that have died, roll for new ones against season- and
geography-dependent probabilities. A player cannot know next week's storms,
and two playthroughs of the same opening diverge — weather is a risk to hedge,
not a timetable to learn. A new game draws a fresh seed.

That does not cost the explorer anything, because what it needs is
*reproducibility on fork*, not predictability. The draws come from
`WeatherModel.rng` and the live systems are stored on the model; both pickle
with the world. Forking a node copies the generator state, so re-running a
branch replays the identical season and two branches differ only by the
decisions taken — exactly what `scenario_explorer` asserts. `engine.py` still
contains no `random` call; the randomness lives in state the world owns.

Three subtleties, each of which was a bug first:

1. **The sky is averaged across a tick**, not sampled at its first instant. A
   thunderstorm lives ~6 h, so at 24-hour resolution it was usually born and
   dead between two looks — a coarse run saw almost no weather and the
   explorer's weather knob looked inert.
2. **A tick longer than one spawn slot gets multiple draws**, not one draw at
   a scaled-up probability. Folding the scale into a single Bernoulli
   saturates at `p > 1`, so coarse resolution quietly produced *less* weather
   than fine.
3. **Weather realizations are NOT resolution-independent**, and this is not a
   bug to fix. Different tick sizes consume different numbers of draws, so
   they sample different seasons. The dt-independence guarantee is about the
   *engine* — carriage, cash and fuel with weather off — and that is what
   `airlinesim run weather` asserts.

### Explorer controls

Weather is a variable there rather than a fact:

| knob | what it does |
|---|---|
| `weather` = 0/1 | switch weather off or on **at any node**; switching it on attaches a model to a clear world at a FIXED seed, so two sibling branches face the same season and the comparison between them is a result rather than noise |
| `weather_<kind>` | stage a named event (blizzard, hurricane, icing, ash, …) over a chosen airport at a chosen intensity — one knob per `WeatherKind`, generated from the enum so the picker cannot drift from the model |

A staged event obeys **geography but not the calendar**: a blizzard at ORD in
July is a legitimate what-if, while a hurricane at ORD is refused with a
reason rather than staged as a silent no-op. Its intensity is what gets
*delivered* at the target, so the system is sized to overcome the local
susceptibility gate.

### Calibration, and what it cost to get right

Five things were measurably wrong in the first cut and are worth recording so
they are not reintroduced:

1. **Seasonal amplitude grew without bound with latitude** — Anchorage came
   out at −22 °C in January and +29 °C in July. Capped at 19 °C.
2. **Continentality was measured to any water, including the Great Lakes** —
   Chicago (27 km from Lake Michigan) was as maritime as Boston, which
   flattened its winter and left it too warm to snow. Continentality is now
   measured to the **ocean**; lakes still drive fog and lake-effect snow.
3. **Hurricane exposure used the same any-water distance** — Chicago scored a
   third of Miami's. Hurricanes now use the ocean coastline only.
4. **Convection was latitude-only** — Los Angeles, as warm as Atlanta and at
   the same latitude, drew 200 thunderstorm hours a year. Convection is now
   scaled by distance from the **Gulf of Mexico**, the source of the moisture
   that feeds US storms, with a floor for the desert monsoon.
5. **Nothing ever closed.** The closure test was a threshold on intensity that
   susceptibility had already scaled below it. Closure is now tied to the
   computed capacity, so tuning severity tunes closures with it.

Resulting behaviour over a simulated year (seed 42):

| | delay > 15 min | capacity < 90% | dominant kinds |
|---|---|---|---|
| ORD | 2.7% of hours | 3.5% | rain, snow, convection |
| MSP | 4.3% | 4.6% | snow, blizzard |
| ATL | 1.8% | 2.0% | convection |
| MIA | 0.6% | 0.6% | hurricane |
| SEA | 0.4% | 0.9% | rain, wildfire smoke |

Northern hubs peak in winter, the Gulf coast in summer, Miami in hurricane
season. **None of this is fitted to a weather record** — see §4.

---

## 3. Disruption (`disruption.py`)

The direct cost of weather is the smaller half. The expensive half is the
chain it sets off:

```
weather -> reduced airport capacity -> flights CANCELLED
        -> longer taxi/hold/de-ice  -> flights DELAYED
                                            |
        delay consumes the crew's duty day <+
                                            |
        crew times out -> the NEXT rotation cancels
                                            |
        cancelled flights -> passengers STRANDED
                 |-- rebooked on your own later flights  (cheap)
                 |-- refunded and lost                   (revenue never earned)
                 |-- overnight -> HOTEL + MEALS + compensation
                 `-- crew stuck away from base -> CREW HOTEL
```

Two subsystems, ordered deliberately:

- **`WeatherSubsystem` runs first** — before suitability and operations. It
  annotates each route op with the capacity and delay it faces. It decides
  nothing; Operations remains the single authority on how much flying happens.
- **`DisruptionSubsystem` runs last** — after Operations has recorded what
  actually flew, so the shortfall against schedule is known and can be turned
  into passengers and money.

Weather delay is added to `fh_per_rotation` in the crew-legality gate, which
is what makes the indirect path real: the delay itself is cheap, the rotation
it makes illegal is not.

`world.disruption_history` accumulates per airport, so a hub that costs you
every winter can be *seen* to — the "penalise certain airports over time"
requirement. Over 120 simulated days out of ORD, that hub sat at 0.78
reliability against 0.90 for LAX and DFW.

### Known limitations — do not "fix" silently

1. **Rebooking is same-tick, same-market only.** A stranded passenger is
   re-seated onto the operator's own spare capacity in the same market, in the
   same tick. On a corpus world where flights run near full, that rebooks
   about 7% of them and refunds the rest — which overstates the cost. A real
   recovery model carries stranded passengers forward over a day or two and
   searches alternate routings; that needs a passenger-itinerary object the
   engine does not have.
2. **No alternate airports and no interline.** Real recovery diverts to a
   nearby field and re-accommodates on other carriers.
3. **No advance cancellation.** Real airlines pre-cancel a day ahead of a
   forecast hurricane, which is cheaper than stranding people. The model
   cancels only as weather actually bites.
4. **Aircraft are not out of position** after a cancellation — the tail is
   available again next tick regardless of where the cancelled flight would
   have left it. This is the largest single simplification in the chain.
5. **Maintenance accrues on flight time, not block time**, so a delay does not
   age the airframe.
6. **The AI does not react to weather at all.** It does not pre-cancel, re-time,
   or avoid exposed airports when planning a network.
7. **All disruption cost figures are game-balance heuristics**, not any
   carrier's disclosed costs.

---

## 4. Getting real data in

Everything in §2 is HEURISTIC. Two public datasets would replace it, and the
seam for both already exists. Neither could be fetched while this was built —
the sandbox's network policy denies both hosts — so this is a plan, in the
same position `docs/route-data-design.md` was in before the BTS ingest landed.

### Option A — NOAA Climate Normals (1991–2020) → fixes the CLIMATE

Per-station monthly normals: mean temperature, precipitation days, snowfall,
and (in the hourly normals) fog and ceiling frequencies. Free, stable URLs at
`ncei.noaa.gov`, one CSV per station, and airports are exactly the stations
NOAA reports.

- **Replaces**: the seasonal temperature curve, `winter_severity`,
  `fog_prone`, and the precipitation frequencies — i.e. most of `climate_for()`.
- **Join**: airport ICAO → NOAA station is direct for nearly every US airport
  in the corpus.
- **Cost**: low. ~300 small CSVs, distilled to one `weather.json` of monthly
  per-airport normals, committed like `gravity.json`.
- **Leaves heuristic**: system sizes, speeds, and the capacity hit per
  condition. Normals say how often it snows, not what snow does to a runway.

### Option B — BTS On-Time Performance → fixes the IMPACT

The one dataset that measures what this module is actually claiming.
`WEATHER_DELAY`, `NAS_DELAY`, `CANCELLED`, `CANCELLATION_CODE` (B = weather),
per flight, per airport, per month, 1987–present.

- **Replaces**: the capacity multipliers, the delay minutes per condition, and
  the closure thresholds — calibrated per airport per month against measured
  weather-delay minutes and weather-cancellation rates.
- **Also gives**: the per-airport reliability ranking as a *measurement*
  rather than a model output, which is exactly the "penalise certain
  airports" requirement, grounded.
- **Cost**: medium-high. It is a large monthly download and needs a reader in
  `btsdata/`, but the warehouse pattern already exists for T-100.
- **Caveat**: BTS attributes cause by carrier report, and the NAS bucket mixes
  weather with volume. Weather-attributed delay is a floor, not a total.

### Option C — calibrate A against B, ship both

The end state. NOAA sets *how often* each condition occurs at each airport;
BTS sets *what it costs* when it does. Then `weather.json` carries a MEASURED
frequency and a DERIVED impact per airport-month, and only the system
geometry (radius, speed, track) stays heuristic — which is the part that makes
it a *simulation* rather than a lookup table, and the part a player can
actually plan around.

**Recommendation: A, then B, then C.** A is a day's work and removes the
weakest claim in the model (that latitude alone predicts climate). B is the
one that makes the disruption numbers defensible.

### The seam, as built

- `WeatherModel(seed, climates=...)` takes a climate table; nothing else needs
  to change to feed it measured normals.
- `climate_for(iata, lat, lon)` is the heuristic fallback, used only when no
  measured climate exists for that airport.
- `airlinesim/data/weather.json` is the intended artifact and is **absent** —
  the model runs entirely on the heuristic path today, and there is no code
  that pretends otherwise.
- Anything loaded there must be tagged in `MANIFEST.json` as MEASURED
  (NOAA/BTS values) versus DERIVED (anything fitted from them), the same
  discipline the route corpus follows.

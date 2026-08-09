# Route planning tool — design and phased plan

A **read-only** planning surface that answers the three questions a network
planner actually asks, on the map, before any money moves:

1. **Can I serve this demand, and with what?** Given an origin/destination,
   which types can legally and economically fly it — split into metal already
   in the fleet and metal that would have to be acquired — and, for each,
   where that aircraft would have to be **based**.
2. **What does the plan need to stand up?** Crews (headcount, by base, against
   the duty envelope) and maintenance (a rated hub the type can actually be
   checked at).
3. **Where should this aircraft / this station fly?** Given a tail or an
   airport, rank every reachable destination by profit, distance, achievable
   24-hour frequency, and load factor.

All of it driven off the network map, because that is where a network decision
is legible.

Nothing here commits. `merger_candidates()` is the precedent: an itemised
"should I?" screen that states its own reasoning, with execution left to the
existing actions.

---

## 1. The one architectural decision: ONE forecast, not two

The engine already contains a route forecast. `ai._evaluate()` estimates a
candidate pair's daily profit from corpus demand, a share model, a fare, and
the costs the engine will actually charge. It is the arithmetic three AI
carriers plan their whole networks with.

**A second forecast written for the player would drift from it, and the drift
would land on the player.** A planner that says $41k/day where the engine
charges costs producing $12k/day is worse than no planner — the player would
be out-planned by rivals reading truer numbers, for reasons invisible to them.
This project has already paid this bill twice: `cabin.fit_layout` is one
fitter behind three entry points precisely so a preview cannot disagree with
the installed cabin, and `routedata.gravity_features` is shared with
`btsdata` so the fit and its evaluation cannot diverge.

So the plan is **not** "write a planner". It is:

> Extract the forecast out of `ai.py` into `planner.py`, make it complete and
> explainable, and have `ai.py` call it. The AI and the player then plan with
> one set of numbers by construction.

What `ai._evaluate` does today that a player-facing forecast cannot do:

| gap | today | why it matters to a player |
|---|---|---|
| frequency is an input | `freq = clamp(arch.max_freq_per_plane - 2, 1, 3)` | "max 24h frequency" is an *output* the player asked for, and which constraint binds it is the answer |
| crew is a flat rate | `(220*2 + 60*4) * bh` | the player has to *hire* the crew; they need headcount and a base, not a number |
| no ownership cost | omitted entirely | fine for the AI's relative ranking of pairs; misleading for "should I acquire this aeroplane" |
| no maintenance check | none | a widebody based where the carrier has no rated hub grounds itself weeks later |
| no gate check | none | frequency the airport has no gates for is frequency that will not fly |
| rejection is silent | returns `None` | "why can't I fly this?" is the question the screen exists to answer |

### Identity preservation is a hard requirement of phase 1

The refactor must not change a single AI decision. `airlinesim run explorer`
pins engine determinism and is the only thing that does; the AI participates
in the worlds it forks. So:

- `planner.evaluate_route()` takes the frequency rule, the crew basis and the
  cost basis as **parameters**, and `ai._evaluate` passes exactly what it
  passes today (`CostBasis.CONTRIBUTION`, the flat crew rate, the clamped
  frequency).
- Before touching `ai.py`, capture golden values: for a fixed
  `build_world_from_data()` world, the `(profit, ref_fare)` of ~50 pairs ×
  ~5 types. `scenario_planner` asserts the refactored path reproduces them to
  the cent. Those goldens stay in the scenario permanently — they are what
  stops a later planner improvement from silently re-tuning three AI
  carriers.
- Improvements to the *player's* forecast (ownership, real crew sizing,
  binding-constraint frequency) arrive as different `CostBasis` /
  `FrequencyPolicy` values, not as edits to the AI's path. Moving the AI onto
  the richer basis is a separate, deliberate balance change — out of scope
  here, and worth its own before/after.

---

## 2. Module: `airlinesim/planner.py`

Pure stdlib, no engine mutation, no `random`. Plain functions over
`(world, players, ...)`, the same shape as `actions.py` and `merger.py`.

```
RouteForecast      one (pair, aircraft type, frequency) case, fully itemised
FrequencyPlan      the four candidate frequencies and which one BINDS
CrewRequirement    headcount by type and base, vs what the carrier has
BasingOption       where the tail must sit, what that costs, what it enables
FleetOption        one type: feasibility, source (owned/acquire), quote, forecast
PairPlan           Q1 — one O&D, every type, ranked
DestinationPlan    Q3 — one origin (or one tail), every destination, ranked
```

Everything is a frozen dataclass with a `to_json()`, because these travel down
`/api/plan` and into the map. Every number that is an estimate carries the
provenance that produced it.

### `evaluate_route(...) -> RouteForecast`

The shared core. Itemises, rather than returning a scalar:

```
revenue     pax x fare, per cabin where the layout is known
  less      fuel, maintenance reserve, landing, gate, amenities, baggage
  = CONTRIBUTION MARGIN        <- what RouteOp.last_profit measures
  less      crew payroll (from the crew requirement, not a flat rate)
  less      ownership (lease rent or loan service, from Bank.quote)
  less      hub overhead attributable to this leg
  = FULLY ABSORBED
```

Both lines are reported, both labelled. CLAUDE.md already warns that
`last_profit` is a contribution margin and that a carrier can show every route
"profitable" while the company burns cash — the planner must not repeat that
trap by showing one number. The AI reads the contribution line, which is what
it reads today.

Also returned: break-even load factor, break-even fare, and — when the offer
cannot be flown at all — the specific reasons, from `route.route_can_fly()`
verbatim. Reusing that function rather than re-listing its checks is what
keeps the planner's "yes" identical to the tick's `RouteSuitabilitySubsystem`.

**No archetype fit is applied.** `ai.route_fit` / `airport_fit` tilt an AI's
share by how well a pair suits *its* business model. The human has no
archetype; quietly applying one would rank their map against a preference they
never set.

### `frequency_plan(...) -> FrequencyPlan`

The "max 24 hr freq" the request asks for is four different numbers, and the
useful answer is which one binds:

| limit | derived from |
|---|---|
| **airframe** | `DAILY_UTILIZATION_H / block_hours(dist, cruise)`, rotations not legs |
| **demand** | `databuilder.daily_frequency()`'s form: demand × share ÷ (seats × LF) |
| **gates** | free gates at both ends, `world.gates[iata].free`, scaled `dt/24` |
| **crew** | pool block hours available ÷ block hours the schedule needs |

Report all four and name the binding one. This is the same shape as
`route_can_fly` returning reasons: a bare "6/day" tells a player nothing they
can act on, while "6/day — demand-limited; the airframe could do 9" tells them
to look for a bigger market, and "3/day — gate-limited at LGA" tells them to
look at a different field.

`databuilder.daily_frequency()` currently owns the demand half. Extract it to
`planner` and have `databuilder` import it — the same shared-seam rule that
governs `gravity_features`.

**Rotations, always.** A one-way leg strands its crew at the spoke forever:
deadheading is direct-to-base only, so a crew ending its tick at the
destination can only get home on a leg pointing at its base. `databuilder`
(`_with_return_legs`) and `ai` (`_open_rotation`) both already open routes in
pairs for exactly this reason. The planner plans and costs **rotations**, and
never offers a single leg as a plan.

---

## 3. Q1 — "I have an origin and a destination"

`plan_pair(world, players, player, origin, dest, *, service_tier, fare) -> PairPlan`

For every type in `repo.all(AircraftSpec)`:

**Feasible?** `route_can_fly()` — range, runway at both ends, the corpus seat
window. Infeasible types are returned **with their reasons**, not filtered
out; "why is the 787 not on this list?" is the whole point.

**Where would it come from?** Three sources, and the distinction is the ask:

- *idle in fleet* — owned, unassigned, and (per §5) already sitting at the
  origin.
- *in fleet, elsewhere or committed* — owned but based at another station or
  already flying a route. Reported with what it would cost to free: closing
  the incumbent route loses that route's contribution margin, which the
  planner states as the switching cost.
- *acquire* — with a **non-committing quote** at each of the three methods.

`Bank` has no quote API today: `acquire()` both prices and commits, and
`can_finance()` is the credit gate inside it. Extract
`Bank.quote(player, spec, method, terms) -> {upfront, monthly, term_months,
approved, reason}` and have `acquire()` call it. Duplicating the leverage
check in the planner would put a second copy of the credit rule in the tree,
and it would be the copy that told the player "affordable" the moment the two
drifted.

**Economics** per type at the planner's frequency, both cost lines, ranked by
fully-absorbed daily profit.

**Provenance, prominently.** Every pair carries `data_tier` and
`data_vintage`. An EXACT pair is a BTS measurement; a COMPARABLE pair is a
gravity estimate where roughly a third are off by more than 2×; a SYNTHETIC
pair is an engine default. The tier is rendered on the row, not buried in a
tooltip, and rounding follows it — a COMPARABLE estimate is shown to two
significant figures, because printing `$41,847/day` against a fitted number
claims a precision it has not got.

---

## 4. Q3 — "I have this aircraft / this station: where should it fly?"

`plan_from(world, players, player, *, origin=None, tail=None, rank_by=...) -> DestinationPlan`

Scan every airport in the repo, evaluate the rotation, rank. Rank keys:
fully-absorbed profit per day, contribution margin, margin %, distance,
achievable frequency, load factor at the planner's fare, payback on the
acquisition where one is implied.

Filters that matter: **measured pairs only** (drop COMPARABLE/SYNTHETIC),
minimum demand, maximum stage, "only where I already have a crew base", "only
where I can do maintenance".

### Performance, measured

Full 300-airport scan from ORD, on the committed corpus:

```
provider.route_spec() x 299          0.19 s     <- essentially all of it
same 299, memoized, 16 further passes 0.001 s
```

`route_spec()` is the whole cost and it is per-*pair*, not per-type — so
"every destination × every type" is 300 spec builds plus ~4,800 pieces of
trivial arithmetic, not 4,800 spec builds. Two consequences:

1. **Memoize `RouteDataProvider.route_spec`.** It is a pure function of
   immutable committed data returning a frozen dataclass, so an `lru_cache`
   is safe. Cold 0.19 s, warm free. Route opening and `ai._spec_for` benefit
   too.
2. **Do not hold `GameSession.lock` across the scan.** 0.19 s of held lock
   stalls the tick loop, the command API and the SSE stream together — the
   same failure mode CLAUDE.md documents for unbounded clock catch-up. Take
   the lock, copy the handful of mutable facts the scan needs (fleet
   locations, hubs, crew pools, gate ledgers, rival route counts), release,
   then compute. The result is a snapshot-in-time forecast, which is what a
   forecast is.

---

## 5. Q2 — basing, crew, maintenance

### Basing: state the limitation, then plan around it

`Airplane.location_iata` is set once, at acquisition, from `base_iata`. Grep
confirms **nothing ever updates it** — not `OperationsSubsystem`, not
`MaintenanceSubsystem`. There are no ferry flights and no repositioning. An
aircraft is permanently where it was bought.

That is not a bug to fix inside this feature, but it is decisive for the
request: *"identifying planes and at which airports to base planes"* is a
decision made **at acquisition and never again**, which makes getting it right
before the money moves exactly the thing the planner is for. So:

- The planner's basing output is a **pre-acquisition recommendation**: for a
  chosen set of candidate routes, which base serves the most of them, what its
  hub fee is, whether it can do maintenance for the class, and which of the
  routes it strands.
- For metal already owned, the planner reports the base as a **hard
  constraint**: routes not touching `location_iata` are shown as unreachable
  by that tail, with that stated as the reason. It must not quietly rank a
  destination the tail cannot start from.
- Ferry flights / repositioning are listed in §7 as the extension that would
  relax this, alongside multi-hop crew positioning.

### Crew

Size the requirement rather than charging a flat rate:

```
block hours the schedule needs = block_hours(dist, cruise) x 2 x frequency
crews needed at the base       = ceil(that / DutyLimits.max_daily_flight_hours)
                                 x CREW_DEPTH
```

`CREW_DEPTH = 2.5` (rest rotation) and the duty envelope come from existing
data — `databuilder.CREW_DEPTH`, `crew.DEFAULT_DUTY_LIMITS` — not new
constants. `ai._crew_target` already derives a pool from scheduled block
hours over the daily cap; that derivation moves to `planner` and `ai` imports
it, one seam again.

Output: cockpit and cabin headcount required at each base, what the carrier
has there now (`_crew_bases` already computes this for the crew panel), the
gap, and the hourly cost of closing it. A plan whose crew gap is unfunded is
flagged — CLAUDE.md's crew-starvation history is precisely a network that
looked fine and could not be flown.

### Maintenance

`MaintenanceEngine._find_facility` will only use an airport that is a
**declared hub** of the owner, has `has_maintenance_facility`, and whose
`facility_max_class` covers the check tier. A plan that bases a widebody where
the carrier has no rated hub produces an aircraft that flies for weeks and
then cannot be checked.

The planner runs that same predicate for the chosen type and reports: the
rated hubs that can take it, the nearest rated field that is *not* yet a hub
plus its `hub_fee_per_day`, or — if there is none — a blocking warning. This
is the same class of quiet, delayed failure as closing your last hub, which
`set_hub` already refuses outright.

---

## 6. Q4 — on the map

A **layer on the existing map**, not a fourth front end. `map.js` already has
the projection, zoom/pan, carrier colours and selection; the planner adds a
`MAP.plan` group inside `MAP.view`, drawn from planner results and cleared
when planner mode is off.

**Interaction.** Airports are not currently clickable — `map.js` hit-tests
only `[data-tail]` and `[data-op]`. Add `selectOn("airport", iata)`:

- one airport selected → **Q3**: candidate spokes fan out from it, ranked, the
  panel listing them; hovering a row highlights its spoke and vice versa.
- two airports selected → **Q1**: the pair is drawn alone and the panel becomes
  the equipment table.
- an aircraft selected → **Q3 for that tail**, constrained to its base.

**Rendering rules that are not optional**, all inherited from CLAUDE.md's map
section: geography scales but strokes do not (`vector-effect:
non-scaling-stroke`); symbols counter-scale by `1/ZOOM.k`; point features are
redrawn by `drawLive`, so a zoom must redraw them or the planner layer goes
stale while the player is studying a market **paused** — the exact case this
tool is used in; `DRAG_SLOP_PX` and `touch-action: none` must keep working.

**Candidates must not read as flights.** Operated routes are already solid
(or dashed-and-faint when grounded). Planner candidates are a visually
distinct class — thin, dashed differently, tinted by rank — and the About
dialog gains a paragraph saying they are forecasts of routes that do not
exist. The map already carries the "aircraft positions are DERIVED" note for
the same reason, and `scenario_map` pins that note's whole reachability path
because "the string is in index.html" stopped being evidence a player can
read it. `scenario_planner` does the same for this one.

**Panel.** Full-width, like the map and the Alliances card, and directly under
the map. A ranked equipment table with a cost breakdown per row does not fit a
column; the alliance card is full-width for exactly this reason.

---

## 7. Delivery seam — the part that gets forgotten

CLAUDE.md is explicit: *a feature only the scenario can reach is not
delivered.* `attach_alliances()` shipped once, never called from the game
path, with every action written and nothing reachable. The planner is
read-only, so the failure mode is quieter and just as complete: a `planner.py`
with no endpoint.

The chain, asserted end to end by `scenario_planner`:

```
planner.plan_pair / plan_from        pure functions
  -> GameSession.plan_pair / .plan_from   lock discipline per §4
  -> GET /api/plan                        read-only, like /api/cabin and /api/mergers
  -> webui: planner panel + MAP.plan layer + About paragraph
  -> execution via the EXISTING actions
```

**No new mutating action is needed**, and that is the right boundary. The
planner's output pre-fills the acquisition, route-opening, hire-crew and
set-hub forms the player already has. A one-click "execute this plan" is
deliberately deferred: it is a compound of four actions with four independent
failure modes (credit denied, gate refused, no crew available, hub fee
unaffordable), and a half-executed plan — an aircraft acquired for a route
that then would not open — is a worse outcome than four confirmed clicks.
Listed in §9 as an extension, with that cost stated.

`/api/plan` is a GET with query parameters, mirroring `/api/cabin`. It carries
the same hazard as the `COMMANDS` table: `_cabin_fit` hand-parses its query,
and a parameter the form sends but the handler does not read is silently
ignored — which is how `seats` was dropped from acquisition and
`service_tier` from route opening. The scenario asserts every planner
parameter round-trips.

---

## 8. Honest limits (to be stated in the UI, not only here)

1. **Static forecast; no competitive response.** The share model dilutes by
   incumbent count and stops there. It does not model a rival matching your
   fare or adding frequency against you. This is the same limit `ai._evaluate`
   carries, and deliberately: a perfectly predictive planner would be an
   oracle, and the error is the seam skill lives in.
2. **A third of COMPARABLE pairs are off by more than 2×.** Ranking 300
   destinations by a fitted gravity estimate will confidently rank noise.
   Mitigated by showing the tier per row, offering a measured-only filter, and
   rounding to the precision the tier supports — not by hiding it.
3. **Demand is censored.** The shipped corpus is T-100 *Market*, which has no
   `SEATS`, so passengers flown cannot be de-censored back to demand. The
   planner understates demand on already-full routes, and therefore
   understates the frequency and the profit of the busiest pairs. A T-100
   **Segment** export fixes this and is the single highest-value missing
   input for this feature as well as for route data generally.
4. **Connecting traffic is a connectivity index, not itineraries.**
   `alliance.feed_factor()` scores what departs the destination; no passenger
   is traced to a final destination and connections are one stop only. The
   planner may surface feed for a candidate — it is genuinely part of why a
   hub spoke is worth more than a point-to-point leg — but must label it an
   index.
5. **Gate contention is read statically.** The planner reads free gates now;
   the arbiter resolves priority-weighted contention during the tick, and a
   hub multiplies its owner's priority. "Gate-limited at 3/day" is a present
   fact, not a prediction.
6. **No repositioning.** Per §5, aircraft never move between bases. Every
   plan for owned metal is constrained to its acquisition base.
7. **Weather is not in the first cut.** Airport reliability is already
   measured and already in the snapshot (ORD lands around 68% after forty
   days on a corpus world) and discounting a forecast by it is a real
   improvement — but it is a *seasonal* number being applied to a daily
   forecast, and getting that wrong quietly is easy. Phase 5, explicitly, so
   the base forecast is trusted first.

---

## 9. Phases

Each phase lands complete and asserted. `airlinesim run integration` and
`airlinesim run explorer` after every one — the latter is what catches an
accidental change to AI behaviour.

**Phase 0 — goldens.** Capture `ai._evaluate` output over a fixed
`build_world_from_data()` world (~50 pairs × ~5 types). Add
`scenarios/scenario_planner.py` with nothing but that assertion, wired into
`cli.py` and `tools/smoke_windows_bundle.py`. Green before anything moves.

**Phase 1 — the shared forecast.** ✅ *Landed.* `planner.py` with
`RouteForecast`, `evaluate_route`, `frequency_plan`; `ai._evaluate` is a
wrapper; `Bank.quote()` and `databuilder.daily_frequency`'s demand term
extracted. All 450 goldens reproduce to the cent. Two things the plan had
wrong, corrected in the building:

- **`route_can_fly` is NOT inside `evaluate_route`.** The two check different
  things — `route_can_fly` reads the ROUTE's banded `min_runway_m` and the
  corpus seat window, while the forecast reads the AIRCRAFT's own takeoff
  length, range and the plan's stage band. Folding suitability into the
  forecast would also have made the AI start rejecting pairs it has always
  accepted (it has never consulted the seat window when scoring), which is a
  balance change wearing a refactor's clothes. `planner.suitability_reasons`
  is a separate function returning `route_can_fly`'s reasons verbatim, and
  the pair screen reports both side by side: one is what will block the
  button, the other is what is true.
- **The crew limit is on the POOL, not on one crew, and it is slack.** It
  binds against the airframe exactly when
  `N x max_daily_flight_hours < DAILY_UTILIZATION_H / 2 x CREW_DEPTH` — leg
  length cancels out of both sides — so with the shipped constants the
  threshold is 17.5 and a single rated crew pair at the 9-hour cap binds
  while two do not. Worth knowing before reading much into this limit at a
  well-staffed station. It is sized as the exact inverse of `ai._crew_target`
  so requirement and ceiling cannot drift, and the scenario asserts that
  inversion directly.

Also found and now asserted: a type rating is part of the answer. The starting
carrier has ten cabin crew and ten A320-family pilots at ORD, so a 787 has
**zero** available cockpit crew there and the planner reports the route
grounded rather than assuming pilots appear.

**Phase 2 — Q1, pair mode, end to end.** ✅ *Landed.* `plan_pair`,
`GameSession.plan_pair`, `GET /api/plan`, and a full-width panel with the
ranked equipment table and an About dialog. All the planned assertions hold,
plus the delivery chain end to end. Two things the plan did not anticipate:

- **Ownership had to come forward from phase 4.** Ranking on contribution
  margin alone put an A350 at the top of ORD-DEN earning $53k/day — and
  losing **$42k/day** once its lease was paid. The plan deferred the absorbed
  line, but shipping a screen whose top recommendation bankrupts you is worse
  than shipping it a phase late. `Bank.quote()` was already in hand from
  phase 1, so `CostLines` gained an `ownership` line and rows are ranked on
  `absorbed`. The scenario asserts the two rankings genuinely disagree.
- **Ownership is charged at the LEASE rate however the aeroplane is paid
  for.** Taking the cheapest quote by daily payment priced a cash purchase at
  $0/day and ranked a $290M 787 as free. Buying outright converts capital, it
  does not avoid the cost — which is how `ai._rank_aircraft` has always
  expensed it, at the same 11%/year `actions.LEASE_TERMS` charges. All three
  quotes are still shown per row, with both halves of each.

Phase 4's remaining work is therefore narrower than planned: crew payroll as
a headcount rather than a flat per-block-hour rate, hub overhead, and the
maintenance/basing outputs.

**Phase 3 — Q3, destination ranking.** ✅ *Landed.* `plan_from` for an airport
and for a tail, seven rank keys, a measured-only filter, `GET /api/plan/from`,
and the same panel in destination mode. All the planned assertions hold.
Three corrections from building it:

- **The bottleneck was not where the plan said.** The measurement in §4 timed
  `route_spec` alone. In a real scan the cost was `observation()` →
  `_comparable` → `_neighbour_season`, which scans the whole route table to
  average an airport's neighbours' seasonal shape: **3.9 of the first scan's
  4.9 seconds**. Memoizing `observation` as well as `route_spec` took a full
  300-destination × 16-type scan from **2.36 s to 0.27 s**.
- **The lock assertion was measuring the wrong thing.** "The planner never
  waits on the lock" is false and *should* be — it takes the lock briefly to
  resolve the origin, so waiting behind the tick loop is correct. The property
  that matters is that the lock is not HELD for the scan's duration, measured
  from the other side: run the scan on a thread, hammer the lock from another,
  and record the worst wait. 212 ms of work, worst wait 0.0 ms over 29
  attempts.
- **A vacuous check is worse than no check.** The same test applied to the
  pair plan sampled the lock zero times — the call finishes in 2 ms — so its
  comparison passed regardless of what the code did. It now reports the
  duration as its evidence and says the wait was never measured.

**Phase 4 — Q2, resourcing.** ✅ *Landed.* Crew headcount by base with the
hiring gap, maintenance-hub feasibility running the engine's own predicate,
and the crew cost corrected. Two things this phase was planned on were wrong:

- **Flight crew is a PURELY VARIABLE cost, and this doc said otherwise.**
  `OperationsSubsystem` bills cockpit + cabin only for the hours FLOWN, and
  `FinanceSubsystem`'s standing payroll covers ground, baggage, meteorology
  and maintenance staff — **not** flight crew. So "payroll for crews that did
  not fly" is not an omission from the absorbed line; it is not a cost this
  engine has, and the About dialog shipped saying it was. What *was* wrong:
  the forecast charged a flat $680/hour on BLOCK hours where the engine
  charges the actual crews' rate on CRUISE hours — $408 per departure too
  much. The player's forecast now reads the carrier's own rated crews at that
  base and charges cruise hours; the AI keeps the flat block figure, because
  its goldens pin it and over-stating a cost is the safe direction for a
  rival.
- **Hub overhead is deliberately NOT amortised.** `hub_fee_per_day` is charged
  per hub per day whatever flies, so it belongs to a network, not a leg. Any
  split across routes needs an arbitrary rule for how many share the hub, so
  it is reported as a plan-level cost — "your hubs cannot check this type;
  nearest that can is MDW at $16,703/day" — and never folded into a per-route
  margin.

One branch is **unreachable on the committed corpus** and is asserted as such
rather than claimed as tested: `routedata` derives `has_maintenance_facility`
and `facility_max_class` from the same test (`hub_rank <= 40` → WIDEBODY), so
every field that can do maintenance can do any check, and "your hub is not
rated for this type" cannot arise from the data. The scenario proves the
branch works by downgrading a facility in a scratch world, and separately
asserts the corpus cannot reach it.

Also noted: every type's D check requires a widebody-class facility
(`databuilder._program`), so `required_class` reads WIDEBODY even for a
regional jet. That is the data, not a planner bug.

**Phase 5 — the map.** ✅ *Landed.* A `MAP.plan` group inside `MAP.view`,
airport click targets, rank-tinted dashed spokes, and an About paragraph. One
click ranks every destination from an airport; a second plans that pair. The
panel pushes what it is showing to the map, so the two cannot disagree.

Building it uncovered a bug that had **broken map selection entirely**, not
just for the planner:

> `pointerdown` calls `svg.setPointerCapture()` so a drag that leaves the
> element still pans. Pointer capture **retargets the compatibility `click`
> that follows to the capturing element** — the bare `<svg>`. So
> `e.target.closest(".mapPlane")` in the click handler always found nothing,
> and clicking an aircraft or a route did nothing at all: the handler ran,
> matched neither, and fell through to "clear the selection". The map has been
> a poster rather than a control surface since zoom/pan landed.

The click handler now reads the **pointerdown** target. Two related fixes came
with it: `pointerup` no longer rebuilds the whole live layer on a plain click
(it was removing the very element the pointer went down on, and a click
changes no zoom to repaint for), and an airport's click target now spans its
dot *and* its label — the visible dot is about three pixels and the label sits
beside it, so the group's own centre used to land on bare geography.

`scenario_map` could not have caught this: it asserts the handler exists in
source, which it did. `scenario_planner` now guards the specific cause.

**Later, deliberately deferred:** reliability discounting; feed index on
candidates; compound one-click execution; ferry/repositioning; multi-leg
network plans (a rotation chain rather than an out-and-back).

---

## 10. What this could break

- **AI behaviour**, if the refactor is not identity-preserving. Phase 0's
  goldens exist for this and stay permanently.
- **Explorer determinism**, if anything in the planner path reaches for
  `random` or `hash()` (salted per process). It must not; the planner is pure.
- **The tick loop**, if a 300-airport scan runs under `GameSession.lock`.
  Measured at 0.19 s cold today, and that is a stall of the command API and
  the SSE stream together.
- **The map**, if the planner layer is drawn outside `MAP.view` (it would not
  pan), or without the non-scaling-stroke / counter-scale split (a route line
  becomes a ribbon at 8×), or without a redraw on zoom (stale while paused,
  which is when it is used).
- **The wheel**, if any new non-`.py` file lands under `airlinesim/` without a
  `[tool.setuptools.package-data]` entry. Nothing here needs one, but
  `tools/smoke_windows_bundle.py` is the check that would catch it.

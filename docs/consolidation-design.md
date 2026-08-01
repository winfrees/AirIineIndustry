# Alliances and consolidation — design, and the historic-data question

Status: **alliances and the valuation/M&A engine are implemented.** The
passenger-itinerary model they would ideally rest on is not, and neither is
any empirical grounding for the merger triggers. This document says what is
built, what is invented, and — answering the brief's question directly —
**what historic data could tell us about when firms actually merge, and
whether we can get it.**

---

## 1. What alliances are for, and what was missing

`route.TravelerSegment.CONNECTING` already existed: every market carries a
connecting share. But it was carried as though it were *local* traffic. A leg
ORD→LAX sold its connecting seats whether or not anything connected to them.
Nothing asked "connecting to where, on whose aircraft?", so:

- a hub was worth no more than a point-to-point station, and
- an alliance was worth nothing at all.

The implemented mechanism supplies the missing half: **connecting demand has
to be fed.** For each leg, look at what departs its destination —

| whose onward flight | counts as feed |
|---|---|
| your own | in full (an online connection, one ticket, bags through) |
| a partner's | at the alliance tier's `feed_efficiency` (0.35 / 0.65 / 0.88) |
| a stranger's | **nothing** |

That last row is the entire commercial case for allying. Feed saturates
(`feed/(feed+FEED_HALF)`), because the tenth onward departure matters far less
than the first, and it is capped at `MAX_FEED_BONUS`, so feed can never carry
an uncompetitive fare on its own.

Measured on the corpus world out of ORD: legs *into* the hub score 1.43–1.45,
legs out to spokes score exactly 1.00 — there is nothing beyond a spoke to
connect to. That asymmetry is the hub-and-spoke logic falling out of the
model rather than being asserted by it.

### Alliances cost something

Three depths (`INTERLINE` / `CODESHARE` / `JOINT_VENTURE`) trade feed quality
against `dues_per_day`, and a connecting passenger is worth less than a local
one (`CONNECT_PENALTY`, scaled by the tier's `connect_quality`) because the
journey is longer and can be missed. `no_compete_hubs` is a real restraint:
a member may not open a route a partner already flies at a coordinated
airport, refused at the action layer *with the reason*. So "ally with
everybody" is not free, and joining is a decision rather than a strictly
better move.

### Two bugs worth remembering

1. **A partner's return leg counted as feed.** Nobody connects onto the flight
   back where they came from. Counting it made every out-and-back pair look
   like a hub, and made an alliance appear to create feed on a network where
   nothing actually connected. `onward_capacity(..., exclude_dest=)` fixes it.
2. **Alliance actions ran before the player roster existed.** The roster was
   stashed on the world by the subsystem *during a tick*, so `form_alliance`
   called before the first tick silently refused and `blocks_route` silently
   allowed. `register_players()` is now called at attach time too.

### Honest limits

- **Connections are ONE STOP.** Nothing models A→B→C→D.
- **This is a connectivity index, not an itinerary ledger.** Feed is measured
  at the leg's destination; no passenger is traced to a final destination. A
  true O&D model needs a passenger-itinerary object the engine does not have
  (see §4).
- Feed is applied to the whole op, not only to its connecting seats, because
  the arbiter's pools are per *cabin* and connecting traffic shares economy
  with local leisure. There is no separate claim to attach it to.
- Revenue sharing is a parameter, not a settled interline invoice.
- No slot/gate coordination, no antitrust immunity.

---

## 2. Valuation

`merger.value_carrier()` returns an itemised `Valuation` rather than one
number, so a bid can be argued with:

```
cash + fleet market value + going concern + network value
     − debt − lease obligations,   floored at liquidation value
```

- **Going concern** is `annual operating cash flow × GOING_CONCERN_MULTIPLE ×
  reputation`, and is **zero for a loss-making carrier** — an airline that
  loses money is worth its metal, not a multiple of its losses.
- **Cash flow is the AI's own smoothed operating figure**, which by
  construction includes every cost the engine charges. Deliberately *not*
  `RouteOp.last_profit`, which is a contribution margin and excludes lease
  rent, loan service, payroll and hub overhead — the number that lets a
  carrier look profitable on every route while the company burns cash.
- **Reputation** is derived from the disruption record (cancellation rate)
  and service tier, so it can only be improved by running a better airline.
  A carrier with no operating history reads as neutral, which is a real
  weakness: a startup is *unmeasured*, not proven.
- **Network value** prices route positions and hubs above the metal flying
  them — the slot, the gate, the market presence.

---

## 3. Why carriers combine — and the test the brief asked for

Three rationales, following the standard taxonomy rather than a single
"is it cheap?" test:

| rationale | condition | the gain |
|---|---|---|
| `HORIZONTAL` | overlapping networks | scale; duplicate legs consolidate |
| `COMPLEMENTARY` | networks barely overlap | connectivity — each becomes the other's feed |
| `SURVIVAL` | **neither can compete alone** | existence: combined they reach minimum efficient scale |

`SURVIVAL` is deliberately the only rationale that can approve a deal with
weak synergies, because that is the real-world condition under which such
deals happen — and it is gated on an explicit test, `Position.cannot_compete_alone()`:

```
outmatched  = not the leader AND the leader has ≥2x your share
cannot_compete_alone = outmatched AND (sub-scale OR losing with a short runway)
```

Being small is **not** enough: a healthy niche carrier is small on purpose,
and the scenario asserts that the same carrier at the same size flips from
"cannot compete alone" to "viable" purely on the sign of its cash flow.
Without that gate, "we are both losing money" would justify every merger in
the game — which is precisely the reasoning that produces bad ones.

A survival deal also prices differently: the control premium collapses from
28% to 5%, because neither party has an alternative, and it accepts a 14-year
payback against the normal 7.

Execution transfers fleet, routes, crews, hubs **and debt** — buying an
airline means buying what it owes, and a valuation that ignored that would
systematically overpay — and consolidates duplicated *directional* legs.

### What is missing, and it is the big one

**There is no regulator.** Real horizontal mergers between large overlapping
carriers are blocked, or cleared only against divestitures. Here they merely
get expensive. An antitrust gate — refuse or force divestiture above a
combined share threshold on overlapping markets — is the single most
obviously missing piece and would change AI behaviour materially.

---

## 4. The historic-data question

> *Could we model this using historic data — under what circumstances have
> companies, airlines or otherwise, merged, to combat competition, vertically
> integrate or horizontally integrate?*

Direct answer: **partly, and the airline-specific half is genuinely
obtainable.** Three tiers, by how much they'd cost and how much they'd buy.

### Tier 1 — observed carrier consolidation from BTS T-100 (obtainable)

T-100 carries `UNIQUE_CARRIER` on every segment row. A merger is *visible* in
it as a signature, without any M&A database at all:

- a carrier code's traffic goes to zero over one or two quarters, while
- another carrier's traffic on the same airports rises by a similar amount,
- and the combined network's overlapping routes thin out afterwards.

That gives a labelled set of real consolidation events with, for each one, the
**pre-merger condition of both parties**: relative size, network overlap
(shared airports and directional legs), hub structure, and traffic trend. That
is exactly the feature set `merger_case()` currently guesses at, and it would
let the three rationales be *classified from data* rather than asserted:
overlap-heavy pairs vs complementary pairs vs both-shrinking pairs.

- **Cost:** medium. The warehouse and reader pattern already exist for T-100;
  what is missing is that the committed corpus is distilled *without* the
  carrier dimension, so `distill.py` would need a carrier-level table.
- **Buys:** the *conditions* under which airlines actually combined, and a
  base rate — how often a carrier in a given position merged rather than
  shrank or failed.
- **Does not buy:** the price paid. T-100 has no financials.

### Tier 2 — the acquisition price (harder)

Nothing free and structured covers airline transaction values at the fidelity
`CONTROL_PREMIUM` and `GOING_CONCERN_MULTIPLE` would need. SEC EDGAR full-text
search reaches merger agreements and 8-Ks for US public carriers, which is a
real but small sample (a few dozen transactions over decades) requiring
document parsing. The honest conclusion is that these two coefficients stay
**game-balance heuristics**, and should be labelled as such rather than dressed
up with a spurious citation.

### Tier 3 — the general cross-industry question (out of scope, and say so)

"Under what circumstances do firms merge" across *all* industries is an
empirical-economics literature, not a dataset one can join to an airline sim.
The stylised findings it would contribute — consolidation clusters in
downturns; horizontal mergers concentrate in industries with high fixed costs
and excess capacity; vertical integration follows input-supply risk — are
already what the three rationales encode qualitatively. Attempting to fit
coefficients to them from outside the airline industry would be borrowing
authority the data does not confer.

### Recommendation

**Do Tier 1.** It is the only one that is both obtainable and specific, it
reuses ingest machinery that already exists, and it targets the part of the
model that is currently weakest — *when* a merger should be triggered, rather
than what it should cost. Leave Tier 2 as heuristics with the label on, and
do not attempt Tier 3.

Concretely:

1. `btsdata/distill.py` gains a carrier-level table: carrier × month × airport
   traffic, plus directional legs.
2. A one-off analysis identifies disappearance/absorption events and
   characterises each party immediately before them.
3. `MIN_EFFICIENT_SHARE`, the 2× leader ratio, and `SHORT_RUNWAY_DAYS` are
   re-derived from the observed distribution instead of chosen — with the
   caveat that survivorship is severe (carriers that merged are not a random
   sample of carriers that *could* have).
4. `MANIFEST.json` tags the carrier table MEASURED and anything fitted from
   it DERIVED, the same discipline the route corpus already follows.

---

## 5. What a real itinerary model would take

Both features are limited by the same absence: there is no passenger object
that knows where it is actually going. Adding one would mean:

- an O&D demand matrix keyed on *true* origin and destination, not per leg;
- itinerary enumeration (nonstop, one-stop, two-stop) with a connection-time
  and misconnect model;
- an allocation step that assigns O&D demand to itineraries rather than legs,
  with the arbiter resolving per itinerary;
- revenue proration across the operating carriers of each leg.

That is a substantial rewrite of the demand core — every subsystem that
currently reads `world.demand[market_key(spec)]` would change — and it would
make the feed index in `alliance.py` redundant, which is the honest way to
read the current mechanism: a stand-in, sized to be useful, for a model the
engine does not yet have.

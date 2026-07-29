# Per-cabin demand — investigation & options

Status: **investigation / plan. Not implemented.** The seam it plugs into
*is* implemented and is inert at its default (see §6). Written to agree an
approach before any data work starts, in the style of
`docs/route-data-design.md`.

Goal (as requested): make the demand for each CABIN on a route depend on the
market rather than on one global constant, using **ZIP-code median income
within about an hour of the airport** as the driving signal. Below: what the
model does today, what the data actually supports, and four candidate
solutions with their costs and their failure modes.

---

## 1. What the sim does today

Route demand splits into three traveler segments (business / leisure /
connecting). Each segment's demand is then partitioned across cabins by a
**single global table**, `route.SEGMENT_CABIN_SPLIT`:

| segment | cabins it feeds |
|---|---|
| BUSINESS | 12.5% FIRST, 87.5% BUSINESS |
| LEISURE | 15.22% PREMIUM, 84.78% ECONOMY |
| CONNECTING | 100% ECONOMY |

Those fractions are game-balance defaults chosen to match the demand-share
ratios already in `finance_cabin.DEFAULT_SEAT_CLASSES`. They are the same on
every route in the world. So today:

- ORD–LGA and ORD–PIT have **identical** premium propensity;
- the business/leisure SEGMENT split is set by the caller
  (`default_segments(total, business_frac, leisure_frac)`) and the data-built
  world passes one ratio for every route;
- configuring a first-class cabin is worth the same on a holiday market as on
  a financial-district trunk, which is the one thing everybody knows is false.

That is the gap. It matters more now than it did: with per-cabin pricing
(`RouteOp.cabin_prices`) a player can price four products per route, and
pricing four products against one global demand shape is a decision without
information behind it.

**Also note what is NOT the gap.** Cabin *capacity* is now geometric and
per-airframe (`airlinesim/cabin.py`); this document is only about the demand
side.

---

## 2. What the committed corpus can and cannot tell us

From `airlinesim/data/MANIFEST.json` and the BTS work already done:

| Signal | Available? |
|---|---|
| passengers per pair, monthly | **yes**, measured (T-100 Market, censored) |
| connecting share per segment | yes, *when DB1B is loaded* (coupons) |
| average nonstop market fare | yes, when DB1B is loaded (`market_coupons = 1`) |
| **fare dispersion** within a market | in principle — DB1B is a 10% coupon sample with a fare per itinerary |
| trip purpose (business vs leisure) | **no. No BTS source carries it.** |
| seats by cabin, or premium-cabin sales | **no.** Not in T-100 Market; not in Segment either (Segment has SEATS, not seats *by cabin*) |
| passenger income | **no** |

So the premium share of a market **cannot be measured from BTS at all**. Any
model of it is an inference from something correlated. That is the honest
framing for everything below: we are choosing a *proxy*, and the quality of
the work is in stating which proxy and how wrong it can be.

Income is a good proxy candidate because the causal story is short and
uncontroversial (higher-income catchment → more discretionary and corporate
premium purchase) and because the data is free, public, stable and joinable
on geography.

---

## 3. The income data itself

**Source.** Census Bureau American Community Survey (ACS) 5-year estimates,
table **B19013** — *Median Household Income in the Past 12 Months*, published
at **ZCTA** (ZIP Code Tabulation Area) granularity. ~33,000 ZCTAs, national
coverage, free, no key required for the bulk files; an API key is free for
`api.census.gov`. 5-year estimates are the right vintage here: 1-year
estimates are only published for areas ≥65,000 people, which excludes most
ZCTAs outright.

**Geography.** ZCTA centroids and boundaries come from the Census
**Gazetteer** files (a plain TSV of ZCTA → land area, lat, lon — small, no
GIS dependency) or TIGER/Line shapefiles (proper polygons, needs a shapefile
reader).

**Population weights.** Median income alone is not enough — a catchment is a
weighted average, and the weight is households. ACS **B11001** (household
count) or **B01003** (population) at the same ZCTA level supplies it.

Three files, all public, all static enough to snapshot. Total download is on
the order of tens of MB and distills to a few hundred rows (one per corpus
airport), consistent with how `airlinesim/data/` already works.

**The caveat that must ride along:** median *household* income of a
residential ZCTA is not the income of the *travelers* departing that airport.
It ignores business travel paid by an employer (the largest premium-cabin
buyer), ignores inbound visitors entirely, and ignores that a metro's premium
demand concentrates in a few ZCTAs rather than spreading evenly. It is a
proxy. It should be labeled HEURISTIC in the manifest, never MEASURED —
the *income* is measured, the *premium propensity derived from it* is not.

---

## 4. "Within one hour" — four ways to define the catchment

This is the part that decides the cost of the whole feature, so the options
are laid out by increasing fidelity. Each produces the same output:
`AirportSpec.catchment_income_index` (a household-weighted median income for
the airport, relative to the national median).

### Option A — fixed-radius circle (no routing at all)

Take every ZCTA whose centroid is within R km of the airport; weight by
households; done. R ≈ **80 km** is the usual stand-in for an hour's drive.

- **Cost:** very low. Haversine over ~33k centroids per airport; the whole
  corpus computes in seconds with the `haversine()` already in `route.py`.
- **Deps:** none beyond stdlib. Fits the project's pure-stdlib rule exactly.
- **Wrong where:** geography that isn't a circle. A coastal airport's circle
  is half ocean (weights are fine — no ZCTAs there — but the radius is then
  effectively smaller by land). Mountains, bridges, and one-road-in valleys
  are invisible. Worst of all, it cannot distinguish airports **inside one
  metro**: LGA, JFK and EWR at 80 km all return essentially the New York
  metro, i.e. the same index — and telling those three apart is the whole
  point of the exercise.
- **Verdict:** the right *first* implementation, and a permanently useful
  fallback for airports the fancier method can't resolve. Not sufficient
  alone.

### Option B — radius + distance decay, and share the catchment between
### competing airports

Same circle, but two fixes:

1. Weight each ZCTA by `households × exp(-d / d₀)` (d₀ ≈ 30 km) instead of a
   hard cut, so nearby ZIPs dominate and the edge of the circle stops being a
   cliff.
2. When several corpus airports claim the same ZCTA, **split** that ZCTA's
   households between them by a gravity share, e.g. proportional to
   `airport_traffic / distance^β`. That is exactly the structure of the
   gravity fit already in `routedata.py`, so it reuses a calibration approach
   the project has already validated rather than inventing one.

- **Cost:** low. Same inputs as A, ~40 more lines, still stdlib.
- **Wrong where:** the share rule is a model, not a measurement, and β is a
  free parameter with nothing in the corpus to fit it against. Still road-blind.
- **Verdict:** **the recommended first real implementation.** It fixes A's
  fatal multi-airport-metro failure with data already in hand (the corpus
  carries per-airport traffic and runway length, which is what `ai.airport_fit`
  already uses to tell a primary field from a reliever).

### Option C — true drive-time isochrones

Compute an actual 60-minute drive polygon per airport and take the ZCTAs
inside it.

- **How:** either a hosted routing API (Mapbox Isochrone, HERE, Google) —
  free tiers exist but all require a key and network access at build time —
  or self-hosted **OSRM/Valhalla** over an OpenStreetMap extract, which is a
  container and a several-GB regional extract.
- **Cost:** high. This is a build-time pipeline with an external service or a
  large binary dependency. It is dev-time only (like `btsdata/`), so it never
  touches the runtime rule about third-party deps — but it does mean the
  distilled artifact can no longer be regenerated by a contributor with just
  a Python install.
- **Wrong where:** less wrong than A/B, but it is a *free-flow* drive time
  unless traffic-aware routing is paid for. A free-flow hour into Manhattan
  at 8am is fiction.
- **Verdict:** the correct answer if this ever needs to be defensible rather
  than plausible. Overkill for the current fidelity of the sim, where runway
  length and traffic rank are the airport-character signals.

### Option D — skip the catchment; regress premium propensity on fares

Don't model income at all. When DB1B is loaded, take the **fare distribution**
within each nonstop market and use its dispersion (e.g. P90/P50 fare ratio) as
the premium signal directly: markets where the top decile pays a large
multiple of the median are markets with a real front cabin.

- **Cost:** medium — no new data source (DB1B is already planned), but it
  needs coupon-level fares retained through distillation rather than just the
  market average, which is a change to `btsdata/distill.py` and a bigger
  snapshot.
- **Wrong where:** DB1B is a 10% sample, so thin markets are noisy; fare
  dispersion also reflects advance-purchase and refundability spread within
  economy, not only cabin mix; and fares carry no deflator across vintages.
- **Verdict:** the most *direct* evidence available, and it does not depend on
  a residential-income proxy at all. Best combined with B rather than chosen
  against it — see §5.

### Comparison

| | A radius | B radius+decay+share | C isochrone | D fare dispersion |
|---|---|---|---|---|
| new runtime deps | none | none | none (dev-time only) | none |
| new dev-time deps | none | none | OSRM/OSM or API key | none |
| distinguishes LGA/JFK/EWR | **no** | yes (modeled) | yes (measured) | yes (measured) |
| effort | ~1 day | ~2 days | ~1–2 weeks | ~3 days (after DB1B) |
| honesty label | HEURISTIC | HEURISTIC | DERIVED | DERIVED |

**Recommendation: B now, D when DB1B lands, C only if it ever needs to be
defensible.** B and D measure different things (who lives there vs what got
paid), so the useful end state is both, with D trusted where the sample is
thick enough and B filling in where it isn't — the same
EXACT/COMPARABLE/SYNTHETIC tiering `routedata.py` already uses, applied to a
second quantity.

---

## 5. From an income index to a cabin split

Whichever option supplies `catchment_income_index` per airport, the route-level
step is the same and is deliberately small:

```
propensity(route) = ((income_index(origin) × income_index(dest)) ** 0.5) ** γ
```

- Geometric mean of the two endpoints, because both ends of a market
  contribute travelers — the same reasoning `route.service_desirability()`
  already uses for airport access.
- γ is a single elasticity knob controlling how hard income moves the cabin
  mix. γ = 0 disables the whole feature; γ ≈ 1 makes a catchment with 1.4×
  the national median income roughly 1.4× as premium-inclined per rung.
- Clamp the result (say 0.5–2.0). An unclamped power law on a long-tail
  income distribution will hand some suburb a first-class market ten times the
  size of anyone else's, which is exactly the class of runaway that produced
  nine-figure fares in the AI pricing loop.

The propensity then flows into `route.cabin_split_for()`, which re-weights
each segment's cabin fractions by `propensity ** cabin_rank` and
**re-normalizes within the segment**. Re-normalizing is the load-bearing part:
a wealthier market moves travelers between cabins, it does not add travelers.
A version that scaled premium demand without taking it out of economy would
make every market richer in total, and total route demand is measured — it is
not ours to inflate.

Second-order effect worth taking at the same time, since it costs nothing:
income should also move the **business/leisure segment split** that
`default_segments()` takes as an argument, not just the cabin split inside the
business segment. `databuilder` currently passes one business fraction for
every route.

---

## 6. What is already in place (the seam)

Implemented, neutral, and doing nothing until data arrives:

| Piece | Where | Default |
|---|---|---|
| `AirportSpec.catchment_income_index` | `engine.py` | `0.0` = unknown |
| `RouteSpec.premium_propensity` | `engine.py` | `1.0` = global split |
| `DemandMarket.premium_propensity` | `engine.py` | `1.0`, copied from the route |
| `route.cabin_split_for(segment, propensity)` | `route.py` | `1.0` returns `SEGMENT_CABIN_SPLIT` **byte-identical** |
| `cabin_demand_on(..., premium_propensity)` | `route.py` | threaded from the arbiter |

`airlinesim run cabin` asserts the neutrality: propensity 1.0 reproduces the
global split exactly, a tilt moves demand between cabins in the right
direction, and the total is conserved to within floating point. So this can be
merged, shipped and left alone indefinitely without changing a single number,
and turning it on later is a data-loading change rather than an engine change.

---

## 7. Implementation plan (option B)

1. **Ingest, dev-time only** — `btsdata/census.py`: fetch ACS B19013 + B11001
   at ZCTA level and the Gazetteer centroid file; cache like the other BTS
   downloads. Must never be imported by runtime code, same rule as the rest of
   `btsdata/`.
2. **Distill** — for each of the 300 corpus airports, compute the
   decayed, competition-shared household-weighted median income; divide by the
   national median; write `catchment.json` into `airlinesim/data/` alongside
   `gravity.json`. Add the `package-data` entry (a missing one is how
   `btsdata/fixtures/*.csv` shipped broken).
3. **Manifest** — record income as MEASURED with its ACS vintage, and the
   catchment index itself as HEURISTIC, naming the radius, the decay constant
   and the share rule. Record the national median used as the denominator.
4. **Serve** — `routedata.RouteDataProvider` sets `catchment_income_index` on
   each `AirportSpec` and `premium_propensity` on each generated `RouteSpec`
   from the formula in §5. Airports missing from the file stay at the neutral
   default; a corpus with no `catchment.json` behaves exactly as today.
5. **Withhold on thin evidence**, mirroring the gravity fit: if fewer than N
   ZCTAs contribute to an airport's catchment, or the corpus median can't be
   computed, leave that airport neutral rather than serving a fabricated
   index.
6. **Extend `airlinesim run cabin`** with the real-data case: assert a
   high-income pair gets a bigger front cabin than a low-income pair of the
   same size, and that total demand across cabins is unchanged.
7. **Then, and only then, let the AI read it** — `ai._cabin_for` currently
   configures cabins off a fixed archetype fraction. With a per-route
   propensity available it should size the front cabin to the routes it
   actually flies. That is a separate change and should not be bundled in.

## 8. Open questions

- **Should propensity move fares as well as demand?** A wealthier market
  plausibly sustains higher fares at the same load factor. `DemandMarket`
  already has `reference_price` for exactly that, and it is currently never
  set. Wiring both from one index risks double-counting.
- **Inbound vs outbound asymmetry.** A leisure destination's catchment income
  is nearly irrelevant — its travelers live at the other end. The geometric
  mean quietly assumes symmetry. Splitting it needs a directional model of who
  originates the trip, which the corpus does not carry.
- **γ has nothing to fit against.** With no measured per-cabin sales anywhere
  in BTS, γ can be *chosen* but not *validated*. It should be tuned for game
  balance and labeled as such, exactly like `CARRIER_MARKET_SHARE`.

"""
ROUTE PLANNER CHECK — phase 0: the goldens
==========================================

The engine already contains a route forecast. `ai._evaluate()` estimates a
candidate pair's daily profit from corpus demand, a share model, a fare and
the costs the engine will actually charge, and it is the arithmetic three AI
carriers plan their entire networks with.

The route-planning tool (`docs/route-planning-design.md`) is built by
EXTRACTING that forecast into `planner.py` and having `ai.py` call it, rather
than writing a second one for the player. A second forecast would drift from
the first, and the drift would land on the player: a planner promising
$41k/day where the engine charges costs producing $12k/day means being
out-planned by rivals reading truer numbers, for reasons invisible from
inside the game.

That refactor must not change a single AI decision. So before any of it
moves, this scenario records what `_evaluate` answers today — every
(archetype x aircraft type x airport pair) case below — and asserts the
extracted path reproduces it TO THE CENT.

These goldens are not scaffolding to delete once phase 1 lands. They stay,
because the same risk recurs every time the planner is improved: adding
ownership cost or real crew sizing to the PLAYER's forecast must not
silently re-tune three AI carriers. Improvements arrive as new cost bases
and frequency policies, and the AI keeps passing the values it passes today
until moving it is a deliberate, measured balance change.

WHAT THE CASES COVER
  - all three archetypes, because stage limits, service tier, fare ratio and
    airport fit all enter the forecast and all differ between them
  - five types spanning regional / narrowbody / widebody, chosen so the
    range, runway and seat-window rejection branches are all reached
  - thirty pairs spanning a 100 km hop to a Pacific crossing, primary and
    secondary fields, measured (EXACT) and fitted (COMPARABLE) corpus tiers

WHAT THEY DO NOT COVER, and it is worth knowing before trusting them: every
recorded reference fare is $200.00. That is not a fixture artefact — DB1B is
not loaded in the committed corpus, so `suggested_price` returns "engine
default (no DB1B fare for this pair)" for every pair in it, and the AI prices
its whole network off one flat number. The goldens therefore pin the cost and
demand sides hard and the fare side not at all. Loading fares would move all
450 of these legitimately.

THE CORPUS IS PART OF THE FIXTURE. These numbers are tied to the committed
snapshot, so a corpus refresh legitimately invalidates them. The vintage is
recorded and reported alongside any mismatch, so the two causes are never
confused:

    vintage matches, values differ  -> a code change moved AI behaviour
    vintage differs                 -> regenerate, AFTER confirming the code
                                       path is unchanged on the old corpus

Regenerate with:  python -m airlinesim.scenarios.scenario_planner --regenerate

Run:  airlinesim run planner
"""
from __future__ import annotations

import sys

from airlinesim import actions
from airlinesim.ai import AICarrierSubsystem, archetype
from airlinesim.databuilder import build_world_from_data
from airlinesim.engine import AircraftSpec

CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")


# ============================================================
# THE FIXTURE
# ============================================================

GOLDEN_HUB = "ORD"
GOLDEN_DESTS = 4
GOLDEN_VINTAGE = "t100_market 2023-2025"

GOLDEN_ARCHETYPES = ("Low-Cost", "Legacy", "Regional")

# Five types, chosen for the branches they reach rather than for variety:
# E175 is short-range enough to fail transcons, B77W needs 3,100 m so it
# fails at every secondary field, and the widebodies blow the corpus seat
# window on thin pairs.
GOLDEN_TYPES = ("E175", "A320", "B752", "B789", "B77W")

# Thirty pairs. Grouped by what each group is here to exercise; the grouping
# is a comment, the tuple order is the fixture key order and must not change
# without regenerating.
GOLDEN_PAIRS = (
    # short hops — the per-seat cost curve's expensive end, and below some
    # archetypes' min_stage_km
    ("ORD", "MKE"), ("ORD", "DTW"), ("LGA", "BOS"), ("SFO", "LAX"),
    ("DAL", "HOU"), ("ATL", "CLT"),
    # medium trunk
    ("ORD", "LGA"), ("ORD", "DEN"), ("ATL", "MCO"), ("DFW", "PHX"),
    ("MSP", "DCA"), ("BNA", "MDW"), ("PDX", "SLC"), ("CLE", "BUF"),
    # transcon
    ("JFK", "LAX"), ("ORD", "SFO"), ("BOS", "SEA"), ("EWR", "SAN"),
    ("MIA", "SEA"), ("ATL", "PDX"),
    # long and off-window — Alaska, Hawaii, the territories
    ("SEA", "HNL"), ("LAX", "HNL"), ("ORD", "ANC"), ("SJU", "JFK"),
    ("GUM", "HNL"),
    # runway-constrained: LGA is 2,134 m, so it refuses the widebodies
    ("LGA", "DEN"), ("LGA", "MIA"),
    # thin pairs, where the corpus has no measurement and the tier is
    # COMPARABLE — a fitted gravity estimate, not a measurement
    ("FAT", "BTV"), ("BUR", "PVD"), ("SNA", "BDL"),
)


def golden_world():
    """The fixed world every golden is measured on."""
    world, engine, _report = build_world_from_data(
        hub=GOLDEN_HUB, n_destinations=GOLDEN_DESTS, verbose=False)
    return world, engine


def evaluate_all(world, engine) -> dict:
    """
    Every fixture case through `ai._evaluate`, keyed `archetype|type|ORG-DST`.

    Deliberately calls the AI's own method rather than a copy of it: the whole
    value of the goldens is that they run the path the AI runs. When phase 1
    turns `_evaluate` into a wrapper over `planner.evaluate_route`, this
    function does not change — which is the point.
    """
    ai = AICarrierSubsystem(profiles={})
    # `_evaluate` counts incumbents across every carrier, which the subsystem
    # normally receives at tick time. Nothing here ticks, so wire it directly.
    ai._players = list(engine.players)
    player = engine.players[0]

    out = {}
    for arch_name in GOLDEN_ARCHETYPES:
        arch = archetype(arch_name)
        for spec_id in GOLDEN_TYPES:
            spec = world.repo.get(AircraftSpec, spec_id)
            plane = AICarrierSubsystem._Prospect(spec)
            for o_code, d_code in GOLDEN_PAIRS:
                origin = actions.airport(world, o_code)
                dest = actions.airport(world, d_code)
                res = ai._evaluate(world, player, arch, plane, origin, dest)
                key = f"{arch_name}|{spec_id}|{o_code}-{d_code}"
                out[key] = (None if res is None
                            else (round(res[0], 2), round(res[1], 4)))
    return out


# The recorded answers. Generated by --regenerate; see the module docstring
# before changing any of them by hand (the answer is: don't).
GOLDENS = {
    # --- Low-Cost / E175 ---
    "Low-Cost|E175|ORD-MKE": None,
    "Low-Cost|E175|ORD-DTW": (9027.32, 200.0),
    "Low-Cost|E175|LGA-BOS": None,
    "Low-Cost|E175|SFO-LAX": (5417.37, 200.0),
    "Low-Cost|E175|DAL-HOU": (15540.2, 200.0),
    "Low-Cost|E175|ATL-CLT": (3524.2, 200.0),
    "Low-Cost|E175|ORD-LGA": (875.0, 200.0),
    "Low-Cost|E175|ORD-DEN": (-9411.69, 200.0),
    "Low-Cost|E175|ATL-MCO": (872.59, 200.0),
    "Low-Cost|E175|DFW-PHX": (-4690.72, 200.0),
    "Low-Cost|E175|MSP-DCA": (1589.23, 200.0),
    "Low-Cost|E175|BNA-MDW": (11371.89, 200.0),
    "Low-Cost|E175|PDX-SLC": (7283.06, 200.0),
    "Low-Cost|E175|CLE-BUF": (18308.83, 200.0),
    "Low-Cost|E175|JFK-LAX": None,
    "Low-Cost|E175|ORD-SFO": (-17155.5, 200.0),
    "Low-Cost|E175|BOS-SEA": None,
    "Low-Cost|E175|EWR-SAN": (-21544.27, 200.0),
    "Low-Cost|E175|MIA-SEA": None,
    "Low-Cost|E175|ATL-PDX": (-20489.43, 200.0),
    "Low-Cost|E175|SEA-HNL": None,
    "Low-Cost|E175|LAX-HNL": None,
    "Low-Cost|E175|ORD-ANC": None,
    "Low-Cost|E175|SJU-JFK": (-7871.65, 200.0),
    "Low-Cost|E175|GUM-HNL": None,
    "Low-Cost|E175|LGA-DEN": (-18107.28, 200.0),
    "Low-Cost|E175|LGA-MIA": (-1865.92, 200.0),
    "Low-Cost|E175|FAT-BTV": None,
    "Low-Cost|E175|BUR-PVD": None,
    "Low-Cost|E175|SNA-BDL": None,
    # --- Low-Cost / A320 ---
    "Low-Cost|A320|ORD-MKE": None,
    "Low-Cost|A320|ORD-DTW": (42471.37, 200.0),
    "Low-Cost|A320|LGA-BOS": None,
    "Low-Cost|A320|SFO-LAX": (38095.24, 200.0),
    "Low-Cost|A320|DAL-HOU": (48940.81, 200.0),
    "Low-Cost|A320|ATL-CLT": (37022.41, 200.0),
    "Low-Cost|A320|ORD-LGA": (30642.93, 200.0),
    "Low-Cost|A320|ORD-DEN": (19212.65, 200.0),
    "Low-Cost|A320|ATL-MCO": (33056.95, 200.0),
    "Low-Cost|A320|DFW-PHX": (24084.41, 200.0),
    "Low-Cost|A320|MSP-DCA": (29898.5, 200.0),
    "Low-Cost|A320|BNA-MDW": None,
    "Low-Cost|A320|PDX-SLC": (37810.66, 200.0),
    "Low-Cost|A320|CLE-BUF": (21753.56, 200.0),
    "Low-Cost|A320|JFK-LAX": (-9966.99, 200.0),
    "Low-Cost|A320|ORD-SFO": (4408.62, 200.0),
    "Low-Cost|A320|BOS-SEA": (-9459.31, 200.0),
    "Low-Cost|A320|EWR-SAN": (-4254.51, 200.0),
    "Low-Cost|A320|MIA-SEA": None,
    "Low-Cost|A320|ATL-PDX": (-1336.81, 200.0),
    "Low-Cost|A320|SEA-HNL": None,
    "Low-Cost|A320|LAX-HNL": (-7516.97, 200.0),
    "Low-Cost|A320|ORD-ANC": None,
    "Low-Cost|A320|SJU-JFK": (15464.79, 200.0),
    "Low-Cost|A320|GUM-HNL": None,
    "Low-Cost|A320|LGA-DEN": (5128.03, 200.0),
    "Low-Cost|A320|LGA-MIA": (25188.61, 200.0),
    "Low-Cost|A320|FAT-BTV": (-62050.4, 200.0),
    "Low-Cost|A320|BUR-PVD": None,
    "Low-Cost|A320|SNA-BDL": None,
    # --- Low-Cost / B752 ---
    "Low-Cost|B752|ORD-MKE": None,
    "Low-Cost|B752|ORD-DTW": (45443.51, 200.0),
    "Low-Cost|B752|LGA-BOS": None,
    "Low-Cost|B752|SFO-LAX": (40290.15, 200.0),
    "Low-Cost|B752|DAL-HOU": (51868.87, 200.0),
    "Low-Cost|B752|ATL-CLT": (40049.48, 200.0),
    "Low-Cost|B752|ORD-LGA": (29885.94, 200.0),
    "Low-Cost|B752|ORD-DEN": (17295.59, 200.0),
    "Low-Cost|B752|ATL-MCO": (34751.22, 200.0),
    "Low-Cost|B752|DFW-PHX": (22320.29, 200.0),
    "Low-Cost|B752|MSP-DCA": (27661.82, 200.0),
    "Low-Cost|B752|BNA-MDW": None,
    "Low-Cost|B752|PDX-SLC": (37824.3, 200.0),
    "Low-Cost|B752|CLE-BUF": (17798.98, 200.0),
    "Low-Cost|B752|JFK-LAX": (-23751.2, 200.0),
    "Low-Cost|B752|ORD-SFO": (-4670.47, 200.0),
    "Low-Cost|B752|BOS-SEA": (-23389.65, 200.0),
    "Low-Cost|B752|EWR-SAN": (-17669.6, 200.0),
    "Low-Cost|B752|MIA-SEA": None,
    "Low-Cost|B752|ATL-PDX": (-12862.17, 200.0),
    "Low-Cost|B752|SEA-HNL": None,
    "Low-Cost|B752|LAX-HNL": (-21926.71, 200.0),
    "Low-Cost|B752|ORD-ANC": None,
    "Low-Cost|B752|SJU-JFK": (8183.57, 200.0),
    "Low-Cost|B752|GUM-HNL": None,
    "Low-Cost|B752|LGA-DEN": (-2255.78, 200.0),
    "Low-Cost|B752|LGA-MIA": (21679.09, 200.0),
    "Low-Cost|B752|FAT-BTV": (-83042.81, 200.0),
    "Low-Cost|B752|BUR-PVD": None,
    "Low-Cost|B752|SNA-BDL": None,
    # --- Low-Cost / B789 ---
    "Low-Cost|B789|ORD-MKE": None,
    "Low-Cost|B789|ORD-DTW": (69372.22, 200.0),
    "Low-Cost|B789|LGA-BOS": None,
    "Low-Cost|B789|SFO-LAX": (62751.3, 200.0),
    "Low-Cost|B789|DAL-HOU": None,
    "Low-Cost|B789|ATL-CLT": (64081.93, 200.0),
    "Low-Cost|B789|ORD-LGA": None,
    "Low-Cost|B789|ORD-DEN": (31992.44, 200.0),
    "Low-Cost|B789|ATL-MCO": (56267.06, 200.0),
    "Low-Cost|B789|DFW-PHX": (37305.95, 200.0),
    "Low-Cost|B789|MSP-DCA": None,
    "Low-Cost|B789|BNA-MDW": None,
    "Low-Cost|B789|PDX-SLC": (56166.74, 200.0),
    "Low-Cost|B789|CLE-BUF": None,
    "Low-Cost|B789|JFK-LAX": (-31462.07, 200.0),
    "Low-Cost|B789|ORD-SFO": (-3497.05, 200.0),
    "Low-Cost|B789|BOS-SEA": (-31376.45, 200.0),
    "Low-Cost|B789|EWR-SAN": (-43013.76, 200.0),
    "Low-Cost|B789|MIA-SEA": None,
    "Low-Cost|B789|ATL-PDX": (-37764.46, 200.0),
    "Low-Cost|B789|SEA-HNL": None,
    "Low-Cost|B789|LAX-HNL": (-30818.71, 200.0),
    "Low-Cost|B789|ORD-ANC": None,
    "Low-Cost|B789|SJU-JFK": (12751.75, 200.0),
    "Low-Cost|B789|GUM-HNL": None,
    "Low-Cost|B789|LGA-DEN": None,
    "Low-Cost|B789|LGA-MIA": None,
    "Low-Cost|B789|FAT-BTV": None,
    "Low-Cost|B789|BUR-PVD": None,
    "Low-Cost|B789|SNA-BDL": None,
    # --- Low-Cost / B77W ---
    "Low-Cost|B77W|ORD-MKE": None,
    "Low-Cost|B77W|ORD-DTW": (93428.86, 200.0),
    "Low-Cost|B77W|LGA-BOS": None,
    "Low-Cost|B77W|SFO-LAX": (92310.95, 200.0),
    "Low-Cost|B77W|DAL-HOU": None,
    "Low-Cost|B77W|ATL-CLT": None,
    "Low-Cost|B77W|ORD-LGA": None,
    "Low-Cost|B77W|ORD-DEN": (30519.96, 200.0),
    "Low-Cost|B77W|ATL-MCO": (84936.11, 200.0),
    "Low-Cost|B77W|DFW-PHX": (59822.78, 200.0),
    "Low-Cost|B77W|MSP-DCA": None,
    "Low-Cost|B77W|BNA-MDW": None,
    "Low-Cost|B77W|PDX-SLC": (55184.47, 200.0),
    "Low-Cost|B77W|CLE-BUF": None,
    "Low-Cost|B77W|JFK-LAX": (-30328.1, 200.0),
    "Low-Cost|B77W|ORD-SFO": (-30121.41, 200.0),
    "Low-Cost|B77W|BOS-SEA": None,
    "Low-Cost|B77W|EWR-SAN": None,
    "Low-Cost|B77W|MIA-SEA": None,
    "Low-Cost|B77W|ATL-PDX": (-71010.66, 200.0),
    "Low-Cost|B77W|SEA-HNL": None,
    "Low-Cost|B77W|LAX-HNL": (-30797.53, 200.0),
    "Low-Cost|B77W|ORD-ANC": None,
    "Low-Cost|B77W|SJU-JFK": None,
    "Low-Cost|B77W|GUM-HNL": None,
    "Low-Cost|B77W|LGA-DEN": None,
    "Low-Cost|B77W|LGA-MIA": None,
    "Low-Cost|B77W|FAT-BTV": None,
    "Low-Cost|B77W|BUR-PVD": None,
    "Low-Cost|B77W|SNA-BDL": None,
    # --- Legacy / E175 ---
    "Legacy|E175|ORD-MKE": None,
    "Legacy|E175|ORD-DTW": None,
    "Legacy|E175|LGA-BOS": None,
    "Legacy|E175|SFO-LAX": None,
    "Legacy|E175|DAL-HOU": None,
    "Legacy|E175|ATL-CLT": None,
    "Legacy|E175|ORD-LGA": (-9160.66, 200.0),
    "Legacy|E175|ORD-DEN": (-26711.57, 200.0),
    "Legacy|E175|ATL-MCO": (-15984.34, 200.0),
    "Legacy|E175|DFW-PHX": (-18619.68, 200.0),
    "Legacy|E175|MSP-DCA": (-2031.54, 200.0),
    "Legacy|E175|BNA-MDW": (9957.66, 200.0),
    "Legacy|E175|PDX-SLC": (6074.09, 200.0),
    "Legacy|E175|CLE-BUF": None,
    "Legacy|E175|JFK-LAX": None,
    "Legacy|E175|ORD-SFO": (-28000.15, 200.0),
    "Legacy|E175|BOS-SEA": None,
    "Legacy|E175|EWR-SAN": (-25411.26, 200.0),
    "Legacy|E175|MIA-SEA": None,
    "Legacy|E175|ATL-PDX": (-31934.39, 200.0),
    "Legacy|E175|SEA-HNL": None,
    "Legacy|E175|LAX-HNL": None,
    "Legacy|E175|ORD-ANC": None,
    "Legacy|E175|SJU-JFK": (-8705.78, 200.0),
    "Legacy|E175|GUM-HNL": None,
    "Legacy|E175|LGA-DEN": (-30031.59, 200.0),
    "Legacy|E175|LGA-MIA": (-6194.02, 200.0),
    "Legacy|E175|FAT-BTV": None,
    "Legacy|E175|BUR-PVD": None,
    "Legacy|E175|SNA-BDL": None,
    # --- Legacy / A320 ---
    "Legacy|A320|ORD-MKE": None,
    "Legacy|A320|ORD-DTW": None,
    "Legacy|A320|LGA-BOS": None,
    "Legacy|A320|SFO-LAX": None,
    "Legacy|A320|DAL-HOU": None,
    "Legacy|A320|ATL-CLT": None,
    "Legacy|A320|ORD-LGA": (28563.26, 200.0),
    "Legacy|A320|ORD-DEN": (9868.78, 200.0),
    "Legacy|A320|ATL-MCO": (24156.01, 200.0),
    "Legacy|A320|DFW-PHX": (18111.44, 200.0),
    "Legacy|A320|MSP-DCA": (34233.73, 200.0),
    "Legacy|A320|BNA-MDW": None,
    "Legacy|A320|PDX-SLC": (44557.7, 200.0),
    "Legacy|A320|CLE-BUF": None,
    "Legacy|A320|JFK-LAX": (-9607.92, 200.0),
    "Legacy|A320|ORD-SFO": (1519.98, 200.0),
    "Legacy|A320|BOS-SEA": (-8676.94, 200.0),
    "Legacy|A320|EWR-SAN": (-165.5, 200.0),
    "Legacy|A320|MIA-SEA": (-13018.64, 200.0),
    "Legacy|A320|ATL-PDX": (-4825.77, 200.0),
    "Legacy|A320|SEA-HNL": (-6353.83, 200.0),
    "Legacy|A320|LAX-HNL": (-5344.44, 200.0),
    "Legacy|A320|ORD-ANC": (-24372.84, 200.0),
    "Legacy|A320|SJU-JFK": (22586.66, 200.0),
    "Legacy|A320|GUM-HNL": (-49207.3, 200.0),
    "Legacy|A320|LGA-DEN": (1159.72, 200.0),
    "Legacy|A320|LGA-MIA": (28816.52, 200.0),
    "Legacy|A320|FAT-BTV": (-64007.84, 200.0),
    "Legacy|A320|BUR-PVD": None,
    "Legacy|A320|SNA-BDL": None,
    # --- Legacy / B752 ---
    "Legacy|B752|ORD-MKE": None,
    "Legacy|B752|ORD-DTW": None,
    "Legacy|B752|LGA-BOS": None,
    "Legacy|B752|SFO-LAX": None,
    "Legacy|B752|DAL-HOU": None,
    "Legacy|B752|ATL-CLT": None,
    "Legacy|B752|ORD-LGA": (29336.27, 200.0),
    "Legacy|B752|ORD-DEN": (9481.71, 200.0),
    "Legacy|B752|ATL-MCO": (27380.29, 200.0),
    "Legacy|B752|DFW-PHX": (17877.33, 200.0),
    "Legacy|B752|MSP-DCA": (33527.05, 200.0),
    "Legacy|B752|BNA-MDW": None,
    "Legacy|B752|PDX-SLC": (46101.34, 200.0),
    "Legacy|B752|CLE-BUF": None,
    "Legacy|B752|JFK-LAX": (-21862.13, 200.0),
    "Legacy|B752|ORD-SFO": (-6029.11, 200.0),
    "Legacy|B752|BOS-SEA": (-21077.28, 200.0),
    "Legacy|B752|EWR-SAN": (-12050.6, 200.0),
    "Legacy|B752|MIA-SEA": (-27159.98, 200.0),
    "Legacy|B752|ATL-PDX": (-14821.13, 200.0),
    "Legacy|B752|SEA-HNL": (-20163.08, 200.0),
    "Legacy|B752|LAX-HNL": (-18224.19, 200.0),
    "Legacy|B752|ORD-ANC": (-48168.61, 200.0),
    "Legacy|B752|SJU-JFK": (16835.44, 200.0),
    "Legacy|B752|GUM-HNL": (-80177.74, 200.0),
    "Legacy|B752|LGA-DEN": (-4694.08, 200.0),
    "Legacy|B752|LGA-MIA": (26837.0, 200.0),
    "Legacy|B752|FAT-BTV": (-85000.26, 200.0),
    "Legacy|B752|BUR-PVD": None,
    "Legacy|B752|SNA-BDL": None,
    # --- Legacy / B789 ---
    "Legacy|B789|ORD-MKE": None,
    "Legacy|B789|ORD-DTW": None,
    "Legacy|B789|LGA-BOS": None,
    "Legacy|B789|SFO-LAX": None,
    "Legacy|B789|DAL-HOU": None,
    "Legacy|B789|ATL-CLT": None,
    "Legacy|B789|ORD-LGA": None,
    "Legacy|B789|ORD-DEN": (31063.57, 200.0),
    "Legacy|B789|ATL-MCO": (55781.12, 200.0),
    "Legacy|B789|DFW-PHX": (39747.98, 200.0),
    "Legacy|B789|MSP-DCA": None,
    "Legacy|B789|BNA-MDW": None,
    "Legacy|B789|PDX-SLC": (71328.77, 200.0),
    "Legacy|B789|CLE-BUF": None,
    "Legacy|B789|JFK-LAX": (-22688.0, 200.0),
    "Legacy|B789|ORD-SFO": (2029.3, 200.0),
    "Legacy|B789|BOS-SEA": (-22179.08, 200.0),
    "Legacy|B789|EWR-SAN": (-12179.48, 200.0),
    "Legacy|B789|MIA-SEA": (-59458.31, 200.0),
    "Legacy|B789|ATL-PDX": (-11381.79, 200.0),
    "Legacy|B789|SEA-HNL": (-23925.18, 200.0),
    "Legacy|B789|LAX-HNL": (-20231.19, 200.0),
    "Legacy|B789|ORD-ANC": (-93705.83, 200.0),
    "Legacy|B789|SJU-JFK": (28288.62, 200.0),
    "Legacy|B789|GUM-HNL": (-139262.27, 200.0),
    "Legacy|B789|LGA-DEN": None,
    "Legacy|B789|LGA-MIA": None,
    "Legacy|B789|FAT-BTV": None,
    "Legacy|B789|BUR-PVD": None,
    "Legacy|B789|SNA-BDL": None,
    # --- Legacy / B77W ---
    "Legacy|B77W|ORD-MKE": None,
    "Legacy|B77W|ORD-DTW": None,
    "Legacy|B77W|LGA-BOS": None,
    "Legacy|B77W|SFO-LAX": None,
    "Legacy|B77W|DAL-HOU": None,
    "Legacy|B77W|ATL-CLT": None,
    "Legacy|B77W|ORD-LGA": None,
    "Legacy|B77W|ORD-DEN": (61417.32, 200.0),
    "Legacy|B77W|ATL-MCO": (92559.18, 200.0),
    "Legacy|B77W|DFW-PHX": (70373.82, 200.0),
    "Legacy|B77W|MSP-DCA": None,
    "Legacy|B77W|BNA-MDW": None,
    "Legacy|B77W|PDX-SLC": (105117.11, 200.0),
    "Legacy|B77W|CLE-BUF": None,
    "Legacy|B77W|JFK-LAX": (-13445.03, 200.0),
    "Legacy|B77W|ORD-SFO": (19642.34, 200.0),
    "Legacy|B77W|BOS-SEA": None,
    "Legacy|B77W|EWR-SAN": None,
    "Legacy|B77W|MIA-SEA": (-100079.91, 200.0),
    "Legacy|B77W|ATL-PDX": (-24729.69, 200.0),
    "Legacy|B77W|SEA-HNL": (-17448.51, 200.0),
    "Legacy|B77W|LAX-HNL": (-12101.0, 200.0),
    "Legacy|B77W|ORD-ANC": (-135891.86, 200.0),
    "Legacy|B77W|SJU-JFK": None,
    "Legacy|B77W|GUM-HNL": (-194211.52, 200.0),
    "Legacy|B77W|LGA-DEN": None,
    "Legacy|B77W|LGA-MIA": None,
    "Legacy|B77W|FAT-BTV": None,
    "Legacy|B77W|BUR-PVD": None,
    "Legacy|B77W|SNA-BDL": None,
    # --- Regional / E175 ---
    "Regional|E175|ORD-MKE": None,
    "Regional|E175|ORD-DTW": (7781.35, 200.0),
    "Regional|E175|LGA-BOS": (12116.89, 200.0),
    "Regional|E175|SFO-LAX": (4449.15, 200.0),
    "Regional|E175|DAL-HOU": (18379.03, 200.0),
    "Regional|E175|ATL-CLT": (-593.3, 200.0),
    "Regional|E175|ORD-LGA": (-539.04, 200.0),
    "Regional|E175|ORD-DEN": (-13668.25, 200.0),
    "Regional|E175|ATL-MCO": (-3210.64, 200.0),
    "Regional|E175|DFW-PHX": (-7628.22, 200.0),
    "Regional|E175|MSP-DCA": (2685.36, 200.0),
    "Regional|E175|BNA-MDW": (13331.45, 200.0),
    "Regional|E175|PDX-SLC": (9322.94, 200.0),
    "Regional|E175|CLE-BUF": (22133.33, 200.0),
    "Regional|E175|JFK-LAX": None,
    "Regional|E175|ORD-SFO": None,
    "Regional|E175|BOS-SEA": None,
    "Regional|E175|EWR-SAN": None,
    "Regional|E175|MIA-SEA": None,
    "Regional|E175|ATL-PDX": None,
    "Regional|E175|SEA-HNL": None,
    "Regional|E175|LAX-HNL": None,
    "Regional|E175|ORD-ANC": None,
    "Regional|E175|SJU-JFK": None,
    "Regional|E175|GUM-HNL": None,
    "Regional|E175|LGA-DEN": None,
    "Regional|E175|LGA-MIA": (-1046.57, 200.0),
    "Regional|E175|FAT-BTV": None,
    "Regional|E175|BUR-PVD": None,
    "Regional|E175|SNA-BDL": None,
    # --- Regional / A320 ---
    "Regional|A320|ORD-MKE": None,
    "Regional|A320|ORD-DTW": (47777.4, 200.0),
    "Regional|A320|LGA-BOS": (52480.23, 200.0),
    "Regional|A320|SFO-LAX": (43679.02, 200.0),
    "Regional|A320|DAL-HOU": (58331.63, 200.0),
    "Regional|A320|ATL-CLT": (39456.91, 200.0),
    "Regional|A320|ORD-LGA": (35780.89, 200.0),
    "Regional|A320|ORD-DEN": (21508.09, 200.0),
    "Regional|A320|ATL-MCO": (35525.71, 200.0),
    "Regional|A320|DFW-PHX": (27698.9, 200.0),
    "Regional|A320|MSP-DCA": (37546.63, 200.0),
    "Regional|A320|BNA-MDW": None,
    "Regional|A320|PDX-SLC": (46402.55, 200.0),
    "Regional|A320|CLE-BUF": (28971.24, 200.0),
    "Regional|A320|JFK-LAX": None,
    "Regional|A320|ORD-SFO": None,
    "Regional|A320|BOS-SEA": None,
    "Regional|A320|EWR-SAN": None,
    "Regional|A320|MIA-SEA": None,
    "Regional|A320|ATL-PDX": None,
    "Regional|A320|SEA-HNL": None,
    "Regional|A320|LAX-HNL": None,
    "Regional|A320|ORD-ANC": None,
    "Regional|A320|SJU-JFK": None,
    "Regional|A320|GUM-HNL": None,
    "Regional|A320|LGA-DEN": None,
    "Regional|A320|LGA-MIA": (32559.96, 200.0),
    "Regional|A320|FAT-BTV": None,
    "Regional|A320|BUR-PVD": None,
    "Regional|A320|SNA-BDL": None,
    # --- Regional / B752 ---
    "Regional|B752|ORD-MKE": None,
    "Regional|B752|ORD-DTW": (52009.54, 200.0),
    "Regional|B752|LGA-BOS": (57084.95, 200.0),
    "Regional|B752|SFO-LAX": (47133.93, 200.0),
    "Regional|B752|DAL-HOU": (62519.7, 200.0),
    "Regional|B752|ATL-CLT": (43743.98, 200.0),
    "Regional|B752|ORD-LGA": (36283.89, 200.0),
    "Regional|B752|ORD-DEN": (20851.03, 200.0),
    "Regional|B752|ATL-MCO": (38479.98, 200.0),
    "Regional|B752|DFW-PHX": (27194.78, 200.0),
    "Regional|B752|MSP-DCA": (36569.95, 200.0),
    "Regional|B752|BNA-MDW": None,
    "Regional|B752|PDX-SLC": (47676.18, 200.0),
    "Regional|B752|CLE-BUF": (25016.66, 200.0),
    "Regional|B752|JFK-LAX": None,
    "Regional|B752|ORD-SFO": None,
    "Regional|B752|BOS-SEA": None,
    "Regional|B752|EWR-SAN": None,
    "Regional|B752|MIA-SEA": None,
    "Regional|B752|ATL-PDX": None,
    "Regional|B752|SEA-HNL": None,
    "Regional|B752|LAX-HNL": None,
    "Regional|B752|ORD-ANC": None,
    "Regional|B752|SJU-JFK": None,
    "Regional|B752|GUM-HNL": None,
    "Regional|B752|LGA-DEN": None,
    "Regional|B752|LGA-MIA": (30310.45, 200.0),
    "Regional|B752|FAT-BTV": None,
    "Regional|B752|BUR-PVD": None,
    "Regional|B752|SNA-BDL": None,
    # --- Regional / B789 ---
    "Regional|B789|ORD-MKE": None,
    "Regional|B789|ORD-DTW": (81608.26, 200.0),
    "Regional|B789|LGA-BOS": None,
    "Regional|B789|SFO-LAX": (75265.07, 200.0),
    "Regional|B789|DAL-HOU": None,
    "Regional|B789|ATL-CLT": (73446.43, 200.0),
    "Regional|B789|ORD-LGA": None,
    "Regional|B789|ORD-DEN": (41217.88, 200.0),
    "Regional|B789|ATL-MCO": (65665.82, 200.0),
    "Regional|B789|DFW-PHX": (47850.44, 200.0),
    "Regional|B789|MSP-DCA": None,
    "Regional|B789|BNA-MDW": None,
    "Regional|B789|PDX-SLC": (71688.62, 200.0),
    "Regional|B789|CLE-BUF": None,
    "Regional|B789|JFK-LAX": None,
    "Regional|B789|ORD-SFO": None,
    "Regional|B789|BOS-SEA": None,
    "Regional|B789|EWR-SAN": None,
    "Regional|B789|MIA-SEA": None,
    "Regional|B789|ATL-PDX": None,
    "Regional|B789|SEA-HNL": None,
    "Regional|B789|LAX-HNL": None,
    "Regional|B789|ORD-ANC": None,
    "Regional|B789|SJU-JFK": None,
    "Regional|B789|GUM-HNL": None,
    "Regional|B789|LGA-DEN": None,
    "Regional|B789|LGA-MIA": None,
    "Regional|B789|FAT-BTV": None,
    "Regional|B789|BUR-PVD": None,
    "Regional|B789|SNA-BDL": None,
    # --- Regional / B77W ---
    "Regional|B77W|ORD-MKE": None,
    "Regional|B77W|ORD-DTW": (119228.54, 200.0),
    "Regional|B77W|LGA-BOS": None,
    "Regional|B77W|SFO-LAX": (111502.73, 200.0),
    "Regional|B77W|DAL-HOU": None,
    "Regional|B77W|ATL-CLT": None,
    "Regional|B77W|ORD-LGA": None,
    "Regional|B77W|ORD-DEN": (68564.37, 200.0),
    "Regional|B77W|ATL-MCO": (101012.88, 200.0),
    "Regional|B77W|DFW-PHX": (77045.28, 200.0),
    "Regional|B77W|MSP-DCA": None,
    "Regional|B77W|BNA-MDW": None,
    "Regional|B77W|PDX-SLC": (87980.96, 200.0),
    "Regional|B77W|CLE-BUF": None,
    "Regional|B77W|JFK-LAX": None,
    "Regional|B77W|ORD-SFO": None,
    "Regional|B77W|BOS-SEA": None,
    "Regional|B77W|EWR-SAN": None,
    "Regional|B77W|MIA-SEA": None,
    "Regional|B77W|ATL-PDX": None,
    "Regional|B77W|SEA-HNL": None,
    "Regional|B77W|LAX-HNL": None,
    "Regional|B77W|ORD-ANC": None,
    "Regional|B77W|SJU-JFK": None,
    "Regional|B77W|GUM-HNL": None,
    "Regional|B77W|LGA-DEN": None,
    "Regional|B77W|LGA-MIA": None,
    "Regional|B77W|FAT-BTV": None,
    "Regional|B77W|BUR-PVD": None,
    "Regional|B77W|SNA-BDL": None,
}


# ============================================================
# CHECKS
# ============================================================

def check_fixture(world):
    print("\n=== FIXTURE ===")
    vintage = getattr(getattr(world, "route_data", None), "vintage", "")
    check("the corpus vintage matches the one the goldens were taken on",
          vintage == GOLDEN_VINTAGE,
          f"live '{vintage}' vs golden '{GOLDEN_VINTAGE}'"
          + ("" if vintage == GOLDEN_VINTAGE else
             " — a refresh invalidates the goldens legitimately; confirm the "
             "code path is unchanged on the OLD corpus, then --regenerate"))
    check("the goldens are recorded",
          bool(GOLDENS),
          f"{len(GOLDENS)} cases"
          if GOLDENS else "empty — run with --regenerate")
    missing = [c for c in GOLDEN_TYPES
               if c not in {s.spec_id for s in world.repo.all(AircraftSpec)}]
    check("every fixture type is in the catalog", not missing,
          f"missing {missing}" if missing else ", ".join(GOLDEN_TYPES))
    absent = [f"{o}-{d}" for o, d in GOLDEN_PAIRS
              if actions.airport(world, o) is None
              or actions.airport(world, d) is None]
    check("every fixture airport is in the corpus", not absent,
          f"missing {absent}" if absent else
          f"{len(GOLDEN_PAIRS)} pairs, {len(GOLDEN_PAIRS) * len(GOLDEN_TYPES) * len(GOLDEN_ARCHETYPES)} cases")


def check_coverage(live: dict):
    """
    The fixture is only worth what it reaches. A set of cases that all reject
    for the same reason would pass forever while proving nothing, so assert
    that both outcomes — and a spread of magnitudes — are actually present.
    """
    print("\n=== COVERAGE ===")
    feasible = {k: v for k, v in live.items() if v is not None}
    rejected = [k for k, v in live.items() if v is None]
    check("the fixture reaches feasible cases", len(feasible) > 100,
          f"{len(feasible)} of {len(live)} price out")
    check("the fixture reaches rejected cases", len(rejected) > 30,
          f"{len(rejected)} rejected (out of stage band, out of range, or "
          f"runway-limited)")
    profits = sorted(v[0] for v in feasible.values())
    check("feasible cases span losses and profits",
          bool(profits) and profits[0] < 0 < profits[-1],
          f"{profits[0]:,.0f} to {profits[-1]:,.0f} $/day" if profits else "none")
    per_arch = {a: sum(1 for k, v in live.items()
                       if k.startswith(a + "|") and v is not None)
                for a in GOLDEN_ARCHETYPES}
    check("every archetype reaches feasible cases",
          all(n > 0 for n in per_arch.values()), str(per_arch))


def check_goldens(live: dict):
    """The assertion the whole scenario exists for."""
    print("\n=== GOLDENS ===")
    if not GOLDENS:
        check("recorded goldens reproduce to the cent", False,
              "no goldens recorded — run with --regenerate")
        return

    missing = sorted(set(GOLDENS) - set(live))
    added = sorted(set(live) - set(GOLDENS))
    check("the live case set matches the recorded one",
          not missing and not added,
          f"{len(missing)} missing, {len(added)} new"
          if (missing or added) else f"{len(live)} cases")

    drifted = []
    for key, want in GOLDENS.items():
        got = live.get(key)
        if want is None or got is None:
            if want is not got:
                drifted.append((key, want, got))
            continue
        if (abs(got[0] - want[0]) > 0.005) or (abs(got[1] - want[1]) > 0.00005):
            drifted.append((key, want, got))

    check("recorded goldens reproduce to the cent", not drifted,
          f"{len(live)} cases, none moved" if not drifted else
          f"{len(drifted)} case(s) moved — AI route evaluation has changed")
    for key, want, got in drifted[:12]:
        print(f"         {key}: golden {want} -> live {got}")
    if len(drifted) > 12:
        print(f"         ... and {len(drifted) - 12} more")


# ============================================================
# REGENERATION
# ============================================================

def regenerate():
    """Print the GOLDENS literal to paste back into this module."""
    world, engine = golden_world()
    live = evaluate_all(world, engine)
    vintage = getattr(getattr(world, "route_data", None), "vintage", "")
    print(f"# corpus vintage: {vintage}")
    print(f'GOLDEN_VINTAGE = "{vintage}"')
    print("GOLDENS = {")
    for arch_name in GOLDEN_ARCHETYPES:
        for spec_id in GOLDEN_TYPES:
            print(f"    # --- {arch_name} / {spec_id} ---")
            for o_code, d_code in GOLDEN_PAIRS:
                key = f"{arch_name}|{spec_id}|{o_code}-{d_code}"
                v = live[key]
                val = "None" if v is None else f"({v[0]}, {v[1]})"
                print(f'    "{key}": {val},')
    print("}")


def main():
    if "--regenerate" in sys.argv:
        regenerate()
        return
    print("ROUTE PLANNER CHECK — phase 0: the goldens")
    print("=" * 70)
    world, engine = golden_world()
    live = evaluate_all(world, engine)
    check_fixture(world)
    check_coverage(live)
    check_goldens(live)
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(CHECKS) else 'FAILURES ABOVE'}")


if __name__ == "__main__":
    main()

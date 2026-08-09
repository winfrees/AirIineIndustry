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

import json
import sys
from pathlib import Path

from airlinesim import actions
from airlinesim.ai import AICarrierSubsystem, archetype
from airlinesim.databuilder import build_world_from_data
from airlinesim.engine import AircraftSpec, AirportSpec

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


# ============================================================
# PHASE 1 — the extracted forecast
# ============================================================

def check_forecast(world, engine):
    """
    The forecast explains itself, and its parts add up.

    `_evaluate` returned a bare `None` for everything it could not price, so
    "why is the 787 not on this list?" was unanswerable. The extracted
    forecast returns the case WITH its reasons; the AI wrapper collapses that
    back to None, which is why the goldens still hold.
    """
    from airlinesim import planner
    print("\n=== FORECAST ===")
    players = list(engine.players)
    player = players[0]
    a320 = world.repo.get(AircraftSpec, "A320")
    b77w = world.repo.get(AircraftSpec, "B77W")
    ord_, lga = actions.airport(world, "ORD"), actions.airport(world, "LGA")

    ok = planner.evaluate_route(world, players, ord_, lga, a320, 3)
    check("a priceable case comes back viable", ok.viable,
          f"ORD-LGA A320 x3: {ok.pax:.0f} pax, ${ok.contribution:,.0f}/day "
          f"contribution, tier '{ok.data_tier}'")
    check("cost lines add up to the contribution the AI reads",
          abs((ok.revenue - ok.costs.direct) - ok.contribution) < 1e-9,
          f"revenue ${ok.revenue:,.0f} - direct ${ok.costs.direct:,.0f}")
    check("the itemised lines sum to the direct cost",
          abs((ok.costs.fuel + ok.costs.maintenance + ok.costs.crew
               + ok.costs.fees) - ok.costs.direct) < 1e-9)
    check("with no ownership charged, absorbed equals contribution",
          not ok.has_ownership and abs(ok.absorbed - ok.contribution) < 1e-9,
          "the AI's path never charges ownership, which is why the goldens "
          "survived adding the line")
    check("break-even fare covers costs at the forecast load",
          abs(ok.break_even_fare * ok.pax - ok.costs.total) < 0.01,
          f"break-even ${ok.break_even_fare:,.2f} vs fare ${ok.fare:,.2f}")
    check("break-even load and fare agree with each other",
          abs(ok.break_even_load * ok.seats_offered * ok.fare
              - ok.costs.total) < 0.01,
          f"break-even load {ok.break_even_load:.1%} at "
          f"{ok.load_factor:.1%} forecast")

    # LGA is 2,134 m; the 777-300ER needs 3,100 m to get airborne.
    bad = planner.evaluate_route(world, players, ord_, lga, b77w, 1)
    check("an infeasible pairing explains itself instead of vanishing",
          not bad.feasible and bool(bad.reasons),
          "; ".join(bad.reasons) or "NO REASONS GIVEN")
    check("the runway rejection names the field and both numbers",
          any("LGA" in r and "runway" in r for r in bad.reasons))

    # Suitability is the OTHER rejection path — what open_route would refuse.
    sr = planner.suitability_reasons(world, ord_, lga, b77w)
    from airlinesim.route import route_can_fly
    spec = planner.route_spec_for(world, ord_, lga)
    _ok2, verbatim = route_can_fly(spec, b77w, ord_, lga)
    check("suitability reasons are route_can_fly's, verbatim",
          list(sr) == list(verbatim),
          "; ".join(sr) if sr else "route_can_fly raised nothing")
    check("suitability and physics are DIFFERENT checks",
          set(sr) != set(bad.reasons),
          "route_can_fly reads the route's banded requirement and the seat "
          "window; the forecast reads the aircraft's own takeoff length")

    # Provenance must survive to the caller, or a fitted estimate reads as a
    # measurement.
    thin = planner.evaluate_route(world, players, actions.airport(world, "FAT"),
                                  actions.airport(world, "BTV"), a320, 1)
    check("every forecast carries its corpus tier",
          bool(ok.data_tier) and bool(thin.data_tier),
          f"ORD-LGA '{ok.data_tier}', FAT-BTV '{thin.data_tier}'")
    check("a measured pair and a fitted one are distinguishable",
          ok.data_tier != thin.data_tier)
    check("the forecast is JSON-safe", isinstance(ok.to_json(), dict)
          and isinstance(json.dumps(ok.to_json()), str))
    _ = player  # the forecast does not read the carrier; frequency does


def check_frequency(world, engine):
    """
    All four limits, and each one reachable as the binding one.

    A limit that can never bind is decoration. Two of these were nearly
    that, and finding out is what the check is for — see the notes on each
    case below.
    """
    from airlinesim import planner
    from airlinesim.crew import DEFAULT_DUTY_LIMITS, DutyLimits
    print("\n=== FREQUENCY ===")
    players = list(engine.players)
    player = players[0]
    a320 = world.repo.get(AircraftSpec, "A320")
    a321 = world.repo.get(AircraftSpec, "A321")
    b789 = world.repo.get(AircraftSpec, "B789")

    def plan(o, d, spec, demand, share, **kw):
        return planner.frequency_plan(
            world, players, actions.airport(world, o), actions.airport(world, d),
            spec, demand_per_day=demand, share=share, **kw)

    names = [l.name for l in plan("ORD", "LGA", a320, 3000, 0.4).limits]
    check("all four limits are reported, in a fixed order",
          names == ["airframe", "demand", "gates", "crew"], str(names))

    # A TYPE RATING IS PART OF THE ANSWER. The carrier's pilots are rated
    # A319/A320/A321; its cabin crew carry no certs and so are universal. A
    # 787 therefore has ten cabin crew and NO cockpit crew available at its
    # own hub, and the planner says the route would be grounded rather than
    # quietly assuming pilots appear. Every case below uses a rated type
    # except the one testing exactly this.
    cockpit, cabin = planner.available_crews(player, "ORD", b789)
    check("an unrated type has no crew, however deep the pool",
          cockpit == 0 and cabin > 0,
          f"B789 at ORD: {cockpit} cockpit, {cabin} cabin — the pilots are "
          f"rated {a320.type_rating or 'A320-family'}, not 787")

    binding = {}

    # AIRFRAME: a ~5.6h leg is an 11h rotation, so one aeroplane manages one a
    # day whatever the market or the crew room could support.
    p_af = plan("ORD", "ANC", a320, 9_000, 0.9, player=player)
    binding["airframe"] = p_af.binding.name

    # DEMAND: a big narrowbody against a thin market. Plenty of aeroplane and
    # plenty of crew; nobody to carry.
    p_dm = plan("ORD", "LGA", a321, 1_200, 0.25, player=player)
    binding["demand"] = p_dm.binding.name

    # GATES: the corpus has 2-gate fields. Squeeze one and the schedule is
    # capped by the airport, not by anything the carrier owns.
    small = min((a for a in world.repo.all(AirportSpec) if a.total_gates > 0),
                key=lambda a: (a.total_gates, a.iata))
    saved = world.gates[small.iata].total_gates
    world.gates[small.iata].total_gates = 1
    p_gt = plan("ORD", small.iata, a320, 5_000, 0.9, player=player)
    binding["gates"] = p_gt.binding.name
    world.gates[small.iata].total_gates = saved

    # CREW: no crew based at a field the carrier does not serve, so the route
    # is GROUNDED — the one case this limit reads zero rather than "thin".
    p_cw = plan("BTV", "ORD", a320, 5_000, 0.9, player=player)
    binding["crew"] = p_cw.binding.name

    for want in ("airframe", "demand", "gates", "crew"):
        got = binding[want]
        detail = {"airframe": p_af, "demand": p_dm,
                  "gates": p_gt, "crew": p_cw}[want]
        check(f"the {want} limit is reachable as the binding one", got == want,
              f"binds on '{got}' at {detail.rotations}/day — "
              f"{detail.binding.detail}")

    check("no plan exceeds any of its own limits",
          all(p.rotations <= min(l.value for l in p.limits) + 1e-9
              for p in (p_af, p_dm, p_gt, p_cw)))
    check("a grounded plan is zero rotations, not a small number",
          p_cw.rotations == 0, p_cw.binding.detail)

    # The airframe limit must charge the RETURN leg. databuilder's own
    # frequency divides by one leg and so permits about twice what a tail
    # flying the out-and-back can manage; the planner must not repeat it.
    one_way = planner.airframe_frequency(2000, 850, rotation=False)
    both = planner.airframe_frequency(2000, 850, rotation=True)
    check("the airframe limit charges the return leg to the same tail",
          abs(one_way - 2 * both) < 1e-9,
          f"{both:.2f}/day as a rotation vs {one_way:.2f} as a bare leg")

    # HOW SLACK THE CREW LIMIT IS, quantified. Crew binds against the airframe
    # exactly when N x max_daily_flight_hours < DAILY_UTILIZATION_H / 2 x
    # CREW_DEPTH, and the leg length cancels out of both sides — so with the
    # shipped constants it is 17.5, and a single rated crew pair at the 9-hour
    # cap (9 < 17.5) binds while two (18) do not. That is a narrow window, and
    # worth knowing before reading much into this limit on a well-staffed
    # station. DutyLimits is authorable reference data, so a stricter regime
    # widens it.
    threshold = planner.DAILY_UTILIZATION_H / 2 * planner.CREW_DEPTH
    thin, _d1 = planner.crew_frequency(1200, 850, 1, 1)
    deep, _d2 = planner.crew_frequency(1200, 850, 2, 2)
    af_same = planner.airframe_frequency(1200, 850)
    check("one rated crew pair binds; two do not",
          thin < af_same <= deep,
          f"1 pair {thin:.2f}/day, airframe {af_same:.2f}/day, "
          f"2 pairs {deep:.2f}/day — crew binds while "
          f"N x {DEFAULT_DUTY_LIMITS.max_daily_flight_hours:.0f}h < "
          f"{threshold:.1f}")

    # And it binds inside a real plan, not only in the arithmetic: one hired
    # crew pair at a station the carrier does not otherwise staff.
    actions.hire_crew(world, player, "COCKPIT", "BUF", 2, 220.0,
                      (a320.type_rating or "A320",))
    actions.hire_crew(world, player, "CABIN", "BUF", 4, 60.0, ())
    p_thin = plan("BUF", "ORD", a320, 5_000, 0.9, player=player)
    check("a thin crew pool binds inside a real plan",
          p_thin.binding.name == "crew" and p_thin.rotations > 0,
          f"BUF-ORD on one hired crew pair: {p_thin.rotations}/day, "
          f"binding '{p_thin.binding.name}' — {p_thin.binding.detail}")
    _ = DutyLimits  # authorable reference data; see the note above

    check("crew requirement and crew ceiling are exact inverses",
          _crew_inverse_holds(),
          "a pool of N sustains N x max_daily / CREW_DEPTH block hours, which "
          "is ai._crew_target solved for N")


# ============================================================
# PHASE 2 — Q1, pair mode, end to end
# ============================================================

def check_pair_plan():
    """
    One city pair, every type, ranked — and the two things most likely to be
    got wrong: where the aeroplane comes from, and which margin decides.
    """
    from airlinesim import planner
    print("\n=== PAIR PLAN ===")
    world, engine = golden_world()
    players = list(engine.players)
    me = players[0]
    ord_ = actions.airport(world, "ORD")

    plan = planner.plan_pair(world, players, me, ord_,
                             actions.airport(world, "DEN"))
    n_types = len(world.repo.all(AircraftSpec))
    check("every type in the catalog is judged, none filtered out",
          len(plan.options) == n_types,
          f"{len(plan.options)} rows for {n_types} types — a type that cannot "
          f"fly the pair is returned WITH its reasons")
    check("options are ranked by absorbed margin, best first",
          _sorted_by_absorbed(plan),
          f"top: {plan.options[0].spec_id} at "
          f"${plan.options[0].forecast.absorbed:,.0f}/day absorbed")
    check("every row states its provenance", bool(plan.data_tier)
          and all(o.forecast.data_tier for o in plan.options),
          f"{plan.data_tier} — {tier_line(plan)}")

    # WHERE THE AEROPLANE COMES FROM. The starting carrier has A321s at ORD,
    # all committed to routes, plus tails at LGA and LAX it cannot use from
    # ORD — the engine has no ferry flights.
    by_id = {o.spec_id: o for o in plan.options}
    a321 = by_id["A321"]
    sources = {t.source for t in a321.tails}
    check("owned tails are found and classified by availability",
          a321.tails and sources <= {planner.SOURCE_IDLE,
                                     planner.SOURCE_COMMITTED,
                                     planner.SOURCE_ELSEWHERE},
          "; ".join(f"{t.tail_number} {t.source}" for t in a321.tails))
    check("a tail based elsewhere is not offered as available here",
          all(t.source == planner.SOURCE_ELSEWHERE
              for t in a321.tails if t.location_iata != "ORD"),
          "location_iata is set at acquisition and no subsystem updates it — "
          "there are no ferry flights, so a tail cannot start from ORD")
    check("a committed tail states what freeing it gives up",
          all(t.note and "contribution" in t.note
              for t in a321.tails if t.source == planner.SOURCE_COMMITTED)
          or not any(t.source == planner.SOURCE_COMMITTED for t in a321.tails))
    unowned = next(o for o in plan.options if not o.tails)
    check("a type the carrier does not own is marked for acquisition",
          unowned.source == planner.SOURCE_ACQUIRE and len(unowned.quotes) == 3,
          f"{unowned.spec_id}: three quotes, "
          f"{sum(1 for q in unowned.quotes if q.approved)} approved")

    # THE RANKING TRAP. Charging ownership is what stops the screen
    # recommending the aeroplane that bankrupts you.
    acq = [o for o in plan.options if o.source == planner.SOURCE_ACQUIRE]
    check("an acquisition is charged ownership; owned metal is not",
          all(o.forecast.costs.ownership > 0 for o in acq)
          and all(o.forecast.costs.ownership == 0 for o in plan.options
                  if o.source != planner.SOURCE_ACQUIRE),
          "a tail you already own is paid for either way, so flying it costs "
          "nothing extra")
    by_contrib = max(acq, key=lambda o: o.forecast.contribution)
    by_absorbed = max(acq, key=lambda o: o.forecast.absorbed)
    check("ranking on contribution would pick a different, worse aeroplane",
          by_contrib.spec_id != by_absorbed.spec_id,
          f"contribution picks {by_contrib.spec_id} "
          f"(${by_contrib.forecast.absorbed:,.0f}/day absorbed); absorbed "
          f"picks {by_absorbed.spec_id} "
          f"(${by_absorbed.forecast.absorbed:,.0f}/day)")
    check("ownership is charged at the lease rate, not the cheapest payment",
          _ownership_is_lease_rate(acq),
          "buying outright has no daily payment but converts capital — "
          "pricing it at $0/day ranked a $290M widebody as free")

    # ORD-LGA is the pair where the ENGINE is looser than the aeroplane.
    lga = planner.plan_pair(world, players, me, ord_,
                            actions.airport(world, "LGA"))
    loose = [o for o in lga.options if o.engine_would_allow]
    check("where the engine is looser than physics, the plan says so",
          bool(loose) and any("takeoff length" in n for n in lga.notes),
          f"{', '.join(o.spec_id for o in loose)} clear LGA's banded ROUTE "
          f"requirement but not their own takeoff length — route_can_fly "
          f"never checks the airframe figure, and databuilder flies an A321 "
          f"into LGA on every data world because of it")
    check("the planner takes the stricter line rather than endorsing the gap",
          all(not o.operable for o in loose))
    blocked = [o for o in lga.options if o.suitability and not o.forecast.reasons]
    check("the seat window blocks small types on a big market, with the number",
          bool(blocked) and all("min viable" in "; ".join(o.suitability)
                                for o in blocked),
          "; ".join(f"{o.spec_id}: {o.suitability[0]}" for o in blocked[:2]))


def tier_line(plan) -> str:
    from airlinesim import planner
    return planner.tier_note(plan.data_tier)


def _sorted_by_absorbed(plan) -> bool:
    """Operable rows first, then descending absorbed margin."""
    keys = [(not o.operable, -o.forecast.absorbed) for o in plan.options]
    return keys == sorted(keys)


def _ownership_is_lease_rate(options) -> bool:
    from airlinesim.actions import METHOD_BY_NAME
    for o in options:
        lease = next((q for q in o.quotes
                      if q.method is METHOD_BY_NAME["LEASE"]), None)
        if lease is None or abs(o.forecast.costs.ownership - lease.daily) > 0.01:
            return False
    return True


# ============================================================
# PHASE 3 — Q3, destination ranking
# ============================================================

# What a full scan out of one airport is allowed to cost. The scan runs
# OUTSIDE GameSession.lock (see GameSession.plan_from), but a budget still
# matters: this is a synchronous HTTP request a player is waiting on.
SCAN_BUDGET_S = 2.0


def check_dest_plan():
    """
    "Where should this aeroplane fly?" over the whole corpus.

    The two things that decide whether this is usable: it must be fast enough
    to be a synchronous request, and it must never offer a plan the carrier
    cannot execute.
    """
    import time

    from airlinesim import planner
    print("\n=== DESTINATION RANKING ===")
    world, engine = golden_world()
    players = list(engine.players)
    me = players[0]
    ord_ = actions.airport(world, "ORD")

    t0 = time.time()
    plan = planner.plan_from(world, players, me, origin=ord_, limit=25)
    cold = time.time() - t0
    t0 = time.time()
    planner.plan_from(world, players, me, origin=ord_, limit=25)
    warm = time.time() - t0
    check("a full-corpus scan fits the interactive budget",
          cold < SCAN_BUDGET_S,
          f"{plan.scanned} destinations x {len(world.repo.all(AircraftSpec))} "
          f"types in {cold:.2f}s cold, {warm:.2f}s warm (budget "
          f"{SCAN_BUDGET_S:.0f}s)")
    check("every corpus airport is considered",
          plan.scanned == len(world.repo.all(AirportSpec)) - 1,
          f"{plan.scanned} of {len(world.repo.all(AirportSpec))} — all but "
          f"the origin itself")
    check("the ranking is honoured and operable rows come first",
          _ranked_ok(plan),
          f"top: {plan.candidates[0].dest} on {plan.candidates[0].spec_id} at "
          f"${plan.candidates[0].forecast.absorbed:,.0f}/day")
    check("every rank key orders as it claims", _every_rank_key_orders(
        world, players, me, ord_), ", ".join(planner.RANK_KEYS))
    check("a destination nothing can serve is returned with its reason, "
          "not dropped",
          _unservable_explained(world, players, me, ord_))

    # THE TAIL CONSTRAINT. An aircraft is permanently where it was based, so a
    # plan that starts anywhere else cannot be executed.
    tail = next(a for a in me.fleet if a.location_iata != "ORD")
    byTail = planner.plan_from(world, players, me, tail_number=tail.tail_number,
                               limit=10)
    check("a tail plans only from its own base",
          byTail.origin == tail.location_iata
          and all(c.forecast.origin == tail.location_iata
                  for c in byTail.candidates),
          f"{tail.tail_number} is based at {tail.location_iata}, and every "
          f"candidate starts there — the engine has no ferry flights")
    check("a tail plans only with its own type",
          all(c.spec_id == tail.spec.spec_id for c in byTail.candidates),
          f"{tail.spec.spec_id} throughout")
    check("owned metal is not charged ownership in its own plan",
          all(c.forecast.costs.ownership == 0 for c in byTail.candidates))
    check("an unknown tail is refused by name",
          _raises_keyerror(lambda: planner.plan_from(
              world, players, me, tail_number="NOT-A-TAIL")))

    # THE PROVENANCE FILTER. A third of comparable pairs are out by >2x, so
    # "measured only" is the difference between a ranking and a shortlist.
    measured = planner.plan_from(world, players, me, origin=ord_, limit=40,
                                 measured_only=True)
    check("measured-only drops every fitted estimate",
          all(c.forecast.data_tier == "exact" for c in measured.candidates)
          and len(measured.candidates) <= len(plan.candidates) + 15,
          f"{len(measured.candidates)} measured destinations")
    check("the unfiltered list warns when it is showing estimates",
          any("FITTED" in n for n in plan.notes)
          or all(c.forecast.data_tier == "exact" for c in plan.candidates))
    check("the censoring caveat travels with every ranking",
          any("CENSORED" in n for n in plan.notes))


def _ranked_ok(plan) -> bool:
    from airlinesim import planner
    _label, fn = planner.RANK_KEYS[plan.rank_by]
    keys = [(not c.operable, fn(c)) for c in plan.candidates]
    return keys == sorted(keys)


def _every_rank_key_orders(world, players, me, origin) -> bool:
    from airlinesim import planner
    for key in planner.RANK_KEYS:
        p = planner.plan_from(world, players, me, origin=origin, rank_by=key,
                              limit=12)
        if p.rank_by != key or not _ranked_ok(p):
            return False
    return True


def _unservable_explained(world, players, me, origin) -> bool:
    """At least one destination comes back unservable, and says why."""
    from airlinesim import planner
    p = planner.plan_from(world, players, me, origin=origin, limit=300)
    bad = [c for c in p.candidates if not c.operable]
    return bool(bad) and all(c.suitability or c.forecast.reasons
                             or c.frequency.rotations == 0 for c in bad)


def _raises_keyerror(fn) -> bool:
    try:
        fn()
    except KeyError:
        return True
    return False


def check_lock_discipline():
    """
    THE SCAN MUST NOT RUN UNDER `GameSession.lock`.

    The lock serialises the tick loop, every command and the SSE broadcast. A
    0.3-second scan held across it is a 0.3-second freeze of the whole game
    for every connected tab — the same failure mode CLAUDE.md documents for
    unbounded clock catch-up, arriving by a different door.

    The property is that the lock is not HELD for the scan's duration, NOT
    that a planner call never waits for it — it takes the lock briefly to
    resolve the origin and copy the player list, so if the tick loop happens
    to hold it first, waiting is correct behaviour.

    So it is measured from the other side: run a scan on a background thread
    and, from this one, repeatedly acquire the lock and record the worst wait.
    If the scan held the lock throughout, some acquisition would wait roughly
    the whole scan. That is exactly what the tick loop would experience.
    """
    print("\n=== LOCK DISCIPLINE ===")
    from airlinesim.game import new_game
    gs = new_game(world="data", hub="ORD")
    gs.pause()
    try:
        gs.plan_from(origin="ORD", limit=5)      # warm the corpus caches
        for label, call in (
                ("destination scan",
                 lambda: gs.plan_from(origin="ORD", limit=25)),
                ("pair plan", lambda: gs.plan_pair("ORD", "DEN"))):
            duration, worst, samples = _lock_pressure(gs, call)
            if samples == 0:
                # Too brief to sample. Saying so beats a comparison against
                # zero attempts, which would pass whatever the code did.
                check(f"the {label} is too brief for lock-holding to matter",
                      duration < 0.05,
                      f"finished in {duration * 1000:.0f} ms — under the "
                      f"sampler's resolution, so the evidence here is the "
                      f"duration, not a measured wait")
            else:
                check(f"the {label} does not hold the session lock while it "
                      f"runs", worst < duration * 0.5,
                      f"{duration * 1000:.0f} ms of work, worst lock wait "
                      f"{worst * 1000:.1f} ms over {samples} attempts — the "
                      f"tick loop stays responsive throughout")
    finally:
        gs.stop()


def _lock_pressure(gs, call):
    """Run `call` on a thread; return (its duration, worst lock wait, tries)."""
    import threading
    import time
    took = {}

    def run():
        t0 = time.time()
        call()
        took["s"] = time.time() - t0

    t = threading.Thread(target=run, daemon=True)
    t.start()
    worst, tries = 0.0, 0
    while t.is_alive():
        t0 = time.time()
        with gs.lock:
            pass
        worst = max(worst, time.time() - t0)
        tries += 1
        time.sleep(0.002)
    t.join(10.0)
    return took.get("s", 0.0), worst, tries


def check_resourcing():
    """
    PHASE 4 — what a plan needs to actually be stood up.

    The plan for this phase was written on a wrong assumption and the UI
    shipped the wrong caveat, so the first thing asserted here is the fact
    that corrected it: flight crew in this engine is a PURELY VARIABLE cost.
    """
    import dataclasses

    from airlinesim import planner
    from airlinesim.engine import AirportSpec, PlaneClass
    print("\n=== RESOURCING ===")
    world, engine = golden_world()
    players = list(engine.players)
    me = players[0]
    ord_ = actions.airport(world, "ORD")
    a321 = world.repo.get(AircraftSpec, "A321")
    b789 = world.repo.get(AircraftSpec, "B789")

    # --- the correction itself, asserted against the engine's own source ---
    src = (Path(__file__).parent.parent / "engine.py").read_text()
    check("flight crew is billed only for hours flown",
          "crew_cost = (cockpit_cost + cabin_cost) * fh" in src,
          "OperationsSubsystem, where fh = distance / cruise_speed x freq")
    finance = src.split("class FinanceSubsystem")[1].split("class ")[0]
    check("cockpit and cabin are NOT on standing payroll",
          "CrewType.GROUND" in finance and "COCKPIT" not in finance,
          "FinanceSubsystem pays ground, baggage, meteorology and "
          "maintenance staff only — a flight crew that does not fly is free")
    check("the player's forecast charges crew on CRUISE hours, like the ledger",
          planner.player_policy(world, ord_).crew_hours_basis == "cruise")
    check("the AI still charges block hours, so its goldens hold",
          planner.ForecastPolicy().crew_hours_basis == "block",
          f"the difference is 0.6h x ${planner.AI_CREW_COST_PER_BLOCK_HOUR:,.0f} "
          f"= ${0.6 * planner.AI_CREW_COST_PER_BLOCK_HOUR:,.0f} a departure, "
          f"and over-stating a cost is the safe direction for a rival")

    # --- crew rate is read, not assumed ---
    ch, cb, mine = planner.crew_rates(world, me, "ORD", a321)
    _mh, _mb, market = planner.crew_rates(world, me, "ORD", b789)
    check("the rate comes from the carrier's own crews where it has them",
          "own crews" in mine and "market rate" in market,
          f"A321 ${ch + cb:,.0f}/h from your crews; B789 falls back to market "
          f"— your pilots are not rated for it")

    # --- headcount, and the inverse property that makes it trustworthy ---
    plan = planner.plan_pair(world, players, me, ord_,
                             actions.airport(world, "DEN"))
    by_id = {o.spec_id: o for o in plan.options}
    req = by_id["A321"].crew
    check("a rotation can never need fewer than two crews of each kind",
          req.cockpit_needed >= 2 and req.cabin_needed >= 2,
          f"{req.cockpit_needed} cockpit / {req.cabin_needed} cabin for "
          f"{req.daily_block_h:.1f} block hours a day — the roster assigns "
          f"exclusively, and a rotation is two ops")
    check("the requirement is the exact inverse of the frequency ceiling",
          _requirement_inverts(world, players, me, ord_),
          "hire what the planner asks for and the schedule it was hired for "
          "is exactly flyable")
    unrated = by_id["B789"].crew
    check("a type the carrier is not rated for shows the full hiring gap",
          unrated.cockpit_gap == unrated.cockpit_needed
          and unrated.cockpit_have == 0,
          f"B789: {unrated.cockpit_gap} cockpit crews to hire, "
          f"{unrated.cabin_have} cabin already universal")

    # --- maintenance runs the ENGINE's predicate ---
    mx = by_id["A321"].maintenance
    check("with no hubs declared, the legacy path is reported as such",
          mx.ok and "no hubs" in mx.reason,
          mx.reason)
    w2, e2 = golden_world()
    p2 = e2.players[0]
    actions.set_hub(w2, p2, "ORD", True)
    mx2 = planner.maintenance_plan(w2, p2, actions.airport(w2, "ORD"), a321)
    check("a rated hub satisfies the check and is named",
          mx2.ok and mx2.at_iata == "ORD", mx2.reason)
    check("the required class is the heaviest check in the type's program",
          mx2.required_class == "WIDEBODY",
          "every type's D check needs a widebody-class facility "
          "(databuilder._program), so this reads WIDEBODY for a narrowbody too")

    # THE UNRATED-HUB BRANCH IS UNREACHABLE ON THE COMMITTED CORPUS, because
    # routedata sets has_maintenance_facility and facility_max_class from the
    # SAME test (hub_rank <= 40 -> WIDEBODY). Every field that can do
    # maintenance can do any check. Rather than claim the branch is tested by
    # a corpus that cannot reach it, it is reached deliberately here.
    nb = [a for a in world.repo.all(AirportSpec)
          if a.has_maintenance_facility
          and a.facility_max_class is not None
          and a.facility_max_class.value < PlaneClass.WIDEBODY.value]
    check("no corpus airport is maintenance-capable but under-rated",
          not nb,
          "routedata derives both fields from hub_rank <= 40, so the "
          "'your hub cannot do this check' branch cannot arise from the data")
    w3, e3 = golden_world()
    p3 = e3.players[0]
    actions.set_hub(w3, p3, "ORD", True)
    downgraded = dataclasses.replace(
        w3.repo.get(AirportSpec, "ORD"),
        facility_max_class=PlaneClass.NARROWBODY)
    w3.repo._tables[AirportSpec]["ORD"] = downgraded
    mx3 = planner.maintenance_plan(w3, p3, actions.airport(w3, "ORD"), a321)
    check("an under-rated hub is caught, with the fee of the nearest that "
          "is not", not mx3.ok and mx3.candidate_iata
          and mx3.candidate_fee_per_day > 0, mx3.reason)


def _requirement_inverts(world, players, me, origin) -> bool:
    """
    Hiring exactly what `crew_requirement` asks for must make
    `crew_frequency` permit exactly the schedule it was sized for. If these
    disagree the planner tells a player to hire a number that then cannot fly.
    """
    from airlinesim import planner
    for spec_id, freq in (("A321", 3), ("A320", 5), ("E175", 2)):
        spec = world.repo.get(AircraftSpec, spec_id)
        block = __import__("airlinesim.route", fromlist=["x"]).block_hours(
            1400.0, spec.cruise_speed_kmh)
        req = planner.crew_requirement(world, me, origin, spec,
                                       block_h=block, frequency=freq)
        allowed, _d = planner.crew_frequency(1400.0, spec.cruise_speed_kmh,
                                             req.cockpit_needed,
                                             req.cabin_needed)
        # crew_frequency sizes ONE op; the requirement covers the rotation's
        # two, so the pool it asks for must cover at least the leg frequency.
        if allowed + 1e-9 < freq:
            return False
    return True


def check_delivery():
    """
    THE PLANNER IS ONLY DELIVERED IF A PLAYER CAN REACH IT.

    `attach_alliances()` shipped once with every action written and nothing
    reachable from the game. A read-only feature fails the same way, more
    quietly: a `planner.py` with no endpoint. So the whole chain is asserted
    — session method, HTTP route, panel, and the caveat the panel must carry.

    The parameter round-trip is the specific hazard. `server.COMMANDS` is a
    hand-written argument mapping and it is where `seats` went missing from
    acquisition and `service_tier` from route opening; a GET handler that
    parses its own query string has exactly the same failure mode, and a
    parameter nobody reads just takes its default in silence.
    """
    import json as _json
    import threading
    import time
    import urllib.request
    from pathlib import Path

    from airlinesim.game import GameSession
    from airlinesim.server import WEBUI_DIR, run_server
    print("\n=== DELIVERY ===")

    src = (Path(__file__).parent.parent / "server.py").read_text()
    check("GET /api/plan is a route on the server", '"/api/plan"' in src)
    check("GET /api/plan/from is a route on the server",
          '"/api/plan/from"' in src)
    check("GameSession exposes both planner questions",
          hasattr(GameSession, "plan_pair") and hasattr(GameSession, "plan_from"))

    html = (WEBUI_DIR / "index.html").read_text()
    # Collapsed, because the prose in the dialog is hard-wrapped and a check
    # that depends on where the line breaks fall is a check about formatting.
    flat = " ".join(html.split())
    js = (WEBUI_DIR / "app.js").read_text()
    check("the GUI has a planner panel and a form",
          'id="plannerCard"' in html and 'id="formPlan"' in html)
    check("the panel is wired to both endpoints",
          '"/api/plan"' in js and '"/api/plan/from"' in js
          and 'getElementById("formPlan")' in js)
    check("the rank picker is driven off the server's own key table",
          "rank_keys" in js and "planRank" in js,
          "adding a key in planner.RANK_KEYS is the whole change — the GUI "
          "cannot offer a different set")
    check("every planner parameter has an input in the form",
          all(f'name="{p}"' in html for p in
              ("origin", "dest", "service_tier", "fare_vs_reference")))
    check("the About dialog exists and the button opens it",
          'id="planAboutDlg"' in html and 'id="btnPlanAbout"' in html
          and "planAboutDlg" in js)
    # The one thing a viewer could reasonably get wrong, pinned the way
    # scenario_map pins the map's derived-position note.
    check("the panel says these are FORECASTS of routes that do not exist",
          "FORECASTS of routes that do not exist" in flat)
    check("the panel distinguishes the two margin lines in words",
          "Absorbed" in flat and "Contribution" in flat
          and "not deducted" in flat)
    check("the panel states the estimate's error rate",
          "out by more than 2" in flat)

    httpd, hub = run_server(host="127.0.0.1", port=8907, world="data",
                            hub_iata="ORD")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    hub.session.pause()
    time.sleep(0.3)
    try:
        def get(q):
            with urllib.request.urlopen(
                    "http://127.0.0.1:8907/api/plan?" + q, timeout=90) as r:
                return _json.loads(r.read())

        base = get("origin=ORD&dest=DEN")
        check("the endpoint answers with a ranked plan",
              "error" not in base and len(base.get("options", [])) > 1,
              f"{len(base.get('options', []))} options, top "
              f"{base['options'][0]['spec_id']}")
        check("the response is JSON-safe end to end",
              isinstance(_json.dumps(base), str))

        # Each parameter must CHANGE the answer, or it is being dropped.
        tier3 = get("origin=ORD&dest=DEN&service_tier=3")
        check("service_tier round-trips through the endpoint",
              tier3.get("service_tier") == 3
              and tier3["options"][0]["forecast"]["costs"]["amenities"]
              != base["options"][0]["forecast"]["costs"]["amenities"],
              "tier 3 buys better gates and lounges, and is charged for them")
        pricey = get("origin=ORD&dest=DEN&fare_vs_reference=1.5")
        check("fare_vs_reference round-trips through the endpoint",
              abs(pricey["options"][0]["forecast"]["fare"]
                  - base["options"][0]["forecast"]["fare"] * 1.5) < 0.01,
              f"${base['options'][0]['forecast']['fare']:.0f} -> "
              f"${pricey['options'][0]['forecast']['fare']:.0f}")
        check("origin and dest round-trip, case-insensitively",
              get("origin=ord&dest=lga").get("dest") == "LGA")
        for bad, why in (("origin=ORD&dest=ZZZ", "unknown airport"),
                         ("origin=ORD", "missing dest"),
                         ("origin=ORD&dest=ORD", "same endpoints")):
            r = get(bad)
            check(f"a bad request is refused with a reason ({why})",
                  "error" in r and bool(r["error"]), r.get("error", ""))
    finally:
        httpd.shutdown()
        hub.session.stop()


def check_quote():
    """
    `Bank.quote()` must answer what `try_acquire()` would DO.

    The planner has to tell a player "you can afford this aeroplane" before
    they buy it, and the only other way to answer was to re-derive the
    leverage cap and the payment schedule inside the planner. That second copy
    would be the one saying "affordable" the moment the two drifted — so the
    check that matters is not that the arithmetic is pretty, it is that the
    verdict matches the real thing on every method and both outcomes.

    Runs on its own world because it spends money.
    """
    from airlinesim.actions import METHOD_BY_NAME, TERMS_BY_METHOD, bank_for
    print("\n=== ACQUISITION QUOTES ===")
    world, engine = golden_world()
    player = engine.players[0]
    bank = bank_for(world)
    spec = world.repo.get(AircraftSpec, "A320")

    lease = bank.quote(player, spec, METHOD_BY_NAME["LEASE"],
                       TERMS_BY_METHOD[METHOD_BY_NAME["LEASE"]])
    check("a lease quotes no capital outlay and a monthly rent",
          lease.approved and lease.upfront == 0.0 and lease.monthly > 0,
          f"${lease.monthly:,.0f}/mo (${lease.daily:,.0f}/day) for "
          f"{lease.term_months} months")

    agree, tested = True, []
    for name in ("CASH", "FINANCE", "LEASE"):
        method = METHOD_BY_NAME[name]
        terms = TERMS_BY_METHOD[method]
        for cash in (spec.list_price * 4, 1.0):
            player.ledger.cash = cash
            q = bank.quote(player, spec, method, terms)
            got = bank.try_acquire(player, spec, f"Q-{name}-{cash:.0f}",
                                   method, terms, [])
            tested.append(f"{name}@${cash:,.0f}:{q.approved}/{got}")
            if q.approved != got:
                agree = False
    check("the quote's verdict matches try_acquire on every method, "
          "flush and broke", agree, "  ".join(tested))

    player.ledger.cash = spec.list_price * 4
    q = bank.quote(player, spec, METHOD_BY_NAME["FINANCE"],
                   TERMS_BY_METHOD[METHOD_BY_NAME["FINANCE"]])
    check("a financed quote states the down payment and the debt separately",
          abs(q.upfront + q.financed - spec.list_price) < 0.01 and q.monthly > 0,
          f"${q.upfront:,.0f} down + ${q.financed:,.0f} financed, "
          f"${q.monthly:,.0f}/mo over {q.term_months} months")
    check("a refused quote says why, in words a player can act on",
          _refusal_explains(bank, player, spec))


def _refusal_explains(bank, player, spec) -> bool:
    """A denial with no reason is the thing this replaced."""
    from airlinesim.actions import METHOD_BY_NAME, TERMS_BY_METHOD
    player.ledger.cash = 1.0
    for name in ("CASH", "FINANCE"):
        method = METHOD_BY_NAME[name]
        q = bank.quote(player, spec, method, TERMS_BY_METHOD[method])
        if q.approved or not q.reason or q.reason == "ok":
            return False
    return True


def _crew_inverse_holds() -> bool:
    """
    `crew_frequency` must invert `ai._crew_target` exactly, or the planner
    tells a player to hire a number that then cannot fly the schedule it was
    hired for.
    """
    import math

    from airlinesim import planner
    from airlinesim.crew import DEFAULT_DUTY_LIMITS
    from airlinesim.route import block_hours

    per_crew = DEFAULT_DUTY_LIMITS.max_daily_flight_hours
    for dist, cruise, crews in ((1200, 850, 3), (400, 780, 2), (3800, 900, 6)):
        freq, _d = planner.crew_frequency(dist, cruise, crews, crews)
        leg = block_hours(dist, cruise)
        daily_bh = leg * freq
        # ai._crew_target's arithmetic, run backwards
        need = daily_bh / per_crew * planner.CREW_DEPTH
        if abs(need - crews) > 1e-9:
            return False
        # and the rounding the AI applies must never ask for fewer
        if math.ceil(need) < crews:
            return False
    return True


def main():
    if "--regenerate" in sys.argv:
        regenerate()
        return
    print("ROUTE PLANNER CHECK")
    print("=" * 70)
    world, engine = golden_world()
    live = evaluate_all(world, engine)
    check_fixture(world)
    check_coverage(live)
    check_goldens(live)
    check_forecast(world, engine)
    check_frequency(world, engine)
    check_quote()
    check_pair_plan()
    check_dest_plan()
    check_resourcing()
    check_lock_discipline()
    check_delivery()
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(CHECKS) else 'FAILURES ABOVE'}")


if __name__ == "__main__":
    main()

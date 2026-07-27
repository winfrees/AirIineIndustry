"""
explorer.py — outcome-space exploration over the simulation engine.
====================================================================

game.py answers "what happens in the run I am playing?". This module answers
"what is the shape of everything that *could* happen?".

The whole design rests on one measured property of engine.py: **the tick
pipeline contains no randomness at all**. There is not a single `random` call
in the engine, so a given state plus a given set of edits plus N ticks always
produces byte-identical results. That is what makes a branching tree of runs
meaningful rather than noise — every node here is reproducible, and two nodes
that differ differ *because of the edit*, not because of a seed.

The model:

  - A NODE is a simulation state: a forked (world, engine, ctx) triple, plus
    the outcome metrics projected from it.
  - An EDGE (parent -> child) is a transition: apply some MUTATIONS to a copy
    of the parent state, then run it for N CYCLES.
  - A DERIVATION is a user-written expression evaluated against a node's
    metrics (`human.net_worth > ai.net_worth`). It classifies outcomes so a
    large tree can be read at a glance.

Iterating on that indefinitely — branch, run, test, branch again from whichever
node is interesting — is how a map of the outcome space gets built. `sweep()`
and `expand()` automate the branching so the map can be generated rather than
clicked out one node at a time.

Forking is `pickle` round-tripping the state triple, the same mechanism
GameSession.save/load already relies on. It is not free: a demo world costs
~31 KB per node, so the tree is capped (see MAX_NODES) rather than allowed to
grow until the process dies.
"""

from __future__ import annotations

import ast
import pickle
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from airlinesim.engine import MarketConditions
from airlinesim.finance_cabin import aircraft_value
from airlinesim.game import build_game_world, route_op_id

# A "cycle" is one engine tick. engine.dt is 24h, so one cycle is one sim day;
# the explorer reports both so a changed dt can't silently redefine the unit.
MAX_NODES = 400          # ~12 MB of state blobs for the demo world
MAX_CYCLES_PER_EDGE = 3650   # ten sim years in one hop; beyond that, chain edges


# ============================================================
# MUTATIONS — what "setting a state" means
# ============================================================

@dataclass(frozen=True)
class Mutation:
    """One edit applied to a forked state before its cycles are run.

    `kind` names the knob, `target` says which instance it applies to (a route
    op id, a player id, a route spec id, or "*" / "" where the knob is global).
    """
    kind: str
    target: str = ""
    value: float = 0.0

    def describe(self) -> str:
        tgt = f" {self.target}" if self.target and self.target != "*" else ""
        return f"{self.kind}{tgt}={_fmt(self.value)}"

    def to_json(self) -> dict:
        return {"kind": self.kind, "target": self.target, "value": self.value,
                "describe": self.describe()}


def _fmt(v: float) -> str:
    return f"{v:g}"


def _apply_price(world, engine, m: Mutation):
    ops = _find_ops(engine, m.target)
    if not ops:
        raise ValueError(f"no route op matching '{m.target}'")
    for op in ops:
        if m.value <= 0:
            raise ValueError("price must be positive")
        op.ticket_price = round(float(m.value), 2)


def _apply_frequency(world, engine, m: Mutation):
    ops = _find_ops(engine, m.target)
    if not ops:
        raise ValueError(f"no route op matching '{m.target}'")
    for op in ops:
        op.daily_frequency = max(0, int(m.value))


def _apply_price_scale(world, engine, m: Mutation):
    ops = _find_ops(engine, m.target)
    if not ops:
        raise ValueError(f"no route op matching '{m.target}'")
    for op in ops:
        op.ticket_price = round(max(1.0, op.ticket_price * float(m.value)), 2)


def _apply_cash(world, engine, m: Mutation):
    players = _find_players(engine, m.target)
    if not players:
        raise ValueError(f"no player matching '{m.target}'")
    for p in players:
        p.ledger.cash = float(m.value)


def _apply_fuel_price(world, engine, m: Mutation):
    """Scale the fuel base price at one airport, or everywhere.

    Deliberately NOT MarketConditions.fuel_index: that field exists on the
    dataclass but no subsystem reads it (grep engine.py — the only hit is its
    own declaration), so setting it changes nothing. OperationsSubsystem prices
    fuel off FuelMarket.spot_price(), which derives from base_price_per_l, so
    that is the knob with a real effect. Wiring fuel_index up is an engine
    change and belongs in its own commit, not smuggled in behind a GUI.
    """
    factor = float(m.value)
    if factor < 0:
        raise ValueError("fuel price scale must be >= 0")
    targets = (list(world.fuel.values()) if m.target in ("", "*")
               else [world.fuel[m.target]] if m.target in world.fuel else [])
    if not targets:
        raise ValueError(f"no fuel market at '{m.target}'")
    for fm in targets:
        fm.base_price_per_l = fm.base_price_per_l * factor


def _apply_demand_scale(world, engine, m: Mutation):
    """Scale a route's (or every route's) demand by a multiplier.

    DemandMarket is a mutable dataclass but SegmentDemand is frozen, so the
    segments are rebuilt with dataclasses.replace rather than assigned into.
    """
    factor = float(m.value)
    if factor < 0:
        raise ValueError("demand scale must be >= 0")
    targets = ([dm for rid, dm in world.demand.items()]
               if m.target in ("", "*")
               else [world.demand[m.target]] if m.target in world.demand else [])
    if not targets:
        raise ValueError(f"no demand market matching '{m.target}'")
    for dm in targets:
        dm.base_demand_per_day = dm.base_demand_per_day * factor
        if dm.segments:
            dm.segments = tuple(replace(s, base_per_day=s.base_per_day * factor)
                                for s in dm.segments)


# kind -> (applier, needs_ctx, human label, unit hint, target kind)
# `target kind` tells the GUI which picker to show: route ops, players,
# airports, demand markets, or nothing at all.
MUTATION_KINDS: dict[str, tuple] = {
    "price":        (_apply_price, False, "Ticket price", "$", "route_op"),
    "price_scale":  (_apply_price_scale, False, "Ticket price ×", "×", "route_op"),
    "frequency":    (_apply_frequency, False, "Daily frequency", "/day", "route_op"),
    "cash":         (_apply_cash, False, "Cash balance", "$", "player"),
    "fuel_price":   (_apply_fuel_price, False, "Fuel price ×", "×", "airport"),
    "demand_scale": (_apply_demand_scale, False, "Route demand ×", "×", "route"),
}


def _find_ops(engine, target: str) -> list:
    """Resolve a route-op target. '*' means every op of every player; a bare
    player id means every op that player flies; otherwise an exact op id."""
    all_ops = [op for p in engine.players for op in p.route_ops]
    if target in ("", "*"):
        return all_ops
    exact = [op for op in all_ops if route_op_id(op) == target]
    if exact:
        return exact
    return [op for op in all_ops if op.owner_id == target]


def _find_players(engine, target: str) -> list:
    if target in ("", "*"):
        return list(engine.players)
    return [p for p in engine.players if p.player_id == target]


def _apply_mutations(world, engine, ctx, mutations) -> None:
    for m in mutations:
        entry = MUTATION_KINDS.get(m.kind)
        if entry is None:
            raise ValueError(f"unknown mutation kind '{m.kind}'")
        fn, needs_ctx = entry[0], entry[1]
        if needs_ctx:
            fn(world, engine, m, ctx)
        else:
            fn(world, engine, m)


# ============================================================
# METRICS — the projected outcome of a state
# ============================================================

def project_metrics(world, engine, human_player_id: str) -> dict:
    """Flatten a simulation state into the numbers a derivation can test.

    Deliberately a pure function of (world, engine) rather than a GameSession
    method: the explorer holds raw state triples and never builds a session.
    """
    players = []
    for p in engine.players:
        debt = sum(l.remaining for l in p.loans)
        assets = sum(aircraft_value(a, world.sim_time) for a in p.fleet if a.owned)
        ops = list(p.route_ops)
        flown = [o for o in ops if o.last_eff_freq > 0]
        pax = sum(o.last_pax for o in ops)
        revenue = sum(o.last_revenue for o in ops)
        profit = sum(o.last_profit for o in ops)
        # Load factor is averaged over ops that actually flew; averaging in
        # grounded ops as 0.0 would report a fleet-wide collapse whenever one
        # route lost its crew, which reads as a demand signal and is not one.
        lf = (sum(o.last_load_factor for o in flown) / len(flown)) if flown else 0.0
        players.append({
            "player_id": p.player_id,
            "name": p.name,
            "is_ai": p.is_ai,
            "is_human": p.player_id == human_player_id,
            "cash": p.ledger.cash,
            "debt": debt,
            "assets": assets,
            "net_worth": p.ledger.cash + assets - debt,
            "fleet": len(p.fleet),
            "routes": len(ops),
            "flying": len(flown),
            "pax": pax,
            "revenue": revenue,
            "profit": profit,
            "load_factor": lf,
            "grounded": sum(1 for a in p.fleet if not a.in_service),
        })
    gates_used = sum(gl.used() for gl in world.gates.values())
    gates_total = sum(gl.total_gates for gl in world.gates.values())
    fuel_spots = [fm.spot_price() for fm in world.fuel.values()]
    return {
        "day": int(world.sim_time // 24),
        "sim_time_hours": world.sim_time,
        "gates_used": gates_used,
        "gates_total": gates_total,
        "gate_utilization": (gates_used / gates_total) if gates_total else 0.0,
        "fuel_spot": (sum(fuel_spots) / len(fuel_spots)) if fuel_spots else 0.0,
        "players": players,
    }


# ============================================================
# DERIVATIONS — safe user-written expressions over metrics
# ============================================================

class _Ns:
    """Attribute view over a metrics dict, so `human.net_worth` reads naturally."""

    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError:
            raise ValueError(
                f"unknown field '{name}' (have: {', '.join(sorted(self._data))})"
            ) from None


# The expression language is a whitelist, not a sandbox around eval(). Every
# AST node is checked against this set BEFORE anything is compiled, so an
# expression that reaches eval() cannot name an attribute we didn't produce,
# call anything but the four functions below, or reach an import/dunder. This
# matters because the server binds 0.0.0.0 by default: a derivation box wired
# to a bare eval() would be remote code execution for anyone on the LAN.
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare, ast.IfExp,
    ast.Name, ast.Load, ast.Attribute, ast.Constant, ast.Call,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)
_ALLOWED_CALLS = {"abs", "min", "max", "round"}
_SAFE_FUNCS = {"abs": abs, "min": min, "max": max, "round": round}


def validate_derivation(expr: str) -> Optional[str]:
    """Return None if `expr` is a legal derivation, else the reason it isn't."""
    if not expr or not expr.strip():
        return "empty expression"
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return f"syntax error: {e.msg}"
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return f"{type(node).__name__} is not allowed in a derivation"
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
                return f"only {', '.join(sorted(_ALLOWED_CALLS))}() may be called"
            if node.keywords:
                return "keyword arguments are not allowed"
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return "underscore attributes are not allowed"
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            return "underscore names are not allowed"
    return None


def derivation_namespace(metrics: dict) -> dict:
    """Build the variable bindings a derivation sees.

    Players are reachable three ways so expressions stay short whatever the
    world looks like: by role (`human`, `ai`), by index (`p0`, `p1`, ...) and
    by lowercased player id (`fin`, `lse`).
    """
    ns: dict[str, Any] = {
        "day": metrics["day"],
        "fuel_spot": metrics["fuel_spot"],
        "gate_utilization": metrics["gate_utilization"],
        "gates_used": metrics["gates_used"],
        "gates_total": metrics["gates_total"],
    }
    for i, pm in enumerate(metrics["players"]):
        view = _Ns(pm)
        ns[f"p{i}"] = view
        ns[pm["player_id"].lower().replace("-", "_")] = view
        if pm["is_human"]:
            ns["human"] = view
        elif pm["is_ai"]:
            ns.setdefault("ai", view)
    return ns


def evaluate_derivation(expr: str, metrics: dict):
    """Evaluate `expr` against one node's metrics.

    Returns (value, error). Exactly one is non-None.
    """
    err = validate_derivation(expr)
    if err:
        return None, err
    ns = derivation_namespace(metrics)
    try:
        code = compile(ast.parse(expr, mode="eval"), "<derivation>", "eval")
        value = eval(code, {"__builtins__": {}, **_SAFE_FUNCS}, ns)  # noqa: S307
    except ValueError as e:          # _Ns raises this for an unknown field
        return None, str(e)
    except ZeroDivisionError:
        return None, "division by zero"
    except Exception as e:           # a derivation must never kill the request
        return None, f"{type(e).__name__}: {e}"
    if isinstance(value, bool):
        return value, None
    if isinstance(value, (int, float)):
        return float(value), None
    return None, f"expression produced {type(value).__name__}, expected number or bool"


# ============================================================
# THE TREE
# ============================================================

@dataclass
class ScenarioNode:
    node_id: str
    parent_id: Optional[str]
    label: str
    mutations: tuple
    cycles: int
    depth: int
    metrics: dict
    blob: bytes = field(repr=False, default=b"")
    created_at: float = field(default_factory=time.time)

    def to_json(self, include_metrics: bool = True) -> dict:
        d = {
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "label": self.label,
            "mutations": [m.to_json() for m in self.mutations],
            "cycles": self.cycles,
            "depth": self.depth,
            "day": self.metrics["day"],
            "state_bytes": len(self.blob),
        }
        if include_metrics:
            d["metrics"] = self.metrics
        return d


class ScenarioTree:
    """A branching set of simulation states, rooted at one starting world.

    Thread-safe: server.py runs a ThreadingHTTPServer, so two browser tabs can
    branch at once. Every public method takes the lock; the engine itself has
    no concurrency story and must only ever be ticked by one thread.
    """

    def __init__(self, root_factory: Optional[Callable] = None,
                 max_nodes: int = MAX_NODES):
        self._lock = threading.RLock()
        self._root_factory = root_factory or build_game_world
        self.max_nodes = max_nodes
        self.nodes: dict[str, ScenarioNode] = {}
        self.root_id: str = ""
        self._seq = 0
        self.human_player_id = ""
        self.reset()

    # -- lifecycle -------------------------------------------------------
    def reset(self, cycles: int = 0):
        """Discard the tree and re-root it from a fresh world."""
        with self._lock:
            world, engine, human_id = self._root_factory()
            ctx = {"market": MarketConditions()}
            self.human_player_id = human_id
            self.nodes = {}
            self._seq = 0
            for _ in range(max(0, int(cycles))):
                engine.tick(ctx)
            node = self._make_node(None, "root", (), int(cycles), 0,
                                   world, engine, ctx)
            self.root_id = node.node_id
            return node

    def _next_id(self) -> str:
        self._seq += 1
        return f"n{self._seq}"

    def _make_node(self, parent_id, label, mutations, cycles, depth,
                   world, engine, ctx) -> ScenarioNode:
        node = ScenarioNode(
            node_id=self._next_id(), parent_id=parent_id, label=label,
            mutations=tuple(mutations), cycles=cycles, depth=depth,
            metrics=project_metrics(world, engine, self.human_player_id),
            blob=pickle.dumps((world, engine, ctx), protocol=pickle.HIGHEST_PROTOCOL),
        )
        self.nodes[node.node_id] = node
        return node

    # -- branching -------------------------------------------------------
    def branch(self, parent_id: str, mutations=(), cycles: int = 30,
               label: str = "") -> ScenarioNode:
        """Fork `parent_id`, apply `mutations`, run `cycles` ticks, store it."""
        with self._lock:
            parent = self.nodes.get(parent_id)
            if parent is None:
                raise KeyError(f"no such node '{parent_id}'")
            cycles = int(cycles)
            if cycles < 0:
                raise ValueError("cycles must be >= 0")
            if cycles > MAX_CYCLES_PER_EDGE:
                raise ValueError(f"cycles must be <= {MAX_CYCLES_PER_EDGE}")
            if len(self.nodes) >= self.max_nodes:
                raise ValueError(
                    f"tree is at its {self.max_nodes}-node cap; delete a subtree "
                    f"or reset before branching further")
            world, engine, ctx = pickle.loads(parent.blob)
            mutations = tuple(mutations)
            _apply_mutations(world, engine, ctx, mutations)
            for _ in range(cycles):
                engine.tick(ctx)
            if not label:
                label = ", ".join(m.describe() for m in mutations) or f"+{cycles}d"
            return self._make_node(parent.node_id, label, mutations, cycles,
                                   parent.depth + 1, world, engine, ctx)

    def sweep(self, parent_id: str, kind: str, target: str, values,
              cycles: int = 30) -> list[ScenarioNode]:
        """Branch once per value — one parameter varied across a range.

        This is the smallest unit that produces a *map* rather than a point:
        the children are identical in every respect except the swept knob, so
        differences between them are attributable to it.
        """
        with self._lock:
            if kind not in MUTATION_KINDS:
                raise ValueError(f"unknown mutation kind '{kind}'")
            values = list(values)
            if not values:
                raise ValueError("sweep needs at least one value")
            if len(self.nodes) + len(values) > self.max_nodes:
                raise ValueError(
                    f"sweep of {len(values)} would exceed the {self.max_nodes}-node "
                    f"cap (tree has {len(self.nodes)})")
            return [self.branch(parent_id, (Mutation(kind, target, v),), cycles)
                    for v in values]

    def expand(self, parent_id: str, kind: str, target: str, values,
               cycles: int = 30, depth: int = 2) -> list[ScenarioNode]:
        """Apply the same sweep recursively to `depth` levels.

        Breadth**depth nodes, so it is checked against the cap up front rather
        than discovered halfway through and left as a half-built tree.
        """
        with self._lock:
            values = list(values)
            depth = int(depth)
            if depth < 1:
                raise ValueError("depth must be >= 1")
            total = sum(len(values) ** d for d in range(1, depth + 1))
            if len(self.nodes) + total > self.max_nodes:
                raise ValueError(
                    f"expand would add {total} nodes and exceed the "
                    f"{self.max_nodes}-node cap (tree has {len(self.nodes)})")
            created: list[ScenarioNode] = []
            frontier = [parent_id]
            for _ in range(depth):
                nxt = []
                for nid in frontier:
                    kids = self.sweep(nid, kind, target, values, cycles)
                    created.extend(kids)
                    nxt.extend(k.node_id for k in kids)
                frontier = nxt
            return created

    def delete(self, node_id: str) -> int:
        """Prune a node and everything under it. Returns the count removed."""
        with self._lock:
            if node_id == self.root_id:
                raise ValueError("cannot delete the root; use reset")
            if node_id not in self.nodes:
                raise KeyError(f"no such node '{node_id}'")
            doomed, frontier = set(), [node_id]
            while frontier:
                nid = frontier.pop()
                doomed.add(nid)
                frontier.extend(n.node_id for n in self.nodes.values()
                                if n.parent_id == nid and n.node_id not in doomed)
            for nid in doomed:
                del self.nodes[nid]
            return len(doomed)

    # -- reads -----------------------------------------------------------
    def path(self, node_id: str) -> list[ScenarioNode]:
        """Root -> node, i.e. the full recipe that produced this state."""
        with self._lock:
            out, cur = [], self.nodes.get(node_id)
            while cur is not None:
                out.append(cur)
                cur = self.nodes.get(cur.parent_id) if cur.parent_id else None
            return list(reversed(out))

    def evaluate(self, expr: str) -> dict:
        """Run one derivation across every node in the tree.

        A malformed or unsafe expression raises ValueError, like every other
        tree operation, so the HTTP layer reports it through one path. Per-node
        failures (an unknown field, a divide by zero) are NOT that: they are
        results about a node and travel inside `results`.
        """
        with self._lock:
            err = validate_derivation(expr)
            if err:
                raise ValueError(err)
            results, failures = {}, 0
            for nid, node in self.nodes.items():
                value, verr = evaluate_derivation(expr, node.metrics)
                if verr:
                    failures += 1
                results[nid] = {"value": value, "error": verr}
            return {"expr": expr, "results": results,
                    "evaluated": len(results) - failures, "errored": failures}

    def to_json(self) -> dict:
        with self._lock:
            return {
                "root_id": self.root_id,
                "human_player_id": self.human_player_id,
                "max_nodes": self.max_nodes,
                "node_count": len(self.nodes),
                "state_bytes": sum(len(n.blob) for n in self.nodes.values()),
                "cycle_hours": 24.0,
                "mutation_kinds": [
                    {"kind": k, "label": v[2], "unit": v[3], "target_kind": v[4]}
                    for k, v in MUTATION_KINDS.items()
                ],
                "nodes": [n.to_json() for n in self.nodes.values()],
            }

    def node_detail(self, node_id: str) -> dict:
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                raise KeyError(f"no such node '{node_id}'")
            d = node.to_json()
            d["path"] = [
                {"node_id": n.node_id, "label": n.label, "cycles": n.cycles,
                 "mutations": [m.to_json() for m in n.mutations]}
                for n in self.path(node_id)
            ]
            d["children"] = [n.node_id for n in self.nodes.values()
                             if n.parent_id == node_id]
            return d

    def targets(self) -> dict:
        """What the GUI can offer as mutation targets, read off the root state."""
        with self._lock:
            root = self.nodes.get(self.root_id)
            if root is None:
                return {"route_ops": [], "players": [], "routes": [], "airports": []}
            world, engine, _ctx = pickle.loads(root.blob)
            return {
                "players": [{"id": p.player_id, "name": p.name, "is_ai": p.is_ai}
                            for p in engine.players],
                "airports": [{"id": iata,
                              "label": f"{iata} (${fm.base_price_per_l:.2f}/L)"}
                             for iata, fm in sorted(world.fuel.items())],
                "route_ops": [
                    {"id": route_op_id(op), "owner": op.owner_id,
                     "label": f"{op.spec.origin_iata}->{op.spec.dest_iata} "
                              f"({op.plane.tail_number})",
                     "price": op.ticket_price, "freq": op.daily_frequency}
                    for p in engine.players for op in p.route_ops
                ],
                "routes": [{"id": rid, "label": rid} for rid in sorted(world.demand)],
            }


def linspace(start: float, stop: float, count: int) -> list[float]:
    """Inclusive evenly-spaced values — what the GUI's sweep range produces."""
    count = int(count)
    if count < 1:
        raise ValueError("count must be >= 1")
    if count == 1:
        return [float(start)]
    step = (float(stop) - float(start)) / (count - 1)
    return [round(float(start) + step * i, 6) for i in range(count)]

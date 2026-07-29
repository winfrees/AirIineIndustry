"""
GameSession — the game-logic layer on top of the simulation engine.
=====================================================================

engine.py is a simulation library: build a World, add players, tick it.
Nothing in it lets a human change a decision once the sim is running, and
nothing declares a winner or loser. GameSession adds exactly that, without
touching engine invariants:

  - a designated HUMAN player (the rest are AI, driven by AIStrategySubsystem)
  - a validated COMMAND API (price/frequency/layout/acquire/open route/hire
    crew) — thin wrappers around existing mutators (Bank.acquire, RouteOp
    fields, crew pools), not new engine logic
  - a background REAL-TIME LOOP with pause/speed, guarded by a lock shared
    with the command API so a command and a tick are never interleaved
  - a JSON-safe SNAPSHOT projection for a GUI to consume
  - bankruptcy-based WIN/LOSS detection
  - pickle SAVE/LOAD (simplest correct option given frozen Spec dataclasses,
    enums, and deque-based crew duty state)
"""

from __future__ import annotations

import pickle
import threading
import time
from typing import Optional

from airlinesim.engine import (
    AircraftSpec, AirportSpec, RouteSpec, CrewSpec, CrewUnit, RouteOp,
    Airplane, MarketConditions, OperationsSubsystem, AIStrategySubsystem,
    CrewType,
)
from airlinesim.cabin import CABIN_ORDER, fit_report, geometry_for, presets_for
from airlinesim.finance_cabin import (
    DEFAULT_SEAT_CLASSES, cabin_slots_for,
    AcquisitionMethod, FinancingTerms, Bank, aircraft_value,
)
from airlinesim import actions
from airlinesim.builder import build_demo_world


def route_op_id(op: RouteOp) -> str:
    """Stable identifier for a route operation.

    Module-level so explorer.py addresses route ops by the same string the game
    GUI shows; if this format ever changes, both move together.
    """
    return f"{op.owner_id}:{op.spec.spec_id}:{op.plane.tail_number}"


def _pkg_version() -> str:
    # Inline import: `from airlinesim import __version__` at module top would
    # work today (nothing in __init__ imports game), but keeping it lazy means
    # this file can never become the module that closes an import cycle.
    import airlinesim
    return airlinesim.__version__

# Reference financing products a human can choose at acquisition time. Same
# shape/values as builder.py and scenarios/integration.py already use.
_LOAN_TERMS = FinancingTerms("LOAN", AcquisitionMethod.FINANCE,
                              down_payment_frac=0.20, annual_rate=0.06, term_months=120)
_LEASE_TERMS = FinancingTerms("LEASE", AcquisitionMethod.OPERATING_LEASE,
                               lease_rate_frac_per_year=0.11, lease_term_months=84)
_METHOD_BY_NAME = {
    "CASH": AcquisitionMethod.BUY_CASH,
    "FINANCE": AcquisitionMethod.FINANCE,
    "LEASE": AcquisitionMethod.OPERATING_LEASE,
}
_TERMS_BY_METHOD = {
    AcquisitionMethod.BUY_CASH: None,
    AcquisitionMethod.FINANCE: _LOAN_TERMS,
    AcquisitionMethod.OPERATING_LEASE: _LEASE_TERMS,
}


def build_game_world(human_name: str = "You", ai_name: str = "SkyRival",
                     ai_step_frac: float = 0.03, world: str = "demo",
                     ai_profiles=None, hub: str = "ORD", n_destinations: int = 5):
    """
    Build the two-carrier game world and return (world, engine, human_player_id)
    WITHOUT wrapping it in a GameSession.

    Split out of new_game() because GameSession.__init__ starts a background
    real-time thread, and explorer.py needs this exact starting state hundreds
    of times over with no threads attached. Keeping it as one seam means the
    explorer maps the same game the player plays — a second constructor here
    would drift from it silently.
    """
    if world == "data":
        # The BTS-corpus world, with rivals that plan networks (ai.py) rather
        # than only repricing. Routes opened later resolve through the same
        # corpus, so a mid-game route is sourced like a starting one.
        from airlinesim.databuilder import build_world_from_data
        profiles = ai_profiles or {"LSE": "Low-Cost", "CRW": "Legacy",
                                   "RGN": "Regional"}
        w, engine, _report = build_world_from_data(
            hub=hub, n_destinations=n_destinations, verbose=False,
            ai_profiles=profiles)
        human = next(p for p in engine.players if p.player_id not in profiles)
        human.name = human_name
        for p in engine.players:
            p.is_ai = p.player_id in profiles
            if p.is_ai and p.player_id == next(iter(profiles), None):
                p.name = ai_name
        return w, engine, human.player_id

    world, engine = build_demo_world()
    human, ai = engine.players
    human.name = human_name
    ai.name = ai_name
    ai.is_ai = True
    # AIStrategySubsystem's docstring: "Runs BEFORE Operations so changes
    # take effect now" — the same ordering scenarios/competitive.py uses.
    ops_idx = next(i for i, s in enumerate(engine.subsystems)
                   if isinstance(s, OperationsSubsystem))
    engine.subsystems.insert(ops_idx, AIStrategySubsystem(step_frac=ai_step_frac))
    return world, engine, human.player_id


def new_game(human_name: str = "You", ai_name: str = "SkyRival",
             ai_step_frac: float = 0.03, world: str = "demo",
             ai_profiles=None, hub: str = "ORD",
             n_destinations: int = 5) -> "GameSession":
    """
    Build a ready-to-play game.

      world="demo" — the two-airport sandbox, with the price/frequency bot.
      world="data" — the BTS-corpus network out of `hub`, with rivals that run
                     whole airlines (routes, fleet, cabins, service, crew).
                     Assign styles with ai_profiles={player_id: archetype};
                     see ai.ARCHETYPES.
    """
    w, engine, human_id = build_game_world(human_name, ai_name, ai_step_frac,
                                           world=world, ai_profiles=ai_profiles,
                                           hub=hub, n_destinations=n_destinations)
    return GameSession(w, engine, human_player_id=human_id)


class GameSession:
    def __init__(self, world, engine, human_player_id: str,
                 sim_days_per_real_second: float = 0.5,
                 bankruptcy_floor: float = -5_000_000.0):
        self.world = world
        self.engine = engine
        self.human_player_id = human_player_id
        self.speed = sim_days_per_real_second
        self.bankruptcy_floor = bankruptcy_floor
        self.paused = True   # start paused so the player can look around first
        self.game_over = False
        self.game_over_reason = ""
        # the world's shared lender — AI carriers borrow from the same bank,
        # under the same leverage cap, with globally unique loan/lease ids
        self.bank = actions.bank_for(world)
        self._init_runtime()

    # -- runtime state that can't (and shouldn't) be pickled --------------
    def _init_runtime(self):
        self.lock = threading.RLock()
        self.ctx = {"market": MarketConditions()}
        self._carry = 0.0
        self._stop = threading.Event()
        self.on_tick = None   # optional callback(snapshot: dict), set by server.py
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def __getstate__(self):
        state = self.__dict__.copy()
        for k in ("lock", "ctx", "_carry", "_stop", "on_tick", "_thread"):
            state.pop(k, None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._init_runtime()

    def stop(self):
        self._stop.set()

    # -- real-time loop -----------------------------------------------------
    def _loop(self):
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(0.2)
            now = time.monotonic()
            elapsed = now - last
            last = now
            if self.paused or self.game_over:
                continue
            with self.lock:
                self._carry += elapsed * self.speed
                ticked = False
                while self._carry >= 1.0:
                    self.engine.tick(self.ctx)
                    self._carry -= 1.0
                    ticked = True
                if ticked:
                    self._check_game_over()
                    snap = self.snapshot()
            if ticked and self.on_tick:
                self.on_tick(snap)

    def pause(self):
        with self.lock:
            self.paused = True

    def resume(self):
        with self.lock:
            if not self.game_over:
                self.paused = False
                self._carry = 0.0

    def set_speed(self, sim_days_per_real_second: float):
        with self.lock:
            self.speed = max(0.01, float(sim_days_per_real_second))

    def advance_days(self, n: int = 1) -> dict:
        """Manual fast-forward, independent of real-time/pause state."""
        with self.lock:
            for _ in range(max(0, int(n))):
                if self.game_over:
                    break
                self.engine.tick(self.ctx)
            self._check_game_over()
            snap = self.snapshot()
        if self.on_tick:
            self.on_tick(snap)
        return snap

    # -- win/loss -------------------------------------------------------
    def _net_worth(self, p) -> float:
        debt = sum(l.remaining for l in p.loans)
        assets = sum(aircraft_value(a, self.world.sim_time) for a in p.fleet if a.owned)
        return p.ledger.cash + assets - debt

    def _check_game_over(self):
        human = self._human()
        nw = self._net_worth(human)
        if nw < self.bankruptcy_floor:
            self.game_over = True
            self.paused = True
            self.game_over_reason = f"bankrupt: net worth ${nw:,.0f}"

    # -- lookups -----------------------------------------------------------
    def _player(self, player_id: str):
        return next((p for p in self.engine.players if p.player_id == player_id), None)

    def _human(self):
        return self._player(self.human_player_id)

    _op_id = staticmethod(route_op_id)

    @staticmethod
    def _op_id(op: RouteOp) -> str:
        return actions.op_id(op)

    def _find_route_op(self, player, route_op_id: str) -> Optional[RouteOp]:
        return actions.find_route_op(player, route_op_id)

    # Delegating shims so scenarios/tests can inspect without a command.
    def _find_plane(self, player, tail_number: str):
        return actions.find_plane(player, tail_number)

    def _plane_is_busy(self, plane) -> str:
        return actions.plane_is_busy(self.world, plane)

    def _build_layout(self, seats: dict, aircraft_spec):
        return actions.build_layout(seats, aircraft_spec)

    def _resolve_route(self, route_spec_id: str):
        return actions.resolve_route(self.world, route_spec_id)

    def _ensure_market(self, route_spec):
        return actions.ensure_market(self.world, route_spec)

    def _validate_equipment(self, route_spec, aircraft_spec):
        return actions.validate_equipment(self.world, route_spec, aircraft_spec)

    def _retire_tail(self, player, tail_number: str) -> int:
        return actions.retire_tail(player, tail_number)

    # -- command API ---------------------------------------------------
    # Thin wrappers: take the session lock, then call the shared action with
    # the HUMAN player. AI carriers call those identical functions inside the
    # tick (see ai.py), so both sides face the same validation, credit gate,
    # fees and teardown — the AI cannot cheat by construction.

    def _do(self, fn, *args, **kwargs):
        with self.lock:
            return fn(self.world, self._human(), *args, **kwargs)

    def set_price(self, route_op_id: str, price: float):
        return self._do(actions.set_price, route_op_id, price)

    def set_frequency(self, route_op_id: str, freq: int):
        return self._do(actions.set_frequency, route_op_id, freq)

    def set_layout(self, route_op_id: str, seats: dict):
        return self._do(actions.set_layout, route_op_id, seats)

    def set_cabin_price(self, route_op_id: str, cabin: str, price):
        return self._do(actions.set_cabin_price, route_op_id, cabin, price)

    def set_service_tier(self, route_op_id: str, tier: int):
        return self._do(actions.set_service_tier, route_op_id, tier)

    def open_route(self, route_spec_id: str, tail_number: str, price: float,
                   freq: int = 1, seats: Optional[dict] = None, service_tier: int = 2):
        return self._do(actions.open_route, route_spec_id, tail_number, price,
                        freq, seats, service_tier)

    def close_route(self, route_op_id: str):
        return self._do(actions.close_route, route_op_id)

    def acquire_aircraft(self, spec_id: str, tail_number: str, method: str,
                         base_iata: Optional[str] = None, seats: Optional[dict] = None):
        return self._do(actions.acquire_aircraft, spec_id, tail_number, method,
                        base_iata, seats, self.bank)

    def sell_aircraft(self, tail_number: str):
        return self._do(actions.sell_aircraft, tail_number)

    def break_lease(self, tail_number: str):
        return self._do(actions.break_lease, tail_number)

    def reconfigure_aircraft(self, tail_number: str, seats: dict):
        return self._do(actions.reconfigure_aircraft, tail_number, seats)

    def set_hub(self, iata: str, enabled: bool = True):
        return self._do(actions.set_hub, iata, enabled)

    def hire_crew(self, crew_type: str, base_iata: str, headcount: int,
                  cost_per_hour: float, certs: tuple = ()):
        return self._do(actions.hire_crew, crew_type, base_iata, headcount,
                        cost_per_hour, certs)

    # -- catalog (what's available to buy/fly) --------------------------
    def catalog(self) -> dict:
        with self.lock:
            repo = self.world.repo
            return {
                # Everything a fleet decision turns on: mission fit (seats,
                # range, runway need), commonality (type rating), and what a
                # later change of mind costs (recabin price + downtime).
                "aircraft": [{"spec_id": s.spec_id, "display_name": s.display_name,
                              "manufacturer": s.manufacturer,
                              "plane_class": s.plane_class.name,
                              "list_price": s.list_price, "max_seats": s.max_seats,
                              "max_range_km": s.max_range_km,
                              "takeoff_runway_m": s.takeoff_runway_m,
                              "type_rating": s.type_rating,
                              "reconfig_cost": s.reconfig_cost_per_slot
                                               * cabin_slots_for(s.max_seats),
                              "reconfig_days": s.reconfig_days,
                              # what physically fits: cabin length, seats per
                              # row per class, and ready-made cabin plans. The
                              # UI reads these instead of re-deriving geometry
                              # in the browser.
                              "cabin": geometry_for(s).describe(),
                              "cabin_presets": presets_for(s)}
                             for s in sorted(repo.all(AircraftSpec),
                                             key=lambda s: s.max_seats)],
                "routes": [{"spec_id": s.spec_id, "display_name": s.display_name,
                           "origin": s.origin_iata, "dest": s.dest_iata,
                           "distance_km": s.distance_km}
                          for s in repo.all(RouteSpec)],
                # The full route-openable set — on a data world this is every
                # corpus airport, which is exactly the point of the picker.
                "airports": [{"iata": s.iata, "display_name": s.display_name,
                              "runway_m": s.runway_length_m,
                              "has_mx": s.has_maintenance_facility,
                              "hub_fee_per_day": s.hub_fee_per_day}
                            for s in sorted(repo.all(AirportSpec),
                                            key=lambda s: s.iata)],
            }

    def cabin_fit(self, spec_id: str, seats: Optional[dict] = None) -> dict:
        """
        "If I asked for this cabin, what would I get, and what else fits?"

        The acquisition and recabin screens call this as the player types, so
        the numbers they see come from the SAME fitter that installs the
        cabin — the browser never gets its own copy of the geometry to drift
        from. Read-only: nothing is committed here.
        """
        with self.lock:
            try:
                spec = self.world.repo.get(AircraftSpec, spec_id)
            except KeyError:
                return {"error": f"unknown aircraft spec {spec_id}"}
        try:
            return fit_report(spec, seats or {})
        except ValueError as e:
            return {"error": str(e)}

    # -- snapshot projection (JSON-safe read model) ----------------------
    def snapshot(self) -> dict:
        with self.lock:
            w = self.world
            return {
                # Which build produced this state. The GUI shows it in the
                # topbar so "which version am I actually running?" is read off
                # the screen instead of reverse-engineered from the layout.
                "engine_version": _pkg_version(),
                "sim_time_hours": w.sim_time,
                "day": int(w.sim_time // 24),
                "paused": self.paused,
                "speed": self.speed,
                "game_over": self.game_over,
                "game_over_reason": self.game_over_reason,
                "human_player_id": self.human_player_id,
                "players": [self._player_snapshot(p) for p in self.engine.players],
                # Only airports the game is actually touching — on a data world
                # 300 gate ledgers exist so any pair can be opened, but pushing
                # all of them down the SSE stream every tick (and rendering a
                # 300-row card) buries the ones that matter. The full set stays
                # available through /api/catalog for the route picker.
                "airports": {
                    iata: {
                        "gates_used": gl.used(), "gates_total": gl.total_gates,
                        "fuel_spot": w.fuel[iata].spot_price() if iata in w.fuel else None,
                    } for iata, gl in w.gates.items()
                    if iata in self._active_iatas()
                },
            }

    def _active_iatas(self) -> set:
        """Airports in play: route endpoints, hubs, and fleet locations."""
        out = set()
        for p in self.engine.players:
            out.update(getattr(p, "hub_iatas", []))
            for a in p.fleet:
                if a.location_iata:
                    out.add(a.location_iata)
            for o in p.route_ops:
                out.add(o.spec.origin_iata)
                out.add(o.spec.dest_iata)
        return out

    def _player_snapshot(self, p) -> dict:
        debt = sum(l.remaining for l in p.loans)
        assets = sum(aircraft_value(a, self.world.sim_time) for a in p.fleet if a.owned)
        return {
            "player_id": p.player_id, "name": p.name, "is_ai": p.is_ai,
            "cash": p.ledger.cash, "debt": debt, "net_worth": p.ledger.cash + assets - debt,
            "fleet": [self._plane_snapshot(a) for a in p.fleet],
            "route_ops": [self._op_snapshot(o) for o in p.route_ops],
            "cockpit_pool": [self._crew_snapshot(c) for c in p.cockpit_pool],
            "cabin_pool": [self._crew_snapshot(c) for c in p.cabin_pool],
            "crews": [self._crew_snapshot(c) for c in p.crews],
            "loans": [{"loan_id": l.loan_id, "remaining": l.remaining,
                      "monthly_payment": l.monthly_payment(), "tail_number": l.tail_number}
                     for l in p.loans],
            "leases": [{"lease_id": l.lease_id, "tail_number": l.tail_number,
                       "months_elapsed": l.months_elapsed, "term_months": l.term_months}
                      for l in p.leases],
            "hubs": list(getattr(p, "hub_iatas", [])),
            "log": list(p.log[-20:]),
            "ai_profile": self._ai_profile(p),
        }

    def _ai_profile(self, p) -> Optional[dict]:
        """
        Which strategy an AI rival is playing, and its recent moves. Surfaced
        deliberately: an opponent whose style you can read is one you can plan
        against, and it keeps the AI's decisions legible rather than magic.
        """
        if not p.is_ai:
            return None
        from airlinesim.ai import AICarrierSubsystem
        for s in self.engine.subsystems:
            if isinstance(s, AICarrierSubsystem):
                return s.profile_of(p.player_id)
        return None

    def _plane_snapshot(self, a) -> dict:
        return {
            "tail_number": a.tail_number, "spec_id": a.spec.spec_id,
            "display_name": a.spec.display_name, "owned": a.owned,
            "location_iata": a.location_iata, "in_service": a.in_service,
            "grounded_until": a.grounded_until, "airframe_hours": a.airframe_hours,
            "retired": a.retired, "value": aircraft_value(a, self.world.sim_time),
            "reconfiguring_until": getattr(a, "reconfiguring_until", 0.0),
            # cabin as the player configured it; None = all-economy default
            "cabin": ({c.name: n for c, n in a.layout.seats.items() if n > 0}
                      if getattr(a, "layout", None) else None),
        }

    def _op_snapshot(self, o: RouteOp) -> dict:
        return {
            "route_op_id": self._op_id(o),
            "origin": o.spec.origin_iata, "dest": o.spec.dest_iata,
            "tail_number": o.plane.tail_number,
            "ticket_price": o.ticket_price, "daily_frequency": o.daily_frequency,
            "load_factor": o.last_load_factor, "pax": o.last_pax,
            "revenue": o.last_revenue, "profit": o.last_profit,
            "suitable": o.suitable, "suitability_reasons": list(o.suitability_reasons),
            "crew_block": o.last_crew_block,
            "has_cockpit": o.cockpit is not None, "has_cabin": o.cabin is not None,
            "service_tier": getattr(o, "service_tier", 2),
            "fees": getattr(o, "last_fees", 0.0),
            "data_tier": getattr(o.spec, "data_tier", ""),
            # Per-cabin economics for the cabins the ASSIGNED aircraft has:
            # the fare being charged, whether that fare is the route's own or
            # the base-fare default, and how that cabin actually sold. This is
            # what makes pricing a decision per cabin rather than one number
            # for the whole aeroplane.
            "cabins": self._cabin_snapshot(o),
        }

    def _cabin_snapshot(self, o: RouteOp) -> list:
        layout = o.effective_layout()
        overrides = getattr(o, "cabin_prices", None) or {}
        out = []
        for cc in CABIN_ORDER:
            seats = layout.seats_of(cc)
            if seats <= 0:
                continue
            pax = o.last_class_pax.get(cc.name, 0.0)
            offered = (getattr(o, "last_class_seats", None) or {}).get(cc.name, 0.0)
            out.append({
                "cabin": cc.name,
                "seats": seats,
                "fare": o.fare_for(cc),
                "priced": cc in overrides,
                "default_fare": o.ticket_price * DEFAULT_SEAT_CLASSES[cc].price_multiplier,
                "pax": pax,
                "revenue": (getattr(o, "last_class_revenue", None) or {}).get(cc.name, 0.0),
                "load_factor": (pax / offered) if offered > 1e-6 else 0.0,
            })
        return out

    def _crew_snapshot(self, c: CrewUnit) -> dict:
        return {
            "spec_id": c.spec.spec_id, "crew_type": c.spec.crew_type.name,
            "headcount": c.headcount, "home_iata": c.home_iata,
            "location_iata": c.location_iata, "resting": getattr(c.duty, "resting", False),
        }

    # -- save/load --------------------------------------------------------
    def save(self, path: str):
        with self.lock:
            with open(path, "wb") as f:
                pickle.dump(self, f)

    @classmethod
    def load(cls, path: str) -> "GameSession":
        with open(path, "rb") as f:
            return pickle.load(f)

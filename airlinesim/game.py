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
from airlinesim.finance_cabin import (
    CabinClass, SeatLayout, DEFAULT_SEAT_CLASSES, cabin_slots_for,
    AcquisitionMethod, FinancingTerms, Bank, aircraft_value,
)
from airlinesim.builder import build_demo_world

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


def new_game(human_name: str = "You", ai_name: str = "SkyRival",
             ai_step_frac: float = 0.03) -> "GameSession":
    """
    Build a ready-to-play two-carrier game. Reuses build_demo_world()'s
    validated setup (see builder.py / the integration scenario) and promotes
    the second carrier to an active AI opponent.
    """
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
    return GameSession(world, engine, human_player_id=human.player_id)


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
        self.bank = Bank(max_debt_to_cash=6.0)   # same leverage cap builder.py uses
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

    @staticmethod
    def _op_id(op: RouteOp) -> str:
        return f"{op.owner_id}:{op.spec.spec_id}:{op.plane.tail_number}"

    def _find_route_op(self, player, route_op_id: str) -> Optional[RouteOp]:
        return next((o for o in player.route_ops if self._op_id(o) == route_op_id), None)

    # -- command API ---------------------------------------------------
    # Every command takes the lock, mutates via existing engine machinery,
    # and returns (ok: bool, message: str) so the HTTP layer can surface
    # engine-side rejections (e.g. Bank's credit gate) instead of no-ops.

    def set_price(self, route_op_id: str, price: float):
        with self.lock:
            op = self._find_route_op(self._human(), route_op_id)
            if op is None:
                return False, "route not found"
            if price <= 0:
                return False, "price must be positive"
            op.ticket_price = round(float(price), 2)
            return True, f"price set to ${op.ticket_price:.0f}"

    def set_frequency(self, route_op_id: str, freq: int):
        with self.lock:
            op = self._find_route_op(self._human(), route_op_id)
            if op is None:
                return False, "route not found"
            op.daily_frequency = max(0, int(freq))
            return True, f"frequency set to {op.daily_frequency}/day"

    def set_layout(self, route_op_id: str, seats: dict):
        with self.lock:
            op = self._find_route_op(self._human(), route_op_id)
            if op is None:
                return False, "route not found"
            layout, err = self._build_layout(seats, op.plane.spec.max_seats)
            if err:
                return False, err
            op.layout = layout
            return True, "layout updated"

    def _build_layout(self, seats: dict, max_seats: int):
        try:
            seat_counts = {CabinClass[k.upper()]: int(v) for k, v in seats.items()}
        except KeyError as e:
            return None, f"unknown cabin class {e}"
        layout = SeatLayout(seat_counts)
        if not layout.is_valid(cabin_slots_for(max_seats), DEFAULT_SEAT_CLASSES):
            return None, "layout exceeds cabin capacity"
        return layout, None

    def acquire_aircraft(self, spec_id: str, tail_number: str, method: str,
                          base_iata: Optional[str] = None):
        with self.lock:
            p = self._human()
            if any(a.tail_number == tail_number for a in p.fleet):
                return False, "tail number already in use"
            try:
                spec = self.world.repo.get(AircraftSpec, spec_id)
            except KeyError:
                return False, f"unknown aircraft spec {spec_id}"
            method_enum = _METHOD_BY_NAME.get(method.upper())
            if method_enum is None:
                return False, f"unknown acquisition method {method}"
            terms = _TERMS_BY_METHOD[method_enum]

            # try_acquire() is the authoritative answer to "did it fund?". This
            # used to decide by matching "DENIED" in the freshly appended log
            # lines, which worked but coupled a financial invariant to log
            # wording — rename a message and the player silently gets a free
            # aircraft. The log lines are still used for the REASON shown to the
            # player, which is presentation rather than control flow.
            before = len(p.log)
            if not self.bank.try_acquire(p, spec, tail_number, method_enum,
                                         terms, p.log):
                reasons = [m.strip() for m in p.log[before:] if m.strip()]
                return False, "; ".join(reasons) or "acquisition denied"

            plane = Airplane(spec=spec, tail_number=tail_number, owner_id=p.player_id,
                              owned=(method_enum != AcquisitionMethod.OPERATING_LEASE),
                              location_iata=base_iata or next(iter(self.world.gates)),
                              acquired_at=self.world.sim_time)
            p.fleet.append(plane)
            return True, f"acquired {tail_number} ({spec.display_name}) via {method_enum.name}"

    def open_route(self, route_spec_id: str, tail_number: str, price: float,
                    freq: int = 1, seats: Optional[dict] = None):
        with self.lock:
            p = self._human()
            try:
                route_spec = self.world.repo.get(RouteSpec, route_spec_id)
            except KeyError:
                return False, f"unknown route {route_spec_id}"
            plane = next((a for a in p.fleet if a.tail_number == tail_number), None)
            if plane is None:
                return False, f"no aircraft {tail_number} in fleet"
            if any(o.plane.tail_number == tail_number and o.spec.spec_id == route_spec_id
                   for o in p.route_ops):
                return False, "already operating this route with that aircraft"
            layout = None
            if seats:
                layout, err = self._build_layout(seats, plane.spec.max_seats)
                if err:
                    return False, err
            op = RouteOp(spec=route_spec, plane=plane, cockpit=None, cabin=None,
                         ticket_price=float(price), daily_frequency=max(0, int(freq)),
                         owner_id=p.player_id, layout=layout)
            p.route_ops.append(op)
            return True, f"opened {route_spec.origin_iata}->{route_spec.dest_iata} with {tail_number}"

    def hire_crew(self, crew_type: str, base_iata: str, headcount: int,
                  cost_per_hour: float, certs: tuple = ()):
        with self.lock:
            p = self._human()
            try:
                ctype = CrewType[crew_type.upper()]
            except KeyError:
                return False, f"unknown crew type {crew_type}"
            if headcount <= 0:
                return False, "headcount must be positive"
            seq = len(p.crews) + len(p.cockpit_pool) + len(p.cabin_pool) + 1
            spec = CrewSpec(spec_id=f"{ctype.name}-{seq}", display_name=f"{ctype.name} crew {seq}",
                             crew_type=ctype, cost_per_member_hour=float(cost_per_hour),
                             certifications=tuple(certs))
            unit = CrewUnit(spec, headcount=int(headcount), owner_id=p.player_id, home_iata=base_iata)
            if ctype == CrewType.COCKPIT:
                p.cockpit_pool.append(unit)
            elif ctype == CrewType.CABIN:
                p.cabin_pool.append(unit)
            else:
                p.crews.append(unit)
            return True, f"hired {headcount}x {ctype.name} at {base_iata}"

    # -- catalog (what's available to buy/fly) --------------------------
    def catalog(self) -> dict:
        with self.lock:
            repo = self.world.repo
            return {
                "aircraft": [{"spec_id": s.spec_id, "display_name": s.display_name,
                              "list_price": s.list_price, "max_seats": s.max_seats,
                              "max_range_km": s.max_range_km}
                             for s in repo.all(AircraftSpec)],
                "routes": [{"spec_id": s.spec_id, "display_name": s.display_name,
                           "origin": s.origin_iata, "dest": s.dest_iata,
                           "distance_km": s.distance_km}
                          for s in repo.all(RouteSpec)],
                "airports": [{"iata": s.iata, "display_name": s.display_name}
                            for s in repo.all(AirportSpec)],
            }

    # -- snapshot projection (JSON-safe read model) ----------------------
    def snapshot(self) -> dict:
        with self.lock:
            w = self.world
            return {
                "sim_time_hours": w.sim_time,
                "day": int(w.sim_time // 24),
                "paused": self.paused,
                "speed": self.speed,
                "game_over": self.game_over,
                "game_over_reason": self.game_over_reason,
                "human_player_id": self.human_player_id,
                "players": [self._player_snapshot(p) for p in self.engine.players],
                "airports": {
                    iata: {
                        "gates_used": gl.used(), "gates_total": gl.total_gates,
                        "fuel_spot": w.fuel[iata].spot_price() if iata in w.fuel else None,
                    } for iata, gl in w.gates.items()
                },
            }

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
            "log": list(p.log[-20:]),
        }

    def _plane_snapshot(self, a) -> dict:
        return {
            "tail_number": a.tail_number, "spec_id": a.spec.spec_id,
            "display_name": a.spec.display_name, "owned": a.owned,
            "location_iata": a.location_iata, "in_service": a.in_service,
            "grounded_until": a.grounded_until, "airframe_hours": a.airframe_hours,
            "retired": a.retired, "value": aircraft_value(a, self.world.sim_time),
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
        }

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

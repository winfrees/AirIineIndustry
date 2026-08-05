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

import logging
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
from airlinesim.disruption import airport_reliability, disruption_snapshot
from airlinesim.finance_cabin import (
    DEFAULT_SEAT_CLASSES, cabin_slots_for,
    AcquisitionMethod, FinancingTerms, Bank, aircraft_value,
)
from airlinesim import actions, gamelog
from airlinesim.builder import build_demo_world

log = gamelog.get("session")

# A played game steps the world hourly and passes a day of game time every
# real second. Resolution and rate are separate knobs: DEFAULT_TICK_HOURS is
# how finely the world is simulated, DEFAULT_SPEED_H_PER_S is how fast it goes.
DEFAULT_TICK_HOURS = 1.0
DEFAULT_SPEED_H_PER_S = 24.0
# None means "draw a fresh seed", so every new game gets its own season and
# weather is a genuine risk rather than a schedule to be learned. Pass an
# explicit seed to replay a particular season. Either way the seed and the
# generator state pickle with the world, so a save — and an explorer fork —
# reproduces the weather it would have had.
DEFAULT_WEATHER_SEED = None

# The old speed slider ran 0.1-5.0 sim-DAYS per real second. A stored speed
# inside that range is read as a legacy day-rate and converted to hours on
# load; above it, the value is already hours. 5 h/s and below is slower than
# anything the hour slider offers, so nothing current is misread.
_LEGACY_MAX_DAYS_PER_S = 5.0

# -- real-time clock guards -------------------------------------------------
# The loop wakes every TICK_POLL_S. A wall-clock gap much larger than that
# doesn't mean we ran late — it means the PROCESS WAS FROZEN: laptop asleep,
# VM suspended, container stopped, debugger paused.
TICK_POLL_S = 0.2
# Past this, treat the gap as a suspend and refuse to replay it. 5s is ~25
# poll intervals: far beyond any scheduling delay, far below anything a player
# would experience as the game silently skipping ahead.
SUSPEND_GAP_S = 5.0
# Even a legitimate gap is capped, so a burst can't be replayed inside one
# locked iteration. At the default 0.5 days/s this is 8 sim-days of catch-up
# per wake, which is plenty to absorb a slow snapshot or a GC pause.
MAX_CATCHUP_S = 16.0
# Hard ceiling on ticks per wake regardless of speed: the session lock is held
# for the whole burst, so an unbounded loop makes the UI unresponsive.
MAX_TICKS_PER_WAKE = 64


def route_op_id(op: RouteOp) -> str:
    """Stable identifier for a route operation.

    Module-level so explorer.py addresses route ops by the same string the game
    GUI shows; if this format ever changes, both move together.
    """
    return f"{op.owner_id}:{op.spec.spec_id}:{op.plane.tail_number}"


def _fmt_args(args) -> str:
    """Command arguments for the log line. Scalars only — the Bank and other
    objects threaded through actions are identity, not information."""
    parts = [repr(a) for a in args
             if isinstance(a, (str, int, float, bool, dict, tuple, type(None)))]
    return "(" + ", ".join(parts) + ")"


def _fmt_sim_time(hours: float) -> str:
    """A span of SIM time, in the largest unit that reads naturally."""
    if hours < 48:
        return f"{hours:,.0f} hour{'' if 0.5 <= hours < 1.5 else 's'}"
    if hours < 24 * 365:
        return f"{hours / 24:,.0f} days"
    return f"{hours / (24 * 365):,.1f} years"


def _fmt_gap(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


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
                     ai_profiles=None, hub: str = "ORD", n_destinations: int = 5,
                     cash: float = 0.0, ai_cash=None):
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
            ai_profiles=profiles, cash=cash, ai_cash=ai_cash)
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
    # The sandbox has no fleet plan to size cash off, so it ships with whatever
    # build_demo_world chose. Honour an explicit figure anyway, so the same two
    # knobs mean the same thing in both worlds.
    if cash and cash > 0:
        human.ledger.cash = float(cash)
    if ai_cash is not None and float(ai_cash) > 0:
        ai.ledger.cash = float(ai_cash)
    # AIStrategySubsystem's docstring: "Runs BEFORE Operations so changes
    # take effect now" — the same ordering scenarios/competitive.py uses.
    ops_idx = next(i for i, s in enumerate(engine.subsystems)
                   if isinstance(s, OperationsSubsystem))
    engine.subsystems.insert(ops_idx, AIStrategySubsystem(step_frac=ai_step_frac))
    return world, engine, human.player_id


def new_game(human_name: str = "You", ai_name: str = "SkyRival",
             ai_step_frac: float = 0.03, world: str = "demo",
             ai_profiles=None, hub: str = "ORD",
             n_destinations: int = 5,
             tick_hours: float = DEFAULT_TICK_HOURS,
             weather: bool = True,
             weather_seed=DEFAULT_WEATHER_SEED,
             cash: float = 0.0, ai_cash=None) -> "GameSession":
    """
    Build a ready-to-play game.

      world="demo" — the two-airport sandbox, with the price/frequency bot.
      world="data" — the BTS-corpus network out of `hub`, with rivals that run
                     whole airlines (routes, fleet, cabins, service, crew).
                     Assign styles with ai_profiles={player_id: archetype};
                     see ai.ARCHETYPES.

    `tick_hours` is the simulation RESOLUTION. A played game runs hourly so
    weather, delays and duty timeouts land at a time of day; the scenarios keep
    the engine's 24-hour default, where a day is the smallest interesting unit
    and 24x the ticks would buy nothing.

    `cash` is the HUMAN's opening balance and `ai_cash` each rival's; both
    default to auto-sizing off the starting fleet, and `ai_cash=None` means
    "same as the human". Setting them apart is the difficulty dial.
    """
    w, engine, human_id = build_game_world(human_name, ai_name, ai_step_frac,
                                           world=world, ai_profiles=ai_profiles,
                                           hub=hub, n_destinations=n_destinations,
                                           cash=cash, ai_cash=ai_cash)
    log.info("new game: world=%s hub=%s dests=%d human=%s profiles=%s",
             world, hub, n_destinations, human_id, ai_profiles or {})
    for p in engine.players:
        log.info("  start %-14s %s fleet=%d routes=%d cash=$%.0f",
                 p.name, "AI" if p.is_ai else "human",
                 len(p.fleet), len(p.route_ops), p.ledger.cash)
    engine.dt = max(0.25, min(24.0, float(tick_hours)))
    if weather:
        # Weather is opt-in at the engine level (see disruption.attach_weather)
        # but ON for a played game: the whole point of an hourly clock is that
        # a storm can arrive during the afternoon and cost you the evening.
        from airlinesim.disruption import attach_weather
        model = attach_weather(w, engine, seed=weather_seed)
        log.info("weather attached (seed=%d) over %d airports",
                 model.seed, len(model.climates))
    # Alliances are always on in a played game. Unlike weather there is no
    # reason to switch them off: with no alliance formed the subsystem only
    # computes each op's own-network feed, which is a property of the network
    # the player built and not an extra rule imposed on them.
    from airlinesim.alliance import attach_alliances
    attach_alliances(w, engine)
    return GameSession(w, engine, human_player_id=human_id)


class GameSession:
    """
    Wraps a world + engine in a real-time clock.

    The clock is denominated in SIM HOURS PER REAL SECOND. It used to be
    sim-DAYS per real second, with one tick hard-wired to one day — which made
    the finest thing the player could observe a whole day, and left no way to
    express a three-hour weather delay or a crew timing out mid-afternoon.
    `speed` is hours now, and the loop steps the engine at whatever resolution
    `engine.dt` is set to, so the two are independent: how fast time passes and
    how finely it is simulated are separate decisions.

    Old saves pickled with a days-per-second `speed` are converted on load —
    see `__setstate__`. Without that a resumed game would run 24x too slow and
    look frozen.
    """

    def __init__(self, world, engine, human_player_id: str,
                 sim_hours_per_real_second: float = DEFAULT_SPEED_H_PER_S,
                 bankruptcy_floor: float = -5_000_000.0):
        self.world = world
        self.engine = engine
        self.human_player_id = human_player_id
        self.speed = float(sim_hours_per_real_second)
        self.bankruptcy_floor = bankruptcy_floor
        self.paused = True   # start paused so the player can look around first
        self.game_over = False
        self.game_over_reason = ""
        # the world's shared lender — AI carriers borrow from the same bank,
        # under the same leverage cap, with globally unique loan/lease ids
        self.bank = actions.bank_for(world)
        # Whether the human has ever held a fleet or a route. A data world
        # starts them with neither, so 'lost everything' can only be judged
        # against having had something. See _check_game_over.
        self._had_assets = False
        self._init_runtime()

    # -- runtime state that can't (and shouldn't) be pickled --------------
    def _init_runtime(self):
        self.lock = threading.RLock()
        self.ctx = {"market": MarketConditions()}
        self._carry = 0.0
        self._stop = threading.Event()
        # Set by the loop when it detects a suspend-sized clock gap; surfaced
        # in the snapshot so the GUI can say why the game paused itself.
        # Runtime state, not game state — a loaded save starts with none.
        self.clock_notice = ""
        self.on_tick = None   # optional callback(snapshot: dict), set by server.py
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def __getstate__(self):
        state = self.__dict__.copy()
        for k in ("lock", "ctx", "_carry", "_stop", "on_tick", "_thread",
                  "clock_notice"):
            state.pop(k, None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        # Saves from before the clock was denominated in hours carry a speed in
        # DAYS per second (0.1-5.0). Left alone, such a save resumes at ~0.5
        # sim-hours per second — a day of game time every 48 real seconds,
        # which reads as a hung clock rather than a slow one. Anything at or
        # below the old slider's ceiling is a day-rate and is converted.
        speed = getattr(self, "speed", 0.0)
        if 0 < speed <= _LEGACY_MAX_DAYS_PER_S:
            self.speed = speed * 24.0
        self._init_runtime()

    def stop(self):
        self._stop.set()

    # -- real-time loop -----------------------------------------------------
    def _loop(self):
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(TICK_POLL_S)
            now = time.monotonic()
            elapsed = now - last
            last = now

            # SUSPEND. The machine was asleep (or the VM/container frozen), so
            # this gap is real time that nobody played. Converting it to
            # sim-time is what silently destroyed a player's airline overnight:
            # three hours at the default speed is ~5,400 sim-days, ~15 years,
            # during which every 84-month lease legitimately expires and
            # BankingSubsystem correctly hands the metal back — taking the
            # route ops flown by those tails with it. Nothing there is wrong
            # except that the player was asleep and could not re-lease. The AI
            # appears immune only because it re-acquires every review cycle.
            #
            # So the gap is discarded, not replayed, and the game pauses so the
            # state the player left is the state they come back to.
            if elapsed > SUSPEND_GAP_S:
                snap = None
                with self.lock:
                    self._carry = 0.0
                    running = not (self.paused or self.game_over)
                    if running:
                        self.paused = True
                        skipped = elapsed * self.speed        # sim HOURS
                        self.clock_notice = (
                            f"Paused: the clock jumped {_fmt_gap(elapsed)} "
                            f"(computer asleep or process suspended). "
                            f"{_fmt_sim_time(skipped)} of simulated time was "
                            f"skipped rather than fast-forwarded, so your fleet "
                            f"and routes are as you left them. Press Resume to "
                            f"continue.")
                        snap = self.snapshot()
                    log.warning("clock gap %.1fs on day %d — %s",
                                elapsed, int(self.world.sim_time // 24),
                                "auto-paused" if running else "already paused")
                if snap and self.on_tick:
                    self.on_tick(snap)
                continue

            if self.paused or self.game_over:
                continue

            try:
                with self.lock:
                    # Clamp even a normal gap: the lock is held for the whole
                    # catch-up burst, so an unbounded one freezes the command
                    # API and the SSE stream along with it.
                    # _carry is in SIM HOURS owed; a tick spends engine.dt of
                    # them. Keeping the debt in hours rather than ticks is what
                    # lets tick resolution change without touching the clock.
                    self._carry += min(elapsed, MAX_CATCHUP_S) * self.speed
                    step = max(1e-6, self.engine.dt)
                    ticks = 0
                    while self._carry >= step and ticks < MAX_TICKS_PER_WAKE:
                        self.engine.tick(self.ctx)
                        self._carry -= step
                        ticks += 1
                    if ticks >= MAX_TICKS_PER_WAKE:
                        # Speed is set faster than this process can simulate.
                        # Dropping the backlog keeps the game responsive and
                        # slow rather than responsive-then-lurching.
                        self._carry = 0.0
                    if ticks:
                        self._check_game_over()
                        snap = self.snapshot()
                if ticks and self.on_tick:
                    self.on_tick(snap)
            except Exception:
                # A raise here used to kill the thread outright: the clock
                # simply stopped and the GUI showed a frozen but healthy game
                # with no error anywhere. Log it and pause, so the failure is
                # visible in the file and the session is still inspectable.
                log.exception("tick failed on day %d — pausing",
                              int(self.world.sim_time // 24))
                with self.lock:
                    self.paused = True
                    self.clock_notice = ("Paused: the simulation hit an "
                                         "internal error. See the log file.")

    def pause(self):
        with self.lock:
            self.paused = True
            log.info("paused on day %d", int(self.world.sim_time // 24))

    def resume(self):
        with self.lock:
            if not self.game_over:
                self.paused = False
                self._carry = 0.0
                # Acknowledging the pause is what dismisses the notice — the
                # banner should survive a reload, not one SSE frame.
                self.clock_notice = ""
                log.info("resumed on day %d at %.2f days/s",
                         int(self.world.sim_time // 24), self.speed)

    def set_speed(self, sim_hours_per_real_second: float):
        with self.lock:
            self.speed = max(0.1, float(sim_hours_per_real_second))

    def set_tick_hours(self, hours: float):
        """
        Change simulation RESOLUTION without changing how fast time passes.
        Finer ticks cost proportionally more CPU per simulated day and buy
        sharper timing on anything that happens within a day — weather windows,
        delays, a crew running out of duty hours mid-afternoon.
        """
        with self.lock:
            self.engine.dt = max(0.25, min(24.0, float(hours)))
            self._carry = 0.0
            return self.engine.dt

    def advance_hours(self, hours: float) -> dict:
        """
        Manual fast-forward, independent of real-time/pause state. Runs whole
        ticks, so the world lands on a tick boundary: asking for 5 hours at
        6-hour resolution advances nothing, which is honest about what the
        engine can actually resolve.
        """
        with self.lock:
            step = max(1e-6, self.engine.dt)
            for _ in range(int(max(0.0, float(hours)) / step)):
                if self.game_over:
                    break
                self.engine.tick(self.ctx)
            self._check_game_over()
            snap = self.snapshot()
        if self.on_tick:
            self.on_tick(snap)
        return snap

    def advance_days(self, n: int = 1) -> dict:
        return self.advance_hours(max(0, int(n)) * 24.0)

    # -- win/loss -------------------------------------------------------
    def _net_worth(self, p) -> float:
        debt = sum(l.remaining for l in p.loans)
        assets = sum(aircraft_value(a, self.world.sim_time) for a in p.fleet if a.owned)
        return p.ledger.cash + assets - debt

    def _check_game_over(self):
        human = self._human()
        if human is None:
            # A human_player_id that matches nobody is a caller error, but it
            # must not raise INSIDE the tick loop: GameSession._loop holds the
            # session lock for the whole burst, and an exception there used to
            # leave a frozen-but-healthy GUI with the error nowhere.
            return
        # Belt and braces: AI carriers are barred from acquiring the human
        # (see ai._merger_review), but if that ever changes, or a scenario
        # does it deliberately, the player must be TOLD rather than left
        # staring at an empty airline with no explanation.
        #
        # Gated on having HAD assets, not on having none: a data-world game
        # deliberately starts the human with cash and nothing else, so a bare
        # "no fleet and no routes" test would declare game over on the first
        # tick of every new game.
        has_assets = bool(human.fleet or human.route_ops)
        if has_assets:
            self._had_assets = True
        elif getattr(self, "_had_assets", False):
            if not self.game_over:
                self.game_over = True
                self.paused = True
                self.game_over_reason = ("no fleet and no routes left — "
                                         "the airline is gone")
                log.warning("game over on day %d — %s",
                            int(self.world.sim_time // 24), self.game_over_reason)
            return
        nw = self._net_worth(human)
        if nw < self.bankruptcy_floor:
            self.game_over = True
            self.paused = True
            self.game_over_reason = f"bankrupt: net worth ${nw:,.0f}"
            log.warning("game over on day %d — %s",
                        int(self.world.sim_time // 24), self.game_over_reason)

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
            result = fn(self.world, self._human(), *args, **kwargs)
            # Every human command, with its outcome, at the sim-day it
            # happened. This is the record that answers "my routes vanished —
            # did I close them, did the engine, or did the clock?".
            try:
                ok, message = result
            except (TypeError, ValueError):
                ok, message = True, repr(result)
            log.log(logging.INFO if ok else logging.WARNING,
                    "day %d cmd %s%s -> %s %s",
                    int(self.world.sim_time // 24), fn.__name__,
                    _fmt_args(args), "ok" if ok else "REFUSED", message)
            return result

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

    # -- alliances and consolidation -------------------------------------
    def form_alliance(self, name: str, kind: str = "CODESHARE", partners=None):
        return self._do(actions.form_alliance, name, kind, partners)

    def join_alliance(self, alliance_id: str):
        return self._do(actions.join_alliance, alliance_id)

    def leave_alliance(self):
        return self._do(actions.leave_alliance)

    def set_no_compete_hub(self, iata: str, enabled: bool = True):
        return self._do(actions.set_no_compete_hub, iata, enabled)

    def acquire_carrier(self, target_id: str, force: bool = False):
        """
        Buy a rival outright. Cash flow for both sides comes from the AI
        subsystem's own smoothed figures where they exist, so a human's bid is
        priced off exactly the numbers an AI bidder would use — neither side
        gets a better valuation than the other.
        """
        with self.lock:
            return actions.acquire_carrier(
                self.world, self._human(), target_id,
                acquirer_cf=self._cash_flow_of(self.human_player_id),
                target_cf=self._cash_flow_of(target_id), force=force)

    def _cash_flow_of(self, player_id: str) -> float:
        """
        A carrier's smoothed operating cash flow, from the AI subsystem's
        memory if it tracks that carrier. The human is not tracked there, so
        it falls back to the ledger trend — see merger_candidates().
        """
        from airlinesim.ai import AICarrierSubsystem
        for s in self.engine.subsystems:
            if isinstance(s, AICarrierSubsystem):
                mem = s.memory.get(player_id)
                if mem is not None:
                    return mem.cash_flow_per_day
        return 0.0

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
                              "hub_fee_per_day": s.hub_fee_per_day,
                              # the map needs geography; served once with the
                              # catalog rather than per tick down the SSE stream
                              "lat": s.lat, "lon": s.lon,
                              "hub_rank": s.hub_rank}
                            for s in sorted(repo.all(AirportSpec),
                                            key=lambda s: s.iata)],
            }

    def merger_candidates(self) -> dict:
        """
        Every rival the human could bid for, each with its itemised valuation
        and a fully costed case: rationale, price, integration cost, synergies,
        payback, and the reason it would or wouldn't be approved.

        Read-only — this is the "should I?" screen, and nothing here commits.
        Rejected candidates are returned WITH their reason rather than filtered
        out, because "why can't I buy them?" is the question the screen exists
        to answer.
        """
        from airlinesim.alliance import alliance_snapshot
        from airlinesim.merger import (competitive_position, merger_case,
                                       value_carrier)
        with self.lock:
            me = self._human()
            players = list(self.engine.players)
            my_cf = self._cash_flow_of(self.human_player_id)
            my_pos = competitive_position(self.world, players, me, my_cf)
            out = []
            for other in players:
                if other.player_id == self.human_player_id:
                    continue
                if not other.fleet and not other.route_ops:
                    continue
                cf = self._cash_flow_of(other.player_id)
                val = value_carrier(self.world, other, cf)
                case = merger_case(self.world, players, me, other, my_cf, cf)
                pos = competitive_position(self.world, players, other, cf)
                payback = case.payback_years()
                out.append({
                    "player_id": other.player_id, "name": other.name,
                    "fleet": len([a for a in other.fleet if not a.retired]),
                    "routes": len(other.route_ops),
                    "enterprise_value": round(val.enterprise_value()),
                    "liquidation_value": round(val.liquidation_value()),
                    "cash": round(val.cash), "fleet_value": round(val.fleet_value),
                    "debt": round(val.debt),
                    "lease_obligations": round(val.lease_obligations),
                    "going_concern": round(val.going_concern),
                    "network_value": round(val.network_value),
                    "reputation": round(val.reputation, 3),
                    "rationale": case.rationale.name,
                    "price": round(case.price),
                    "integration_cost": round(case.integration_cost),
                    "total_outlay": round(case.total_outlay()),
                    "annual_synergy": round(case.annual_synergy()),
                    "overlap_routes": case.overlap_routes,
                    "complementary_stations": case.complementary_stations,
                    "fleet_commonality": round(case.fleet_commonality, 3),
                    # inf doesn't survive JSON; None reads as "never pays back"
                    "payback_years": (round(payback, 1)
                                      if payback != float("inf") else None),
                    "approved": case.verdict,
                    "reason": case.reason,
                    "affordable": me.ledger.cash >= case.total_outlay(),
                    "cannot_compete_alone": pos.cannot_compete_alone(),
                    "share": round(pos.share, 4),
                    "alliance": alliance_snapshot(self.world, other.player_id),
                })
            out.sort(key=lambda c: (not c["approved"],
                                    c["payback_years"] if c["payback_years"]
                                    is not None else 1e9))
            return {
                "cash": round(me.ledger.cash),
                "my_share": round(my_pos.share, 4),
                "leader_share": round(my_pos.leader_share, 4),
                "cannot_compete_alone": my_pos.cannot_compete_alone(),
                "alliance": alliance_snapshot(self.world, self.human_player_id),
                "alliances": [
                    {"alliance_id": a.alliance_id, "name": a.name,
                     "kind": a.kind.name, "members": list(a.members)}
                    for a in getattr(self.world, "alliances", [])],
                "candidates": out,
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
            # Computed once for the whole snapshot: the weather lookup and the
            # reliability roll-up are both per-airport, and the airports block
            # below would otherwise call them inside a comprehension.
            active = self._active_iatas()
            model = getattr(w, "weather", None)
            wx = model.snapshot(w.sim_time, active) if model else {}
            rel = airport_reliability(w, active)
            return {
                # Which build produced this state. The GUI shows it in the
                # topbar so "which version am I actually running?" is read off
                # the screen instead of reverse-engineered from the layout.
                "engine_version": _pkg_version(),
                "sim_time_hours": w.sim_time,
                "day": int(w.sim_time // 24),
                # Hour of the simulated day. The clock is hours now, so the
                # GUI can show when in the day something happened rather than
                # only which day it was.
                "hour": int(w.sim_time % 24),
                "paused": self.paused,
                "speed": self.speed,                      # sim hours / real second
                "tick_hours": self.engine.dt,             # simulation resolution
                "game_over": self.game_over,
                "game_over_reason": self.game_over_reason,
                # Non-empty when the loop caught the process being suspended.
                # Cleared by resume(), so it survives a reload of the page.
                "clock_notice": getattr(self, "clock_notice", ""),
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
                        # live sky + the cumulative record, so a hub that
                        # costs you every winter shows it
                        "weather": wx.get(iata, {}),
                        "reliability": rel.get(iata, {}),
                    } for iata, gl in w.gates.items()
                    if iata in active
                },
                "weather_systems": (w.weather.system_snapshot(w.sim_time)
                                    if getattr(w, "weather", None) else []),
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
            "disruption": disruption_snapshot(self.world, p),
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
            # frequencies actually operated after gates, crew and weather —
            # the map draws an aircraft only where something flew.
            "eff_freq": round(getattr(o, "last_eff_freq", 0.0), 3),
            "distance_km": o.spec.distance_km,
            "block_h": round(o.spec.distance_km / o.plane.spec.cruise_speed_kmh, 3),
            "load_factor": o.last_load_factor, "pax": o.last_pax,
            "revenue": o.last_revenue, "profit": o.last_profit,
            "suitable": o.suitable, "suitability_reasons": list(o.suitability_reasons),
            "crew_block": o.last_crew_block,
            "has_cockpit": o.cockpit is not None, "has_cabin": o.cabin is not None,
            "service_tier": getattr(o, "service_tier", 2),
            "fees": getattr(o, "last_fees", 0.0),
            "data_tier": getattr(o.spec, "data_tier", ""),
            # what the weather is doing to THIS route right now
            "weather": getattr(o, "weather_text", ""),
            "weather_capacity": round(getattr(o, "weather_capacity", 1.0), 3),
            "weather_delay_h": round(getattr(o, "weather_delay_h", 0.0), 2),
            "weather_cancelled": round(getattr(o, "last_weather_cancelled", 0.0), 2),
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
            log.info("saved day %d to %s", int(self.world.sim_time // 24), path)

    @classmethod
    def load(cls, path: str) -> "GameSession":
        with open(path, "rb") as f:
            session = pickle.load(f)
        log.info("loaded day %d from %s",
                 int(session.world.sim_time // 24), path)
        return session

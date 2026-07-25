"""
CREW ENTITY — brought to the Aircraft standard.
================================================

Turns crew from cost line-items into a real operational constraint. The core
mechanic is DUTY/REST: a crew that has flown its legal limit cannot be assigned,
so a flight with no legal crew does not operate — even if the plane and gate are
available. This is the single biggest real-world airline constraint.

Grounded in FAR Part 117 (defensible game defaults, not certified figures):
  - daily flight-time cap ~8-9h for a standard 2-pilot crew
  - rolling duty caps: 60 FDP-hours / 168h (7 days), 190 / 672h (28 days)
  - mandatory rest: >=10 consecutive hours before a new duty period

Model scope: we track flight hours in rolling windows + a rest timer. We do NOT
model circadian/WOCL detail or augmented crews — those are future refinements.

Pieces:
  CrewSpec (extended) . type-ratings, duty limits, rest requirement
  CrewUnit (extended) . live duty state: rolling hours, rest timer, base
  CrewDutyState ....... the accumulating per-crew state
  is_legal_for_flight . the gate every flight assignment must pass
  CrewLegalitySubsystem advances rest/decay each tick
"""

from __future__ import annotations
from dataclasses import dataclass, field
from collections import deque


# ============================================================
# DUTY LIMITS — reference data (Part 117-shaped defaults)
# ============================================================

@dataclass(frozen=True)
class DutyLimits:
    """Regulatory duty/rest envelope. Authorable / importable per crew or region."""
    max_daily_flight_hours: float = 9.0       # ~Table A un-augmented 2-pilot
    max_weekly_fdp_hours: float = 60.0        # 60 FDP-hrs / 168h
    max_28day_fdp_hours: float = 190.0        # 190 FDP-hrs / 672h
    min_rest_hours: float = 10.0              # >=10 consecutive hours before duty
    # hours of continuous duty that TRIGGER a mandatory rest requirement
    duty_before_rest_hours: float = 13.0      # ~max FDP before rest is owed


DEFAULT_DUTY_LIMITS = DutyLimits()
# Non-flight crews (ground/baggage/met) aren't flight-duty limited the same way;
# give them a permissive envelope so the same machinery applies uniformly.
GROUND_DUTY_LIMITS = DutyLimits(max_daily_flight_hours=1e9, max_weekly_fdp_hours=1e9,
                                max_28day_fdp_hours=1e9, min_rest_hours=0.0,
                                duty_before_rest_hours=1e9)


# ============================================================
# CREW DUTY STATE — live, accumulating per-crew
# ============================================================

@dataclass
class CrewDutyState:
    """
    Rolling-window duty tracking. We keep timestamped flight-hour entries and
    sum those inside each window, so the limits are true rolling caps rather
    than calendar-bucket approximations.
    """
    # entries: (sim_time_hours, flight_hours_logged)
    log: deque = field(default_factory=deque)
    continuous_duty_hours: float = 0.0     # since last rest
    resting: bool = False
    rest_accumulated: float = 0.0          # consecutive rest hours banked
    hours_today: float = 0.0               # flight hours in current calendar day
    _day_index: int = -1

    def _trim(self, now: float, window: float):
        cutoff = now - window
        while self.log and self.log[0][0] < cutoff:
            self.log.popleft()

    def hours_in_window(self, now: float, window: float) -> float:
        self._trim(now, window)
        return sum(h for (t, h) in self.log if t >= now - window)

    def log_flight(self, now: float, hours: float):
        self.log.append((now, hours))
        self.continuous_duty_hours += hours
        self.hours_today += hours
        self.resting = False
        self.rest_accumulated = 0.0

    def log_deadhead(self, now: float, hours: float):
        """
        Deadhead positioning: the crew rides as passengers to reposition. Per
        Part 117 this is DUTY time (it breaks rest and counts toward continuous
        duty) but NOT flight time (it does not hit the flight-hour windows or the
        daily flight-time cap). So we advance duty/rest but skip the flight log.
        """
        self.continuous_duty_hours += hours
        self.resting = False
        self.rest_accumulated = 0.0

    def roll_day(self, day_index: int):
        if day_index != self._day_index:
            self._day_index = day_index
            self.hours_today = 0.0


# ============================================================
# LEGALITY GATE — the check every flight assignment must pass
# ============================================================

def is_legal_for_flight(crew, now: float, added_flight_hours: float,
                        limits: "DutyLimits") -> tuple:
    """
    Returns (legal: bool, reason: str). A crew is illegal to assign if the added
    flight hours would breach the daily cap, either rolling cap, or if the crew
    is mid-rest and hasn't banked the minimum rest yet.
    """
    st = crew.duty
    # mid-rest and not yet rested enough
    if st.resting and st.rest_accumulated < limits.min_rest_hours:
        return (False, f"resting ({st.rest_accumulated:.1f}/{limits.min_rest_hours:.0f}h)")
    # daily flight-time cap
    if st.hours_today + added_flight_hours > limits.max_daily_flight_hours + 1e-6:
        return (False, f"daily cap ({st.hours_today:.1f}+{added_flight_hours:.1f}>"
                       f"{limits.max_daily_flight_hours:.0f}h)")
    # rolling 7-day cap
    wk = st.hours_in_window(now, 168.0)
    if wk + added_flight_hours > limits.max_weekly_fdp_hours + 1e-6:
        return (False, f"7-day cap ({wk:.0f}+{added_flight_hours:.1f}>"
                       f"{limits.max_weekly_fdp_hours:.0f}h)")
    # rolling 28-day cap
    mo = st.hours_in_window(now, 672.0)
    if mo + added_flight_hours > limits.max_28day_fdp_hours + 1e-6:
        return (False, f"28-day cap ({mo:.0f}+{added_flight_hours:.1f}>"
                       f"{limits.max_28day_fdp_hours:.0f}h)")
    return (True, "ok")


def crew_is_type_rated(crew, aircraft_spec) -> bool:
    """Generalized type-rating: crew certs must include the aircraft type/mfr.
    Empty certs = universal (e.g. ground staff not type-restricted)."""
    certs = crew.spec.certifications
    if not certs:
        return True
    return (aircraft_spec.spec_id in certs or aircraft_spec.manufacturer in certs)


# ============================================================
# SUBSYSTEM — advances rest and continuous-duty decay each tick
# ============================================================

class CrewLegalitySubsystem:
    """
    Each tick: roll the calendar day, and for crews that did NOT fly this tick,
    accumulate rest. Once a crew banks the minimum rest, its continuous-duty
    counter resets and it's legal again. Crews that flew had their rest reset
    in log_flight(). This is what lets a timed-out crew recover.
    """
    def __init__(self, limits: "DutyLimits" = DEFAULT_DUTY_LIMITS):
        self.limits = limits

    def tick(self, world, players, dt: float, ctx: dict):
        flew = ctx.get("_crew_flew_this_tick", set())
        for p in players:
            for crew in _all_crews(p):
                st = crew.duty
                if id(crew) not in flew:
                    # idle this tick -> banking rest
                    st.resting = True
                    st.rest_accumulated += dt
                    if st.rest_accumulated >= self.limits.min_rest_hours:
                        st.continuous_duty_hours = 0.0


def _all_crews(player):
    seen = set()
    out = []
    for c in list(player.crews):
        if id(c) not in seen:
            seen.add(id(c)); out.append(c)
    for op in player.route_ops:
        for c in (op.cockpit, op.cabin):
            if c is not None and id(c) not in seen:
                seen.add(id(c)); out.append(c)
    return out


# ============================================================
# ROSTERING — proactive crew assignment from a pool
# ============================================================
#
# Instead of each RouteOp owning a fixed crew, the player holds POOLS of
# cockpit and cabin crews. Each tick the roster assigns the best-positioned,
# legal, type-rated crew to each op that needs flying. Crews that fly end the
# tick at the DESTINATION (out of base); they must rest there or fly the return.
# This makes positioning a real constraint.


def _candidate_score(crew, op, now, fh_needed):
    """Higher = better. -inf if the crew cannot legally/physically take the op."""
    if not crew_is_type_rated(crew, op.plane.spec):
        return float("-inf")
    ok, _ = is_legal_for_flight(crew, now, fh_needed, crew.limits)
    if not ok:
        return float("-inf")
    score = 0.0
    if crew.location_iata == op.spec.origin_iata:
        score += 1000.0          # already in position: ideal
    elif crew.location_iata == op.spec.dest_iata:
        score += 100.0           # on the route; could fly the return leg
    else:
        score -= 500.0           # off-network: expensive to position
    wk = crew.duty.hours_in_window(now, 168.0)
    score += max(0.0, crew.limits.max_weekly_fdp_hours - wk)   # prefer rested
    return score


class RosterSubsystem:
    """
    Assigns crews from player.cockpit_pool / player.cabin_pool to each route op,
    BEFORE Operations. If no legal, positioned crew exists, the op gets None and
    is grounded. Falls back to legacy fixed-crew mode if no pools are defined.
    """
    def tick(self, world, players, dt, ctx):
        now = world.sim_time
        deadheaded = ctx.get("_crew_deadheaded_this_tick", set())
        for p in players:
            cockpit_pool = list(getattr(p, "cockpit_pool", []))
            cabin_pool = list(getattr(p, "cabin_pool", []))
            if not cockpit_pool and not cabin_pool:
                continue
            taken = set(deadheaded)   # deadheading crews are unavailable to work
            for op in p.route_ops:
                # Score against ONE rotation's worth of flying — a crew that can
                # legally take at least one rotation is a valid assignment. The
                # Operations duty gate then caps how many rotations they actually
                # fly. This lets a multi-rotation op be served by a rested crew.
                fh_one = (op.spec.distance_km / op.plane.spec.cruise_speed_kmh) \
                    * (dt / 24.0)
                op.cockpit = self._assign(cockpit_pool, taken, op, now, fh_one)
                op.cabin = self._assign(cabin_pool, taken, op, now, fh_one)
                if op.cockpit is None or op.cabin is None:
                    op.last_crew_block = "no legal crew available to roster"
                else:
                    op.last_crew_block = ""

    def _assign(self, pool, taken, op, now, fh_needed):
        best, best_score = None, float("-inf")
        for crew in pool:
            if id(crew) in taken:
                continue
            s = _candidate_score(crew, op, now, fh_needed)
            if s > best_score:
                best, best_score = crew, s
        if best is not None and best_score > float("-inf"):
            taken.add(id(best))
            return best
        return None


class CrewPositioningSubsystem:
    """
    Runs AFTER Operations. Moves crews that flew to the destination they flew to
    (out of base). Idle crews stay put and bank rest.
    """
    def tick(self, world, players, dt, ctx):
        flew = ctx.get("_crew_flew_this_tick", set())
        for p in players:
            for op in p.route_ops:
                for crew in (op.cockpit, op.cabin):
                    if crew is not None and id(crew) in flew:
                        crew.location_iata = op.spec.dest_iata


class DeadheadSubsystem:
    """
    Gets out-of-base crews home by booking them as passengers (deadheading) on
    the airline's own revenue flights heading toward their base. Runs BEFORE the
    roster each tick, so a crew that deadheads home is in position to be rostered.

    Rules modeled:
      - only crews that are (a) off-duty / not flying this tick, (b) away from
        home base, and (c) legal for the deadhead duty are eligible
      - the crew rides a revenue flight whose ORIGIN == crew location and whose
        DEST advances them toward home (here: == home base; a network model would
        do multi-hop). The seat is RESERVED, reducing sellable capacity.
      - deadhead logs DUTY time (breaks rest) but not flight time
      - if no suitable flight exists, the crew stays put (future: positioning flt)
    """
    def tick(self, world, players, dt, ctx):
        flew = ctx.get("_crew_flew_this_tick", set())
        # crews that deadhead this tick are riding as passengers, not available
        # to be rostered onto a working flight this tick.
        deadheaded = ctx.setdefault("_crew_deadheaded_this_tick", set())
        now = world.sim_time
        for p in players:
            # reset per-tick deadhead reservations on every op
            for op in p.route_ops:
                op.deadhead_seats = 0
            all_crew = list(getattr(p, "cockpit_pool", [])) + \
                       list(getattr(p, "cabin_pool", []))
            for crew in all_crew:
                if id(crew) in flew:
                    continue  # flying this tick, not available to deadhead
                if not crew.home_iata or crew.location_iata == crew.home_iata:
                    continue  # already home
                # find a revenue flight from the crew's location toward home
                for op in p.route_ops:
                    if op.spec.origin_iata != crew.location_iata:
                        continue
                    if op.spec.dest_iata != crew.home_iata:
                        continue  # only direct-to-base in this model
                    if op.last_eff_freq <= 0:
                        continue  # that flight isn't actually operating
                    dh_hours = op.spec.distance_km / op.plane.spec.cruise_speed_kmh
                    # reserve a seat and reposition the crew
                    op.deadhead_seats = getattr(op, "deadhead_seats", 0) + crew.headcount
                    crew.duty.log_deadhead(now, dh_hours)
                    crew.location_iata = op.spec.dest_iata
                    deadheaded.add(id(crew))  # not available to work this tick
                    break

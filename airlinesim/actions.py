"""
ACTIONS — the airline decision surface, player-agnostic.
========================================================

Every decision an airline can make (open a route, buy or sell an airframe,
recabin it, declare a hub, hire crew, set a price) lives here as a plain
function over ``(world, player, ...)`` returning ``(ok, message)``.

Why this module exists: these actions used to live inside ``GameSession``,
hard-wired to the human player and to the session lock, which meant an AI
competitor could only compete by mutating engine state directly — running
its own parallel copy of the validation, fee, credit and teardown rules.
Two copies drift, and when they drift the AI ends up playing a different
game than the human.

So: this is the single implementation, and it is *neutral*.

  - ``GameSession`` commands are thin wrappers: take the lock, call in here
    with the human player.
  - ``AIStrategySubsystem`` calls exactly the same functions with an AI
    player, inside the tick.

An AI therefore cannot cheat by construction. It passes the same equipment
validation, the same ``Bank`` credit gate, the same cash checks, and the
same route teardown as the player does. If a rule changes, it changes for
everyone at once.

Nothing here locks or assumes a session — callers own concurrency.
"""

from __future__ import annotations

from typing import Optional

from airlinesim.engine import (
    AircraftSpec, AirportSpec, RouteSpec, CrewSpec, CrewUnit, RouteOp,
    Airplane, CrewType, market_key,
)
from airlinesim.finance_cabin import (
    CabinClass, cabin_slots_for,
    AcquisitionMethod, FinancingTerms, Bank, aircraft_value,
)
from airlinesim.cabin import fit_layout, parse_seats, preset_layout

# Reference financing products. Same shape/values builder.py and the
# integration scenario already use.
LOAN_TERMS = FinancingTerms("LOAN", AcquisitionMethod.FINANCE,
                            down_payment_frac=0.20, annual_rate=0.06, term_months=120)
LEASE_TERMS = FinancingTerms("LEASE", AcquisitionMethod.OPERATING_LEASE,
                             lease_rate_frac_per_year=0.11, lease_term_months=84)
METHOD_BY_NAME = {
    "CASH": AcquisitionMethod.BUY_CASH,
    "FINANCE": AcquisitionMethod.FINANCE,
    "LEASE": AcquisitionMethod.OPERATING_LEASE,
}
TERMS_BY_METHOD = {
    AcquisitionMethod.BUY_CASH: None,
    AcquisitionMethod.FINANCE: LOAN_TERMS,
    AcquisitionMethod.OPERATING_LEASE: LEASE_TERMS,
}

# Selling into the used market doesn't realize full book value: dealers take
# a spread and a motivated seller takes a discount. Game-balance figure.
SALE_HAIRCUT = 0.85


def bank_for(world) -> Bank:
    """
    The world's shared lender. Attached lazily so worlds pickled before the
    bank moved onto World still work. Sharing one Bank keeps loan/lease ids
    unique across every carrier; a resumed pre-existing save may briefly run
    a session-owned bank alongside this one, which only affects the cosmetic
    id sequence (lookups are by tail number, never by id).
    """
    bank = getattr(world, "bank", None)
    if bank is None:
        bank = Bank(max_debt_to_cash=6.0)
        world.bank = bank
    return bank


# ============================================================
# LOOKUP / VALIDATION HELPERS (shared by commands and AI policy)
# ============================================================

def op_id(op: RouteOp) -> str:
    return f"{op.owner_id}:{op.spec.spec_id}:{op.plane.tail_number}"


def find_route_op(player, route_op_id: str) -> Optional[RouteOp]:
    return next((o for o in player.route_ops if op_id(o) == route_op_id), None)


def find_plane(player, tail_number: str):
    return next((a for a in player.fleet if a.tail_number == tail_number), None)


def plane_is_busy(world, plane) -> str:
    """Reason the tail can't be traded/reconfigured right now, or ''."""
    if plane.reconfiguring_until > 0:
        return "aircraft is being reconfigured"
    if not plane.in_service or plane.grounded_until > world.sim_time:
        return "aircraft is grounded for maintenance"
    return ""


def airport(world, iata: str) -> Optional[AirportSpec]:
    try:
        return world.repo.get(AirportSpec, iata)
    except KeyError:
        return None


def build_layout(seats: dict, aircraft_spec):
    """
    (SeatLayout, notes, None) or (None, None, error).

    Seat counts are FITTED to the airframe's real cabin geometry rather than
    accepted-or-rejected against a flat slot count: they snap to whole
    installable rows, overflow is trimmed cheapest-cabin-first, and a cabin
    left unspecified fills the space that's left (see airlinesim.cabin). The
    adjustments come back as `notes` so nothing is changed silently.

    A named preset ("two-class", "three-class", ...) may be passed instead of
    a seat dict — the same plan resolved against whatever airframe it lands
    on. Only a genuinely unparseable request is an error.
    """
    if isinstance(seats, str):
        try:
            fit = preset_layout(aircraft_spec, seats.strip().lower())
        except KeyError as e:
            return None, None, str(e).strip("\"'")
        return fit.layout, fit.notes, None
    parsed, err = parse_seats(seats)
    if err:
        return None, None, err
    fit = fit_layout(aircraft_spec, parsed)
    return fit.layout, fit.notes, None


def _with_notes(msg: str, notes) -> str:
    return f"{msg} ({'; '.join(notes)})" if notes else msg


def _cabin_summary(layout) -> str:
    """"12 first, 40 business, 210 economy" — forward to aft."""
    from airlinesim.cabin import CABIN_ORDER
    return ", ".join(f"{layout.seats_of(cc)} {cc.name.lower()}"
                     for cc in CABIN_ORDER if layout.seats_of(cc) > 0)


def validate_equipment(world, route_spec, aircraft_spec):
    """(ok, reasons) — the same check RouteSuitabilitySubsystem runs per tick."""
    from airlinesim.route import route_can_fly
    return route_can_fly(route_spec, aircraft_spec,
                         airport(world, route_spec.origin_iata),
                         airport(world, route_spec.dest_iata))


def resolve_route(world, route_spec_id: str):
    """
    Find a pre-authored RouteSpec, or build one on the fly for an "ORG-DST"
    airport pair (great-circle distance, metro-pair market). Registers the
    spec and its demand market. Returns (spec, error_message).
    """
    repo = world.repo
    try:
        return repo.get(RouteSpec, route_spec_id), None
    except KeyError:
        pass
    parts = route_spec_id.replace("->", "-").split("-")
    if len(parts) != 2:
        return None, f"unknown route {route_spec_id}"
    origin_iata, dest_iata = parts[0].strip().upper(), parts[1].strip().upper()
    if origin_iata == dest_iata:
        return None, "origin and destination must differ"
    origin, dest = airport(world, origin_iata), airport(world, dest_iata)
    if origin is None:
        return None, f"unknown airport {origin_iata}"
    if dest is None:
        return None, f"unknown airport {dest_iata}"

    spec = _route_spec_from_corpus(world, origin, dest)
    repo._tables[RouteSpec][spec.spec_id] = spec
    ensure_market(world, spec)
    return spec, None


def _route_spec_from_corpus(world, origin, dest):
    """
    Build a RouteSpec for an arbitrary pair. Prefers the committed BTS corpus
    (measured demand where the pair exists, a fitted gravity estimate where it
    doesn't — see routedata.RouteDataProvider), and falls back to engine
    defaults for worlds with no corpus attached. The provider stamps
    data_tier/data_vintage so an estimate can't be mistaken for a measurement.
    """
    provider = getattr(world, "route_data", None)
    if provider is not None:
        spec = provider.route_spec(origin.iata, dest.iata)
        if spec is not None:
            return spec
    from airlinesim.route import haversine, default_segments
    dist = haversine(origin.lat, origin.lon, dest.lat, dest.lon)
    demand = 400.0
    return RouteSpec(
        spec_id=f"{origin.iata}-{dest.iata}",
        display_name=f"{origin.iata}-{dest.iata}",
        origin_iata=origin.iata, dest_iata=dest.iata, distance_km=dist,
        base_demand_per_day=int(demand), segments=default_segments(demand),
        data_tier="synthetic")


def ensure_market(world, route_spec):
    """
    Register the demand pool this route draws from, if it's new. The pool is
    keyed by market_key(), so when routes carry a market_id every airport pair
    in that market competes for the same travelers; otherwise a route is its
    own market, which is the legacy behavior.
    """
    from airlinesim.engine import DemandMarket
    key = market_key(route_spec)
    if key not in world.demand:
        world.demand[key] = DemandMarket(
            route_id=key,
            base_demand_per_day=route_spec.base_demand_per_day,
            seasonality_amplitude=route_spec.seasonality_amplitude,
            segments=route_spec.segments,
            reference_price=getattr(route_spec, "reference_price", 0.0) or 0.0,
            premium_propensity=getattr(route_spec, "premium_propensity", 1.0))
    return world.demand[key]


def retire_tail(player, tail_number: str) -> int:
    """
    Remove an airframe and every route op flying it. Mirrors the teardown
    BankingSubsystem performs when a lease expires. Returns how many route
    ops were closed — the "disabling route(s)" consequence of losing a plane.
    """
    closed = [o for o in player.route_ops if o.plane.tail_number == tail_number]
    player.route_ops = [o for o in player.route_ops if o.plane.tail_number != tail_number]
    player.fleet = [a for a in player.fleet if a.tail_number != tail_number]
    return len(closed)


# ============================================================
# ROUTE ACTIONS
# ============================================================

def set_price(world, player, route_op_id: str, price: float):
    op = find_route_op(player, route_op_id)
    if op is None:
        return False, "route not found"
    if price <= 0:
        return False, "price must be positive"
    op.ticket_price = round(float(price), 2)
    return True, f"price set to ${op.ticket_price:.0f}"


def set_frequency(world, player, route_op_id: str, freq: int):
    op = find_route_op(player, route_op_id)
    if op is None:
        return False, "route not found"
    op.daily_frequency = max(0, int(freq))
    return True, f"frequency set to {op.daily_frequency}/day"


def set_layout(world, player, route_op_id: str, seats: dict):
    """
    Override the cabin FOR THIS ROUTE ONLY, without touching the airframe.
    Kept for scenarios that configure a cabin per operation; the airframe's
    own configuration (see reconfigure_aircraft) is what a player normally
    changes, since a seat installed for one route is installed for all.
    """
    op = find_route_op(player, route_op_id)
    if op is None:
        return False, "route not found"
    layout, notes, err = build_layout(seats, op.plane.spec)
    if err:
        return False, err
    op.layout = layout
    return True, _with_notes("layout updated", notes)


def set_cabin_price(world, player, route_op_id: str, cabin: str, price):
    """
    Price ONE cabin on ONE route. `price` of 0 or None clears the override,
    returning that cabin to the base fare times its class multiplier.

    A cabin the assigned aircraft doesn't have is refused rather than stored:
    a fare on seats that don't exist reads like revenue you're not getting,
    and re-cabining the tail is what makes such a fare meaningful.
    """
    op = find_route_op(player, route_op_id)
    if op is None:
        return False, "route not found"
    try:
        cc = cabin if isinstance(cabin, CabinClass) else CabinClass[str(cabin).strip().upper()]
    except KeyError:
        return False, f"unknown cabin class '{cabin}'"
    # A route op from a save older than per-cabin pricing has no dict at all.
    if not isinstance(getattr(op, "cabin_prices", None), dict):
        op.cabin_prices = {}

    # Blank, zero or negative all mean "stop overriding this cabin" — the one
    # way to say it, whether it arrives as None from a cleared form field or
    # as a typed 0.
    value = None
    if price is not None and str(price).strip() != "":
        try:
            value = float(price)
        except (TypeError, ValueError):
            return False, f"price must be a number, got '{price}'"
    if value is None or value <= 0:
        had = op.cabin_prices.pop(cc, None)
        return True, (f"{cc.name.lower()} fare follows the base fare again "
                      f"(${op.fare_for(cc):,.0f})" if had is not None else
                      f"{cc.name.lower()} was already at the base fare "
                      f"(${op.fare_for(cc):,.0f})")

    seats = op.effective_layout().seats_of(cc)
    if seats <= 0:
        return False, (f"{op.plane.tail_number} has no {cc.name.lower()} seats — "
                       f"recabin the aircraft before pricing that cabin")
    op.cabin_prices[cc] = round(value, 2)
    return True, (f"{cc.name.lower()} fare set to ${value:,.0f} "
                  f"across {seats} seats")


def set_service_tier(world, player, route_op_id: str, tier: int):
    """
    Buy a better (or cheaper) passenger experience at the airports this route
    uses. Higher tiers cost more in gate/amenities/baggage fees and make the
    op more desirable to passengers.
    """
    op = find_route_op(player, route_op_id)
    if op is None:
        return False, "route not found"
    t = int(tier)
    if t < 1 or t > 3:
        return False, "service tier must be 1 (basic), 2 (standard) or 3 (premium)"
    op.service_tier = t
    return True, f"service tier set to {t}"


def open_route(world, player, route_spec_id: str, tail_number: str, price: float,
               freq: int = 1, seats: Optional[dict] = None, service_tier: int = 2):
    """
    Open a route. `route_spec_id` may be a pre-authored route id or an
    "ORG-DST" airport pair — any two airports in the repository can become a
    route. Equipment is validated up front so an aircraft that can't
    physically serve the pair is refused with reasons rather than silently
    grounded every tick.
    """
    plane = find_plane(player, tail_number)
    if plane is None:
        return False, f"no aircraft {tail_number} in fleet"

    route_spec, err = resolve_route(world, route_spec_id)
    if err:
        return False, err
    if any(o.plane.tail_number == tail_number and o.spec.spec_id == route_spec.spec_id
           for o in player.route_ops):
        return False, "already operating this route with that aircraft"

    ok, reasons = validate_equipment(world, route_spec, plane.spec)
    if not ok:
        return False, "; ".join(reasons)

    # An alliance's no-compete agreement is a real restraint with a real cost:
    # refused here with the reason, rather than allowed and left to
    # under-perform, because a self-imposed rule the player can't see is
    # indistinguishable from a bug.
    from airlinesim.alliance import blocks_route
    blocked, why = blocks_route(world, player.player_id,
                                route_spec.origin_iata, route_spec.dest_iata)
    if blocked:
        return False, why

    layout, notes = None, []
    if seats:
        layout, notes, err = build_layout(seats, plane.spec)
        if err:
            return False, err
    player.route_ops.append(RouteOp(
        spec=route_spec, plane=plane, cockpit=None, cabin=None,
        ticket_price=float(price), daily_frequency=max(0, int(freq)),
        owner_id=player.player_id, layout=layout,
        service_tier=max(1, min(3, int(service_tier)))))
    return True, _with_notes(
        f"opened {route_spec.origin_iata}->{route_spec.dest_iata} "
        f"({route_spec.distance_km:.0f}km) with {tail_number}", notes)


def close_route(world, player, route_op_id: str):
    op = find_route_op(player, route_op_id)
    if op is None:
        return False, "route not found"
    player.route_ops.remove(op)
    return True, f"closed {op.spec.origin_iata}->{op.spec.dest_iata}"


# ============================================================
# FLEET ACTIONS
# ============================================================

def acquire_aircraft(world, player, spec_id: str, tail_number: str, method: str,
                     base_iata: Optional[str] = None, seats: Optional[dict] = None,
                     bank: Optional[Bank] = None):
    """
    Acquire an airframe. `seats` sets the cabin CONFIGURATION at acquisition —
    the cheap moment to choose it, since changing it later costs money and
    downtime (see reconfigure_aircraft).

    `seats` may be a per-cabin count ({"BUSINESS": 16}), a preset name
    ("two-class"), or None for all-economy. Counts are fitted to the type's
    cabin geometry: name the premium cabins you want and economy fills what's
    left, with every adjustment reported back in the message.
    """
    if any(a.tail_number == tail_number for a in player.fleet):
        return False, "tail number already in use"
    try:
        spec = world.repo.get(AircraftSpec, spec_id)
    except KeyError:
        return False, f"unknown aircraft spec {spec_id}"
    method_enum = METHOD_BY_NAME.get(method.upper())
    if method_enum is None:
        return False, f"unknown acquisition method {method}"
    terms = TERMS_BY_METHOD[method_enum]

    # The cabin is validated BEFORE any money moves: an unparseable request
    # shouldn't leave the carrier holding a financed airframe it never meant
    # to buy.
    layout, notes = None, []
    if seats:
        layout, notes, err = build_layout(seats, spec)
        if err:
            return False, err

    bank = bank or bank_for(world)
    # try_acquire() is the authoritative answer to "did it fund?" — acquire()
    # returns None both for a denial AND for a successful cash buy, and the
    # call sites that re-derived that distinction got it wrong, attaching
    # aircraft that were never paid for. Never attach the Airplane unless this
    # returns True.
    before = len(player.log)
    if not bank.try_acquire(player, spec, tail_number, method_enum, terms, player.log):
        reason = "; ".join(m.strip() for m in player.log[before:]) or "financing denied"
        return False, reason

    player.fleet.append(Airplane(
        spec=spec, tail_number=tail_number, owner_id=player.player_id,
        owned=(method_enum != AcquisitionMethod.OPERATING_LEASE),
        location_iata=base_iata or next(iter(world.gates)),
        acquired_at=world.sim_time, layout=layout))
    cabin_msg = (f" — {layout.total_seats()} seats: {_cabin_summary(layout)}"
                 if layout else "")
    return True, _with_notes(
        f"acquired {tail_number} ({spec.display_name}) via {method_enum.name}{cabin_msg}",
        notes)


def sell_aircraft(world, player, tail_number: str):
    """
    Sell an owned airframe at market value less a liquidity haircut. Any loan
    secured on the tail must be cleared out of the proceeds — you cannot sell
    collateral and keep the debt.
    """
    plane = find_plane(player, tail_number)
    if plane is None:
        return False, f"no aircraft {tail_number} in fleet"
    if not plane.owned:
        return False, "leased aircraft can't be sold — break the lease instead"
    busy = plane_is_busy(world, plane)
    if busy:
        return False, busy

    proceeds = aircraft_value(plane, world.sim_time) * SALE_HAIRCUT
    loan = next((l for l in player.loans if l.tail_number == tail_number), None)
    payoff = loan.remaining if loan else 0.0
    if payoff > proceeds + player.ledger.cash:
        return False, (f"sale denied: ${payoff:,.0f} loan payoff exceeds "
                       f"${proceeds:,.0f} proceeds plus cash on hand")

    player.ledger.credit(proceeds, f"sold {tail_number} ({plane.spec.display_name})", player.log)
    if loan is not None:
        player.ledger.debit(payoff, f"loan {loan.loan_id} payoff on sale of {tail_number}",
                            player.log)
        player.loans.remove(loan)
    closed = retire_tail(player, tail_number)
    net = proceeds - payoff
    msg = f"sold {tail_number} for ${proceeds:,.0f} (net ${net:,.0f})"
    if closed:
        msg += f"; closed {closed} route(s)"
    return True, msg


def break_lease(world, player, tail_number: str):
    """
    Hand a leased airframe back early. Costs the early-termination penalty
    (capped months of rent) plus the usual return-condition cost.
    """
    plane = find_plane(player, tail_number)
    if plane is None:
        return False, f"no aircraft {tail_number} in fleet"
    lease = next((l for l in player.leases if l.tail_number == tail_number), None)
    if lease is None:
        return False, "no lease on that aircraft — sell it instead"
    busy = plane_is_busy(world, plane)
    if busy:
        return False, busy

    penalty = lease.break_penalty(LEASE_TERMS.lease_break_penalty_months)
    if penalty > player.ledger.cash:
        return False, f"insufficient cash for ${penalty:,.0f} early-termination cost"
    player.ledger.debit(penalty, f"lease {lease.lease_id} early termination ({tail_number})",
                        player.log)
    player.leases.remove(lease)
    closed = retire_tail(player, tail_number)
    msg = f"returned {tail_number} early for ${penalty:,.0f}"
    if closed:
        msg += f"; closed {closed} route(s)"
    return True, msg


def reconfigure_aircraft(world, player, tail_number: str, seats: dict):
    """
    Change an airframe's cabin configuration. Costs per cabin slot and grounds
    the tail for the type's reconfiguration downtime — the reason choosing
    well at acquisition matters.

    Takes the same input acquisition does (per-cabin counts or a preset name)
    and fits it to the same geometry, so "20 business" means the identical
    cabin whether you ask for it on day one or on day four hundred.
    """
    plane = find_plane(player, tail_number)
    if plane is None:
        return False, f"no aircraft {tail_number} in fleet"
    busy = plane_is_busy(world, plane)
    if busy:
        return False, busy
    layout, notes, err = build_layout(seats, plane.spec)
    if err:
        return False, err
    current = plane.effective_layout()
    if layout.seats == current.seats:
        return False, (f"{tail_number} is already configured that way "
                       f"({current.total_seats()} seats)")

    slots = cabin_slots_for(plane.spec.max_seats)
    cost = plane.spec.reconfig_cost_per_slot * slots
    if cost > player.ledger.cash:
        return False, f"insufficient cash for ${cost:,.0f} reconfiguration"
    days = plane.spec.reconfig_days
    if cost > 0 and not player.ledger.debit(cost, f"reconfigure {tail_number}", player.log):
        return False, "reconfiguration payment failed"

    plane.layout = layout
    # Only ground the tail if the type actually carries downtime. Specs
    # authored before reconfig_days existed default to 0, which means an
    # instant swap — grounding on a zero-length window would strand the
    # aircraft, since there'd be no future time to return at.
    if days > 0:
        plane.reconfiguring_until = world.sim_time + days * 24.0
        plane.in_service = False
    dropped = set()
    for op in player.route_ops:
        if op.plane.tail_number != tail_number:
            continue
        # a per-op layout override would mask the new airframe config
        op.layout = None
        # A per-cabin fare on a cabin this tail no longer has would sit in the
        # books priced against seats that don't exist. Drop those, keep the
        # rest: repricing every route because one aircraft changed would throw
        # away decisions the player made deliberately.
        priced = getattr(op, "cabin_prices", None) or {}
        for cc in [c for c in priced if layout.seats_of(c) <= 0]:
            priced.pop(cc, None)
            dropped.add(cc.name.lower())

    msg = (f"reconfiguring {tail_number} to {layout.total_seats()} seats "
           f"({_cabin_summary(layout)}): ${cost:,.0f}, down {days:.0f} days"
           if days > 0 else
           f"reconfigured {tail_number} to {layout.total_seats()} seats "
           f"({_cabin_summary(layout)}): ${cost:,.0f}, no downtime")
    if dropped:
        msg += f"; cleared {', '.join(sorted(dropped))} fares (cabin removed)"
    return True, _with_notes(msg, notes)


# ============================================================
# NETWORK / STAFFING ACTIONS
# ============================================================

def set_hub(world, player, iata: str, enabled: bool = True):
    """
    Declare (or drop) an airport as a maintenance base. Hubs cost their daily
    overhead, and once ANY hub is declared a carrier's aircraft can only get
    checks done at its own hubs.
    """
    code = iata.strip().upper()
    ap = airport(world, code)
    if ap is None:
        return False, f"unknown airport {code}"
    if enabled:
        if code in player.hub_iatas:
            return False, f"{code} is already a hub"
        if not ap.has_maintenance_facility:
            return False, f"{code} has no maintenance facility"
        player.hub_iatas.append(code)
        return True, f"{code} opened as a hub (${ap.hub_fee_per_day:,.0f}/day)"
    if code not in player.hub_iatas:
        return False, f"{code} is not a hub"
    # Closing the last hub would leave the fleet with nowhere to be maintained
    # while still flying, which quietly turns into "flying on risk" every time
    # a check comes due. Refuse it rather than let the carrier discover that
    # weeks later; closing a hub you still fly from is allowed, but you lose
    # its preferential gates, which is the honest trade.
    if len(player.hub_iatas) == 1 and player.fleet:
        return False, (f"{code} is your only hub — with a fleet to maintain, "
                       f"open another before closing this one")
    player.hub_iatas.remove(code)
    based = sum(1 for a in player.fleet if a.location_iata == code)
    msg = f"{code} closed as a hub"
    if based:
        msg += f"; {based} aircraft still there lose preferential gates"
    return True, msg


def hire_crew(world, player, crew_type: str, base_iata: str, headcount: int,
              cost_per_hour: float, certs: tuple = ()):
    try:
        ctype = CrewType[str(crew_type).upper()]
    except KeyError:
        return False, f"unknown crew type {crew_type}"
    if headcount <= 0:
        return False, "headcount must be positive"
    seq = len(player.crews) + len(player.cockpit_pool) + len(player.cabin_pool) + 1
    spec = CrewSpec(spec_id=f"{ctype.name}-{seq}", display_name=f"{ctype.name} crew {seq}",
                    crew_type=ctype, cost_per_member_hour=float(cost_per_hour),
                    certifications=tuple(certs))
    unit = CrewUnit(spec, headcount=int(headcount), owner_id=player.player_id,
                    home_iata=base_iata)
    if ctype == CrewType.COCKPIT:
        player.cockpit_pool.append(unit)
    elif ctype == CrewType.CABIN:
        player.cabin_pool.append(unit)
    else:
        player.crews.append(unit)
    return True, f"hired {headcount}x {ctype.name} at {base_iata}"


# ============================================================
# ALLIANCE ACTIONS
# ============================================================

def form_alliance(world, player, name: str, kind: str = "CODESHARE",
                  partners: Optional[list] = None):
    """
    Found a co-operation agreement. Partners must exist and must not already
    belong to one — a carrier is in at most one alliance, because overlapping
    memberships make feed easy to double-count and the mechanism hard to read.
    """
    from airlinesim.alliance import (Alliance, AllianceKind, alliance_of,
                                     alliances)
    try:
        k = kind if isinstance(kind, AllianceKind) else AllianceKind[str(kind).strip().upper()]
    except KeyError:
        return False, (f"unknown alliance kind '{kind}' — one of "
                       f"{', '.join(a.name for a in AllianceKind)}")
    if alliance_of(world, player.player_id) is not None:
        return False, "already in an alliance — leave it first"

    members = [player.player_id]
    for pid in (partners or []):
        other = _find_player(world, pid)
        if other is None:
            return False, f"no carrier '{pid}'"
        if alliance_of(world, pid) is not None:
            return False, f"{other.name} is already in an alliance"
        members.append(pid)

    reg = alliances(world)
    al = Alliance(alliance_id=f"AL{len(reg) + 1}", name=name.strip() or "Alliance",
                  kind=k, members=members, formed_at=world.sim_time)
    reg.append(al)
    return True, (f"formed {al.name} ({k.name}) with "
                  f"{len(members) - 1} partner(s)")


def join_alliance(world, player, alliance_id: str):
    from airlinesim.alliance import alliance_of, alliances
    if alliance_of(world, player.player_id) is not None:
        return False, "already in an alliance — leave it first"
    al = next((a for a in alliances(world) if a.alliance_id == alliance_id), None)
    if al is None:
        return False, f"no alliance '{alliance_id}'"
    al.members.append(player.player_id)
    return True, f"joined {al.name} ({al.kind.name})"


def leave_alliance(world, player):
    from airlinesim.alliance import alliance_of
    al = alliance_of(world, player.player_id)
    if al is None:
        return False, "not in an alliance"
    al.members.remove(player.player_id)
    return True, (f"left {al.name} — its partners' onward flights no longer "
                  f"feed your routes")


def set_no_compete_hub(world, player, iata: str, enabled: bool = True):
    """
    Agree (or stop agreeing) not to compete with partners at an airport.
    A real restraint with a real cost: it blocks the member from opening a
    route a partner already flies there.
    """
    from airlinesim.alliance import alliance_of
    al = alliance_of(world, player.player_id)
    if al is None:
        return False, "not in an alliance"
    code = iata.strip().upper()
    if airport(world, code) is None:
        return False, f"unknown airport {code}"
    if enabled:
        if code in al.no_compete_hubs:
            return False, f"{code} is already coordinated"
        al.no_compete_hubs.append(code)
        return True, f"{al.name} now coordinates {code} — members won't overlap there"
    if code not in al.no_compete_hubs:
        return False, f"{code} is not coordinated"
    al.no_compete_hubs.remove(code)
    return True, f"{al.name} no longer coordinates {code}"


def _find_player(world, player_id: str):
    from airlinesim.alliance import _players
    return next((p for p in _players(world) if p.player_id == player_id), None)


# ============================================================
# MERGERS AND ACQUISITIONS
# ============================================================

def evaluate_merger(world, player, target_id: str,
                    acquirer_cf: float = 0.0, target_cf: float = 0.0):
    """
    (ok, message) plus the case itself on the message — a costed, reasoned
    answer to "should I buy this carrier?" that commits to nothing.
    """
    from airlinesim.alliance import _players
    from airlinesim.merger import merger_case
    target = _find_player(world, target_id)
    if target is None:
        return False, f"no carrier '{target_id}'"
    if target.player_id == player.player_id:
        return False, "a carrier can't acquire itself"
    case = merger_case(world, _players(world), player, target,
                       acquirer_cf, target_cf)
    return True, case.describe()


def acquire_carrier(world, player, target_id: str,
                    acquirer_cf: float = 0.0, target_cf: float = 0.0,
                    force: bool = False):
    """
    Buy another airline outright. Refused when the case doesn't stand up,
    unless `force` — a human may overrule the valuation, but they do it
    knowingly and the refusal says why.
    """
    from airlinesim.alliance import _players
    from airlinesim.merger import execute_merger, merger_case
    players = _players(world)
    target = _find_player(world, target_id)
    if target is None:
        return False, f"no carrier '{target_id}'"
    if target.player_id == player.player_id:
        return False, "a carrier can't acquire itself"
    if not target.fleet and not target.route_ops:
        return False, f"{target.name} has nothing left to buy"

    case = merger_case(world, players, player, target, acquirer_cf, target_cf)
    if not case.verdict and not force:
        return False, f"acquisition rejected — {case.reason}"
    return execute_merger(world, players, player, target, case)

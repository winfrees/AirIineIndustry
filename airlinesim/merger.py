"""
VALUATION, AND WHY CARRIERS COMBINE.
====================================

Two questions, in order:

  1. **What is an airline worth?**  ``value_carrier()`` — assets, debt, the
     going concern, and the network, each separately so a bid can be argued
     with rather than asserted.
  2. **Is buying this one a good idea?**  ``merger_case()`` — the synergies it
     would create, the integration cost it would incur, the price that follows,
     and a verdict with a stated RATIONALE.

The second is the interesting one, because the answer is usually no. Most
mergers destroy value; the ones that don't are the ones with a specific,
nameable reason. This module makes the AI state that reason before it spends
the money, so a merger it proposes can be read and disagreed with.

THE THREE RATIONALES
--------------------
Modelled after the standard taxonomy of why firms actually combine — the same
one antitrust practice uses — rather than a single "is it cheap?" test:

  HORIZONTAL   Same business, overlapping networks. The gain is scale and the
               removal of a competitor: duplicate routes consolidate, unit
               costs fall, fares firm up. The risk is that you paid for
               revenue you then cannibalise.
  COMPLEMENTARY A horizontal merger's better half: networks that BARELY
               overlap. The gain is connectivity — each carrier's stations
               become the other's feed, and journeys neither could sell alone
               become sellable. This is the airline-specific case, and it is
               why hub complementarity is worth more here than raw size.
  SURVIVAL     Neither carrier can compete alone. Both sub-scale against a
               dominant rival, or bleeding cash with a short runway. The gain
               is existence: the combined carrier reaches minimum efficient
               scale that neither reaches separately.

``SURVIVAL`` is the one the brief specifically asks for, and it is deliberately
the only rationale that can approve a deal with WEAK synergies — because that
is the real-world condition under which such deals happen. It requires an
explicit competitive test (see ``competitive_position``): it is not enough to
be small, you have to be small *against someone specific* and running out of
road. Without that gate, "we're both losing money" would justify every merger
in the game, which is exactly the reasoning that produces bad ones.

HONEST LIMITS
-------------
- Every coefficient here is a game-balance HEURISTIC. No M&A dataset is
  committed to this repository, and none of these multiples is fitted to
  observed transactions. ``docs/consolidation-design.md`` sets out what data
  would ground them and what it would take to get it.
- There is no regulator. Real horizontal mergers between large overlapping
  carriers get blocked or conditioned on divestitures; here they only get
  expensive. An antitrust gate is the most obvious missing piece.
- Valuation has no capital market: there is no share price, no financing
  structure, and an acquirer pays cash it must already hold.
- Reputation is derived from the disruption record, which means a carrier
  with no operating history reads as spotless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from airlinesim.finance_cabin import aircraft_value


class Rationale(Enum):
    HORIZONTAL = auto()
    COMPLEMENTARY = auto()
    SURVIVAL = auto()
    NONE = auto()


# --- valuation coefficients (all HEURISTIC) --------------------------------
# What a going concern is worth as a multiple of annualised operating cash
# flow. Airlines trade at low multiples: the business is cyclical, capital
# intensive and thin-margined.
GOING_CONCERN_MULTIPLE = 4.5
# What a route position is worth beyond the metal flying it — the slot, the
# gate, the market presence. Per daily departure.
ROUTE_POSITION_VALUE = 260_000.0
# A hub is worth more than the sum of its routes: it is where feed happens.
HUB_POSITION_VALUE = 3_500_000.0
# A lease is an obligation, not an asset. Valued as the present value of the
# rent still owed, undiscounted (the model has no rate curve).
LEASE_OBLIGATION_WEIGHT = 0.55
# Control premium: what a buyer pays above standalone value to actually get
# the thing. Real takeovers clear at 20-40% over the undisturbed price.
CONTROL_PREMIUM = 0.28
# Integrating two airlines is famously expensive — fleet harmonisation, labour
# integration, systems, and a period of degraded operation.
INTEGRATION_COST_PER_AIRCRAFT = 900_000.0
INTEGRATION_COST_PER_ROUTE = 120_000.0


@dataclass
class Valuation:
    """What a carrier is worth, itemised so a bid can be argued with."""
    player_id: str
    name: str
    cash: float = 0.0
    fleet_value: float = 0.0
    debt: float = 0.0
    lease_obligations: float = 0.0
    going_concern: float = 0.0
    network_value: float = 0.0
    reputation: float = 1.0            # 1.0 neutral; scales the going concern

    def enterprise_value(self) -> float:
        """
        What the whole carrier is worth. Floored at the liquidation value of
        its assets: a business that loses money is worth its metal, not less —
        a buyer would break it up rather than pay a negative price.
        """
        raw = (self.cash + self.fleet_value + self.going_concern
               + self.network_value - self.debt - self.lease_obligations)
        return max(self.liquidation_value(), raw)

    def liquidation_value(self) -> float:
        return max(0.0, self.cash + self.fleet_value * 0.8
                   - self.debt - self.lease_obligations)

    def describe(self) -> str:
        return (f"{self.name}: EV ${self.enterprise_value():,.0f} "
                f"(cash ${self.cash:,.0f} + fleet ${self.fleet_value:,.0f} "
                f"+ concern ${self.going_concern:,.0f} "
                f"+ network ${self.network_value:,.0f} "
                f"− debt ${self.debt:,.0f} "
                f"− leases ${self.lease_obligations:,.0f})")


def reputation_of(world, player) -> float:
    """
    How reliable this carrier looks to a passenger, from the operating record
    it has actually accumulated: cancellations and disruption cost against the
    flying it did. 1.0 is neutral.

    Derived rather than stored, so it cannot be gamed by anything except
    running a better airline. A carrier with no history reads as neutral,
    which is a real limitation — a startup is not proven, it is unmeasured.
    """
    from airlinesim.disruption import tally_for
    t = tally_for(player)
    flights = max(1.0, t.cancelled_flights + t.delayed_flights)
    cancel_rate = t.cancelled_flights / flights
    # A 10% cancellation rate costs about a fifth of the reputation multiple.
    rep = 1.0 - 2.0 * min(0.35, cancel_rate)
    # Service tier is the other half of what a passenger perceives.
    ops = list(player.route_ops)
    if ops:
        avg_tier = sum(getattr(o, "service_tier", 2) for o in ops) / len(ops)
        rep *= 0.9 + 0.1 * avg_tier
    return max(0.45, min(1.4, rep))


def value_carrier(world, player, cash_flow_per_day: float = 0.0) -> Valuation:
    """
    Value a carrier. `cash_flow_per_day` is the AI's own smoothed operating
    cash flow where it has one — the number that already includes every cost
    the engine charges, rather than the contribution margin that flatters it.
    """
    v = Valuation(player_id=player.player_id, name=player.name)
    v.cash = player.ledger.cash
    v.fleet_value = sum(aircraft_value(a, world.sim_time)
                        for a in player.fleet if a.owned and not a.retired)
    v.debt = sum(l.remaining for l in player.loans)
    v.lease_obligations = sum(
        l.monthly_rent() * l.months_remaining() * LEASE_OBLIGATION_WEIGHT
        for l in player.leases)
    v.reputation = reputation_of(world, player)

    # Going concern: only a profitable operation is worth more than its parts.
    annual_cf = cash_flow_per_day * 365.0
    v.going_concern = max(0.0, annual_cf * GOING_CONCERN_MULTIPLE * v.reputation)

    daily_departures = sum(max(0, o.daily_frequency) for o in player.route_ops)
    v.network_value = (daily_departures * ROUTE_POSITION_VALUE
                       + len(getattr(player, "hub_iatas", [])) * HUB_POSITION_VALUE)
    return v


# ============================================================
# COMPETITIVE POSITION — the "can't compete alone" test
# ============================================================

@dataclass
class Position:
    """Where a carrier stands against the field."""
    player_id: str
    share: float = 0.0                 # of total daily departures in the world
    leader_share: float = 0.0
    is_leader: bool = False
    cash_runway_days: float = 999.0
    sub_scale: bool = False            # below minimum efficient scale
    losing: bool = False

    def cannot_compete_alone(self) -> bool:
        """
        The specific condition a survival merger answers: outmatched by a
        specific rival AND unable to fix it from here. Being small is not
        enough — a healthy niche carrier is small on purpose.
        """
        outmatched = (not self.is_leader) and self.leader_share >= 2.0 * max(self.share, 1e-6)
        return outmatched and (self.sub_scale or self.losing)


# Below this share of the world's departures a carrier lacks the scale to
# spread its fixed costs — hubs, crew pools, maintenance base.
MIN_EFFICIENT_SHARE = 0.12
# A runway shorter than this means the problem is not fixable organically.
SHORT_RUNWAY_DAYS = 120.0


def competitive_position(world, players, player, cash_flow_per_day: float = 0.0) -> Position:
    totals = {}
    for p in players:
        totals[p.player_id] = sum(max(0, o.daily_frequency) for o in p.route_ops)
    world_total = max(1.0, sum(totals.values()))
    mine = totals.get(player.player_id, 0)
    leader_id = max(totals, key=lambda k: totals[k]) if totals else player.player_id

    pos = Position(player_id=player.player_id)
    pos.share = mine / world_total
    pos.leader_share = totals.get(leader_id, 0) / world_total
    pos.is_leader = leader_id == player.player_id
    pos.sub_scale = pos.share < MIN_EFFICIENT_SHARE
    burn = -cash_flow_per_day
    pos.losing = burn > 0
    pos.cash_runway_days = (player.ledger.cash / burn) if burn > 0 else 999.0
    if pos.losing and pos.cash_runway_days > SHORT_RUNWAY_DAYS:
        # Losing money with years of cash is a problem, not an emergency.
        pos.losing = False
    return pos


# ============================================================
# SYNERGY AND THE CASE
# ============================================================

@dataclass
class MergerCase:
    """A costed, reasoned answer to 'should I buy this carrier?'."""
    acquirer_id: str
    target_id: str
    target_name: str = ""
    rationale: Rationale = Rationale.NONE
    standalone_value: float = 0.0
    price: float = 0.0
    integration_cost: float = 0.0
    # synergies, per year
    cost_synergy: float = 0.0          # overlapping routes, shared fleet type
    revenue_synergy: float = 0.0       # new connections the combination creates
    overlap_routes: int = 0
    complementary_stations: int = 0
    fleet_commonality: float = 0.0
    verdict: bool = False
    reason: str = ""

    def annual_synergy(self) -> float:
        return self.cost_synergy + self.revenue_synergy

    def payback_years(self) -> float:
        gain = self.annual_synergy()
        if gain <= 0:
            return float("inf")
        return (self.price + self.integration_cost) / gain

    def total_outlay(self) -> float:
        return self.price + self.integration_cost

    def describe(self) -> str:
        return (f"{self.rationale.name} bid for {self.target_name}: "
                f"${self.price:,.0f} + ${self.integration_cost:,.0f} integration, "
                f"synergy ${self.annual_synergy():,.0f}/yr, "
                f"payback {self.payback_years():.1f}y — {self.reason}")


# Annual saving from consolidating one duplicated route (one of the two ops
# stops flying, its costs go away, its traffic mostly transfers).
OVERLAP_SAVING_PER_ROUTE = 1_450_000.0
# Annual value of one new station the combined network reaches, through the
# feed it creates on both sides.
COMPLEMENTARY_STATION_VALUE = 620_000.0
# A shared type rating means one training pipeline, one spares pool.
FLEET_COMMONALITY_SAVING = 2_100_000.0
# Payback horizon an acquirer will accept.
MAX_PAYBACK_YEARS = 7.0
# A survival merger accepts a worse payback: the alternative is not "keep the
# cash", it is "lose slowly".
SURVIVAL_MAX_PAYBACK_YEARS = 14.0


def _stations(player) -> set:
    out = set()
    for op in player.route_ops:
        out.add(op.spec.origin_iata)
        out.add(op.spec.dest_iata)
    return out


def _route_pairs(player) -> set:
    """
    The DIRECTIONAL legs a carrier flies. Directional on purpose: ORD->LGA and
    LGA->ORD are two operations with two aircraft and two crews, and treating
    them as one route made a merger consolidate the return leg of every market
    where only the outbound overlapped — shutting twelve of the target's legs
    where six markets were duplicated.
    """
    return {(o.spec.origin_iata, o.spec.dest_iata) for o in player.route_ops}


def _type_ratings(player) -> set:
    return {a.spec.type_rating or a.spec.spec_id for a in player.fleet}


def merger_case(world, players, acquirer, target,
                acquirer_cf: float = 0.0, target_cf: float = 0.0) -> MergerCase:
    """
    Build the full case for acquiring `target`. Always returns a case — a
    refusal with a reason is more useful than None, both for the AI's log and
    for anyone reading why a sensible-looking deal didn't happen.
    """
    case = MergerCase(acquirer_id=acquirer.player_id, target_id=target.player_id,
                      target_name=target.name)
    val = value_carrier(world, target, target_cf)
    case.standalone_value = val.enterprise_value()

    a_routes, t_routes = _route_pairs(acquirer), _route_pairs(target)
    a_stations, t_stations = _stations(acquirer), _stations(target)
    overlap = a_routes & t_routes
    case.overlap_routes = len(overlap)
    case.complementary_stations = len(t_stations - a_stations)

    common = _type_ratings(acquirer) & _type_ratings(target)
    total_types = _type_ratings(acquirer) | _type_ratings(target)
    case.fleet_commonality = len(common) / max(1, len(total_types))

    case.cost_synergy = (case.overlap_routes * OVERLAP_SAVING_PER_ROUTE
                         + case.fleet_commonality * FLEET_COMMONALITY_SAVING)
    case.revenue_synergy = case.complementary_stations * COMPLEMENTARY_STATION_VALUE

    case.integration_cost = (
        len([a for a in target.fleet if not a.retired]) * INTEGRATION_COST_PER_AIRCRAFT
        + len(target.route_ops) * INTEGRATION_COST_PER_ROUTE)

    a_pos = competitive_position(world, players, acquirer, acquirer_cf)
    t_pos = competitive_position(world, players, target, target_cf)

    # Rationale. Complementary beats horizontal where both apply, because the
    # connectivity gain is the airline-specific one and does not depend on
    # taking capacity out of the market.
    if a_pos.cannot_compete_alone() and t_pos.cannot_compete_alone():
        case.rationale = Rationale.SURVIVAL
    elif case.complementary_stations >= 2 and case.overlap_routes <= case.complementary_stations:
        case.rationale = Rationale.COMPLEMENTARY
    elif case.overlap_routes > 0:
        case.rationale = Rationale.HORIZONTAL
    else:
        case.rationale = Rationale.COMPLEMENTARY if case.complementary_stations else Rationale.NONE

    # Price. A distressed target sells for less; a healthy one costs the
    # control premium on top of its standalone value.
    premium = CONTROL_PREMIUM
    if case.rationale is Rationale.SURVIVAL:
        # Neither party has an alternative, so the premium collapses toward a
        # merger of equals rather than a takeover.
        premium = 0.05
    elif t_pos.losing and t_pos.cash_runway_days < SHORT_RUNWAY_DAYS:
        premium = 0.10
    case.price = max(val.liquidation_value(), case.standalone_value * (1.0 + premium))

    # Verdict.
    horizon = (SURVIVAL_MAX_PAYBACK_YEARS if case.rationale is Rationale.SURVIVAL
               else MAX_PAYBACK_YEARS)
    payback = case.payback_years()
    affordable = acquirer.ledger.cash >= case.total_outlay()

    if case.rationale is Rationale.NONE:
        case.reason = "no overlap and no new stations — nothing to gain"
    elif not affordable:
        case.reason = (f"can't fund it: ${case.total_outlay():,.0f} needed, "
                       f"${acquirer.ledger.cash:,.0f} on hand")
    elif payback > horizon:
        case.reason = (f"payback {payback:.1f}y exceeds the {horizon:.0f}y "
                       f"horizon for a {case.rationale.name.lower()} deal")
    else:
        case.verdict = True
        if case.rationale is Rationale.SURVIVAL:
            case.reason = (f"neither carrier can compete alone (share "
                           f"{a_pos.share:.0%}/{t_pos.share:.0%} against a leader "
                           f"at {a_pos.leader_share:.0%}); combined they reach scale")
        elif case.rationale is Rationale.COMPLEMENTARY:
            case.reason = (f"{case.complementary_stations} new stations feed the "
                           f"existing network; payback {payback:.1f}y")
        else:
            case.reason = (f"{case.overlap_routes} overlapping route(s) consolidate; "
                           f"payback {payback:.1f}y")
    return case


# ============================================================
# EXECUTION
# ============================================================

def execute_merger(world, players, acquirer, target, case: MergerCase):
    """
    Combine two carriers. The target's assets, obligations and people move to
    the acquirer; the target is left an empty shell and removed from play by
    the caller.

    Everything transfers, including the DEBT — buying an airline means buying
    what it owes, and a valuation that ignored that would systematically
    overpay. Overlapping routes are closed rather than flown twice.
    """
    price = case.price + case.integration_cost
    if acquirer.ledger.cash < price:
        return False, (f"insufficient cash: ${price:,.0f} needed, "
                       f"${acquirer.ledger.cash:,.0f} on hand")

    acquirer.ledger.debit(price, f"acquisition of {target.name}", acquirer.log)
    # The target's cash comes across with the company.
    if target.ledger.cash:
        acquirer.ledger.credit(target.ledger.cash,
                               f"{target.name} cash balance", acquirer.log)
        target.ledger.cash = 0.0

    existing = _route_pairs(acquirer)
    closed = 0
    for a in list(target.fleet):
        a.owner_id = acquirer.player_id
        acquirer.fleet.append(a)
    for op in list(target.route_ops):
        pair = (op.spec.origin_iata, op.spec.dest_iata)
        if pair in existing:
            closed += 1          # consolidated: this is the cost synergy
            continue
        op.owner_id = acquirer.player_id
        acquirer.route_ops.append(op)
        existing.add(pair)
    for crew in list(target.cockpit_pool):
        crew.owner_id = acquirer.player_id
        acquirer.cockpit_pool.append(crew)
    for crew in list(target.cabin_pool):
        crew.owner_id = acquirer.player_id
        acquirer.cabin_pool.append(crew)
    for crew in list(target.crews):
        crew.owner_id = acquirer.player_id
        acquirer.crews.append(crew)
    for loan in list(target.loans):
        loan.owner_id = acquirer.player_id
        acquirer.loans.append(loan)
    for lease in list(target.leases):
        lease.owner_id = acquirer.player_id
        acquirer.leases.append(lease)
    for hub in list(getattr(target, "hub_iatas", [])):
        if hub not in acquirer.hub_iatas:
            acquirer.hub_iatas.append(hub)

    target.fleet, target.route_ops = [], []
    target.cockpit_pool, target.cabin_pool, target.crews = [], [], []
    target.loans, target.leases, target.hub_iatas = [], [], []

    # An alliance seat the target held is now redundant.
    from airlinesim.alliance import alliances
    for al in alliances(world):
        if target.player_id in al.members:
            al.members.remove(target.player_id)

    msg = (f"acquired {target.name} for ${case.price:,.0f} "
           f"(+${case.integration_cost:,.0f} integration) — {case.rationale.name}")
    if closed:
        msg += f"; consolidated {closed} overlapping route(s)"
    return True, msg

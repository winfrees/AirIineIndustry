"""
ALLIANCES — co-ops, unions, and the feed traffic they exist for.
================================================================

Why carriers ally, in one sentence: a passenger going A -> C does not care
whose metal flies each half, and a carrier that can offer the whole journey
sells a seat that a carrier offering only half of it cannot.

WHAT WAS MISSING
----------------
``route.TravelerSegment.CONNECTING`` already existed — every market carries a
connecting share — but it was carried as if it were LOCAL traffic. A leg
ORD->LAX sold its connecting seats whether or not anything actually connected
to them. Nothing in the model asked "connecting to WHERE, on WHOSE aircraft?",
so a hub was worth no more than a point-to-point station and an alliance was
worth nothing at all.

This module supplies the missing half: **connecting demand has to be fed.**

THE MECHANISM
-------------
For each route op, look at what departs its DESTINATION:

    feed(op) = Σ over onward departures from op.dest
                 (seats · frequency · quality of the connection)

- Your own onward flights count at full weight. An ONLINE connection — one
  carrier, one ticket, bags checked through — is the strongest product.
- A partner's onward flights count at the alliance's ``feed_efficiency``:
  an interline agreement is weak, a codeshare stronger, a joint venture
  nearly as good as flying it yourself.
- A non-partner's onward flights count for NOTHING. That is precisely what
  makes an alliance worth joining.

That feed factor scales how attractive the op is to connecting passengers,
through the same ``desirability`` seam the arbiter already uses for airport
access and service tier. So a well-connected hub carrier wins the
connecting-heavy pools, and a carrier that allies into a partner's hub gains
traffic it could not otherwise reach — without any new allocation logic.

DIRECT VS CONNECTING, AND WHY IT IS A PENALTY
---------------------------------------------
A one-stop itinerary is a worse product than a nonstop: it takes longer, adds
a missed-connection risk, and passengers price that in. ``CONNECT_PENALTY``
applies that discount, and ``AllianceKind`` scales it — an interline
connection between two carriers with no through-checking is worse than an
online one. This is what stops "ally with everybody" from being free: feed is
worth having, but a connecting passenger is worth less than a local one, so a
carrier that fills its aircraft with feed has a lower yield than one that
fills them locally.

NO-COMPETE, THE HONEST WAY
--------------------------
Real alliances coordinate rather than compete on overlapping hubs, and real
regulators care about that. ``Alliance.no_compete_hubs`` records where members
have agreed not to go head to head; ``blocks_route()`` enforces it at the
action layer, so it is refused up front with a reason rather than silently
under-performing. It is a self-imposed restraint, and it costs the member the
routes it gives up — which is what makes joining a decision rather than a
free win.

HONEST LIMITS
-------------
- Connections are ONE STOP. Nothing models A->B->C->D.
- Feed is measured at the leg's destination only; it does not trace an actual
  passenger through to a final destination, so this is a *connectivity
  index*, not an itinerary ledger. A true origin-and-destination model needs
  a passenger-itinerary object the engine does not have — see
  ``docs/consolidation-design.md``.
- Revenue sharing is modelled as a share of the connecting uplift, not as a
  settled interline invoice between carriers.
- Slot/gate coordination and antitrust immunity are not modelled at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from airlinesim.engine import Subsystem


class AllianceKind(Enum):
    """
    How deep the cooperation runs. Deeper means better feed and a better
    passenger product, and (see ``dues_per_day``) costs more to maintain.
    """
    INTERLINE = auto()        # bags checked through; nothing else
    CODESHARE = auto()        # sell each other's seats under your own code
    JOINT_VENTURE = auto()    # coordinated schedules and pooled revenue


@dataclass(frozen=True)
class AllianceTerms:
    """
    Reference data for one depth of cooperation. Game-balance HEURISTICS,
    shaped by how the real products differ rather than fitted to anything.

      feed_efficiency ..... how much of a partner's onward capacity counts as
                            usable feed, against 1.0 for your own metal
      connect_quality ..... how good the through-journey feels to a passenger,
                            against 1.0 for an online connection
      revenue_share ....... the operating carrier's share of a connecting fare
                            it carries on a partner's behalf
      dues_per_day ........ the cost of maintaining the relationship
    """
    feed_efficiency: float
    connect_quality: float
    revenue_share: float
    dues_per_day: float


ALLIANCE_TERMS = {
    AllianceKind.INTERLINE: AllianceTerms(0.35, 0.72, 0.90, 1_200.0),
    AllianceKind.CODESHARE: AllianceTerms(0.65, 0.86, 0.75, 4_500.0),
    AllianceKind.JOINT_VENTURE: AllianceTerms(0.88, 0.95, 0.55, 11_000.0),
}

# A connecting passenger is worth less than a local one even on a perfect
# online connection: the journey is longer and carries a misconnect risk.
# This is the discount at connect_quality = 1.0.
CONNECT_PENALTY = 0.88

# Feed saturates. The tenth onward departure from a hub is worth much less
# than the first — after a point a connecting passenger already has a
# reasonable onward option and more frequency adds little. Feed is scaled as
# feed / (feed + FEED_HALF), so FEED_HALF is the daily onward seat count at
# which the connectivity bonus reaches half its maximum.
FEED_HALF = 900.0
# The most a perfectly-fed hub can lift an op's attractiveness to connecting
# traffic. Game balance: high enough that a hub beats a point-to-point
# station, low enough that feed alone cannot carry an uncompetitive fare.
MAX_FEED_BONUS = 0.55


@dataclass
class Alliance:
    """
    A live co-operation agreement between carriers. Lives on the World —
    it is a relationship between players, not a possession of one.
    """
    alliance_id: str
    name: str
    kind: AllianceKind
    members: list = field(default_factory=list)          # player_id
    # Airports where members have agreed not to compete head to head. Enforced
    # by blocks_route() at the action layer.
    no_compete_hubs: list = field(default_factory=list)
    formed_at: float = 0.0

    def terms(self) -> AllianceTerms:
        return ALLIANCE_TERMS[self.kind]

    def has(self, player_id: str) -> bool:
        return player_id in self.members

    def partners_of(self, player_id: str) -> list:
        return [m for m in self.members if m != player_id]


# ============================================================
# WORLD ACCESS
# ============================================================

def alliances(world) -> list:
    """The world's alliance register, created on first use so a world pickled
    before alliances existed still loads."""
    reg = getattr(world, "alliances", None)
    if reg is None:
        reg = []
        world.alliances = reg
    return reg


def alliance_of(world, player_id: str) -> Optional[Alliance]:
    """The alliance a carrier belongs to, or None. A carrier is in at most
    one — overlapping memberships are a real thing but they make feed
    double-counting easy and the mechanism hard to read."""
    for a in alliances(world):
        if a.has(player_id):
            return a
    return None


def partner_ids(world, player_id: str) -> set:
    a = alliance_of(world, player_id)
    return set(a.partners_of(player_id)) if a else set()


def relationship(world, a_id: str, b_id: str):
    """
    (feed_efficiency, connect_quality) between two carriers.

    Same carrier -> a perfect online connection. Partners -> their alliance's
    terms. Strangers -> nothing: their flights do not combine into a journey
    anyone can buy, which is the whole reason to ally.
    """
    if a_id == b_id:
        return 1.0, 1.0
    al = alliance_of(world, a_id)
    if al is not None and al.has(b_id):
        t = al.terms()
        return t.feed_efficiency, t.connect_quality
    return 0.0, 0.0


def blocks_route(world, player_id: str, origin: str, dest: str):
    """
    (blocked, reason). A member may not open a route that competes with a
    partner's existing one at a hub the alliance has agreed to coordinate.

    Refused at the action layer with a reason, rather than allowed and left
    to under-perform: a self-imposed restraint the player cannot see is
    indistinguishable from a bug.
    """
    al = alliance_of(world, player_id)
    if al is None or not al.no_compete_hubs:
        return False, ""
    touched = {origin, dest} & set(al.no_compete_hubs)
    if not touched:
        return False, ""
    for p in _players(world):
        if p.player_id == player_id or not al.has(p.player_id):
            continue
        for op in p.route_ops:
            if {op.spec.origin_iata, op.spec.dest_iata} == {origin, dest}:
                hub = sorted(touched)[0]
                return True, (f"{al.name} coordinates {hub}: {p.name} already "
                              f"flies {origin}-{dest}, and members don't compete "
                              f"head to head there")
    return False, ""


def register_players(world, players) -> None:
    """
    Make the engine's player list reachable from the world.

    World deliberately holds no player-owned state, but alliance rules are
    relationships BETWEEN players, so both the subsystem and the action layer
    need the roster. Set at attach time as well as every tick: an earlier
    version only set it during a tick, so `form_alliance` called before the
    first tick could not find its own partners and silently refused, and
    `blocks_route` had nobody to conflict with and silently allowed.
    """
    world._alliance_players = list(players)


def _players(world) -> list:
    return list(getattr(world, "_alliance_players", ()))


# ============================================================
# FEED — what makes a hub, and an alliance, worth anything
# ============================================================

def onward_capacity(world, players, iata: str, for_player_id: str,
                    exclude_dest: str = "") -> float:
    """
    Daily seats departing `iata` that a passenger arriving there could
    actually continue on, weighted by whether they could buy the journey:
    own metal at full weight, a partner's at the alliance's efficiency, a
    stranger's at nothing.

    `exclude_dest` drops flights heading back where the passenger came from.
    Nobody connects onto the return leg of the flight they just got off, and
    counting it made every out-and-back pair look like a hub — including a
    PARTNER's return leg, which is how forming an alliance appeared to create
    feed on a network where nothing actually connected.
    """
    total = 0.0
    for p in players:
        eff, _quality = relationship(world, for_player_id, p.player_id)
        if eff <= 0.0:
            continue
        for op in p.route_ops:
            if op.spec.origin_iata != iata:
                continue
            if exclude_dest and op.spec.dest_iata == exclude_dest:
                continue
            if not getattr(op, "suitable", True) or op.plane.retired:
                continue
            seats = op.effective_layout().total_seats()
            total += seats * max(0, op.daily_frequency) * eff
    return total


def feed_factor(world, players, op, for_player_id: str) -> float:
    """
    How much better this op looks to a CONNECTING passenger because of what
    it connects to. 1.0 = no onward connectivity at all (a dead-end station);
    up to 1 + MAX_FEED_BONUS at a richly-served hub.

    Saturating on purpose: the first onward departure transforms a station,
    the tenth barely moves it.
    """
    onward = onward_capacity(world, players, op.spec.dest_iata, for_player_id,
                             exclude_dest=op.spec.origin_iata)
    if onward <= 0.0:
        return 1.0
    return 1.0 + MAX_FEED_BONUS * (onward / (onward + FEED_HALF))


def connect_discount(world, players, op, for_player_id: str) -> float:
    """
    What a connecting seat on this op is worth relative to a local one, given
    the best connection available beyond it. A nonstop passenger pays full
    fare; a connecting one is buying a longer, riskier journey.
    """
    best_quality = 0.0
    for p in players:
        _eff, quality = relationship(world, for_player_id, p.player_id)
        if quality <= best_quality:
            continue
        if any(o.spec.origin_iata == op.spec.dest_iata for o in p.route_ops):
            best_quality = quality
    if best_quality <= 0.0:
        return 1.0            # nothing connects; this op sells local traffic
    return CONNECT_PENALTY * best_quality


# ============================================================
# SUBSYSTEM
# ============================================================

class AllianceSubsystem(Subsystem):
    """
    Runs BEFORE Operations. Computes each op's feed factor and connecting
    discount and writes them onto the op, where the demand claim reads them.
    Decides nothing itself — Operations remains the single authority on how
    much flying happens and who gets which passenger.

    Also bills alliance dues and stashes the player list on the world so the
    action layer can evaluate no-compete rules without an engine reference.
    """

    def tick(self, world, players, dt: float, ctx: dict):
        register_players(world, players)
        day_frac = dt / 24.0

        for p in players:
            for op in p.route_ops:
                op.feed_factor = feed_factor(world, players, op, p.player_id)
                op.connect_discount = connect_discount(world, players, op,
                                                       p.player_id)

        # Dues, split across the membership. A relationship costs something to
        # maintain, which is what makes the deepest tier a real choice rather
        # than a strictly better one.
        for al in alliances(world):
            if not al.members:
                continue
            share = al.terms().dues_per_day * day_frac / len(al.members)
            for p in players:
                if al.has(p.player_id) and share > 0:
                    p.ledger.debit(share, f"{al.name} alliance dues", p.log)


# ============================================================
# PROJECTION
# ============================================================

def alliance_snapshot(world, player_id: str) -> Optional[dict]:
    al = alliance_of(world, player_id)
    if al is None:
        return None
    t = al.terms()
    return {
        "alliance_id": al.alliance_id, "name": al.name, "kind": al.kind.name,
        "members": list(al.members), "partners": al.partners_of(player_id),
        "no_compete_hubs": list(al.no_compete_hubs),
        "feed_efficiency": t.feed_efficiency,
        "connect_quality": t.connect_quality,
        "dues_per_day": t.dues_per_day,
    }


def attach_alliances(world, engine):
    """
    Wire alliances into a world: insert AllianceSubsystem immediately BEFORE
    Operations (feed has to be known before demand is claimed) and register
    the player roster now, so alliance actions work before the first tick.
    """
    from airlinesim.engine import OperationsSubsystem
    register_players(world, engine.players)
    if any(isinstance(s, AllianceSubsystem) for s in engine.subsystems):
        return
    idx = next((i for i, s in enumerate(engine.subsystems)
                if isinstance(s, OperationsSubsystem)), len(engine.subsystems))
    engine.subsystems.insert(idx, AllianceSubsystem())

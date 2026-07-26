"""
Seat-Class Structure + Leasing/Banking — brought to the Aircraft standard.
=========================================================================

This module adds two fully-developed entity systems and the behavior that ties
them into revenue and finance. It is written to drop into engine.py (imports the
existing spec/instance vocabulary). Kept separate here for reviewability; the
integration notes at the bottom show exactly where each piece hooks in.

ENTITY 1 — SEAT CLASS STRUCTURE
  CabinClass ......... enum of travel classes
  SeatClassSpec ...... per-class reference data: footprint, price multiplier,
                       demand share, elasticity (data → real-world importable)
  SeatLayout ......... a player-chosen configuration of an aircraft's cabin:
                       how many seats of each class. Validated against the
                       airframe's usable cabin "slots".
  The revenue mechanic: each class fills from its OWN demand pool at its OWN
  price and elasticity, so the layout decision trades total seats against
  revenue-per-seat — the core of cabin strategy.

ENTITY 2 — LEASING / BANKING
  AcquisitionMethod .. BUY_CASH | FINANCE | OPERATING_LEASE
  FinancingTerms ..... reference data for a loan or lease product
  Loan ............... live amortizing debt instance (principal, rate, schedule)
  Lease .............. live operating-lease instance (periodic cost, term)
  Bank ............... issues loans/leases subject to creditworthiness;
                       accrues interest and bills lease payments each tick.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ============================================================
# ENTITY 1: SEAT-CLASS STRUCTURE
# ============================================================

class CabinClass(Enum):
    ECONOMY = auto()
    PREMIUM = auto()
    BUSINESS = auto()
    FIRST = auto()


@dataclass(frozen=True)
class SeatClassSpec:
    """
    Reference data for one travel class. Importable / authorable.
      footprint .......... how many 'cabin slots' one seat consumes
                           (economy = 1.0 baseline; business ~2.5; first ~4.0)
      price_multiplier ... fare relative to the route base fare
      demand_share ....... fraction of the route's total demand that seeks
                           this class (economy large, first tiny)
      elasticity ......... price sensitivity for THIS class (premium cabins are
                           less elastic: business travelers pay up)
    """
    cabin_class: CabinClass
    footprint: float
    price_multiplier: float
    demand_share: float
    elasticity: float


# Sensible defaults grounded in typical industry ratios. Tunable / importable.
DEFAULT_SEAT_CLASSES = {
    CabinClass.ECONOMY:  SeatClassSpec(CabinClass.ECONOMY,  1.0, 1.0,  0.78, -1.5),
    CabinClass.PREMIUM:  SeatClassSpec(CabinClass.PREMIUM,  1.5, 1.8,  0.14, -1.1),
    CabinClass.BUSINESS: SeatClassSpec(CabinClass.BUSINESS, 2.5, 4.0,  0.07, -0.7),
    CabinClass.FIRST:    SeatClassSpec(CabinClass.FIRST,    4.0, 8.0,  0.01, -0.4),
}


@dataclass
class SeatLayout:
    """
    A player-chosen cabin configuration for a specific aircraft.
    seats: how many seats of each class. Validated so total footprint fits the
    airframe's usable cabin slots (derived from max_seats at all-economy).
    """
    seats: dict = field(default_factory=dict)   # CabinClass -> int

    def footprint_used(self, classes: dict) -> float:
        return sum(n * classes[c].footprint for c, n in self.seats.items())

    def total_seats(self) -> int:
        return sum(self.seats.values())

    def seats_of(self, c: CabinClass) -> int:
        return self.seats.get(c, 0)

    def is_valid(self, cabin_slots: float, classes: dict) -> bool:
        return self.footprint_used(classes) <= cabin_slots + 1e-6

    @staticmethod
    def all_economy(max_seats: int) -> "SeatLayout":
        return SeatLayout({CabinClass.ECONOMY: max_seats})


def cabin_slots_for(max_seats: int) -> float:
    """An airframe's usable cabin capacity, measured in economy-seat footprints."""
    return float(max_seats)   # max_seats is defined as the all-economy count


# ============================================================
# AIRCRAFT VALUATION / DEPRECIATION
# Market value declines from list price with BOTH age and airframe hours,
# fast early and tapering toward a residual (scrap/parts) floor. Feeds the
# balance sheet and the retire-vs-overhaul decision.
# ============================================================

@dataclass(frozen=True)
class DepreciationModel:
    """
    Declining-balance depreciation with a residual floor.
      annual_decline ........ fraction of remaining value lost per year (age)
      hours_decline_per_1k .. extra fraction lost per 1000 airframe hours (usage)
      residual_frac ......... value never falls below this fraction of list price
    Declining-balance gives the realistic 'steep early, flatten later' curve.
    """
    annual_decline: float = 0.06          # ~6%/yr of remaining value (age)
    hours_decline_per_1k: float = 0.012   # ~1.2% per 1000 hrs (usage wear)
    residual_frac: float = 0.15           # floor at 15% of list (scrap/parts)

    def value(self, list_price: float, age_years: float, airframe_hours: float) -> float:
        age_factor = (1.0 - self.annual_decline) ** age_years
        hours_factor = (1.0 - self.hours_decline_per_1k) ** (airframe_hours / 1000.0)
        raw = list_price * age_factor * hours_factor
        floor = list_price * self.residual_frac
        return max(floor, raw)


DEFAULT_DEPRECIATION = DepreciationModel()


def aircraft_value(plane, sim_time_hours: float,
                   model: "DepreciationModel" = DEFAULT_DEPRECIATION) -> float:
    """
    Current market value of an aircraft instance. Leased planes are not an asset
    of the operator, so callers should exclude them from net worth.
    """
    acquired = getattr(plane, "acquired_at", 0.0)
    age_years = max(0.0, (sim_time_hours - acquired) / (24.0 * 365.0))
    return model.value(plane.spec.list_price, age_years, plane.airframe_hours)


# ============================================================
# ENTITY 2: LEASING / BANKING
# ============================================================

class AcquisitionMethod(Enum):
    BUY_CASH = auto()          # pay list price now, own outright
    FINANCE = auto()           # down payment + amortizing loan, own with lien
    OPERATING_LEASE = auto()   # no capital, periodic rent, lessor owns


@dataclass(frozen=True)
class FinancingTerms:
    """Reference data for a bank/lessor product."""
    product_id: str
    method: AcquisitionMethod
    # loan terms
    down_payment_frac: float = 0.20      # FINANCE: fraction paid up front
    annual_rate: float = 0.06            # APR on financed balance
    term_months: int = 120               # amortization horizon
    # lease terms
    lease_rate_frac_per_year: float = 0.11   # annual rent as frac of list price
    lease_term_months: int = 84
    lease_return_condition_cost: float = 250_000  # end-of-lease handback cost


@dataclass
class Loan:
    """Live amortizing debt. Interest accrues continuously; principal amortizes."""
    loan_id: str
    owner_id: str
    principal_initial: float
    remaining: float
    annual_rate: float
    term_months: int
    months_elapsed: float = 0.0
    tail_number: str = ""    # the asset this loan financed (collateral)

    def monthly_payment(self) -> float:
        r = self.annual_rate / 12.0
        n = self.term_months
        if r <= 0:
            return self.principal_initial / n
        return self.principal_initial * (r * (1 + r) ** n) / ((1 + r) ** n - 1)

    def accrue_and_bill(self, dt_hours: float) -> float:
        """
        Advance the loan by dt and return the cash payment due this tick.
        Interest accrues on the remaining balance; the payment covers interest
        first, remainder reduces principal. Returns the cash to debit.
        """
        if self.remaining <= 0:
            return 0.0
        dt_months = dt_hours / (24.0 * 30.4375)
        self.months_elapsed += dt_months
        # interest on current balance for this slice of time
        interest = self.remaining * self.annual_rate * (dt_months / 12.0)
        # pro-rated payment for the slice
        payment = self.monthly_payment() * dt_months
        principal_paid = max(0.0, payment - interest)
        self.remaining = max(0.0, self.remaining - principal_paid)
        return payment


@dataclass
class Lease:
    """Live operating lease. Fixed periodic rent; lessor retains ownership."""
    lease_id: str
    owner_id: str
    list_price: float
    annual_rate_frac: float
    term_months: int
    months_elapsed: float = 0.0
    tail_number: str = ""
    return_cost: float = 0.0

    def accrue_and_bill(self, dt_hours: float) -> float:
        dt_months = dt_hours / (24.0 * 30.4375)
        self.months_elapsed += dt_months
        annual_rent = self.list_price * self.annual_rate_frac
        return annual_rent * (dt_months / 12.0)

    def expired(self) -> bool:
        return self.months_elapsed >= self.term_months


@dataclass
class Bank:
    """
    Issues financing subject to creditworthiness, then services the debt.
    Creditworthiness here is a simple debt-to-cash gate; a fuller model would
    fold in revenue history and asset coverage.
    """
    max_debt_to_cash: float = 4.0    # won't lend past this leverage ratio
    _loan_seq: int = 0
    _lease_seq: int = 0

    def _outstanding_debt(self, player) -> float:
        return sum(l.remaining for l in getattr(player, "loans", []))

    def can_finance(self, player, amount: float) -> bool:
        projected = self._outstanding_debt(player) + amount
        cash = max(1.0, player.ledger.cash)
        return (projected / cash) <= self.max_debt_to_cash

    def try_acquire(self, player, spec, tail_number: str,
                    method: AcquisitionMethod, terms: FinancingTerms,
                    log: list) -> bool:
        """
        acquire(), but answering the question callers actually have: DID IT FUND?

        Prefer this over acquire() unless you need the Loan/Lease object. Because
        acquire() returns None both for a denial AND for a successful BUY_CASH,
        every call site was re-deriving that distinction — and three of them got
        it wrong by attaching the Airplane regardless, putting aircraft in fleets
        that were never paid for and overstating net worth by a whole airframe.

        Attach the Airplane only when this returns True.
        """
        before_cash = player.ledger.cash
        result = self.acquire(player, spec, tail_number, method, terms, log)
        if method == AcquisitionMethod.BUY_CASH:
            return player.ledger.cash < before_cash
        return result is not None

    def acquire(self, player, spec, tail_number: str, method: AcquisitionMethod,
                terms: FinancingTerms, log: list) -> Optional[object]:
        """
        Execute an acquisition. Returns the created Loan/Lease (or None for cash).
        Mutates the player's ledger and debt books. Caller attaches the Airplane.

        CAUTION: None means "denied" for FINANCE/OPERATING_LEASE but "succeeded"
        for BUY_CASH. Use try_acquire() unless you need the returned object.
        """
        price = spec.list_price

        if method == AcquisitionMethod.BUY_CASH:
            if not player.ledger.debit(price, f"buy {tail_number} ({spec.display_name})", log):
                return None
            return None

        if method == AcquisitionMethod.FINANCE:
            down = price * terms.down_payment_frac
            financed = price - down
            if not self.can_finance(player, financed):
                log.append(f"  CREDIT DENIED: financing {tail_number} would exceed leverage cap")
                return None
            if not player.ledger.debit(down, f"down payment {tail_number}", log):
                return None
            self._loan_seq += 1
            loan = Loan(loan_id=f"L{self._loan_seq}", owner_id=player.player_id,
                        principal_initial=financed, remaining=financed,
                        annual_rate=terms.annual_rate, term_months=terms.term_months,
                        tail_number=tail_number)
            player.loans.append(loan)
            log.append(f"  FINANCED {tail_number}: ${down:,.0f} down, "
                       f"${financed:,.0f} @ {terms.annual_rate:.1%}/yr, "
                       f"${loan.monthly_payment():,.0f}/mo")
            return loan

        if method == AcquisitionMethod.OPERATING_LEASE:
            # no capital outlay; just start paying rent
            self._lease_seq += 1
            lease = Lease(lease_id=f"LS{self._lease_seq}", owner_id=player.player_id,
                          list_price=price, annual_rate_frac=terms.lease_rate_frac_per_year,
                          term_months=terms.lease_term_months, tail_number=tail_number,
                          return_cost=terms.lease_return_condition_cost)
            player.leases.append(lease)
            annual = price * terms.lease_rate_frac_per_year
            log.append(f"  LEASED {tail_number}: ${annual/12:,.0f}/mo for "
                       f"{terms.lease_term_months}mo, no capital outlay")
            return lease

        return None

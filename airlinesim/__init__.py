"""
airlinesim — an airline asset & resource management simulation engine.

A continuous-time, multi-player simulation with:
  - spec-driven entities (aircraft, airports, crew, routes) loaded from data
  - tiered A/B/C/D maintenance with A+B fold and 3C/IL escalation
  - structured route demand (business/leisure/connecting segments)
  - cabin-class revenue with per-class price elasticity
  - crew duty/rest limits, rostering, deadheading (FAR Part 117-shaped)
  - financing: buy / finance / operating-lease, with depreciation and banking
  - a resource arbiter for gate/fuel/passenger contention between carriers

Quick start:

    from airlinesim import build_demo_world, run

    world, engine = build_demo_world()
    run(engine, days=60)

Or use the bundled scenarios / CLI:

    python -m airlinesim.cli list
    python -m airlinesim.cli run integration
"""

__version__ = "0.2.0"

# --- core engine ---
from airlinesim.engine import (
    # spec layer
    SpecRepository, SpecBase, AircraftSpec, AirportSpec, CrewSpec, RouteSpec,
    PlaneClass, CheckTier, CheckDefinition, StructuralLayover, MaintenanceProgram,
    CrewType,
    # instances
    Airplane, CrewUnit, RouteOp, Ledger, Player,
    # world + markets
    World, DemandMarket, FuelMarket, GateLedger,
    # economics / arbitration
    PricingModel, MarketConditions, ResourceArbiter, Claim, Allocation, ResourceKind,
    # maintenance
    MaintenanceEngine, MaintenanceJob,
    # engine + subsystems
    SimulationEngine, Subsystem, OperationsSubsystem, MaintenanceSubsystem,
    FinanceSubsystem, BankingSubsystem, RouteSuitabilitySubsystem,
)

# --- crew ---
from airlinesim.crew import (
    DutyLimits, DEFAULT_DUTY_LIMITS, GROUND_DUTY_LIMITS, CrewDutyState,
    is_legal_for_flight, crew_is_type_rated,
    CrewLegalitySubsystem, RosterSubsystem, CrewPositioningSubsystem,
    DeadheadSubsystem,
)

# --- route ---
from airlinesim.route import (
    TravelerSegment, SegmentDemand, default_segments,
    EquipmentRequirements, CrewRequirements, route_can_fly,
    block_hours, per_seat_cost_index, augmented_crew_required,
)

# --- finance / cabin ---
from airlinesim.finance_cabin import (
    CabinClass, SeatClassSpec, SeatLayout, DEFAULT_SEAT_CLASSES, cabin_slots_for,
    AcquisitionMethod, FinancingTerms, Loan, Lease, Bank,
    DepreciationModel, DEFAULT_DEPRECIATION, aircraft_value,
)

from airlinesim.builder import build_demo_world, run

__all__ = ["build_demo_world", "run", "__version__"]

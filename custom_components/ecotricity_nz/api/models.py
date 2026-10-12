"""Data models returned by the Ecotricity API clients.

Prices are kept exactly as the API publishes them: cents, excluding GST. Unit conversion and GST are
applied by the integration, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date, datetime


# Electricity registry register content code for a controlled-load register.
CONTROLLED_REGISTERS = frozenset({"CN"})


class RateUnit(StrEnum):
    """How a rate is charged."""

    PER_KWH = "cents/kWh"
    PER_DAY = "cents/d"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Rate:
    """A single retail or network charge line on a plan."""

    id: str
    description: str
    charge: str | None
    """Charge bucket code from the API, for example CHARGE_24, CHARGE_HIGH or CHARGE_LOW."""
    register_type: str | None
    """Meter register this rate applies to. EG means export (generation)."""
    unit: RateUnit
    raw_unit: str
    price: float
    """Price in cents (per kWh or per day), excluding GST. Export rates are negative."""
    is_network: bool
    season: str | None = None
    valid_start: date | None = None
    valid_end: date | None = None

    @property
    def is_export(self) -> bool:
        """Return True when this rate applies to exported energy."""
        return (self.register_type or "").upper() == "EG"

    @property
    def is_controlled(self) -> bool:
        """Return True when the rate applies to a controlled-load register (such as hot water).

        Recognised by the registry's controlled register code, or by a description that says
        controlled (but not "uncontrolled", which is what general import rates are called).
        """
        if (self.register_type or "").upper() in CONTROLLED_REGISTERS:
            return True
        description = self.description.lower()
        return "controlled" in description and "uncontrolled" not in description

    @property
    def is_levy(self) -> bool:
        """Return True for levies (for example the EA levy) added to every import kWh."""
        return "levy" in self.description.lower()

    def applies_on(self, day: date) -> bool:
        """Return True when the rate is valid on the given day."""
        if self.valid_start is not None and day < self.valid_start:
            return False
        return self.valid_end is None or day <= self.valid_end


@dataclass(frozen=True, slots=True)
class Season:
    """A pricing season published with a plan."""

    name: str
    start: date
    end: date

    def contains(self, day: date) -> bool:
        """Return True when the day falls within this season."""
        return self.start <= day <= self.end


@dataclass(frozen=True, slots=True)
class Plan:
    """An Ecotricity price plan with its unbundled retail and network rates."""

    id: str
    code: str
    description: str
    network: str
    price_category: str
    customer_type: str
    standard_low: str | None
    retail_rates: tuple[Rate, ...]
    network_rates: tuple[Rate, ...]
    seasons: tuple[Season, ...]
    timeframe_chart_weekday: str | None = None
    timeframe_chart_weekend: str | None = None
    title: str | None = None
    """The name customers see on the website (for example "Low ecoSOLAR"), when known."""

    @property
    def name(self) -> str:
        """Return the customer-facing name, falling back to the internal description."""
        return self.title or self.description

    @property
    def rates(self) -> tuple[Rate, ...]:
        """Return all retail and network rates."""
        return self.retail_rates + self.network_rates


@dataclass(frozen=True, slots=True)
class PlanList:
    """Plans available to an ICP, as returned by the onboarding service."""

    default: Plan
    alternative: Plan | None = None
    others: tuple[Plan, ...] = ()

    @property
    def all_plans(self) -> tuple[Plan, ...]:
        """Return every distinct plan, default first."""
        seen: set[str] = set()
        plans: list[Plan] = []
        for plan in (self.default, self.alternative, *self.others):
            if plan is not None and plan.id not in seen:
                seen.add(plan.id)
                plans.append(plan)
        return tuple(plans)

    def find(self, plan_id: str) -> Plan | None:
        """Return the plan with the given id, if present."""
        return next((plan for plan in self.all_plans if plan.id == plan_id), None)

    def with_titles(self, titles: Mapping[str, str]) -> PlanList:
        """Return a copy with customer-facing titles, keyed by normalised internal description."""

        def titled(plan: Plan) -> Plan:
            return replace(plan, title=titles.get(_normalise(plan.description), plan.title))

        return PlanList(
            default=titled(self.default),
            alternative=titled(self.alternative) if self.alternative else None,
            others=tuple(titled(plan) for plan in self.others),
        )

    def find_by_name(self, name: str) -> Plan | None:
        """Return the plan whose customer-facing title or internal description matches a name."""
        wanted = _normalise(name)
        return next(
            (
                plan
                for plan in self.all_plans
                if wanted in (_normalise(plan.description), _normalise(plan.title or ""))
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class MeterChannel:
    """A metering channel (register) on the ICP."""

    serial_number: str
    register_code: str
    channel_number: str
    energy_flow: str
    """X for import (consumption), I for export (injection)."""
    settlement: bool


@dataclass(frozen=True, slots=True)
class IcpDetails:
    """Registry details for an ICP, as returned by the onboarding service."""

    icp: str
    network_code: str
    network_name: str
    price_category: str
    price_category_description: str
    retailer_code: str
    retailer_name: str
    address: str
    region: str
    meter_type: str | None
    channels: tuple[MeterChannel, ...] = ()
    smart_meter: bool | None = None
    """Whether the meter is an advanced (smart) meter that reports remotely."""
    generation_capacity_kw: float | None = None
    """Generation (for example solar) capacity registered for the ICP, in kW."""
    certification_expiry: date | None = None
    """When the metering installation's certification expires."""
    loss_factor_consumption: float | None = None
    loss_factor_generation: float | None = None

    @property
    def register_codes(self) -> tuple[str, ...]:
        """Return register codes in API order, as the plan lookup expects them."""
        return tuple(channel.register_code for channel in self.channels)

    @property
    def meter_serial_numbers(self) -> tuple[str, ...]:
        """Return the distinct meter serial numbers."""
        return tuple(dict.fromkeys(channel.serial_number for channel in self.channels))

    @property
    def has_export(self) -> bool:
        """Return True when the ICP has an export (EG) register."""
        return "EG" in self.register_codes


@dataclass(frozen=True, slots=True)
class PortalIndicator:
    """The portal's metering indicator for an ICP, with the range of data it holds."""

    indicator_id: str
    display_name: str
    """The ICP number."""
    last_import: datetime | None = None
    """When the portal last imported meter data."""
    data_start: datetime | None = None
    data_end: datetime | None = None
    features: frozenset[str] = frozenset()
    """IDs of the series the portal holds data for, such as POWER_ACTIVE_PURCHASE_CONSUMPTION."""


@dataclass(frozen=True, slots=True)
class IntervalReading:
    """A single half-hourly reading."""

    start: datetime
    end: datetime
    value: float


@dataclass(frozen=True, slots=True)
class ConsumptionData:
    """Half-hourly series downloaded from the portal."""

    imported_kwh: tuple[IntervalReading, ...] = field(default_factory=tuple)
    exported_kwh: tuple[IntervalReading, ...] = field(default_factory=tuple)
    carbon_savings_kg: tuple[IntervalReading, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """Return True when no series has any readings."""
        return not (self.imported_kwh or self.exported_kwh or self.carbon_savings_kg)


def normalise_plan_name(text: str) -> str:
    """Normalise a plan name for matching: case-insensitive, with whitespace collapsed."""
    return _normalise(text)


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()

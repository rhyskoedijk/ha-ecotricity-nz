"""Sensors for Ecotricity NZ."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfMass, UnitOfPower
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import CONF_INCLUDE_GST, DEFAULT_INCLUDE_GST, GST_FACTOR
from .coordinator import ConsumptionCoordinator, PricingCoordinator
from .entity import EcotricityEntity
from .statistics import CARBON, EXPORT, IMPORT, statistic_id
from .tariff import (
    CONTROLLED_PREFIX,
    EXPORT_PREFIX,
    WEEKDAY_COUNT,
    ComponentKind,
    DailyCharge,
    DayPricing,
    PriceComponent,
    TouPeriod,
    day_windows,
    next_bucket_change,
    next_season_change,
    period_for_bucket,
    price_day,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .coordinator import ConsumptionSummary, EcotricityConfigEntry, PricingData

# Sensors only read coordinator data, so there's no need to limit parallel updates.
PARALLEL_UPDATES = 0

PRICE_UNIT = f"NZD/{UnitOfEnergy.KILO_WATT_HOUR}"
DAILY_UNIT = "NZD/d"


@dataclass(frozen=True, kw_only=True)
class PricingSensorDescription(SensorEntityDescription):
    """A sensor computed from pricing data."""

    value_fn: Callable[[PricingData], StateType | date | datetime]
    attrs_fn: Callable[[PricingData], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class ConsumptionSensorDescription(SensorEntityDescription):
    """A sensor computed from the consumption summary."""

    value_fn: Callable[[ConsumptionSummary], StateType | date | datetime]


def _season_label(data: PricingData) -> str | None:
    if data.season is None:
        return None
    start, end = data.season.start, data.season.end
    return f"{data.season.name} {start:%m/%Y} - {end:%m/%Y}"


PRICING_SENSORS: tuple[PricingSensorDescription, ...] = (
    PricingSensorDescription(
        key="icp",
        translation_key="icp",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.icp.icp,
    ),
    PricingSensorDescription(
        key="network",
        translation_key="network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.icp.network_name or data.icp.network_code,
        attrs_fn=lambda data: {
            "code": data.icp.network_code,
            "loss_factor_consumption": data.icp.loss_factor_consumption,
            "loss_factor_generation": data.icp.loss_factor_generation,
        },
    ),
    PricingSensorDescription(
        key="pricing_category",
        translation_key="pricing_category",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.icp.price_category_description or data.icp.price_category,
        attrs_fn=lambda data: {"code": data.icp.price_category},
    ),
    PricingSensorDescription(
        key="meter",
        translation_key="meter",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: ", ".join(data.icp.meter_serial_numbers) or None,
        attrs_fn=lambda data: {
            "type": data.icp.meter_type,
            "smart": data.icp.smart_meter,
            "certification_expiry": data.icp.certification_expiry,
        },
    ),
    PricingSensorDescription(
        key="plan",
        translation_key="plan",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.plan.name,
        attrs_fn=lambda data: {
            "internal_name": data.plan.description,
            "code": data.plan.code,
        },
    ),
    PricingSensorDescription(
        key="pricing_season",
        translation_key="pricing_season",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_season_label,
        attrs_fn=lambda data: {
            "start": data.season.start if data.season else None,
            "end": data.season.end if data.season else None,
            "published": [
                f"{season.start.isoformat()}/{season.end.isoformat()}"
                for season in data.plan.seasons
            ],
        },
    ),
    PricingSensorDescription(
        key="next_pricing_season_change",
        translation_key="next_pricing_season_change",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: next_season_change(data.plan, data.day),
    ),
    PricingSensorDescription(
        key="last_pricing_update",
        translation_key="last_pricing_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.fetched_at,
    ),
    PricingSensorDescription(
        key="registered_generation_capacity",
        translation_key="registered_generation_capacity",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.icp.generation_capacity_kw,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - signature set by Home Assistant
    entry: EcotricityConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Ecotricity sensors."""
    runtime = entry.runtime_data
    pricing = runtime.pricing
    icp = pricing.icp
    entities: list[SensorEntity] = [
        PricingSensor(pricing, icp, description) for description in PRICING_SENSORS
    ]
    entities += [
        DailyChargeSensor(pricing, icp),
        CurrentPriceSensor(pricing, icp, runtime.local_tz, export=False),
        CurrentPeriodSensor(pricing, icp, runtime.local_tz, export=False),
        NextPeriodChangeSensor(pricing, icp, runtime.local_tz, export=False),
    ]
    if pricing.data.icp.has_export or pricing.data.pricing.export_buckets:
        entities += [
            CurrentPriceSensor(pricing, icp, runtime.local_tz, export=True),
            CurrentPeriodSensor(pricing, icp, runtime.local_tz, export=True),
            NextPeriodChangeSensor(pricing, icp, runtime.local_tz, export=True),
        ]

    if any(bucket.startswith(CONTROLLED_PREFIX) for bucket in _plan_buckets(pricing.data)):
        entities.append(ControlledPriceSensor(pricing, icp))

    if runtime.consumption is not None:
        entities += [
            ConsumptionSensor(runtime.consumption, icp, description)
            for description in CONSUMPTION_SENSORS
        ]
    async_add_entities(entities)

    # Rate sensors depend on the plan and its seasons, which Ecotricity can change at any time.
    # Add a sensor for any bucket that appears after setup.
    known_buckets: set[str] = set()

    @callback
    def _add_rate_sensors() -> None:
        buckets = _plan_buckets(pricing.data)
        new: list[SensorEntity] = [
            BucketPriceSensor(pricing, icp, bucket) for bucket in buckets - known_buckets
        ]
        known_buckets.update(buckets)
        if new:
            async_add_entities(new)

    _add_rate_sensors()
    entry.async_on_unload(pricing.async_add_listener(_add_rate_sensors))


def _plan_buckets(data: PricingData) -> set[str]:
    """Return every bucket the plan uses today or in any published season."""
    buckets: set[str] = set()
    for day in [data.day, *(season.start for season in data.plan.seasons)]:
        day_pricing = price_day(data.plan, day)
        buckets.update(
            day_pricing.import_buckets, day_pricing.export_buckets, day_pricing.controlled_buckets
        )
    return buckets


class _PricingEntity(EcotricityEntity[PricingCoordinator], SensorEntity):
    """Shared helpers for pricing sensors."""

    def _to_nzd(self, cents: float, *, export: bool = False) -> float:
        """Convert cents excluding GST to NZD.

        GST is added to charges when the option is on. Export credits never have GST added:
        residential customers who aren't GST-registered are paid the published rate, and the
        credit is shown as a positive amount, matching how a bill presents it.
        """
        if export:
            return round(abs(cents) / 100, 4)
        include_gst = self.coordinator.config_entry.options.get(
            CONF_INCLUDE_GST, DEFAULT_INCLUDE_GST
        )
        factor = GST_FACTOR if include_gst else 1.0
        return round(cents / 100 * factor, 4)

    def _component_attributes(self, bucket: str | None, *, export: bool) -> dict[str, float]:
        """Return the parts of a bucket's price: supplier_rate, network_rate and any levies.

        Only parts the plan actually has appear, so the attributes vary from plan to plan.
        """
        if bucket is None:
            return {}
        parts: dict[str, float] = {}
        for part in self.coordinator.data.pricing.components.get(bucket, ()):
            if not part.price:
                # A zero part (a network export rate of 0c, say) adds nothing to the price.
                continue
            key = _component_key(part)
            parts[key] = parts.get(key, 0.0) + part.price
        return {key: self._to_nzd(cents, export=export) for key, cents in parts.items()}


class PricingSensor(_PricingEntity):
    """A sensor described by a PricingSensorDescription."""

    entity_description: PricingSensorDescription

    @property
    def native_value(self) -> StateType | date | datetime:
        """Return the value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class DailyChargeSensor(_PricingEntity):
    """The total of all fixed daily charges."""

    _attr_native_unit_of_measurement = DAILY_UNIT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: PricingCoordinator, icp: str) -> None:
        """Initialise the sensor."""
        super().__init__(
            coordinator,
            icp,
            SensorEntityDescription(key="daily_charge", translation_key="daily_charge"),
        )

    @property
    def native_value(self) -> float:
        """Return the daily charge in NZD."""
        return self._to_nzd(self.coordinator.data.pricing.daily_total)

    @property
    def extra_state_attributes(self) -> dict[str, float]:
        """Break the daily charge into its parts, for example supplier_rate and metering_rate."""
        parts: dict[str, float] = {}
        for charge in self.coordinator.data.pricing.daily_charges:
            if not charge.price:
                continue
            key = _daily_charge_key(charge)
            parts[key] = parts.get(key, 0.0) + charge.price
        return {key: self._to_nzd(cents) for key, cents in parts.items()}


class BucketPriceSensor(_PricingEntity):
    """The price of one charge bucket (for example peak import)."""

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_suggested_display_precision = 4
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: PricingCoordinator, icp: str, bucket: str) -> None:
        """Initialise the sensor."""
        export = bucket.startswith(EXPORT_PREFIX)
        direction = _group(bucket)
        period = period_for_bucket(bucket)
        super().__init__(
            coordinator,
            icp,
            SensorEntityDescription(
                key=f"price_{_slug(bucket)}",
                translation_key=f"{period.value if period else 'other'}_{direction}_price",
            ),
        )
        self._bucket = bucket
        self._export = export
        if period is None:
            self._attr_translation_placeholders = {"name": bucket}

    def _applied_bucket(self) -> str | None:
        """Return the bucket whose price applies in this bucket's hours today.

        When a season doesn't price this period separately and only one price applies all day
        (peak and off-peak cost the same, say), that price is what these hours are charged at.
        With several prices in the day and no separate one for this period, which one applies
        can't be known, so there's no value.
        """
        buckets = _group_buckets(self.coordinator.data.pricing, _group(self._bucket))
        if self._bucket in buckets:
            return self._bucket
        if len(buckets) == 1:
            return next(iter(buckets))
        return None

    @property
    def native_value(self) -> float | None:
        """Return the price charged in this bucket's hours today."""
        bucket = self._applied_bucket()
        if bucket is None:
            return None
        buckets = _group_buckets(self.coordinator.data.pricing, _group(self._bucket))
        return self._to_nzd(buckets[bucket], export=self._export)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Break the price down, and say when another period's price is standing in."""
        bucket = self._applied_bucket()
        attributes: dict[str, Any] = {}
        if bucket is not None and bucket != self._bucket and (period := _period(bucket)):
            attributes["charged_as"] = period
        return attributes | self._component_attributes(bucket, export=self._export)


class ControlledPriceSensor(_PricingEntity):
    """The price of controlled-load supply (such as hot water), when the plan has one.

    Controlled load is switched on and off by the network, not by the clock, so there are no
    time windows. When the plan has a single controlled price that's the value; with several,
    which one applies can't be known, and the per-period controlled price sensors show them.
    """

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: PricingCoordinator, icp: str) -> None:
        """Initialise the sensor."""
        super().__init__(
            coordinator,
            icp,
            SensorEntityDescription(
                key="current_controlled_price", translation_key="current_controlled_price"
            ),
        )

    def _bucket(self) -> str | None:
        buckets = self.coordinator.data.pricing.controlled_buckets
        return next(iter(buckets)) if len(buckets) == 1 else None

    @property
    def native_value(self) -> float | None:
        """Return the controlled price, if the plan has exactly one."""
        bucket = self._bucket()
        if bucket is None:
            return None
        return self._to_nzd(self.coordinator.data.pricing.controlled_buckets[bucket])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Break the price down into its parts."""
        return self._component_attributes(self._bucket(), export=False)


class _HalfHourlyEntity(_PricingEntity):
    """A pricing sensor that also updates at every half-hour boundary."""

    def __init__(
        self,
        coordinator: PricingCoordinator,
        icp: str,
        description: SensorEntityDescription,
        local_tz: tzinfo,
    ) -> None:
        super().__init__(coordinator, icp, description)
        self._local_tz = local_tz
        self._unsub_tick: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Start the half-hour timer."""
        await super().async_added_to_hass()
        self._schedule_tick()
        self.async_on_remove(self._cancel_tick)

    @callback
    def _schedule_tick(self) -> None:
        now = dt_util.utcnow()
        boundary = now.replace(minute=30 if now.minute < 30 else 0, second=0, microsecond=0)  # noqa: PLR2004 - minutes in half an hour
        if boundary <= now:
            boundary += timedelta(hours=1)
        self._unsub_tick = async_track_point_in_utc_time(self.hass, self._tick, boundary)

    @callback
    def _tick(self, _now: datetime) -> None:
        self._unsub_tick = None
        self.async_write_ha_state()
        self._schedule_tick()

    @callback
    def _cancel_tick(self) -> None:
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None

    def _current_bucket(self, *, export: bool) -> str | None:
        data = self.coordinator.data
        slots = data.export_slots if export else data.import_slots
        if slots is None:
            return None
        return slots.bucket_at(dt_util.now(self._local_tz))


class CurrentPriceSensor(_HalfHourlyEntity):
    """The price for the current half-hour. Usable as the Energy dashboard price entity."""

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_suggested_display_precision = 4

    def __init__(
        self, coordinator: PricingCoordinator, icp: str, local_tz: tzinfo, *, export: bool
    ) -> None:
        """Initialise the sensor."""
        key = "current_export_price" if export else "current_import_price"
        super().__init__(
            coordinator, icp, SensorEntityDescription(key=key, translation_key=key), local_tz
        )
        self._export = export

    @property
    def available(self) -> bool:
        """Unavailable until the time-of-use windows are known."""
        return super().available and self._current_bucket(export=self._export) is not None

    @property
    def native_value(self) -> float | None:
        """Return the current price in NZD per kWh. Export is shown as a positive credit."""
        bucket = self._current_bucket(export=self._export)
        pricing = self.coordinator.data.pricing
        buckets = pricing.export_buckets if self._export else pricing.import_buckets
        if bucket is None or bucket not in buckets:
            return None
        return self._to_nzd(buckets[bucket], export=self._export)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe the active period."""
        bucket = self._current_bucket(export=self._export)
        attributes: dict[str, Any] = {}
        if bucket is not None and (period := _period(bucket)):
            attributes["period"] = period
        attributes |= self._component_attributes(bucket, export=self._export)
        if (error := self.coordinator.data.tou_error) is not None:
            attributes["tou_error"] = error
        return attributes


class CurrentPeriodSensor(_HalfHourlyEntity):
    """The import or export time-of-use period for the current half-hour."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [period.value for period in TouPeriod]  # noqa: RUF012 - HA attribute

    def __init__(
        self, coordinator: PricingCoordinator, icp: str, local_tz: tzinfo, *, export: bool
    ) -> None:
        """Initialise the sensor."""
        key = "current_export_period" if export else "current_import_period"
        super().__init__(
            coordinator, icp, SensorEntityDescription(key=key, translation_key=key), local_tz
        )
        self._export = export

    @property
    def available(self) -> bool:
        """Unavailable until the time-of-use windows are known."""
        return super().available and self._current_bucket(export=self._export) is not None

    @property
    def native_value(self) -> str | None:
        """Return the current period."""
        bucket = self._current_bucket(export=self._export)
        return _period(bucket) if bucket else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """List today's windows, for example "07:00-11:00 peak"."""
        data = self.coordinator.data
        slots = data.export_slots if self._export else data.import_slots
        if slots is None:
            return None
        today = dt_util.now(self._local_tz)
        day = slots.weekday if today.weekday() < WEEKDAY_COUNT else slots.weekend
        return {
            "windows": [
                f"{start}-{end} {_period(bucket) or bucket}"
                for start, end, bucket in day_windows(day)
            ]
        }


class NextPeriodChangeSensor(_HalfHourlyEntity):
    """When the import or export period next changes, with the period and price from then."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coordinator: PricingCoordinator, icp: str, local_tz: tzinfo, *, export: bool
    ) -> None:
        """Initialise the sensor."""
        key = "next_export_period_change" if export else "next_import_period_change"
        super().__init__(
            coordinator, icp, SensorEntityDescription(key=key, translation_key=key), local_tz
        )
        self._export = export

    def _next_change(self) -> tuple[datetime, str] | None:
        data = self.coordinator.data
        slots = data.export_slots if self._export else data.import_slots
        if slots is None:
            return None
        until: datetime | None = None
        if (change := next_season_change(data.plan, data.day)) is not None:
            until = datetime.combine(change, time.min, self._local_tz)
        return next_bucket_change(slots, dt_util.utcnow(), self._local_tz, until)

    @property
    def native_value(self) -> datetime | None:
        """Return when the period next changes, or None if not before the season ends."""
        change = self._next_change()
        return change[0] if change else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Describe the period and price from then."""
        change = self._next_change()
        if change is None:
            return None
        _, bucket = change
        pricing = self.coordinator.data.pricing
        buckets = pricing.export_buckets if self._export else pricing.import_buckets
        attributes: dict[str, Any] = {}
        if (period := _period(bucket)) is not None:
            attributes["period"] = period
        attributes["price"] = self._to_nzd(buckets[bucket], export=self._export)
        return attributes


# The long-term statistic holding each daily sensor's full half-hourly history.
_HISTORY_SERIES = {
    "latest_day_imported": IMPORT,
    "latest_day_exported": EXPORT,
    "latest_day_carbon_savings": CARBON,
}

CONSUMPTION_SENSORS: tuple[ConsumptionSensorDescription, ...] = (
    ConsumptionSensorDescription(
        key="latest_day_imported",
        translation_key="latest_day_imported",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data: data.imported_kwh,
    ),
    ConsumptionSensorDescription(
        key="latest_day_exported",
        translation_key="latest_day_exported",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data: data.exported_kwh,
    ),
    ConsumptionSensorDescription(
        key="latest_day_carbon_savings",
        translation_key="latest_day_carbon_savings",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=2,
        value_fn=lambda data: data.carbon_savings_kg,
    ),
    ConsumptionSensorDescription(
        key="latest_day_of_usage",
        translation_key="latest_day_of_usage",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data.day,
    ),
    ConsumptionSensorDescription(
        key="last_meter_data_update",
        translation_key="last_meter_data_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.indicator.last_import,
    ),
)


class ConsumptionSensor(EcotricityEntity[ConsumptionCoordinator], SensorEntity):
    """A sensor computed from the latest consumption summary."""

    entity_description: ConsumptionSensorDescription

    @property
    def available(self) -> bool:
        """Unavailable until the first import has finished."""
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> StateType | date | datetime:
        """Return the value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Show which day the daily totals cover, and the statistic holding the full history.

        The history is imported as long-term statistics, which aren't entities, so the
        statistic ID is shown here where it's easy to find and copy into a chart.
        """
        if (series := _HISTORY_SERIES.get(self.entity_description.key)) is None:
            return None
        return {
            "date": self.coordinator.data.day,
            "statistic_id": statistic_id(self.coordinator.icp, series),
        }


def _daily_charge_key(charge: DailyCharge) -> str:
    """Return a stable attribute key for a daily charge item.

    The network's charges are network_rate. Of Ecotricity's items, the metering fee is shown on
    its own (metering_rate), like the EA levy on import prices, and the admin fee is
    Ecotricity's own charge (supplier_rate). Any other item gets a key from its description,
    so a new kind of charge is never hidden inside another.
    """
    if charge.is_network:
        return "network_rate"
    description = charge.description.lower()
    if "meter" in description:
        return "metering_rate"
    if "admin" in description:
        return "supplier_rate"
    return _slug(charge.description)


def _group(bucket: str) -> str:
    """Return which group a bucket belongs to: import, export or controlled."""
    if bucket.startswith(EXPORT_PREFIX):
        return "export"
    if bucket.startswith(CONTROLLED_PREFIX):
        return "controlled"
    return "import"


def _group_buckets(pricing: DayPricing, group: str) -> dict[str, float]:
    if group == "export":
        return pricing.export_buckets
    if group == "controlled":
        return pricing.controlled_buckets
    return pricing.import_buckets


def _period(bucket: str) -> str | None:
    """Return the period for a bucket, or None for a charge code Ecotricity doesn't define."""
    period = period_for_bucket(bucket)
    return period.value if period else None


def _component_key(part: PriceComponent) -> str:
    """Return a stable attribute key for a price part.

    Keys don't include the period, so they stay the same as the time of day changes:
    supplier_rate, network_rate, and ea_levy (other levies get a key from their description).
    """
    if part.kind is ComponentKind.SUPPLIER:
        return "supplier_rate"
    if part.kind is ComponentKind.NETWORK:
        return "network_rate"
    key = _slug(part.description)
    return "ea_levy" if key in ("ea_levy", "electricity_authority_levy") else key


def _slug(text: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in text.lower()).strip("_")

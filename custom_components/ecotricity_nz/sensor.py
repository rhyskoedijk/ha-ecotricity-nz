"""Sensors for Ecotricity NZ."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfMass, UnitOfPower
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import CONF_INCLUDE_GST, DEFAULT_INCLUDE_GST, DOMAIN, GST_FACTOR
from .coordinator import ConsumptionCoordinator, PricingCoordinator
from .entity import EcotricityEntity
from .statistics import CARBON, EXPORT, IMPORT, statistic_id
from .tariff import (
    CONTROLLED_PREFIX,
    EXPORT_PREFIX,
    Alignment,
    ComponentKind,
    DailyCharge,
    PriceComponent,
    SidePricing,
    Slot,
    TouPeriod,
    bucket_name,
    bucket_period,
    day_windows,
    next_change,
    next_season_change,
    period_for_bucket,
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
# Bucket price sensor keys are this prefix and the bucket, for example "price_charge_high".
_PRICE_KEY_PREFIX = "price_"
DAILY_UNIT = "NZD/d"


@dataclass(frozen=True, kw_only=True)
class PricingSensorDescription(SensorEntityDescription):
    """A sensor computed from pricing data."""

    value_fn: Callable[[PricingData], StateType | date | datetime]
    attrs_fn: Callable[[PricingData], dict[str, Any]] | None = None
    exists_fn: Callable[[PricingData], bool] = lambda _: True


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
        attrs_fn=lambda data: {"code": data.icp.network_code},
    ),
    PricingSensorDescription(
        key="network_import_loss_factor",
        translation_key="network_import_loss_factor",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=4,
        value_fn=lambda data: data.icp.loss_factor_consumption,
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
        # Some networks record zero even for ICPs with generation, so a missing or zero capacity
        # says nothing about export and the sensor is left out.
        exists_fn=lambda data: bool(data.icp.generation_capacity_kw),
    ),
)

EXPORT_LOSS_FACTOR = PricingSensorDescription(
    key="network_export_loss_factor",
    translation_key="network_export_loss_factor",
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=4,
    value_fn=lambda data: data.icp.loss_factor_generation,
)

# Ecotricity's and the network's own period sensors, created only for a side that's split.
_SPLIT_PERIOD_KEYS = {
    (export, party): f"current_{party}_{'export' if export else 'import'}_period"
    for export in (False, True)
    for party in ("supplier", "network")
}


def _pricing_sensors(
    entity_registry: er.EntityRegistry, pricing: PricingCoordinator, icp: str
) -> list[SensorEntity]:
    """Return the pricing sensors whose value exists, removing any left from an earlier setup."""
    entities: list[SensorEntity] = []
    for description in PRICING_SENSORS:
        if description.exists_fn(pricing.data):
            entities.append(PricingSensor(pricing, icp, description))
        elif entity_id := entity_registry.async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, f"{icp}_{description.key}"
        ):
            entity_registry.async_remove(entity_id)
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcotricityConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Ecotricity sensors."""
    runtime = entry.runtime_data
    pricing = runtime.pricing
    icp = pricing.icp
    tz = runtime.local_tz
    entity_registry = er.async_get(hass)
    entities: list[SensorEntity] = _pricing_sensors(entity_registry, pricing, icp)
    entities += [
        DailyChargeSensor(pricing, icp),
        CurrentPriceSensor(pricing, icp, tz, export=False),
        CurrentPeriodSensor(pricing, icp, tz, export=False),
        NextPeriodChangeSensor(pricing, icp, tz, export=False),
    ]
    if pricing.data.icp.has_export or pricing.data.pricing.has_export:
        entities += [
            PricingSensor(pricing, icp, EXPORT_LOSS_FACTOR),
            CurrentPriceSensor(pricing, icp, tz, export=True),
            CurrentPeriodSensor(pricing, icp, tz, export=True),
            NextPeriodChangeSensor(pricing, icp, tz, export=True),
        ]

    if any(bucket.startswith(CONTROLLED_PREFIX) for bucket in pricing.data.periods.buckets):
        entities.append(ControlledPriceSensor(pricing, icp))

    if runtime.consumption is not None:
        entities += [
            ConsumptionSensor(runtime.consumption, icp, description)
            for description in CONSUMPTION_SENSORS
        ]
    async_add_entities(entities)

    # Which periods a plan has, and whether Ecotricity's and the network's line up, depend on the
    # plan and its seasons, which Ecotricity can change at any time. Add sensors for any period,
    # or split side, that appears after setup, and remove those the plan no longer has (after
    # choosing another plan, say), so only what the plan has shows.
    sync = partial(_sync_plan_sensors, entity_registry, entry, async_add_entities, tz, known=set())
    sync()
    entry.async_on_unload(pricing.async_add_listener(sync))


@callback
def _sync_plan_sensors(
    entity_registry: er.EntityRegistry,
    entry: EcotricityConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    tz: tzinfo,
    *,
    known: set[str],
) -> None:
    """Add the sensors the plan's periods call for, and remove those it no longer has.

    `known` holds the unique IDs of the sensors already added.
    """
    pricing = entry.runtime_data.pricing
    wanted = _plan_sensors(pricing, pricing.icp, tz)
    registered = _registered_plan_sensors(entity_registry, entry, pricing, pricing.icp, tz)
    # The periods aren't fully known until every season's charts can be read, so keep every
    # sensor until then, including those from before a restart.
    keep_all = pricing.data.tou_error is not None or not pricing.data.periods.complete
    if keep_all:
        wanted = registered | wanted
    if new := [create() for unique_id, create in wanted.items() if unique_id not in known]:
        async_add_entities(new)
    known.update(wanted)
    if keep_all:
        return
    known.intersection_update(wanted)
    for unique_id in registered.keys() - wanted.keys():
        if entity_id := entity_registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, unique_id):
            entity_registry.async_remove(entity_id)


def _plan_sensors(
    pricing: PricingCoordinator, icp: str, tz: tzinfo
) -> dict[str, Callable[[], SensorEntity]]:
    """Return how to create each sensor the plan's periods call for, by unique ID."""
    periods = pricing.data.periods
    wanted: dict[str, Callable[[], SensorEntity]] = {
        f"{icp}_{_PRICE_KEY_PREFIX}{_slug(bucket)}": partial(
            BucketPriceSensor, pricing, icp, bucket
        )
        for bucket in periods.buckets
    }
    for export, alignment in ((False, periods.import_alignment), (True, periods.export_alignment)):
        if alignment is Alignment.SPLIT:
            for party in ("supplier", "network"):
                wanted[f"{icp}_{_SPLIT_PERIOD_KEYS[export, party]}"] = partial(
                    CurrentPeriodSensor, pricing, icp, tz, export=export, party=party
                )
    return wanted


def _registered_plan_sensors(
    entity_registry: er.EntityRegistry,
    entry: EcotricityConfigEntry,
    pricing: PricingCoordinator,
    icp: str,
    tz: tzinfo,
) -> dict[str, Callable[[], SensorEntity]]:
    """Return how to create each plan sensor already in the entity registry, by unique ID.

    These are the price sensors (by the `{icp}_price_` prefix) and split period sensors.
    """
    price_prefix = f"{icp}_{_PRICE_KEY_PREFIX}"
    split = {f"{icp}_{key}": side for side, key in _SPLIT_PERIOD_KEYS.items()}
    sensors: dict[str, Callable[[], SensorEntity]] = {}
    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        unique_id = registry_entry.unique_id
        if registry_entry.domain != SENSOR_DOMAIN:
            continue
        if unique_id.startswith(price_prefix):
            # Bucket codes are upper case, so the unique ID's slug gives the bucket back.
            bucket = unique_id.removeprefix(price_prefix).upper()
            sensors[unique_id] = partial(BucketPriceSensor, pricing, icp, bucket)
        elif unique_id in split:
            export, party = split[unique_id]
            sensors[unique_id] = partial(
                CurrentPeriodSensor, pricing, icp, tz, export=export, party=party
            )
    return sensors


class _PricingEntity(EcotricityEntity[PricingCoordinator], SensorEntity):
    """Shared helpers for pricing sensors."""

    def _period(self, bucket: str) -> str:
        """Return a bucket's period: anytime, peak, off_peak, or a key from the plan's name."""
        return bucket_period(bucket, self.coordinator.data.names)

    def _side(self, *, export: bool) -> SidePricing | None:
        pricing = self.coordinator.data.pricing
        return pricing.export_side if export else pricing.import_side

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

    def _component_attributes(
        self, components: tuple[PriceComponent, ...], *, export: bool
    ) -> dict[str, float]:
        """Return the parts of a price: supplier_rate, network_rate and any levies.

        Only parts the plan actually has appear, so the attributes vary from plan to plan.
        """
        parts: dict[str, float] = {}
        for part in components:
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
                key=f"{_PRICE_KEY_PREFIX}{_slug(bucket)}",
                translation_key=f"{period.value if period else 'named'}_{direction}_price",
            ),
        )
        self._bucket = bucket
        self._export = export
        if period is None:
            self._attr_translation_placeholders = self._name_placeholders()

    def _name_placeholders(self) -> dict[str, str]:
        return {"name": bucket_name(self._bucket, self.coordinator.data.names)}

    @callback
    def _handle_coordinator_update(self) -> None:
        """Follow the plan's name for this bucket, which can change with the plan."""
        if period_for_bucket(self._bucket) is None:
            placeholders = self._name_placeholders()
            if placeholders != self.translation_placeholders:
                self._attr_translation_placeholders = placeholders
                # Home Assistant caches the translated name; drop it so it's worked out again.
                self.__dict__.pop("name", None)
        super()._handle_coordinator_update()

    def _price(self) -> tuple[float, tuple[PriceComponent, ...]] | None:
        """Return this period's own price today and its parts, or None if it doesn't apply."""
        pricing = self.coordinator.data.pricing
        group = _group(self._bucket)
        if group == "controlled":
            if self._bucket not in pricing.controlled_buckets:
                return None
            return (
                pricing.controlled_buckets[self._bucket],
                pricing.controlled_components[self._bucket],
            )
        side = self._side(export=group == "export")
        if side is None:
            # The charts can't be read, so the price comes from the plan's rates where it can.
            parts = pricing.rate_buckets.get(self._bucket)
            return (sum(part.price for part in parts), parts) if parts is not None else None
        if self._bucket not in side.buckets:
            return None
        return side.buckets[self._bucket], side.components[self._bucket]

    @property
    def native_value(self) -> float | None:
        """Return the period's own price today: none if the period doesn't apply today."""
        price = self._price()
        return self._to_nzd(price[0], export=self._export) if price else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Break the price down into its parts."""
        price = self._price()
        return self._component_attributes(price[1], export=self._export) if price else {}


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

    def _components(self) -> tuple[PriceComponent, ...]:
        bucket = self._bucket()
        return () if bucket is None else self.coordinator.data.pricing.controlled_components[bucket]

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
        return self._component_attributes(self._components(), export=False)


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

    def _current_slot(self, *, export: bool) -> Slot | None:
        side = self._side(export=export)
        if side is None:
            return None
        return side.slots.at(dt_util.now(self._local_tz))


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
        return super().available and self._current_slot(export=self._export) is not None

    @property
    def native_value(self) -> float | None:
        """Return the current price in NZD per kWh. Export is shown as a positive credit."""
        slot = self._current_slot(export=self._export)
        return None if slot is None else self._to_nzd(slot.price, export=self._export)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe the active period and break the price down."""
        slot = self._current_slot(export=self._export)
        attributes: dict[str, Any] = {}
        if slot is not None:
            attributes["period"] = self._period(slot.bucket)
            attributes |= self._component_attributes(slot.components, export=self._export)
        if (error := self.coordinator.data.tou_error) is not None:
            attributes["tou_error"] = error
        return attributes


class CurrentPeriodSensor(_HalfHourlyEntity):
    """The import or export time-of-use period for the current half-hour.

    The overall period by default. On a side where Ecotricity's and the network's periods don't
    line up, `party` gives a sensor for just Ecotricity's ("supplier") or the network's periods.
    """

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(
        self,
        coordinator: PricingCoordinator,
        icp: str,
        local_tz: tzinfo,
        *,
        export: bool,
        party: str | None = None,
    ) -> None:
        """Initialise the sensor."""
        direction = "export" if export else "import"
        key = _SPLIT_PERIOD_KEYS[export, party] if party else f"current_{direction}_period"
        super().__init__(
            coordinator, icp, SensorEntityDescription(key=key, translation_key=key), local_tz
        )
        self._export = export
        self._slot_field = party or "bucket"

    @property
    def available(self) -> bool:
        """Unavailable until the time-of-use windows are known."""
        return super().available and self._current_slot(export=self._export) is not None

    @property
    def options(self) -> list[str]:
        """Return only the periods the plan uses, today or in any upcoming season."""
        group = "export" if self._export else "import"
        plan_periods = self.coordinator.data.periods
        buckets = {
            "bucket": plan_periods.buckets,
            "supplier": plan_periods.supplier,
            "network": plan_periods.network,
        }[self._slot_field]
        periods = {self._period(bucket) for bucket in buckets if _group(bucket) == group}
        fixed = [period.value for period in TouPeriod if period.value in periods]
        return fixed + sorted(periods - set(fixed))

    @property
    def native_value(self) -> str | None:
        """Return the current period."""
        slot = self._current_slot(export=self._export)
        return None if slot is None else self._period(getattr(slot, self._slot_field))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """List today's windows, for example "07:00-11:00 peak"."""
        side = self._side(export=self._export)
        if side is None:
            return None
        day = side.slots.day(dt_util.now(self._local_tz))
        return {
            "windows": [
                f"{start}-{end} {self._period(getattr(slot, self._slot_field))}"
                for start, end, slot in day_windows(day, self._slot_field)
            ]
        }


class NextPeriodChangeSensor(_HalfHourlyEntity):
    """When the import or export period or price next changes, with the period and price then."""

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

    def _next_change(self) -> tuple[datetime, Slot] | None:
        data = self.coordinator.data
        side = self._side(export=self._export)
        if side is None:
            return None
        until: datetime | None = None
        if (change := next_season_change(data.plan, data.day)) is not None:
            until = datetime.combine(change, time.min, self._local_tz)
        return next_change(side.slots, dt_util.utcnow(), self._local_tz, until)

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
        _, slot = change
        return {
            "period": self._period(slot.bucket),
            "price": self._to_nzd(slot.price, export=self._export),
        }


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

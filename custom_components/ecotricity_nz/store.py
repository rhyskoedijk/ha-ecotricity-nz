"""Saved pricing, so prices and time-of-use windows keep working across restarts and offline.

Everything the pricing coordinator fetches from the onboarding site is kept here: ICP details,
plans (with their seasons and rates), plan names and the chart readings. Prices for any day are
worked out from this, so nothing needs fetching again until Ecotricity might have changed it.

Personal details no entity uses are left out of the file: the ICP's address and region are
saved blank. The ICP number and meter serial numbers are kept, because sensors show them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .api import IcpDetails, MeterChannel, Plan, PlanList, Rate, RateUnit, Season
from .api.timeframes import ChartReading, LegendEntry
from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

# Version 2 reads chart legends rather than bar heights; older snapshots are fetched again.
STORAGE_VERSION = 2


class _SnapshotStore(Store[dict[str, Any]]):
    """A store that drops snapshots saved in an older format, so pricing is fetched again."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],  # noqa: ARG002 - the signature Store calls
    ) -> dict[str, Any]:
        LOGGER.debug(
            "Discarding saved pricing in format %s.%s", old_major_version, old_minor_version
        )
        return {}


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    """Everything fetched from the onboarding site, and when."""

    icp: IcpDetails
    plans: PlanList
    """Plans with their customer-facing names applied."""
    fetched_at: datetime
    plan_titles: Mapping[str, str] = field(default_factory=dict)
    charts: Mapping[str, ChartReading] = field(default_factory=dict)
    """Chart readings by chart path."""
    charts_plan_id: str | None = None
    """The plan the charts were downloaded for."""
    missing_chart_fetches: int = 0
    """How many fetches in a row, up to and including this one, left charts missing (see
    `PricingData.missing_charts`)."""


class PricingStore:
    """Load and save a config entry's pricing snapshot."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the store."""
        self._store = _SnapshotStore(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.pricing")

    async def async_load(self) -> PricingSnapshot | None:
        """Return the saved snapshot, or None if there isn't a usable one."""
        raw = await self._store.async_load()
        if not raw:
            return None
        try:
            return _snapshot(raw)
        except (KeyError, TypeError, ValueError) as err:
            # A damaged or outdated file is no worse than none: the pricing is fetched again.
            LOGGER.warning("Ignoring the saved pricing, which can't be read: %s", err)
            return None

    async def async_save(self, snapshot: PricingSnapshot) -> None:
        """Save a snapshot."""
        await self._store.async_save(_snapshot_dict(snapshot))

    async def async_remove(self) -> None:
        """Delete the saved snapshot."""
        await self._store.async_remove()


def _snapshot_dict(snapshot: PricingSnapshot) -> dict[str, Any]:
    return {
        "icp": _icp_dict(snapshot.icp),
        "plans": {
            "default": _plan_dict(snapshot.plans.default),
            "alternative": (
                _plan_dict(snapshot.plans.alternative) if snapshot.plans.alternative else None
            ),
            "others": [_plan_dict(plan) for plan in snapshot.plans.others],
        },
        "fetched_at": snapshot.fetched_at.isoformat(),
        "plan_titles": dict(snapshot.plan_titles),
        "charts": {path: _chart_dict(chart) for path, chart in snapshot.charts.items()},
        "charts_plan_id": snapshot.charts_plan_id,
        "missing_chart_fetches": snapshot.missing_chart_fetches,
    }


def _snapshot(raw: dict[str, Any]) -> PricingSnapshot:
    plans = raw["plans"]
    fetched_at = datetime.fromisoformat(raw["fetched_at"])
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at has no time zone")
    return PricingSnapshot(
        icp=_icp(raw["icp"]),
        plans=PlanList(
            default=_plan(plans["default"]),
            alternative=_plan(plans["alternative"]) if plans["alternative"] else None,
            others=tuple(_plan(plan) for plan in plans["others"]),
        ),
        fetched_at=fetched_at,
        plan_titles={str(key): str(value) for key, value in raw["plan_titles"].items()},
        charts={str(path): _chart(chart) for path, chart in raw["charts"].items()},
        charts_plan_id=raw["charts_plan_id"],
        # Files saved before the count was kept don't have it.
        missing_chart_fetches=int(raw.get("missing_chart_fetches", 0)),
    )


def _date_str(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value is not None else None


def _icp_dict(icp: IcpDetails) -> dict[str, Any]:
    return {
        "icp": icp.icp,
        "network_code": icp.network_code,
        "network_name": icp.network_name,
        "price_category": icp.price_category,
        "price_category_description": icp.price_category_description,
        "retailer_code": icp.retailer_code,
        "retailer_name": icp.retailer_name,
        # Not used by any entity, so not written to disk. Loaded back as blank.
        "address": "",
        "region": "",
        "meter_type": icp.meter_type,
        "channels": [
            {
                "serial_number": channel.serial_number,
                "register_code": channel.register_code,
                "channel_number": channel.channel_number,
                "energy_flow": channel.energy_flow,
                "settlement": channel.settlement,
            }
            for channel in icp.channels
        ],
        "smart_meter": icp.smart_meter,
        "generation_capacity_kw": icp.generation_capacity_kw,
        "certification_expiry": _date_str(icp.certification_expiry),
        "loss_factor_consumption": icp.loss_factor_consumption,
        "loss_factor_generation": icp.loss_factor_generation,
    }


def _icp(raw: dict[str, Any]) -> IcpDetails:
    return IcpDetails(
        icp=raw["icp"],
        network_code=raw["network_code"],
        network_name=raw["network_name"],
        price_category=raw["price_category"],
        price_category_description=raw["price_category_description"],
        retailer_code=raw["retailer_code"],
        retailer_name=raw["retailer_name"],
        address=raw["address"],
        region=raw["region"],
        meter_type=raw["meter_type"],
        channels=tuple(
            MeterChannel(
                serial_number=channel["serial_number"],
                register_code=channel["register_code"],
                channel_number=channel["channel_number"],
                energy_flow=channel["energy_flow"],
                settlement=channel["settlement"],
            )
            for channel in raw["channels"]
        ),
        smart_meter=raw["smart_meter"],
        generation_capacity_kw=raw["generation_capacity_kw"],
        certification_expiry=_date(raw["certification_expiry"]),
        loss_factor_consumption=raw["loss_factor_consumption"],
        loss_factor_generation=raw["loss_factor_generation"],
    )


def _rate_dict(rate: Rate) -> dict[str, Any]:
    return {
        "id": rate.id,
        "description": rate.description,
        "charge": rate.charge,
        "register_type": rate.register_type,
        "unit": rate.unit.value,
        "raw_unit": rate.raw_unit,
        "price": rate.price,
        "is_network": rate.is_network,
        "season": rate.season,
        "valid_start": _date_str(rate.valid_start),
        "valid_end": _date_str(rate.valid_end),
    }


def _rate(raw: dict[str, Any]) -> Rate:
    return Rate(
        id=raw["id"],
        description=raw["description"],
        charge=raw["charge"],
        register_type=raw["register_type"],
        unit=RateUnit(raw["unit"]),
        raw_unit=raw["raw_unit"],
        price=float(raw["price"]),
        is_network=raw["is_network"],
        season=raw["season"],
        valid_start=_date(raw["valid_start"]),
        valid_end=_date(raw["valid_end"]),
    )


def _plan_dict(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "code": plan.code,
        "description": plan.description,
        "network": plan.network,
        "price_category": plan.price_category,
        "customer_type": plan.customer_type,
        "standard_low": plan.standard_low,
        "retail_rates": [_rate_dict(rate) for rate in plan.retail_rates],
        "network_rates": [_rate_dict(rate) for rate in plan.network_rates],
        "seasons": [
            {"name": season.name, "start": season.start.isoformat(), "end": season.end.isoformat()}
            for season in plan.seasons
        ],
        "timeframe_chart_weekday": plan.timeframe_chart_weekday,
        "timeframe_chart_weekend": plan.timeframe_chart_weekend,
        "title": plan.title,
    }


def _plan(raw: dict[str, Any]) -> Plan:
    return Plan(
        id=raw["id"],
        code=raw["code"],
        description=raw["description"],
        network=raw["network"],
        price_category=raw["price_category"],
        customer_type=raw["customer_type"],
        standard_low=raw["standard_low"],
        retail_rates=tuple(_rate(rate) for rate in raw["retail_rates"]),
        network_rates=tuple(_rate(rate) for rate in raw["network_rates"]),
        seasons=tuple(
            Season(
                name=season["name"],
                start=date.fromisoformat(season["start"]),
                end=date.fromisoformat(season["end"]),
            )
            for season in raw["seasons"]
        ),
        timeframe_chart_weekday=raw["timeframe_chart_weekday"],
        timeframe_chart_weekend=raw["timeframe_chart_weekend"],
        title=raw["title"],
    )


def _entry_dict(entry: LegendEntry) -> list[Any]:
    return [entry.start, entry.end, entry.text, entry.clipped, entry.estimate]


def _entry(raw: list[Any]) -> LegendEntry:
    start, end, text, clipped, estimate = raw
    return LegendEntry(
        start=int(start),
        end=int(end),
        text=str(text),
        clipped=bool(clipped),
        estimate=None if estimate is None else float(estimate),
    )


def _chart_dict(chart: ChartReading) -> dict[str, Any]:
    return {
        "import": [_entry_dict(entry) for entry in chart.import_entries],
        "export": [_entry_dict(entry) for entry in chart.export_entries],
    }


def _chart(raw: dict[str, Any]) -> ChartReading:
    return ChartReading(
        import_entries=tuple(_entry(entry) for entry in raw["import"]),
        export_entries=tuple(_entry(entry) for entry in raw["export"]),
    )

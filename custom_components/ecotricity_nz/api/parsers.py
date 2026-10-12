"""Parsers that turn raw Ecotricity responses into models."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from .exceptions import EcotricityResponseError, IcpNotFoundError
from .models import (
    ConsumptionData,
    IcpDetails,
    IntervalReading,
    MeterChannel,
    Plan,
    PlanList,
    PortalIndicator,
    Rate,
    RateUnit,
    Season,
    normalise_plan_name,
)

FEATURE_IMPORT = "POWER_ACTIVE_PURCHASE_CONSUMPTION"
FEATURE_EXPORT = "POWER_ACTIVE_SUPPLY_CONSUMPTION"
FEATURE_CARBON = "EMISSION|POWER_ACTIVE_PURCHASE_CONSUMPTION|Carbon Savings"
# The length assumed for a reading whose end time can't be placed.
READING_LENGTH = timedelta(minutes=30)


def _as_list(value: Any) -> list[Any]:
    """Return the value as a list. The ICP API collapses one-item lists into objects."""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _content(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise EcotricityResponseError("Response was not a JSON object")
    return_code = payload.get("returnCode")
    if isinstance(return_code, dict) and return_code.get("ID") not in (0, "0", None):
        raise EcotricityResponseError(f"Service returned error: {return_code.get('name')}")
    return payload.get("content")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_icp(payload: Any) -> IcpDetails:
    """Parse a RESTICP/energy response."""
    try:
        content = _content(payload)
    except EcotricityResponseError as err:
        raise IcpNotFoundError(str(err)) from err
    if not isinstance(content, dict) or not isinstance(content.get("network"), dict):
        raise IcpNotFoundError("ICP not found")
    try:
        network = content["network"]
        pricing = content.get("pricing") or {}
        categories = _as_list((pricing.get("allPriceCategoryCodes") or {}).get("PriceCategoryCode"))
        category = next(
            (
                cat
                for cat in categories
                if str(cat.get("isAllocatedToIcp")).lower() == "true"
                and str(cat.get("isActive")).lower() == "true"
            ),
            categories[0] if categories else {},
        )
        retailer = content.get("retailer") or {}
        metering = content.get("metering") or {}
        channels: list[MeterChannel] = []
        meter_type: str | None = None
        smart_meter: bool | None = None
        for component in _as_list(
            (metering.get("allMeteringComponents") or {}).get("MeteringComponent")
        ):
            meter_type = meter_type or component.get("meterType") or None
            if (flag := component.get("amiFlag")) not in (None, ""):
                smart_meter = bool(smart_meter) or str(flag).lower() == "true"
            channels.extend(
                MeterChannel(
                    serial_number=str(channel.get("vMeteringComponentSerialNumber", "")),
                    register_code=str(channel.get("vRegisterContentCode", "")),
                    channel_number=str(channel.get("channelNumber", "")),
                    energy_flow=str(channel.get("energyFlowDirection", "")),
                    settlement=str(channel.get("settlementIndicator")).lower() == "true",
                )
                for channel in _as_list(
                    (component.get("allMeteringChannels") or {}).get("MeteringChannel")
                )
            )
        my_network = network.get("myNetwork") or {}
        loss_factor = pricing.get("myLossFactorCode") or {}
        return IcpDetails(
            icp=str(network["icpNumber"]),
            network_code=str(network.get("networkIdentifier") or my_network.get("code", "")),
            network_name=str(my_network.get("description", "")),
            price_category=str(category.get("code", "")),
            price_category_description=str(category.get("description", "")),
            retailer_code=str(retailer.get("code", "")),
            retailer_name=str(retailer.get("description", "")),
            address=_format_address(content.get("address") or {}),
            region=str((content.get("address") or {}).get("vRegion", "")),
            meter_type=meter_type,
            channels=tuple(channels),
            smart_meter=smart_meter,
            generation_capacity_kw=_parse_float(network.get("generationCapacityKW")),
            certification_expiry=_parse_registry_date(metering.get("certificationExpiryDate")),
            loss_factor_consumption=_parse_float(loss_factor.get("lossFactorConsumption")),
            loss_factor_generation=_parse_float(loss_factor.get("lossFactorGeneration")),
        )
    except (KeyError, TypeError, AttributeError) as err:
        raise EcotricityResponseError(f"Unexpected ICP response: {err!r}") from err


def _parse_float(value: Any) -> float | None:
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _parse_registry_date(value: Any) -> date | None:
    """Parse a registry date. The registry uses 1900-01-01 and 9999-12-31 for "not set"."""
    parsed = _parse_date(value)
    if parsed is None or parsed.year in (1900, 9999):
        return None
    return parsed


def _format_address(address: dict[str, Any]) -> str:
    unit = str(address.get("unit") or "").strip()
    number = str(address.get("number") or "").strip()
    street = str(address.get("street") or "").strip().title()
    first = " ".join(part for part in (f"{unit}/" if unit else "", number, street) if part)
    parts = [
        first.replace("/ ", "/"),
        str(address.get("suburb") or "").strip().title(),
        " ".join(
            part
            for part in (
                str(address.get("town") or "").strip().title(),
                str(address.get("postCode") or "").strip(),
            )
            if part
        ),
    ]
    return ", ".join(part for part in parts if part)


def _rate_unit(raw: str) -> RateUnit:
    lowered = raw.strip().lower()
    if lowered.endswith("cents/kwh"):
        return RateUnit.PER_KWH
    if lowered.endswith("cents/d"):
        return RateUnit.PER_DAY
    return RateUnit.OTHER


def _parse_rate(raw: dict[str, Any], *, network: bool) -> Rate:
    unit = str(raw.get("unit") or "")
    return Rate(
        id=str(raw.get("id", "")),
        description=str(raw.get("charge_description") or raw.get("rate_description") or ""),
        charge=raw.get("charge") or None,
        register_type=raw.get("register_type") or None,
        unit=_rate_unit(unit),
        raw_unit=unit,
        price=float(raw["price"]),
        is_network=network,
        season=raw.get("season") or None,
        valid_start=_parse_date(raw.get("valid_start")),
        valid_end=_parse_date(raw.get("valid_end")),
    )


def _parse_plan(raw: dict[str, Any]) -> Plan:
    seasons = tuple(
        Season(name=str(item.get("season", "")), start=start, end=end)
        for item in _as_list(raw.get("seasons"))
        if (start := _parse_date(item.get("start"))) and (end := _parse_date(item.get("end")))
    )
    return Plan(
        id=str(raw["id"]),
        code=str(raw.get("price_plan", "")),
        description=str(raw.get("price_plan_description", "")).strip(),
        network=str(raw.get("network", "")),
        price_category=str(raw.get("price_category", "")),
        customer_type=str(raw.get("customer_type", "")),
        standard_low=raw.get("standard_low") or None,
        # The web app treats any rate carrying a priceplan_id key as a network charge.
        retail_rates=tuple(
            _parse_rate(rate, network="priceplan_id" in rate)
            for rate in _as_list(raw.get("retail_rates"))
        ),
        network_rates=tuple(
            _parse_rate(rate, network=True) for rate in _as_list(raw.get("network_rates"))
        ),
        seasons=seasons,
        timeframe_chart_weekday=raw.get("timeframes_chart_wd") or None,
        timeframe_chart_weekend=raw.get("timeframes_chart_we") or None,
    )


def parse_plan_titles(payload: Any) -> dict[str, str]:
    """Parse the onboarding site's planmappings.json into internal name -> customer title.

    Keys are normalised internal plan descriptions (for example "lowbattery user").
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("mappings"), list):
        raise EcotricityResponseError("Plan mappings response has no mappings")
    titles: dict[str, str] = {}
    for mapping in payload["mappings"]:
        if not isinstance(mapping, dict):
            continue
        name = str(mapping.get("name") or "").strip()
        title = str(mapping.get("title") or "").strip()
        if name and title and name != "default":
            titles[normalise_plan_name(name)] = title
    return titles


def parse_plan_list(payload: Any) -> PlanList:
    """Parse a RESTPLAN/plan/list_unbundle response."""
    content = _content(payload)
    try:
        first = _as_list(content)[0]
        default = first.get("default")
        if not default:
            raise EcotricityResponseError("No default plan in response")
        alternative = first.get("alternative")
        return PlanList(
            default=_parse_plan(default),
            alternative=_parse_plan(alternative) if alternative else None,
            others=tuple(_parse_plan(plan) for plan in _as_list(first.get("plan_list"))),
        )
    except (IndexError, KeyError, TypeError, ValueError, AttributeError) as err:
        raise EcotricityResponseError(f"Unexpected plan response: {err!r}") from err


def localise_wall_time(
    naive: datetime, tz: tzinfo, after: datetime | None = None
) -> datetime | None:
    """Attach a timezone to a wall-clock time and return it in UTC.

    Ambiguous times (the hour repeated when daylight saving ends) take whichever fold keeps the
    result after `after`, so a sequence of readings keeps moving forward. Times that don't exist
    (skipped when daylight saving starts) return None.
    """
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        as_utc = aware.astimezone(UTC)
        if as_utc.astimezone(tz).replace(tzinfo=None) != naive:
            return None
        if after is None or as_utc > after:
            return as_utc
    return None


def portal_wall_time(text: str) -> datetime:
    """Return the NZ wall-clock time from a portal timestamp.

    The portal writes NZ local times but labels them with its server's Europe/Berlin offset, so
    the offset is meaningless and is dropped.
    """
    return datetime.fromisoformat(text).replace(tzinfo=None)


def _range_bounds(value: Any) -> tuple[str, str] | None:
    """Return the (from, to) pair from a portal {"from": "to"} range object."""
    if isinstance(value, dict) and len(value) == 1:
        start, end = next(iter(value.items()))
        return str(start), str(end)
    return None


def _localise_range(
    bounds: tuple[str, str], tz: tzinfo, after: datetime | None = None
) -> tuple[datetime, datetime] | None:
    """Return a portal range as UTC instants, or None if its start doesn't exist locally.

    The portal works out end times with wall-clock arithmetic, so the reading before the hour
    skipped when daylight saving starts ends at a time that never happened (01:30 to 02:00), and
    the one before the repeated hour when it ends goes backwards (02:30 to 02:00). The end is
    placed after the start, and when it can't be it's taken as the same wall-clock length on from
    the start.
    """
    naive_start = portal_wall_time(bounds[0])
    naive_end = portal_wall_time(bounds[1])
    start = localise_wall_time(naive_start, tz, after)
    if start is None:
        return None
    end = localise_wall_time(naive_end, tz, start)
    if end is None:
        length = naive_end - naive_start
        end = start + (length if length > timedelta(0) else READING_LENGTH)
    return start, end


def parse_indicator(payload: Any, tz: tzinfo) -> PortalIndicator:
    """Parse a portal analysis/initData response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("currentIndicator"), dict):
        raise EcotricityResponseError("initData response has no current indicator")
    indicator = payload["currentIndicator"]
    indicator_id = str(indicator.get("indicatorID") or "")
    if not indicator_id:
        raise EcotricityResponseError("initData indicator has no ID")

    starts: list[datetime] = []
    ends: list[datetime] = []
    features: set[str] = set()
    for feature, entries in (indicator.get("features") or {}).items():
        for entry in entries if isinstance(entries, list) else []:
            bounds = _range_bounds(entry.get("measuredValueRange"))
            if bounds is None:
                continue
            features.add(str(feature))
            if (localised := _localise_range(bounds, tz)) is not None:
                starts.append(localised[0])
                ends.append(localised[1])

    last_import: datetime | None = None
    if text := indicator.get("lastImportDate"):
        last_import = localise_wall_time(portal_wall_time(str(text)), tz)

    return PortalIndicator(
        indicator_id=indicator_id,
        display_name=str(indicator.get("displayName") or ""),
        last_import=last_import,
        data_start=min(starts, default=None),
        data_end=max(ends, default=None),
        features=frozenset(features),
    )


def _parse_feature_values(values: Any, tz: tzinfo) -> tuple[IntervalReading, ...]:
    readings: list[IntervalReading] = []
    last_start: datetime | None = None
    for item in values if isinstance(values, list) else []:
        bounds = _range_bounds(item.get("measuringPeriod"))
        value = item.get("value")
        if bounds is None or not isinstance(value, int | float):
            continue
        localised = _localise_range(bounds, tz, last_start)
        if localised is None:
            continue
        start, end = localised
        readings.append(IntervalReading(start=start, end=end, value=float(value)))
        last_start = start
    return tuple(readings)


def parse_values(payload: Any, tz: tzinfo) -> ConsumptionData:
    """Parse a portal analysis/valueRequest response into UTC half-hourly readings."""
    if not isinstance(payload, dict):
        raise EcotricityResponseError("valueRequest response was not an object")
    series: dict[str, tuple[IntervalReading, ...]] = {}
    for indicator in payload.get("indicatorValueResponses") or []:
        for feature in indicator.get("featureValueResponses") or []:
            series[str(feature.get("featureID"))] = _parse_feature_values(feature.get("values"), tz)
    return ConsumptionData(
        imported_kwh=series.get(FEATURE_IMPORT, ()),
        exported_kwh=series.get(FEATURE_EXPORT, ()),
        carbon_savings_kg=series.get(FEATURE_CARBON, ()),
    )

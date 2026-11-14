"""Websocket commands for the integration's dashboard card.

The bill card (frontend/) reads the cost statistics over the range a dashboard's date picker
sets. `ecotricity_nz/bill` tells it which statistics make up each line of the bill, and what the
lines are called, so the card needs nothing about the plan of its own. The layout comes from the
saved pricing, and makes no requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.components import websocket_api
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.websocket_api.const import ERR_INVALID_FORMAT, ERR_NOT_FOUND
from homeassistant.components.websocket_api.decorators import async_response, websocket_command
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .api.models import RateUnit
from .const import CONF_ICP, DOMAIN, GST_FACTOR
from .costs import (
    LINE_ENERGY_SUFFIX,
    TOTAL_ELECTRICITY_COST,
    charged_days,
    daily_key,
    levy_key,
)
from .statistics import cost_series, statistic_id
from .tariff import (
    ComponentKind,
    DailyCharge,
    bucket_name,
    bucket_names,
    bucket_period,
    component_key,
    daily_charge_key,
    line_key,
    rate_component,
)

if TYPE_CHECKING:
    from homeassistant.components.websocket_api.connection import ActiveConnection

    from .api import Plan, Rate
    from .coordinator import EcotricityConfigEntry

ERR_NOT_READY = "not_ready"
# Dropped from a daily charge's key, as the cost statistics do: "metering_rate" is "metering".
_RATE_SUFFIX = "_rate"


class BillSection(StrEnum):
    """The parts of an Ecotricity bill, in the order it lists them."""

    NETWORK = "network"
    """The network's charges, passed through: its daily charge and its per-kWh rates."""
    ENERGY = "energy"
    """Ecotricity's per-kWh rates."""
    OTHER = "other"
    """Levies and Ecotricity's other daily charges, such as metering."""


class LineKind(StrEnum):
    """What a line of the bill charges for."""

    DAILY = "daily"
    """A daily charge, by `part`: network, metering, supplier, or another item's key."""
    USAGE = "usage"
    """A per-kWh rate, by `part` (import or export) and `period`."""
    LEVY = "levy"
    """A levy on every kWh imported, by `part`: ea_levy, or another levy's key."""


@dataclass(frozen=True, slots=True)
class BillLine:
    """One line of the bill, and the statistics it's worked out from.

    A line is described by what it is, in the integration's own terms, rather than by the plan's
    descriptions, which differ between networks ("Uncontrolled - Off Peak", "Off Peak Usage").
    The card names it from these; `name` is for what has no name of its own.
    """

    section: BillSection
    key: str
    """The cost statistic's key."""
    kind: LineKind
    part: str
    """For a daily charge or levy, which one; for usage, the side, "import" or "export"."""
    period: str | None
    """For usage, the period's key, like the period sensors' states: "off_peak", "shoulder"."""
    name: str
    """The period's name for usage, like the price sensors'; otherwise the rate's description."""
    usage: str | None
    """The key of the kWh it's charged on, or None for a daily charge."""
    losses: bool
    """Whether it's charged on the kWh plus line losses: Ecotricity's import and export are."""
    order: int
    """Daily charges first, then import, then export, as the bill lists them."""


@callback
def async_setup_websocket(hass: HomeAssistant) -> None:
    """Register the integration's websocket commands."""
    websocket_api.async_register_command(hass, ws_bill)
    websocket_api.async_register_command(hass, ws_bill_days)
    websocket_api.async_register_command(hass, ws_icps)


def bill_lines(plan: Plan) -> list[BillLine]:
    """Return every line the plan can put on a bill, by section and in the bill's order.

    A plan lists each season's rates, and two rates can share a line (one bucket), so each line
    is described by its most recent rate.
    """
    names = bucket_names(plan)
    latest: dict[str, tuple[str, BillLine]] = {}
    for rate in plan.rates:
        if not rate.price or rate.is_controlled:
            continue
        line = _line(rate, names)
        if line is None:
            continue
        start = rate.valid_start.isoformat() if rate.valid_start else ""
        if line.key not in latest or start > latest[line.key][0]:
            latest[line.key] = (start, line)
    sections = list(BillSection)
    return sorted(
        (line for _start, line in latest.values()),
        key=lambda line: (sections.index(line.section), line.order),
    )


def _line(rate: Rate, names: dict[str, str]) -> BillLine | None:
    """Return the line a rate is charged on."""
    if rate.unit is RateUnit.PER_DAY:
        section = BillSection.NETWORK if rate.is_network else BillSection.OTHER
        charge = DailyCharge(rate.description, rate.price, is_network=rate.is_network)
        return BillLine(
            section,
            daily_key(charge),
            LineKind.DAILY,
            part=daily_charge_key(charge).removesuffix(_RATE_SUFFIX),
            period=None,
            name=rate.description,
            usage=None,
            losses=False,
            order=0,
        )
    if rate.unit is not RateUnit.PER_KWH:
        return None
    part = rate_component(rate)
    if part.kind is ComponentKind.LEVY:
        if rate.is_export:
            return None
        return BillLine(
            BillSection.OTHER,
            levy_key(part),
            LineKind.LEVY,
            part=component_key(part),
            period=None,
            name=rate.description,
            usage=levy_key(part) + LINE_ENERGY_SUFFIX,
            losses=False,
            order=1,
        )
    key = line_key(part, export=rate.is_export)
    if key is None:  # pragma: no cover - only levies have no line
        return None
    section = BillSection.NETWORK if part.kind is ComponentKind.NETWORK else BillSection.ENERGY
    return BillLine(
        section,
        key,
        LineKind.USAGE,
        part="export" if rate.is_export else "import",
        period=bucket_period(part.bucket, names),
        name=bucket_name(part.bucket, names),
        usage=key + LINE_ENERGY_SUFFIX,
        losses=part.kind is ComponentKind.SUPPLIER,
        order=2 if rate.is_export else 1,
    )


@websocket_command(
    {
        probatio.Required("type"): f"{DOMAIN}/bill",
        probatio.Optional(ATTR_CONFIG_ENTRY_ID): str,
        probatio.Optional(CONF_ICP): str,
    }
)
@callback
def ws_bill(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return the bill's lines and the statistics each is worked out from.

    With no config entry given, the only loaded one is used. Like a bill, lines exclude GST and
    GST is added at the end, on the total with export credits taken off, whatever the entry's
    options for its sensors: `gst_rate` is the rate charged.
    """
    entry = _entry_for(hass, connection, msg)
    if entry is None:
        return
    pricing = entry.runtime_data.pricing.cost_pricing()
    if pricing is None:
        connection.send_error(msg["id"], ERR_NOT_READY, "Pricing hasn't been fetched yet")
        return
    plan, icp = pricing

    def stat(key: str) -> str:
        return statistic_id(icp.icp, cost_series(key))

    sections: dict[BillSection, list[dict[str, Any]]] = {}
    for line in bill_lines(plan):
        usage = None if line.usage is None else stat(line.usage)
        sections.setdefault(line.section, []).append(
            {
                "key": line.key,
                "kind": line.kind,
                "part": line.part,
                "period": line.period,
                "name": line.name,
                "statistic_id": stat(line.key),
                "usage_statistic_id": usage,
                "losses": line.losses,
            }
        )
    connection.send_result(
        msg["id"],
        {
            "icp": icp.icp,
            "network_name": icp.network_name,
            "loss_factor": icp.loss_factor_consumption or 1.0,
            "export_loss_factor": icp.loss_factor_generation or 1.0,
            "sections": [
                {"section": section, "lines": lines} for section, lines in sections.items()
            ],
            "gst_rate": round(GST_FACTOR - 1, 4),
        },
    )


@websocket_command(
    {
        probatio.Required("type"): f"{DOMAIN}/bill_days",
        probatio.Optional(ATTR_CONFIG_ENTRY_ID): str,
        probatio.Optional(CONF_ICP): str,
        probatio.Required("start_time"): str,
        probatio.Required("end_time"): str,
    }
)
@async_response
async def ws_bill_days(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return how many days of daily charges a range's statistics hold.

    Daily charges are only charged for days whose usage has been imported, so a range reaching
    past the latest import, or into a gap, has fewer days than it spans. Each priced hour carries
    its share of its NZ day (see `charged_days`), the same way the cost statistics spread them.
    The total cost has a row for every priced hour, so its hours are the ones charged.
    """
    entry = _entry_for(hass, connection, msg)
    if entry is None:
        return
    start = dt_util.parse_datetime(msg["start_time"])
    end = dt_util.parse_datetime(msg["end_time"])
    if start is None or end is None:
        connection.send_error(msg["id"], ERR_INVALID_FORMAT, "Invalid start or end time")
        return
    stat_id = statistic_id(entry.data[CONF_ICP], cost_series(TOTAL_ELECTRICITY_COST))
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {stat_id}, "hour", None, {"change"}
    )
    hours = [datetime.fromtimestamp(row["start"], UTC) for row in rows.get(stat_id, [])]
    days = charged_days(hours, entry.runtime_data.local_tz)
    connection.send_result(msg["id"], {"days": days})


@websocket_command({probatio.Required("type"): f"{DOMAIN}/icps"})
@callback
def ws_icps(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return the ICPs set up, for the bill card's editor to choose from."""
    connection.send_result(
        msg["id"],
        {
            "icps": [
                {"icp": entry.data[CONF_ICP], "title": entry.title}
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
            ]
        },
    )


def _entry_for(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> EcotricityConfigEntry | None:
    """Return the loaded entry a command asks for, or send an error saying why there isn't one.

    It's chosen by ICP (in any case) or config entry ID; with neither, the only loaded entry is
    used, so a card only needs one when more than one ICP is set up.
    """
    entry_id: str | None = msg.get(ATTR_CONFIG_ENTRY_ID)
    icp: str | None = msg.get(CONF_ICP)
    loaded: list[EcotricityConfigEntry] = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and (entry_id is None or entry.entry_id == entry_id)
        and (icp is None or str(entry.data[CONF_ICP]).upper() == icp.strip().upper())
    ]
    if len(loaded) == 1:
        return loaded[0]
    if loaded:
        message = "More than one ICP is set up: choose one with the card's icp option"
    elif icp is not None or entry_id is not None:
        message = "No loaded Ecotricity NZ entry matches the card's icp or config_entry_id"
    else:
        message = "No Ecotricity NZ entry is loaded"
    connection.send_error(msg["id"], ERR_NOT_FOUND, message)
    return None

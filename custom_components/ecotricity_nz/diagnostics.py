"""Diagnostics for Ecotricity NZ."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .const import CONF_ACCOUNT_NUMBER, CONF_ICP, CONF_PASSWORD

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import EcotricityConfigEntry

TO_REDACT = {
    CONF_ACCOUNT_NUMBER,
    CONF_PASSWORD,
    CONF_ICP,
    "address",
    "serial_number",
    "indicator_id",
    "display_name",
    "title",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001 - signature set by Home Assistant
    entry: EcotricityConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    pricing = runtime.pricing.data
    consumption = runtime.consumption.data if runtime.consumption else None
    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "pricing": {
                "icp": asdict(pricing.icp),
                "plan": {
                    "id": pricing.plan.id,
                    "code": pricing.plan.code,
                    "description": pricing.plan.description,
                    "title": pricing.plan.title,
                    "seasons": [asdict(season) for season in pricing.plan.seasons],
                },
                "available_plans": [plan.name for plan in pricing.plans.all_plans],
                "day": pricing.day,
                "season": asdict(pricing.season) if pricing.season else None,
                "pricing": asdict(pricing.pricing),
                "import_slots": asdict(pricing.import_slots) if pricing.import_slots else None,
                "export_slots": asdict(pricing.export_slots) if pricing.export_slots else None,
                "tou_error": pricing.tou_error,
            },
            "consumption": asdict(consumption) if consumption else None,
            "consumption_last_update_success": (
                runtime.consumption.last_update_success if runtime.consumption else None
            ),
        },
        TO_REDACT,
    )

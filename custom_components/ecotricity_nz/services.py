"""Actions for the Ecotricity NZ integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import service
from homeassistant.helpers.selector import ConfigEntrySelector

from .const import DOMAIN, SERVICE_REFRESH

if TYPE_CHECKING:
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from .coordinator import EcotricityConfigEntry

REFRESH_SCHEMA = probatio.Schema(
    {probatio.Optional(ATTR_CONFIG_ENTRY_ID): ConfigEntrySelector({"integration": DOMAIN})}
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""

    async def handle_refresh(call: ServiceCall) -> None:
        """Fetch pricing and usage now instead of waiting for the next scheduled update.

        With no config entry given, the only loaded one is used.
        """
        entry: EcotricityConfigEntry = service.async_get_config_entry(
            hass, DOMAIN, call.data.get(ATTR_CONFIG_ENTRY_ID)
        )
        runtime = entry.runtime_data
        coordinators: list[DataUpdateCoordinator[Any]] = [runtime.pricing]
        if runtime.consumption is not None:
            coordinators.append(runtime.consumption)
        failed = False
        for coordinator in coordinators:
            await coordinator.async_refresh()
            failed |= not coordinator.last_update_success
        if failed:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="refresh_failed")

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, handle_refresh, REFRESH_SCHEMA)

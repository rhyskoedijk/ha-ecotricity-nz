"""The Ecotricity NZ integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.const import __version__ as HA_VERSION  # noqa: N812 - Home Assistant convention
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .api import OnboardingClient, PortalClient
from .card import async_register_card
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_PASSWORD,
    DOMAIN,
    LOCAL_TIME_ZONE,
    PORTAL_SERVER_TIME_ZONE,
)
from .coordinator import (
    ConsumptionCoordinator,
    EcotricityConfigEntry,
    EcotricityRuntimeData,
    PricingCoordinator,
)
from .services import async_setup_services
from .store import CostStore, PricingStore
from .websocket_api import async_setup_websocket

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

PLATFORMS: list[Platform] = [Platform.IMAGE, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:  # noqa: ARG001 - signature set by Home Assistant
    """Register the integration's actions and the bill card, with its websocket command.

    They're registered here so they exist even while no entry is loaded.
    """
    async_setup_services(hass)
    async_setup_websocket(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EcotricityConfigEntry) -> bool:
    """Set up Ecotricity NZ from a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    user_agent = f"HomeAssistant/{HA_VERSION} {DOMAIN}/{integration.version}"
    local_tz = await dt_util.async_get_time_zone(LOCAL_TIME_ZONE)
    server_tz = await dt_util.async_get_time_zone(PORTAL_SERVER_TIME_ZONE)
    if local_tz is None or server_tz is None:  # pragma: no cover - tzdata ships with HA
        return False

    onboarding = OnboardingClient(async_get_clientsession(hass), user_agent=user_agent)
    portal: PortalClient | None = None
    if entry.data.get(CONF_ACCOUNT_NUMBER) and entry.data.get(CONF_PASSWORD):
        portal = PortalClient(
            # The portal keeps its login in a cookie, so it needs its own cookie jar.
            async_create_clientsession(hass),
            entry.data[CONF_ACCOUNT_NUMBER],
            entry.data[CONF_PASSWORD],
            user_agent=user_agent,
            local_tz=local_tz,
            server_tz=server_tz,
        )

    pricing = PricingCoordinator(
        hass, entry, onboarding, local_tz, PricingStore(hass, entry.entry_id)
    )
    await pricing.async_config_entry_first_refresh()
    pricing.async_start_half_hour_updates()

    consumption = (
        ConsumptionCoordinator(hass, entry, portal, local_tz, pricing)
        if portal is not None
        else None
    )
    entry.runtime_data = EcotricityRuntimeData(
        pricing=pricing, consumption=consumption, local_tz=local_tz
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_after_start() -> None:
        # Pricing first, so the import prices usage with whatever it fetches.
        await pricing.async_fetch_deferred()
        if consumption is not None:
            await consumption.async_refresh()

    @callback
    def _async_started(_hass: HomeAssistant) -> None:
        # A fetch can take a while, and a first import can backfill years of history, so
        # neither holds up setup or Home Assistant's startup.
        entry.async_create_background_task(
            hass, _async_after_start(), f"{DOMAIN} pricing fetch and consumption import"
        )

    entry.async_on_unload(async_at_started(hass, _async_started))
    return True


async def async_remove_entry(hass: HomeAssistant, entry: EcotricityConfigEntry) -> None:
    """Delete the saved pricing, and where the costs start, when the entry is removed."""
    await PricingStore(hass, entry.entry_id).async_remove()
    await CostStore(hass, entry.entry_id).async_remove()


async def async_unload_entry(hass: HomeAssistant, entry: EcotricityConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.pricing.async_shutdown()
        if entry.runtime_data.consumption is not None:
            await entry.runtime_data.consumption.async_shutdown()
    return unloaded

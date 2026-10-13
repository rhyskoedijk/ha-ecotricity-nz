"""Tests for the integration's actions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecotricity_nz.api import EcotricityConnectionError
from custom_components.ecotricity_nz.const import DOMAIN, SERVICE_REFRESH
from tests.conftest import WINTER_PEAK


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_refresh_fetches_pricing_and_usage(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, portal_config_entry)
    icp_calls = mock_onboarding.async_get_icp.await_count
    indicator_calls = mock_portal.async_get_indicator.await_count

    # With one ICP the config entry can be left out.
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH, blocking=True)

    assert mock_onboarding.async_get_icp.await_count == icp_calls + 1
    assert mock_portal.async_get_indicator.await_count == indicator_calls + 1


async def test_refresh_a_chosen_entry_without_a_portal_login(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    calls = mock_onboarding.async_get_icp.await_count

    await hass.services.async_call(
        DOMAIN, SERVICE_REFRESH, {ATTR_CONFIG_ENTRY_ID: config_entry.entry_id}, blocking=True
    )

    assert mock_onboarding.async_get_icp.await_count == calls + 1


@pytest.mark.usefixtures("mock_onboarding")
async def test_refresh_reports_a_failed_update(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, portal_config_entry)
    mock_portal.async_get_indicator.side_effect = EcotricityConnectionError("down")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, SERVICE_REFRESH, blocking=True)
    assert err.value.translation_key == "refresh_failed"


async def test_refresh_reports_a_failed_pricing_fetch(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    mock_onboarding.async_get_icp.side_effect = EcotricityConnectionError("down")

    # The saved pricing keeps the sensors going, but the action still says the fetch failed.
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, SERVICE_REFRESH, blocking=True)
    assert err.value.translation_key == "refresh_failed"
    assert config_entry.runtime_data.pricing.last_update_success


async def test_refresh_needs_a_loaded_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    # The action is registered when the integration loads, before any entry is set up.
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id) is False

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_REFRESH, {ATTR_CONFIG_ENTRY_ID: config_entry.entry_id}, blocking=True
        )

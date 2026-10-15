"""Tests for diagnostics."""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from tests.conftest import (
    DEFAULT_NETWORK,
    TEST_ICP,
    WINTER_PEAK,
)


@pytest.mark.usefixtures("mock_onboarding", "mock_portal")
# Freeze before fixtures run, so the test client's auth token is issued in the frozen time.
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_diagnostics_redact_personal_data(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    portal_config_entry: MockConfigEntry,
) -> None:
    portal_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(portal_config_entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, portal_config_entry)
    text = json.dumps(diagnostics, default=str)

    assert diagnostics["pricing"]["plan"]["description"] == "Low User"
    assert diagnostics["pricing"]["import_slots"] is not None
    # Registry details are kept; only the identifying fields inside them are redacted.
    details = diagnostics["pricing"]["icp_details"]
    assert details["network_code"] == DEFAULT_NETWORK
    assert details["icp"] == "**REDACTED**"
    assert details["address"] == "**REDACTED**"
    for secret in (TEST_ICP, "123456", "secret", "Example Road", "900000001", "100001"):
        assert secret not in text

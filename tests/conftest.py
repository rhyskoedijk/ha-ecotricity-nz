"""Shared fixtures for the Ecotricity NZ tests."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecotricity_nz.api import EcotricityConnectionError, PortalIndicator
from custom_components.ecotricity_nz.api.parsers import (
    parse_icp,
    parse_plan_list,
    parse_plan_titles,
    parse_values,
)
from custom_components.ecotricity_nz.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_ICP,
    CONF_INCLUDE_GST,
    CONF_PASSWORD,
    CONF_PLAN_ID,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHARTS = FIXTURES / "charts"
TEST_ICP = "0000123456AB1C2"
NZ = ZoneInfo("Pacific/Auckland")
# Plan ids in plan_list.json.
LOW_USER_PLAN_ID = "3001"
LOW_OFF_PEAK_PLAN_ID = "3003"


def load_fixture(name: str) -> str:
    """Return a fixture file's text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_fixture(name: str) -> Any:
    """Return a parsed JSON fixture."""
    return json.loads(load_fixture(name))


@pytest.fixture(autouse=True)
def auto_setup(recorder_mock: Any, enable_custom_integrations: None) -> None:
    """Run every test with a recorder (a dependency) and the integration loadable.

    `recorder_mock` must be resolved before `hass`, so it comes first here.
    """


@pytest.fixture(autouse=True)
def no_backfill_pause() -> Generator[None]:
    """Skip the pause between backfill requests."""
    with patch("custom_components.ecotricity_nz.coordinator.BACKFILL_PAUSE_SECONDS", 0):
        yield


def _chart_bytes(path: str) -> bytes:
    """Serve fixture charts. Weekend charts aren't captured, so the weekday chart stands in."""
    chart = CHARTS / path.rsplit("/", 1)[-1].replace("-WE-", "-WD-")
    if not chart.exists():
        raise EcotricityConnectionError(f"Chart {path} returned HTTP 404")
    return chart.read_bytes()


@pytest.fixture
def mock_onboarding() -> Generator[MagicMock]:
    """Patch the onboarding client everywhere it's created."""
    client = MagicMock()
    client.async_get_icp = AsyncMock(return_value=parse_icp(load_json_fixture("icp.json")))
    client.async_get_plans = AsyncMock(
        return_value=parse_plan_list(load_json_fixture("plan_list.json"))
    )
    client.async_get_chart = AsyncMock(side_effect=_chart_bytes)
    client.async_get_plan_titles = AsyncMock(
        return_value=parse_plan_titles(load_json_fixture("plan_mappings.json"))
    )
    with (
        patch("custom_components.ecotricity_nz.OnboardingClient", return_value=client),
        patch("custom_components.ecotricity_nz.config_flow.OnboardingClient", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_portal() -> Generator[MagicMock]:
    """Patch the portal client everywhere it's created."""
    client = MagicMock()
    client.async_login = AsyncMock()
    client.async_get_indicator = AsyncMock(
        return_value=PortalIndicator(
            indicator_id="ind",
            display_name=TEST_ICP,
            last_import=datetime(2026, 4, 6, 3, 6, tzinfo=NZ),
            data_start=datetime(2026, 4, 5, 0, 0, tzinfo=NZ),
            data_end=datetime(2026, 4, 6, 0, 0, tzinfo=NZ),
        )
    )
    values = parse_values(load_json_fixture("portal_values_dst_end.json"), NZ)

    async def half_hourly(start: datetime, end: datetime) -> Any:
        return type(values)(
            imported_kwh=tuple(r for r in values.imported_kwh if start <= r.start < end),
            exported_kwh=tuple(r for r in values.exported_kwh if start <= r.start < end),
            carbon_savings_kg=tuple(r for r in values.carbon_savings_kg if start <= r.start < end),
        )

    client.async_get_half_hourly = AsyncMock(side_effect=half_hourly)
    with (
        patch("custom_components.ecotricity_nz.PortalClient", return_value=client),
        patch("custom_components.ecotricity_nz.config_flow.PortalClient", return_value=client),
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A config entry without portal credentials, on the Low Off Peak plan."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Ecotricity {TEST_ICP}",
        unique_id=TEST_ICP,
        data={CONF_ICP: TEST_ICP},
        options={CONF_INCLUDE_GST: True, CONF_PLAN_ID: LOW_OFF_PEAK_PLAN_ID},
    )


@pytest.fixture
def portal_config_entry() -> MockConfigEntry:
    """A config entry with portal credentials."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Ecotricity {TEST_ICP}",
        unique_id=TEST_ICP,
        data={CONF_ICP: TEST_ICP, CONF_ACCOUNT_NUMBER: "123456", CONF_PASSWORD: "secret"},
        options={CONF_INCLUDE_GST: True, CONF_PLAN_ID: LOW_USER_PLAN_ID},
    )


# Monday 15 June 2026, 8:15am NZST: winter peak on the fixture network.
WINTER_PEAK = datetime(2026, 6, 14, 20, 15, tzinfo=UTC)

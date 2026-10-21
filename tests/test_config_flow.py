"""Tests for the config and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecotricity_nz.api import (
    AuthError,
    EcotricityConnectionError,
    IcpNotFoundError,
)
from custom_components.ecotricity_nz.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_ICP,
    CONF_INCLUDE_GST,
    CONF_PASSWORD,
    CONF_PLAN_ID,
    DOMAIN,
)
from tests.conftest import ANYTIME_PLAN, TEST_ICP, plan_id

pytestmark = pytest.mark.usefixtures("mock_onboarding", "mock_portal", "mock_setup_entry")


@pytest.fixture
def mock_setup_entry() -> Generator[MagicMock]:
    """Don't set the entry up after the flow creates it."""
    with patch("custom_components.ecotricity_nz.async_setup_entry", return_value=True) as setup:
        yield setup


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return str(result["flow_id"])


async def test_user_without_credentials_picks_plan(hass: HomeAssistant) -> None:
    flow_id = await _start(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ICP: " 0000123456ab1c2 "}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "plan"
    selector = result["data_schema"].schema[CONF_PLAN_ID]
    labels = {option["label"] for option in selector.config["options"]}
    assert {"Low ecoSOLAR", "Low ecoANYTIME", "ecoSAVER"} <= labels

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PLAN_ID: plan_id(ANYTIME_PLAN)}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ICP: TEST_ICP}
    assert result["options"] == {CONF_INCLUDE_GST: True, CONF_PLAN_ID: plan_id(ANYTIME_PLAN)}
    assert result["result"].unique_id == TEST_ICP


async def test_user_with_credentials(hass: HomeAssistant, mock_portal: MagicMock) -> None:
    flow_id = await _start(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ICP: TEST_ICP, CONF_ACCOUNT_NUMBER: " 123456 ", CONF_PASSWORD: "secret"}
    )
    # The portal can't say which plan the ICP is on, so the plan is always asked for.
    assert result["step_id"] == "plan"
    mock_portal.async_login.assert_awaited_once()

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PLAN_ID: plan_id(ANYTIME_PLAN)}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ICP: TEST_ICP,
        CONF_ACCOUNT_NUMBER: "123456",
        CONF_PASSWORD: "secret",
    }
    assert result["options"][CONF_PLAN_ID] == plan_id(ANYTIME_PLAN)


@pytest.mark.parametrize(
    ("user_input", "error_key", "error"),
    [
        ({CONF_ICP: "not-an-icp"}, CONF_ICP, "invalid_icp"),
        ({CONF_ICP: TEST_ICP, CONF_ACCOUNT_NUMBER: "123"}, "base", "credentials_incomplete"),
        ({CONF_ICP: TEST_ICP, CONF_PASSWORD: "x"}, "base", "credentials_incomplete"),
    ],
)
async def test_user_input_errors(
    hass: HomeAssistant, user_input: dict[str, str], error_key: str, error: str
) -> None:
    flow_id = await _start(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {error_key: error}


@pytest.mark.parametrize(
    ("side_effect", "error_key", "error"),
    [
        (IcpNotFoundError("missing"), CONF_ICP, "icp_not_found"),
        (EcotricityConnectionError("down"), "base", "cannot_connect"),
    ],
)
async def test_onboarding_errors_recover(
    hass: HomeAssistant,
    mock_onboarding: MagicMock,
    side_effect: Exception,
    error_key: str,
    error: str,
) -> None:
    icp = mock_onboarding.async_get_icp.return_value
    mock_onboarding.async_get_icp.side_effect = side_effect
    flow_id = await _start(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_ICP: TEST_ICP})
    assert result["errors"] == {error_key: error}

    mock_onboarding.async_get_icp.side_effect = None
    mock_onboarding.async_get_icp.return_value = icp
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_ICP: TEST_ICP})
    assert result["step_id"] == "plan"


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [(AuthError("no"), "invalid_auth"), (EcotricityConnectionError("down"), "cannot_connect")],
)
async def test_portal_errors(
    hass: HomeAssistant, mock_portal: MagicMock, side_effect: Exception, error: str
) -> None:
    mock_portal.async_login.side_effect = side_effect
    flow_id = await _start(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ICP: TEST_ICP, CONF_ACCOUNT_NUMBER: "123456", CONF_PASSWORD: "bad"}
    )

    assert result["errors"] == {"base": error}


@pytest.mark.parametrize("side_effect", [None, AuthError("no"), EcotricityConnectionError("down")])
async def test_portal_session_released(
    hass: HomeAssistant, mock_portal: MagicMock, side_effect: Exception | None
) -> None:
    """Each credential check releases its session instead of keeping it until shutdown."""
    mock_portal.async_login.side_effect = side_effect
    flow_id = await _start(hass)

    with patch(
        "custom_components.ecotricity_nz.config_flow.async_create_clientsession"
    ) as create_session:
        await hass.config_entries.flow.async_configure(
            flow_id, {CONF_ICP: TEST_ICP, CONF_ACCOUNT_NUMBER: "123456", CONF_PASSWORD: "pw"}
        )

    create_session.assert_called_once_with(hass, auto_cleanup=False)
    create_session.return_value.detach.assert_called_once_with()


async def test_already_configured(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    flow_id = await _start(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_ICP: TEST_ICP})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, portal_config_entry: MockConfigEntry, mock_portal: MagicMock
) -> None:
    portal_config_entry.add_to_hass(hass)
    result = await portal_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_portal.async_login.side_effect = AuthError("no")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_NUMBER: "123456", CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_portal.async_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_NUMBER: "123456", CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert portal_config_entry.data[CONF_PASSWORD] == "new"


async def test_reauth_requires_credentials(
    hass: HomeAssistant, portal_config_entry: MockConfigEntry
) -> None:
    portal_config_entry.add_to_hass(hass)
    result = await portal_config_entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_NUMBER: " ", CONF_PASSWORD: ""}
    )

    assert result["errors"] == {"base": "credentials_required"}


async def test_reconfigure_removes_credentials(
    hass: HomeAssistant, portal_config_entry: MockConfigEntry
) -> None:
    portal_config_entry.add_to_hass(hass)
    result = await portal_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert portal_config_entry.data == {CONF_ICP: TEST_ICP}


async def test_reconfigure_error(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_portal: MagicMock
) -> None:
    config_entry.add_to_hass(hass)
    mock_portal.async_login.side_effect = AuthError("no")
    result = await config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_NUMBER: "123456", CONF_PASSWORD: "bad"}
    )

    assert result["errors"] == {"base": "invalid_auth"}

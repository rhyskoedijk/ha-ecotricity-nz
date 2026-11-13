"""Tests for the customer portal client."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.ecotricity_nz.api import (
    AuthError,
    EcotricityConnectionError,
    EcotricityResponseError,
    PortalClient,
)
from custom_components.ecotricity_nz.api.portal import LOGIN_RETRY_DELAY
from tests.conftest import load_json_fixture

BASE = "https://portal.test"
PORTAL = f"{BASE}/powercommerce/eco/fo/portal/"
JSON = {"Content-Type": "application/json;charset=UTF-8"}
NZ = ZoneInfo("Pacific/Auckland")
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def mock_sleep() -> Iterator[AsyncMock]:
    """Skip the wait before a rejected login is tried again."""
    with patch("custom_components.ecotricity_nz.api.portal.asyncio.sleep") as sleep:
        yield sleep


def _client(hass: HomeAssistant) -> PortalClient:
    return PortalClient(
        async_get_clientsession(hass),
        " 123456 ",
        "secret",
        user_agent="test",
        local_tz=NZ,
        server_tz=BERLIN,
        base_url=BASE,
    )


def _mock_login(aioclient_mock: AiohttpClientMocker, target: str = "home") -> None:
    aioclient_mock.get(PORTAL, text="<html></html>")
    aioclient_mock.post(
        f"{PORTAL}loginProcess",
        status=HTTPStatus.FOUND,
        headers={"Location": f"http://portal.test/powercommerce/eco/fo/portal/{target}"},
    )


async def test_login_success(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_sleep: AsyncMock
) -> None:
    _mock_login(aioclient_mock)

    await _client(hass).async_login()

    mock_sleep.assert_not_awaited()

    _, _, data, _ = aioclient_mock.mock_calls[1]
    assert data == {"login": "123456", "password": "secret"}


def _login_attempts(aioclient_mock: AiohttpClientMocker) -> int:
    return sum(call[1].path.endswith("loginProcess") for call in aioclient_mock.mock_calls)


async def test_login_rejected_twice_is_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_sleep: AsyncMock
) -> None:
    _mock_login(aioclient_mock, target="start")

    with pytest.raises(AuthError):
        await _client(hass).async_login()
    assert _login_attempts(aioclient_mock) == 2
    mock_sleep.assert_awaited_once_with(LOGIN_RETRY_DELAY)


async def test_login_rejected_once_is_tried_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_sleep: AsyncMock
) -> None:
    """The portal can briefly send a correct login back to the start page."""
    targets = iter(["start", "home"])

    async def login(method: str, url: URL, data: object) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(
            method,
            url,
            status=HTTPStatus.FOUND,
            headers={"Location": f"http://portal.test/powercommerce/eco/fo/portal/{next(targets)}"},
        )

    aioclient_mock.get(PORTAL, text="<html></html>")
    aioclient_mock.post(f"{PORTAL}loginProcess", side_effect=login)

    await _client(hass).async_login()

    assert _login_attempts(aioclient_mock) == 2
    mock_sleep.assert_awaited_once_with(LOGIN_RETRY_DELAY)
    # The second attempt starts a new session from the start page.
    paths = [call[1].path.rsplit("/", 1)[-1] for call in aioclient_mock.mock_calls]
    assert paths == ["", "loginProcess", "", "loginProcess"]


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (HTTPStatus.SERVICE_UNAVAILABLE, {}),
        (HTTPStatus.INTERNAL_SERVER_ERROR, {"Location": "http://portal.test/x/start"}),
        (HTTPStatus.OK, {}),
        (HTTPStatus.FOUND, {}),
        (HTTPStatus.FOUND, {"Location": "http://portal.test/x/maintenance"}),
    ],
)
async def test_login_unexpected_response_is_not_auth_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: HTTPStatus,
    headers: dict[str, str],
) -> None:
    """Only a redirect back to the start page means the credentials were wrong."""
    aioclient_mock.get(PORTAL, text="<html></html>")
    aioclient_mock.post(f"{PORTAL}loginProcess", status=status, headers=headers)

    with pytest.raises(EcotricityConnectionError) as err:
        await _client(hass).async_login()
    assert not isinstance(err.value, AuthError)


async def test_login_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(PORTAL, exc=TimeoutError())

    with pytest.raises(EcotricityConnectionError):
        await _client(hass).async_login()


async def test_calls_log_in_first(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    _mock_analysis(aioclient_mock)

    indicator = await _client(hass).async_get_indicator()

    assert indicator.indicator_id == "00000000-0000-0000-0000-000000000001"
    paths = [call[1].path.rsplit("/", 1)[-1] for call in aioclient_mock.mock_calls]
    assert paths == ["", "loginProcess", "consumptionDetails", "initData"]


def _mock_analysis(aioclient_mock: AiohttpClientMocker) -> None:
    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{PORTAL}consumptionDetails", text="<html>consumption</html>")
    aioclient_mock.post(
        f"{PORTAL}analysis/initData",
        json=load_json_fixture("ecotricity/portal_init_data.json"),
        headers=JSON,
    )
    aioclient_mock.post(
        f"{PORTAL}analysis/valueRequest",
        json=load_json_fixture("ecotricity/portal_values_dst_end.json"),
        headers=JSON,
    )


async def test_consumption_page_reopened_after_login(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_analysis(aioclient_mock)
    client = _client(hass)

    await client.async_get_indicator()
    await client.async_login()
    await client.async_get_indicator()

    pages = [
        call for call in aioclient_mock.mock_calls if call[1].path.endswith("consumptionDetails")
    ]
    assert len(pages) == 2


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": HTTPStatus.SERVICE_UNAVAILABLE}, EcotricityConnectionError),
        ({"exc": TimeoutError()}, EcotricityConnectionError),
        # Still redirected after logging in again: an outage, not bad credentials.
        ({"status": HTTPStatus.FOUND}, EcotricityConnectionError),
    ],
)
async def test_consumption_page_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock: dict[str, object],
    error: type[Exception],
) -> None:
    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{PORTAL}consumptionDetails", **mock)

    with pytest.raises(error):
        await _client(hass).async_get_indicator()


async def test_error_is_logged_without_its_body(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{PORTAL}consumptionDetails", text="<html></html>")
    aioclient_mock.post(
        f"{PORTAL}analysis/initData",
        status=HTTPStatus.SERVICE_UNAVAILABLE,
        text="Account 123456 is temporarily unavailable",
    )
    caplog.set_level("DEBUG", logger="custom_components.ecotricity_nz.api.portal")

    with pytest.raises(EcotricityConnectionError):
        await _client(hass).async_get_indicator()
    # The status is logged, but not the page, which can show the customer's account.
    assert "analysis/initData returned HTTP 503" in caplog.text
    assert "123456" not in caplog.text


async def test_half_hourly_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_analysis(aioclient_mock)
    client = _client(hass)

    data = await client.async_get_half_hourly(
        datetime(2026, 4, 4, 11, 0, tzinfo=UTC), datetime(2026, 4, 5, 12, 0, tzinfo=UTC)
    )

    assert len(data.imported_kwh) == 50
    # The consumption page is opened once, before the first analysis call, like the web app.
    paths = [call[1].path.rsplit("/", 1)[-1] for call in aioclient_mock.mock_calls]
    assert paths[2:] == ["consumptionDetails", "initData", "valueRequest"]
    _, _, body, headers = aioclient_mock.mock_calls[-1]
    request = body["indicatorValueRequests"][0]
    assert request["indicatorID"] == "00000000-0000-0000-0000-000000000001"
    # NZ midnight, written with the Berlin offset like the web app does.
    assert request["valueRange"] == {
        "2026-04-05T00:00:00.000+02:00": "2026-04-06T00:00:00.000+02:00"
    }
    assert {feature["granularityType"] for feature in request["featureValueRequests"]} == {
        "HALF_HOUR"
    }
    assert headers["X-Requested-With"] == "XMLHttpRequest"

    # The indicator is cached for later requests.
    calls = aioclient_mock.call_count
    await client.async_get_half_hourly(
        datetime(2026, 4, 4, 11, 0, tzinfo=UTC), datetime(2026, 4, 5, 12, 0, tzinfo=UTC)
    )
    assert aioclient_mock.call_count == calls + 1


async def test_expired_session_logs_in_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    responses = iter(
        [
            AiohttpClientMockResponse("post", URL(PORTAL), text="<html>login</html>"),
            AiohttpClientMockResponse(
                "post",
                URL(PORTAL),
                json=load_json_fixture("ecotricity/portal_init_data.json"),
                headers=JSON,
            ),
        ]
    )

    async def init_data(method: str, url: URL, data: object) -> AiohttpClientMockResponse:
        return next(responses)

    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{PORTAL}consumptionDetails", text="<html></html>")
    aioclient_mock.post(f"{PORTAL}analysis/initData", side_effect=init_data)

    indicator = await _client(hass).async_get_indicator()
    assert indicator.indicator_id == "00000000-0000-0000-0000-000000000001"
    logins = [call for call in aioclient_mock.mock_calls if call[1].path.endswith("loginProcess")]
    assert len(logins) == 2


@pytest.mark.parametrize(
    ("page", "init_data"),
    [
        (
            {"text": "<html></html>"},
            {
                "status": HTTPStatus.FOUND,
                "headers": {"Location": "/powercommerce/eco/fo/portal/start"},
            },
        ),
        ({"text": "<html></html>"}, {"text": "<html>maintenance</html>"}),
    ],
    ids=["redirect", "html"],
)
async def test_session_cannot_be_restored(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    page: dict[str, object],
    init_data: dict[str, object],
) -> None:
    """A refusal straight after a successful login is an outage, not bad credentials."""
    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{PORTAL}consumptionDetails", **page)
    aioclient_mock.post(f"{PORTAL}analysis/initData", **init_data)

    with pytest.raises(EcotricityConnectionError) as err:
        await _client(hass).async_get_indicator()
    assert not isinstance(err.value, AuthError)
    logins = [call for call in aioclient_mock.mock_calls if call[1].path.endswith("loginProcess")]
    assert len(logins) == 2


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": HTTPStatus.INTERNAL_SERVER_ERROR}, EcotricityConnectionError),
        ({"exc": TimeoutError()}, EcotricityConnectionError),
        ({"text": "{not json", "headers": JSON}, EcotricityResponseError),
    ],
)
async def test_call_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock: dict[str, object],
    error: type[Exception],
) -> None:
    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{PORTAL}consumptionDetails", text="<html></html>")
    aioclient_mock.post(f"{PORTAL}analysis/initData", **mock)

    with pytest.raises(error):
        await _client(hass).async_get_indicator()

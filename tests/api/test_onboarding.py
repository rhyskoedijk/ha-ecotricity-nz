"""Tests for the onboarding API client."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.ecotricity_nz.api import (
    AttestationError,
    EcotricityConnectionError,
    EcotricityResponseError,
    OnboardingClient,
)
from custom_components.ecotricity_nz.api.attestation import RETRY_HEADER, TOKEN_HEADER
from tests.conftest import DEFAULT_NETWORK, TEST_ICP, load_json_fixture, load_network_fixture

BASE = "https://onboarding.test"
ICP_URL = f"{BASE}/cgi-bin/RESTICP/energy/{TEST_ICP}"
PLAN_URL = f"{BASE}/cgi-bin/RESTPLAN/plan/list_unbundle/"
CHALLENGE = {
    "cid": "c",
    "expiry": 1,
    "sig": "s",
    "params": {"container_width": 10, "items": [5, 5, 5]},
}


def _mock_attestation(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/cgi-bin/RESTATTEST/challenge", json=CHALLENGE)
    aioclient_mock.post(
        f"{BASE}/cgi-bin/RESTATTEST/token", json={"token": "tok", "expires": 9_999_999_999}
    )


def _client(hass: HomeAssistant) -> OnboardingClient:
    return OnboardingClient(async_get_clientsession(hass), user_agent="test", base_url=BASE)


async def test_get_icp_and_plans(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    _mock_attestation(aioclient_mock)
    aioclient_mock.get(ICP_URL, json=load_network_fixture(DEFAULT_NETWORK, "icp.json"))
    aioclient_mock.get(PLAN_URL, json=load_network_fixture(DEFAULT_NETWORK, "plan_list.json"))
    client = _client(hass)

    icp = await client.async_get_icp(TEST_ICP.lower())
    plans = await client.async_get_plans(icp)

    assert icp.icp == TEST_ICP
    assert plans.default.description == "Eco Off Peak"
    _, url, _, headers = aioclient_mock.mock_calls[-1]
    assert url.query["meterType"] == "UN,EG,7304,7304"
    assert url.query["capacityCode"] == "RTOU24"
    assert url.query["networkCode"] == "CKHK"
    assert headers[TOKEN_HEADER] == "tok"
    # One token covers both calls.
    assert aioclient_mock.call_count == 4


async def test_forbidden_is_retried_with_fresh_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    statuses = iter([HTTPStatus.FORBIDDEN, HTTPStatus.OK])

    async def icp_response(method: str, url: URL, data: object) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(
            method,
            url,
            status=next(statuses),
            json=load_network_fixture(DEFAULT_NETWORK, "icp.json"),
        )

    _mock_attestation(aioclient_mock)
    aioclient_mock.get(ICP_URL, side_effect=icp_response)

    assert (await _client(hass).async_get_icp(TEST_ICP)).icp == TEST_ICP
    _, _, _, headers = aioclient_mock.mock_calls[-1]
    assert headers[RETRY_HEADER] == "1"


async def test_repeated_forbidden_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_attestation(aioclient_mock)
    aioclient_mock.get(ICP_URL, status=HTTPStatus.FORBIDDEN)

    with pytest.raises(AttestationError):
        await _client(hass).async_get_icp(TEST_ICP)


async def test_get_chart(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/charts/a.png", content=b"\x89PNG")
    aioclient_mock.get(f"{BASE}/charts/missing.png", status=HTTPStatus.NOT_FOUND)
    aioclient_mock.get(f"{BASE}/charts/slow.png", exc=TimeoutError())
    client = _client(hass)

    assert await client.async_get_chart("charts/a.png") == b"\x89PNG"
    # Charts are static files: no attestation token is minted for them.
    assert all("RESTATTEST" not in str(call[1]) for call in aioclient_mock.mock_calls)
    with pytest.raises(EcotricityConnectionError):
        await client.async_get_chart("/charts/missing.png")
    with pytest.raises(EcotricityConnectionError):
        await client.async_get_chart("/charts/slow.png")


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": HTTPStatus.INTERNAL_SERVER_ERROR}, EcotricityConnectionError),
        ({"exc": TimeoutError()}, EcotricityConnectionError),
        ({"text": "<html>"}, EcotricityResponseError),
    ],
)
async def test_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock: dict[str, object],
    error: type[Exception],
) -> None:
    _mock_attestation(aioclient_mock)
    aioclient_mock.get(ICP_URL, **mock)

    with pytest.raises(error):
        await _client(hass).async_get_icp(TEST_ICP)


async def test_get_plan_titles(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(
        f"{BASE}/assets/planmappings.json", json=load_json_fixture("plan_mappings.json")
    )

    titles = await _client(hass).async_get_plan_titles()

    assert titles["low user"] == "Low ecoANYTIME"
    # A static file: no attestation token is minted for it.
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": HTTPStatus.NOT_FOUND}, EcotricityConnectionError),
        ({"exc": TimeoutError()}, EcotricityConnectionError),
        ({"text": "<html>"}, EcotricityResponseError),
    ],
)
async def test_get_plan_titles_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock: dict[str, object],
    error: type[Exception],
) -> None:
    aioclient_mock.get(f"{BASE}/assets/planmappings.json", **mock)

    with pytest.raises(error):
        await _client(hass).async_get_plan_titles()


async def test_errors_dont_reveal_the_icp(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    # Error messages reach Home Assistant's log, so they name the request, not its path.
    _mock_attestation(aioclient_mock)
    aioclient_mock.get(ICP_URL, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    with pytest.raises(EcotricityConnectionError) as err:
        await _client(hass).async_get_icp(TEST_ICP)
    assert TEST_ICP not in str(err.value)
    assert "ICP details" in str(err.value)

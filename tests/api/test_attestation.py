"""Tests for the onboarding attestation solver and token manager."""

from __future__ import annotations

import hashlib
from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.ecotricity_nz.api import AttestationError, AttestationTokenManager
from custom_components.ecotricity_nz.api.attestation import (
    MAX_CALLS_PER_TOKEN,
    first_wrap_index,
    solve_challenge,
)
from custom_components.ecotricity_nz.api.exceptions import EcotricityConnectionError

BASE = "https://onboarding.test"
CHALLENGE_URL = f"{BASE}/cgi-bin/RESTATTEST/challenge"
TOKEN_URL = f"{BASE}/cgi-bin/RESTATTEST/token"
CHALLENGE = {
    "cid": "abc123",
    "expiry": 1_800_000_000,
    "sig": "signature",
    "params": {"container_width": 100, "items": [40, 30, 20, 25, 10]},
}


@pytest.mark.parametrize(
    ("width", "items", "expected"),
    [
        (100, [40, 30, 20, 25, 10], 3),  # 40+30+20 = 90, adding 25 overflows
        (100, [50, 50, 1], 2),  # exactly full row, the next item wraps
        (100, [10, 10], 2),  # nothing wraps
        (10, [50, 5], 1),  # first item overflows on its own but still sits on row one
        (100, [100], 1),
    ],
)
def test_first_wrap_index(width: float, items: list[float], expected: int) -> None:
    assert first_wrap_index(width, items) == expected


def test_solve_challenge_hashes_cid_and_index() -> None:
    assert solve_challenge(CHALLENGE) == hashlib.sha256(b"abc123|3").hexdigest()


@pytest.mark.parametrize(
    "challenge",
    [
        {},
        {"cid": "x", "params": {"container_width": 10}},
        {"cid": "x", "params": {"container_width": "wide", "items": [1]}},
        {"cid": "x", "params": {"container_width": 10, "items": []}},
    ],
)
def test_solve_challenge_rejects_bad_shapes(challenge: dict[str, object]) -> None:
    with pytest.raises(AttestationError):
        solve_challenge(challenge)


class Clock:
    """A controllable clock."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _manager(hass: HomeAssistant, clock: Clock) -> AttestationTokenManager:
    return AttestationTokenManager(async_get_clientsession(hass), BASE, {}, clock=clock)


async def test_token_is_minted_and_cached(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(CHALLENGE_URL, json=CHALLENGE)
    aioclient_mock.post(TOKEN_URL, json={"token": "tok", "expires": 2_000})
    manager = _manager(hass, Clock(1_000))

    assert await manager.async_get_token() == "tok"
    assert await manager.async_get_token() == "tok"
    assert aioclient_mock.call_count == 2

    _, _, body, _ = aioclient_mock.mock_calls[1]
    assert body["answer"] == solve_challenge(CHALLENGE)
    assert body["cid"] == "abc123"


async def test_token_refreshes_near_expiry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(CHALLENGE_URL, json=CHALLENGE)
    aioclient_mock.post(TOKEN_URL, json={"token": "tok", "expires": 2_000})
    clock = Clock(1_000)
    manager = _manager(hass, clock)

    await manager.async_get_token()
    clock.now = 1_911  # inside the 90 second safety margin
    await manager.async_get_token()
    assert aioclient_mock.call_count == 4


async def test_token_refreshes_after_call_budget(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(CHALLENGE_URL, json=CHALLENGE)
    aioclient_mock.post(TOKEN_URL, json={"token": "tok", "expires": 2_000})
    manager = _manager(hass, Clock(1_000))

    await manager.async_get_token()
    for _ in range(MAX_CALLS_PER_TOKEN):
        manager.record_call()
    await manager.async_get_token()
    assert aioclient_mock.call_count == 4

    manager.invalidate()
    await manager.async_get_token()
    assert aioclient_mock.call_count == 6


async def test_rejected_answer_is_retried_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    responses = iter([HTTPStatus.FORBIDDEN, HTTPStatus.OK])

    async def token_response(method: str, url: URL, data: object) -> AiohttpClientMockResponse:
        status = next(responses)
        return AiohttpClientMockResponse(
            method, url, status=status, json={"token": "tok2", "expires": 2_000}
        )

    aioclient_mock.get(CHALLENGE_URL, json=CHALLENGE)
    aioclient_mock.post(TOKEN_URL, side_effect=token_response)

    assert await _manager(hass, Clock(1_000)).async_get_token() == "tok2"
    assert aioclient_mock.call_count == 4


async def test_repeated_rejection_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(CHALLENGE_URL, json=CHALLENGE)
    aioclient_mock.post(TOKEN_URL, status=HTTPStatus.FORBIDDEN)

    with pytest.raises(AttestationError):
        await _manager(hass, Clock(1_000)).async_get_token()
    assert aioclient_mock.call_count == 4


@pytest.mark.parametrize(
    ("challenge_status", "token_status"),
    [
        (HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.OK),
        (HTTPStatus.NOT_FOUND, HTTPStatus.OK),
        (HTTPStatus.OK, HTTPStatus.INTERNAL_SERVER_ERROR),
    ],
)
async def test_server_errors_are_not_attestation_failures(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    challenge_status: HTTPStatus,
    token_status: HTTPStatus,
) -> None:
    """A site outage mustn't count towards the attestation repair issue."""
    aioclient_mock.get(CHALLENGE_URL, status=challenge_status, json=CHALLENGE)
    aioclient_mock.post(TOKEN_URL, status=token_status, json={"token": "tok", "expires": 2_000})

    with pytest.raises(EcotricityConnectionError):
        await _manager(hass, Clock(1_000)).async_get_token()


@pytest.mark.parametrize(
    "token_json",
    [{"expires": 2_000}, {"token": "tok"}, {"token": "tok", "expires": "soon"}, ["tok"]],
)
async def test_bad_token_response(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, token_json: object
) -> None:
    aioclient_mock.get(CHALLENGE_URL, json=CHALLENGE)
    aioclient_mock.post(TOKEN_URL, json=token_json)

    with pytest.raises(AttestationError):
        await _manager(hass, Clock(1_000)).async_get_token()


async def test_bad_challenge_response(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(CHALLENGE_URL, json=["not", "an", "object"])

    with pytest.raises(AttestationError):
        await _manager(hass, Clock(1_000)).async_get_token()


async def test_invalid_json(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(CHALLENGE_URL, text="<html>")

    with pytest.raises(AttestationError):
        await _manager(hass, Clock(1_000)).async_get_token()


async def test_connection_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(CHALLENGE_URL, exc=TimeoutError())

    with pytest.raises(EcotricityConnectionError):
        await _manager(hass, Clock(1_000)).async_get_token()

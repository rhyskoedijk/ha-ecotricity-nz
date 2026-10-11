"""Attestation tokens for the Ecotricity onboarding API.

The onboarding site gates its ICP and plan endpoints behind a short-lived token. The web app gets
one by fetching a challenge, laying out a row of fixed-width boxes in a flex-wrapped container,
and hashing the index of the first box that wraps onto a second row. This module reproduces that
layout calculation without a browser.

See docs/api.md for the full flow.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp

from .exceptions import AttestationError, EcotricityConnectionError, EcotricityResponseError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

ATTEST_PATH = "/cgi-bin/RESTATTEST"
TOKEN_HEADER = "X-Onboarding-Token"  # noqa: S105 - a header name, not a secret
RETRY_HEADER = "X-Onboarding-Attestation-Retry"

# Matches the web app: refresh 90 seconds before expiry, and after 45 gated calls.
EXPIRY_MARGIN_SECONDS = 90
MAX_CALLS_PER_TOKEN = 45


class _ForbiddenError(AttestationError):
    """An attestation endpoint refused the request (HTTP 403)."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Attestation {path} returned HTTP {HTTPStatus.FORBIDDEN}")


def first_wrap_index(container_width: float, item_widths: list[float]) -> int:
    """Return the index of the first item that wraps onto a second flex row.

    Items have no margin, padding or border, so a row holds items until their combined width
    exceeds the container. The first item always sits on the first row. When everything fits,
    the result is the number of items.
    """
    used = 0.0
    for index, width in enumerate(item_widths):
        if index > 0 and used + width > container_width:
            return index
        used += width
    return len(item_widths)


def solve_challenge(challenge: Mapping[str, Any]) -> str:
    """Return the hex SHA-256 answer for an attestation challenge."""
    try:
        cid = str(challenge["cid"])
        params = challenge["params"]
        width = float(params["container_width"])
        items = [float(item) for item in params["items"]]
    except (KeyError, TypeError, ValueError) as err:
        raise AttestationError(f"Unexpected challenge shape: {err!r}") from err
    if not items:
        raise AttestationError("Challenge has no items")
    index = first_wrap_index(width, items)
    return hashlib.sha256(f"{cid}|{index}".encode()).hexdigest()


class AttestationTokenManager:
    """Mints and caches onboarding attestation tokens."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        headers: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialise the manager."""
        self._session = session
        self._base = base_url.rstrip("/") + ATTEST_PATH
        self._headers = dict(headers)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._calls_used = 0

    def _cached(self) -> str | None:
        if self._token is None:
            return None
        if self._clock() >= self._expires_at - EXPIRY_MARGIN_SECONDS:
            return None
        if self._calls_used >= MAX_CALLS_PER_TOKEN:
            return None
        return self._token

    async def async_get_token(self) -> str:
        """Return a usable token, minting a new one when needed."""
        if (token := self._cached()) is not None:
            return token
        async with self._lock:
            if (token := self._cached()) is not None:
                return token
            return await self._async_mint(retry_on_forbidden=True)

    def record_call(self) -> None:
        """Count a gated call made with the current token."""
        self._calls_used += 1

    def invalidate(self) -> None:
        """Forget the current token, for example after a 403."""
        self._token = None
        self._expires_at = 0.0
        self._calls_used = 0

    async def _async_mint(self, *, retry_on_forbidden: bool) -> str:
        challenge = await self._async_request("GET", "/challenge")
        if not isinstance(challenge, dict):
            raise AttestationError("Challenge response was not an object")
        answer = solve_challenge(challenge)
        body = {
            "cid": challenge.get("cid"),
            "expiry": challenge.get("expiry"),
            "sig": challenge.get("sig"),
            "params": challenge.get("params"),
            "answer": answer,
        }
        try:
            result = await self._async_request("POST", "/token", body)
        except _ForbiddenError:
            # The web app retries once with a fresh challenge when the answer is rejected.
            if retry_on_forbidden:
                return await self._async_mint(retry_on_forbidden=False)
            raise
        if not isinstance(result, dict) or not result.get("token"):
            raise AttestationError("Token response did not include a token")
        try:
            expires_at = float(result["expires"])
        except (KeyError, TypeError, ValueError) as err:
            raise AttestationError("Token response did not include an expiry") from err
        self._token = str(result["token"])
        self._expires_at = expires_at
        self._calls_used = 0
        return self._token

    async def _async_request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:  # noqa: ANN401 - returns parsed JSON
        try:
            async with self._session.request(
                method,
                self._base + path,
                json=body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                # Only a 403 says the token scheme refused us; other errors mean the site is down.
                if response.status == HTTPStatus.FORBIDDEN:
                    raise _ForbiddenError(path)
                if response.status >= HTTPStatus.BAD_REQUEST:
                    raise EcotricityConnectionError(
                        f"Attestation {path} returned HTTP {response.status}"
                    )
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EcotricityConnectionError(f"Attestation {path} failed: {err}") from err
        except ValueError as err:
            # Most likely a maintenance page served with a 200, so it says nothing about tokens.
            raise EcotricityResponseError(f"Attestation {path} returned invalid JSON") from err

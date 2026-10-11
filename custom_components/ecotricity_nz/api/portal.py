"""Client for the Ecotricity customer portal (half-hourly consumption)."""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp
from yarl import URL

from .exceptions import AuthError, EcotricityConnectionError, EcotricityResponseError
from .parsers import (
    FEATURE_CARBON,
    FEATURE_EXPORT,
    FEATURE_IMPORT,
    parse_indicator,
    parse_values,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime, tzinfo

    from .models import ConsumptionData, PortalIndicator

PORTAL_URL = "https://portal.ecotricity.co.nz"
PORTAL_PATH = "/powercommerce/eco/fo/portal/"
LOGIN_SUCCESS_PATH = "home"
LOGIN_REJECTED_PATH = "start"
# Analysis calls need the consumption page to have been loaded in the session first.
ANALYSIS_PREFIX = "analysis/"
CONSUMPTION_PAGE = "consumptionDetails"
# How much of an error response body to include in debug logs.
ERROR_EXCERPT_LENGTH = 300
_LOGGER = logging.getLogger(__name__)
# The portal redirects to the start page, or refuses, once a session has expired.
_SESSION_EXPIRED_STATUSES = frozenset(
    {HTTPStatus.FOUND, HTTPStatus.SEE_OTHER, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
)


_REDIRECT_STATUSES = frozenset(
    {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    }
)


class _SessionExpiredError(Exception):
    """The portal no longer recognises the session."""


class PortalClient:
    """Log in to the customer portal and read account data.

    The client needs a session with its own cookie jar, because the portal keeps the login in a
    JSESSIONID cookie scoped to /powercommerce/eco/fo.

    Portal timestamps are NZ wall-clock times wrongly labelled with the server's Europe/Berlin
    offset. Responses are parsed with `local_tz`, and request ranges are sent the same way the web
    app sends them: NZ wall-clock time with the `server_tz` offset attached.
    """

    def __init__(  # noqa: PLR0913 - session, credentials and both time zones are required
        self,
        session: aiohttp.ClientSession,
        account_number: str,
        password: str,
        *,
        user_agent: str,
        local_tz: tzinfo,
        server_tz: tzinfo,
        base_url: str = PORTAL_URL,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._account_number = account_number.strip()
        self._password = password
        self._base = base_url.rstrip("/") + PORTAL_PATH
        self._origin = base_url.rstrip("/")
        self._headers = {"User-Agent": user_agent}
        self._local_tz = local_tz
        self._server_tz = server_tz
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._analysis_ready = False
        self._indicator_id: str | None = None

    async def async_login(self) -> None:
        """Log in, raising AuthError when the portal rejects the credentials."""
        async with self._lock:
            await self._async_login()

    async def async_get_indicator(self) -> PortalIndicator:
        """Return the metering indicator, including the range of data held and last import."""
        payload = await self._async_call(
            "POST",
            "analysis/initData",
            {
                "fetchCurrentIndicator": True,
                "fetchInstallation": False,
                "fetchLastImport": True,
                "fetchEnergyPerformanceIndicator": True,
                "fetchImsysFeatures": False,
            },
        )
        indicator = parse_indicator(payload, self._local_tz)
        self._indicator_id = indicator.indicator_id
        return indicator

    async def async_get_half_hourly(self, start: datetime, end: datetime) -> ConsumptionData:
        """Return half-hourly import, export and carbon readings between two instants."""
        if self._indicator_id is None:
            await self.async_get_indicator()
        request = {
            "dateType": "INDIVIDUAL",
            "userTimeZone": "Pacific/Auckland",
            "indicatorValueRequests": [
                {
                    "valueRange": {self._portal_time(start): self._portal_time(end)},
                    "indicatorID": self._indicator_id,
                    "indicatorType": "INDICATOR_GROUP",
                    "index": 0,
                    "featureValueRequests": [
                        {
                            "featureID": feature,
                            "granularityType": "HALF_HOUR",
                            "filterByIndicatorAttributesMap": {},
                            "aggregationType": "SUM",
                        }
                        for feature in (FEATURE_IMPORT, FEATURE_CARBON, FEATURE_EXPORT)
                    ],
                }
            ],
        }
        payload = await self._async_call("POST", "analysis/valueRequest", request)
        return parse_values(payload, self._local_tz)

    def _portal_time(self, instant: datetime) -> str:
        wall = instant.astimezone(self._local_tz).replace(tzinfo=None)
        return wall.replace(tzinfo=self._server_tz).isoformat(timespec="milliseconds")

    async def _async_login(self) -> None:
        self._session.cookie_jar.clear()
        self._logged_in = False
        self._analysis_ready = False
        self._indicator_id = None
        try:
            # Load the start page first so the portal issues a session cookie.
            async with self._session.get(
                self._base, headers=self._headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                await response.read()
            # A successful login redirects to /home, a failed one back to /start.
            async with self._session.post(
                self._base + "loginProcess",
                data={"login": self._account_number, "password": self._password},
                headers=self._headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                status = response.status
                location = response.headers.get("Location", "")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EcotricityConnectionError(f"Portal login failed: {err}") from err
        target = URL(location).path.rstrip("/").rsplit("/", 1)[-1]
        if status in _REDIRECT_STATUSES and target == LOGIN_REJECTED_PATH:
            raise AuthError("The portal rejected the account number or password")
        # Anything else, such as a maintenance page, says nothing about the credentials.
        if status not in _REDIRECT_STATUSES or target != LOGIN_SUCCESS_PATH:
            raise EcotricityConnectionError(
                f"Portal login returned HTTP {status} without the expected redirect"
            )
        self._logged_in = True

    async def _async_call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:  # noqa: ANN401 - returns parsed JSON
        """Call a JSON endpoint, logging in first and once more if the session has expired."""
        async with self._lock:
            if not self._logged_in:
                await self._async_login()
            try:
                return await self._async_session_call(method, path, body)
            except _SessionExpiredError:
                await self._async_login()
                try:
                    return await self._async_session_call(method, path, body)
                except _SessionExpiredError as err:
                    # The login just succeeded, so the credentials are fine; the portal is
                    # misbehaving rather than refusing us.
                    raise EcotricityConnectionError(
                        f"Portal {path} still refused the session after logging in again"
                    ) from err

    async def _async_session_call(
        self, method: str, path: str, body: Mapping[str, Any] | None
    ) -> Any:  # noqa: ANN401 - returns parsed JSON
        if path.startswith(ANALYSIS_PREFIX) and not self._analysis_ready:
            await self._async_open_consumption_page()
        return await self._async_json(method, path, body)

    async def _async_open_consumption_page(self) -> None:
        """Load the consumption page, as the web app does before any analysis call.

        The portal keeps the analysis widget's state in the server-side session, and that state
        is created when a page containing the widget is rendered. Analysis calls made before
        that are refused (HTTP 503).
        """
        try:
            async with self._session.get(
                self._base + CONSUMPTION_PAGE,
                headers=self._headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                await response.read()
                if response.status in _SESSION_EXPIRED_STATUSES:
                    raise _SessionExpiredError
                if response.status >= HTTPStatus.BAD_REQUEST:
                    raise EcotricityConnectionError(
                        f"Portal {CONSUMPTION_PAGE} returned HTTP {response.status}"
                    )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EcotricityConnectionError(f"Portal {CONSUMPTION_PAGE} failed: {err}") from err
        self._analysis_ready = True

    async def _async_json(self, method: str, path: str, body: Mapping[str, Any] | None) -> Any:  # noqa: ANN401 - returns parsed JSON
        headers = self._headers | {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self._origin,
            "Referer": self._base,
        }
        try:
            async with self._session.request(
                method,
                self._base + path,
                json=body,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if response.status in _SESSION_EXPIRED_STATUSES:
                    raise _SessionExpiredError
                if response.status >= HTTPStatus.BAD_REQUEST:
                    excerpt = (await response.text(errors="replace"))[:ERROR_EXCERPT_LENGTH]
                    _LOGGER.debug("Portal %s returned HTTP %s: %s", path, response.status, excerpt)
                    raise EcotricityConnectionError(
                        f"Portal {path} returned HTTP {response.status}"
                    )
                if "json" not in response.headers.get("Content-Type", ""):
                    # An expired session serves the HTML start page instead of JSON.
                    raise _SessionExpiredError
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EcotricityConnectionError(f"Portal {path} failed: {err}") from err
        except ValueError as err:
            raise EcotricityResponseError(f"Portal {path} returned invalid JSON") from err

"""Client for the public Ecotricity onboarding API (ICP details and plan pricing)."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp

from .attestation import RETRY_HEADER, TOKEN_HEADER, AttestationTokenManager
from .exceptions import AttestationError, EcotricityConnectionError, EcotricityResponseError
from .parsers import parse_icp, parse_plan_list, parse_plan_titles

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .models import IcpDetails, PlanList

ONBOARDING_URL = "https://onboarding.ecotricity.co.nz"
ICP_PATH = "/cgi-bin/RESTICP/energy/"
PLAN_PATH = "/cgi-bin/RESTPLAN/plan/list_unbundle/"
PLAN_MAPPINGS_PATH = "/assets/planmappings.json"
DEFAULT_CUSTOMER_TYPE = "Residential"


class OnboardingClient:
    """Fetch ICP details and plan pricing from the onboarding site."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        user_agent: str,
        base_url: str = ONBOARDING_URL,
        token_manager: AttestationTokenManager | None = None,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._base = base_url.rstrip("/")
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Origin": self._base,
            "Referer": f"{self._base}/",
        }
        self._tokens = token_manager or AttestationTokenManager(session, self._base, self._headers)

    async def async_get_icp(self, icp: str) -> IcpDetails:
        """Return registry details for an ICP."""
        payload = await self._async_gated_get(ICP_PATH + icp.strip().upper(), "ICP details")
        return parse_icp(payload)

    async def async_get_plans(
        self, icp: IcpDetails, customer_type: str = DEFAULT_CUSTOMER_TYPE
    ) -> PlanList:
        """Return the plans available to an ICP, built from its registry details."""
        params = {
            "networkCode": icp.network_code,
            "capacityCode": icp.price_category,
            "meterType": ",".join(icp.register_codes),
            "customerType": customer_type,
            "icpNumber": icp.icp,
        }
        payload = await self._async_gated_get(PLAN_PATH, "Plan list", params)
        return parse_plan_list(payload)

    async def async_get_plan_titles(self) -> dict[str, str]:
        """Return the customer-facing plan names, keyed by normalised internal description.

        This is a static file the web app loads to label its plan cards, so it needs no token.
        """
        try:
            async with self._session.get(
                self._base + PLAN_MAPPINGS_PATH,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status >= HTTPStatus.BAD_REQUEST:
                    raise EcotricityConnectionError(
                        f"{PLAN_MAPPINGS_PATH} returned HTTP {response.status}"
                    )
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EcotricityConnectionError(f"{PLAN_MAPPINGS_PATH} failed: {err}") from err
        except ValueError as err:
            raise EcotricityResponseError(f"{PLAN_MAPPINGS_PATH} returned invalid JSON") from err
        return parse_plan_titles(payload)

    async def async_get_chart(self, path: str) -> bytes:
        """Return a timeframe chart image. Charts are static files and need no token."""
        if not path.startswith("/"):
            path = "/" + path
        try:
            async with self._session.get(
                self._base + path,
                headers=self._headers | {"Accept": "image/png,image/*;q=0.8"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status >= HTTPStatus.BAD_REQUEST:
                    raise EcotricityConnectionError(f"Chart {path} returned HTTP {response.status}")
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EcotricityConnectionError(f"Chart {path} failed: {err}") from err

    async def _async_gated_get(
        self, path: str, label: str, params: Mapping[str, str] | None = None
    ) -> Any:  # noqa: ANN401 - returns parsed JSON
        """GET a token-gated endpoint.

        Errors name the request by `label`, not its path, because the ICP lookup's path contains
        the ICP number and error messages end up in Home Assistant's log.
        """
        token = await self._tokens.async_get_token()
        self._tokens.record_call()
        status, payload = await self._async_get(path, label, params, {TOKEN_HEADER: token})
        if status == HTTPStatus.FORBIDDEN:
            # Mirror the web app: one retry with a fresh token, flagged as a retry.
            self._tokens.invalidate()
            token = await self._tokens.async_get_token()
            self._tokens.record_call()
            status, payload = await self._async_get(
                path, label, params, {TOKEN_HEADER: token, RETRY_HEADER: "1"}
            )
            if status == HTTPStatus.FORBIDDEN:
                raise AttestationError(f"{label} request rejected the attestation token")
        if status >= HTTPStatus.BAD_REQUEST:
            raise EcotricityConnectionError(f"{label} request returned HTTP {status}")
        return payload

    async def _async_get(
        self,
        path: str,
        label: str,
        params: Mapping[str, str] | None,
        extra_headers: Mapping[str, str],
    ) -> tuple[int, Any]:
        try:
            async with self._session.get(
                self._base + path,
                params=params,
                headers=self._headers | dict(extra_headers),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status >= HTTPStatus.BAD_REQUEST:
                    return response.status, None
                return response.status, await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EcotricityConnectionError(f"{label} request failed: {err}") from err
        except ValueError as err:
            raise EcotricityResponseError(f"{label} request returned invalid JSON") from err

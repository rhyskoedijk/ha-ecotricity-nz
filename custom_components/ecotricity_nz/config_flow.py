"""Config flow for Ecotricity NZ."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import (
    AuthError,
    EcotricityError,
    IcpNotFoundError,
    OnboardingClient,
    PlanList,
    PortalClient,
)
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_ICP,
    CONF_INCLUDE_GST,
    CONF_PASSWORD,
    CONF_PLAN_ID,
    DEFAULT_INCLUDE_GST,
    DOMAIN,
    ICP_PATTERN,
    LOCAL_TIME_ZONE,
    LOGGER,
    PORTAL_SERVER_TIME_ZONE,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .coordinator import EcotricityConfigEntry

USER_AGENT = f"HomeAssistant {DOMAIN} config flow"
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


def _credentials_schema(defaults: Mapping[str, Any]) -> dict[probatio.Marker, Any]:
    return {
        probatio.Optional(
            CONF_ACCOUNT_NUMBER,
            description={"suggested_value": defaults.get(CONF_ACCOUNT_NUMBER)},
        ): str,
        probatio.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
    }


def _plan_selector(plans: PlanList) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=plan.id, label=plan.name) for plan in plans.all_plans],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


class EcotricityConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ecotricity NZ."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._data: dict[str, Any] = {}
        self._plans: PlanList | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: EcotricityConfigEntry,  # noqa: ARG004 - signature set by Home Assistant
    ) -> EcotricityOptionsFlow:
        """Return the options flow."""
        return EcotricityOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the ICP number and, optionally, portal credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            icp = user_input[CONF_ICP].replace(" ", "").upper()
            if not ICP_PATTERN.match(icp):
                errors[CONF_ICP] = "invalid_icp"
            else:
                await self.async_set_unique_id(icp)
                self._abort_if_unique_id_configured()
                credentials = _clean_credentials(user_input)
                errors = await self._async_validate(icp, credentials)
                if not errors:
                    self._data = {CONF_ICP: icp, **credentials}
                    return await self.async_step_plan()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(
                    {probatio.Required(CONF_ICP): str, **_credentials_schema(user_input or {})}
                ),
                {CONF_ICP: (user_input or {}).get(CONF_ICP)},
            ),
            errors=errors,
        )

    async def async_step_plan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which plan the ICP is on. Ecotricity's sites don't reliably say."""
        assert self._plans is not None  # noqa: S101 - set by async_step_user
        if user_input is not None:
            return self._create_entry(options={CONF_PLAN_ID: user_input[CONF_PLAN_ID]})
        return self.async_show_form(
            step_id="plan",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_PLAN_ID, default=self._plans.default.id): _plan_selector(
                        self._plans
                    )
                }
            ),
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002 - signature set by Home Assistant
    ) -> ConfigFlowResult:
        """Start reauthentication after the portal rejects the stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new portal credentials."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = _clean_credentials(user_input)
            if not credentials:
                errors["base"] = "credentials_required"
            else:
                errors = await self._async_validate(entry.data[CONF_ICP], credentials)
                if not errors:
                    return self.async_update_reload_and_abort(entry, data_updates=credentials)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_ACCOUNT_NUMBER, default=entry.data.get(CONF_ACCOUNT_NUMBER, "")
                    ): str,
                    probatio.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            description_placeholders={CONF_ICP: entry.data[CONF_ICP]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add, change or remove portal credentials."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = _clean_credentials(user_input)
            errors = await self._async_validate(entry.data[CONF_ICP], credentials)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data={CONF_ICP: entry.data[CONF_ICP], **credentials}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=probatio.Schema(_credentials_schema(entry.data)),
            description_placeholders={CONF_ICP: entry.data[CONF_ICP]},
            errors=errors,
        )

    def _create_entry(self, options: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"Ecotricity {self._data[CONF_ICP]}",
            data=self._data,
            options={CONF_INCLUDE_GST: DEFAULT_INCLUDE_GST, **(options or {})},
        )

    async def _async_validate(self, icp: str, credentials: Mapping[str, str]) -> dict[str, str]:
        """Check the ICP with the onboarding service and the credentials with the portal."""
        if errors := await self._async_validate_icp(icp):
            return errors
        if not credentials:
            return {}
        if not (credentials.get(CONF_ACCOUNT_NUMBER) and credentials.get(CONF_PASSWORD)):
            return {"base": "credentials_incomplete"}
        return await self._async_validate_portal(credentials)

    async def _async_validate_icp(self, icp: str) -> dict[str, str]:
        onboarding = OnboardingClient(async_get_clientsession(self.hass), user_agent=USER_AGENT)
        try:
            details = await onboarding.async_get_icp(icp)
            self._plans = await onboarding.async_get_plans(details)
            self._plans = self._plans.with_titles(await self._async_plan_titles(onboarding))
        except IcpNotFoundError:
            return {CONF_ICP: "icp_not_found"}
        except EcotricityError as err:
            LOGGER.debug("Could not reach the onboarding service: %s", err)
            return {"base": "cannot_connect"}
        return {}

    @staticmethod
    async def _async_plan_titles(onboarding: OnboardingClient) -> dict[str, str]:
        """Return customer-facing plan names, or none if they can't be fetched."""
        try:
            return await onboarding.async_get_plan_titles()
        except EcotricityError as err:
            LOGGER.debug("Could not fetch plan names: %s", err)
            return {}

    async def _async_validate_portal(self, credentials: Mapping[str, str]) -> dict[str, str]:
        local_tz = await dt_util.async_get_time_zone(LOCAL_TIME_ZONE)
        server_tz = await dt_util.async_get_time_zone(PORTAL_SERVER_TIME_ZONE)
        assert local_tz is not None  # noqa: S101 - tzdata ships with Home Assistant
        assert server_tz is not None  # noqa: S101 - tzdata ships with Home Assistant
        # Flows run outside a config entry, so Home Assistant would only release an auto-cleanup
        # session at shutdown. Release this one as soon as the check is done.
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        portal = PortalClient(
            session,
            credentials[CONF_ACCOUNT_NUMBER],
            credentials[CONF_PASSWORD],
            user_agent=USER_AGENT,
            local_tz=local_tz,
            server_tz=server_tz,
        )
        try:
            await portal.async_login()
        except AuthError:
            return {"base": "invalid_auth"}
        except EcotricityError as err:
            LOGGER.debug("Could not reach the customer portal: %s", err)
            return {"base": "cannot_connect"}
        finally:
            session.detach()
        return {}


def _clean_credentials(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Return the credentials that were filled in, without surrounding whitespace."""
    account = str(user_input.get(CONF_ACCOUNT_NUMBER) or "").strip()
    password = str(user_input.get(CONF_PASSWORD) or "")
    credentials: dict[str, str] = {}
    if account:
        credentials[CONF_ACCOUNT_NUMBER] = account
    if password:
        credentials[CONF_PASSWORD] = password
    return credentials


class EcotricityOptionsFlow(OptionsFlowWithReload):
    """Options: GST and the plan."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        entry: EcotricityConfigEntry = self.config_entry
        if user_input is not None:
            # Keep the plan when it wasn't offered (the entry isn't loaded, so plans are unknown).
            return self.async_create_entry(data={**entry.options, **user_input})

        schema: dict[probatio.Marker, Any] = {
            probatio.Required(
                CONF_INCLUDE_GST,
                default=entry.options.get(CONF_INCLUDE_GST, DEFAULT_INCLUDE_GST),
            ): bool,
        }
        # Plans come from the running coordinator, which only exists while the entry is loaded.
        data = entry.runtime_data.pricing.data if entry.state is ConfigEntryState.LOADED else None
        if data is not None:
            # A saved plan that's no longer listed isn't among the choices, so default to the
            # plan in use (the site's default) or the form can't be submitted unchanged.
            saved = entry.options.get(CONF_PLAN_ID)
            default = saved if saved and data.plans.find(saved) else data.plan.id
            schema[probatio.Required(CONF_PLAN_ID, default=default)] = _plan_selector(data.plans)
        return self.async_show_form(step_id="init", data_schema=probatio.Schema(schema))

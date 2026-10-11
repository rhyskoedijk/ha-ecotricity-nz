"""Data update coordinators for Ecotricity NZ."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AttestationError,
    AuthError,
    ConsumptionData,
    EcotricityError,
    IcpDetails,
    IntervalReading,
    OnboardingClient,
    Plan,
    PlanList,
    PortalClient,
    PortalIndicator,
    Season,
)
from .api.timeframes import ChartReadError, ChartReading, chart_path, match_tiers, read_chart
from .const import (
    BACKFILL_PAUSE_SECONDS,
    CONF_ICP,
    CONF_PLAN_ID,
    CONSUMPTION_CHUNK,
    CONSUMPTION_UPDATE_INTERVAL,
    DOMAIN,
    FAILURE_ISSUE_THRESHOLD,
    ISSUE_ATTESTATION,
    ISSUE_PLAN_NOT_FOUND,
    ISSUE_TOU_CHART,
    LOGGER,
    PRICING_UPDATE_INTERVAL,
    RETRY_AFTER_FAILURE,
)
from .statistics import (
    CARBON,
    EXPORT,
    IMPORT,
    Series,
    async_import_hours,
    async_last_point,
    hourly_totals,
    statistic_id,
)
from .tariff import DayPricing, SlotMap, price_day, season_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from homeassistant.core import HomeAssistant

# Weekday and weekend charts for the current and previous season.
MAX_CACHED_CHARTS = 4
# Seasons, rates and charts can change at any time, so charts are downloaded again at least daily.
# Slightly under a day, so the midnight refresh always gets a fresh copy.
CHART_MAX_AGE = timedelta(hours=23)


@dataclass(slots=True)
class EcotricityRuntimeData:
    """Objects shared by the platforms of a config entry."""

    pricing: PricingCoordinator
    consumption: ConsumptionCoordinator | None
    local_tz: tzinfo


type EcotricityConfigEntry = ConfigEntry[EcotricityRuntimeData]


@dataclass(frozen=True, slots=True)
class PricingData:
    """Pricing state for one ICP on one day."""

    icp: IcpDetails
    plans: PlanList
    plan: Plan
    day: date
    season: Season | None
    pricing: DayPricing
    import_slots: SlotMap | None
    export_slots: SlotMap | None
    tou_error: str | None = None


@dataclass(frozen=True, slots=True)
class ConsumptionSummary:
    """The latest complete day of consumption, and portal import status."""

    indicator: PortalIndicator
    day: date | None = None
    imported_kwh: float | None = None
    exported_kwh: float | None = None
    carbon_savings_kg: float | None = None
    hours_imported: int = 0


class PricingCoordinator(DataUpdateCoordinator[PricingData]):
    """Fetch ICP details, plans, seasons and TOU windows.

    Ecotricity can publish new seasons or change rates at any time, and seasons change every few
    months (on the 1st). Everything is re-fetched daily and again just after each NZ midnight, so
    a new season applies from its first day and any change is picked up within a day.
    """

    config_entry: EcotricityConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EcotricityConfigEntry,
        onboarding: OnboardingClient,
        local_tz: tzinfo,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} pricing",
            update_interval=PRICING_UPDATE_INTERVAL,
        )
        self._onboarding = onboarding
        self._local_tz = local_tz
        self._charts: dict[str, tuple[ChartReading, datetime]] = {}
        self._attestation_failures = 0
        self._chart_failures = 0
        self._unsub_midnight: CALLBACK_TYPE | None = None
        self._plan_titles: dict[str, str] = {}

    @property
    def icp(self) -> str:
        """Return the configured ICP number."""
        return str(self.config_entry.data[CONF_ICP])

    @callback
    def async_start_midnight_refresh(self) -> None:
        """Refresh just after each NZ midnight, so a new season's prices apply on the day."""
        tomorrow = dt_util.now(self._local_tz).date() + timedelta(days=1)
        midnight = datetime.combine(tomorrow, time(0, 0, 5), self._local_tz)

        @callback
        def _refresh(_now: datetime) -> None:
            self._unsub_midnight = None
            self.hass.async_create_task(self.async_request_refresh())
            self.async_start_midnight_refresh()

        self._unsub_midnight = async_track_point_in_utc_time(
            self.hass, _refresh, dt_util.as_utc(midnight)
        )

    async def async_shutdown(self) -> None:
        """Cancel the midnight refresh."""
        if self._unsub_midnight is not None:
            self._unsub_midnight()
            self._unsub_midnight = None
        await super().async_shutdown()

    async def _async_update_data(self) -> PricingData:
        try:
            icp = await self._onboarding.async_get_icp(self.icp)
            plans = await self._onboarding.async_get_plans(icp)
        except AttestationError as err:
            self._attestation_failures += 1
            if self._attestation_failures >= FAILURE_ISSUE_THRESHOLD:
                self._raise_issue(ISSUE_ATTESTATION)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="attestation_failed",
                retry_after=RETRY_AFTER_FAILURE.total_seconds(),
            ) from err
        except EcotricityError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
                retry_after=RETRY_AFTER_FAILURE.total_seconds(),
            ) from err
        self._attestation_failures = 0
        self._clear_issue(ISSUE_ATTESTATION)
        plans = plans.with_titles(await self._async_plan_titles())

        plan = self._select_plan(plans)

        day = dt_util.now(self._local_tz).date()
        season = season_for(plan, day)
        pricing = price_day(plan, day)

        import_slots, export_slots, tou_error = await self._async_slot_maps(plan, season, pricing)
        return PricingData(
            icp=icp,
            plans=plans,
            plan=plan,
            day=day,
            season=season,
            pricing=pricing,
            import_slots=import_slots,
            export_slots=export_slots,
            tou_error=tou_error,
        )

    async def _async_plan_titles(self) -> dict[str, str]:
        """Return customer-facing plan names, keeping the last good set if the fetch fails."""
        try:
            self._plan_titles = await self._onboarding.async_get_plan_titles()
        except EcotricityError as err:
            LOGGER.debug("Could not fetch plan names, keeping the previous ones: %s", err)
        return self._plan_titles

    def _select_plan(self, plans: PlanList) -> Plan:
        """Return the plan chosen in the options.

        If Ecotricity stops offering that plan for the ICP, fall back to the site's default plan
        and raise a repair issue asking for a new choice.
        """
        plan_id = str(self.config_entry.options.get(CONF_PLAN_ID, ""))
        if (plan := plans.find(plan_id)) is not None:
            self._clear_issue(ISSUE_PLAN_NOT_FOUND)
            return plan
        self._raise_issue(ISSUE_PLAN_NOT_FOUND, {"default": plans.default.name})
        return plans.default

    async def _async_slot_maps(
        self, plan: Plan, season: Season | None, pricing: DayPricing
    ) -> tuple[SlotMap | None, SlotMap | None, str | None]:
        """Work out which bucket applies in each half-hour, reading the plan's charts if needed."""
        flat_import = (
            SlotMap.flat(next(iter(pricing.import_buckets)))
            if len(pricing.import_buckets) == 1
            else None
        )
        flat_export = (
            SlotMap.flat(next(iter(pricing.export_buckets)))
            if len(pricing.export_buckets) == 1
            else None
        )
        need_import = flat_import is None and bool(pricing.import_buckets)
        need_export = flat_export is None and bool(pricing.export_buckets)
        if not need_import and not need_export:
            self._chart_failures = 0
            return flat_import, flat_export, None

        import_slots, export_slots = flat_import, flat_export
        errors: list[str] = []
        try:
            weekday = await self._async_chart(plan.timeframe_chart_weekday, season)
            weekend = await self._async_chart(plan.timeframe_chart_weekend, season)
        except (ChartReadError, EcotricityError) as err:
            errors.append(str(err))
        else:
            # Match import and export separately, so a problem with one keeps the other.
            if need_import:
                try:
                    import_slots = SlotMap(
                        weekday=match_tiers(weekday.import_tiers, pricing.import_buckets),
                        weekend=match_tiers(weekend.import_tiers, pricing.import_buckets),
                    )
                except ChartReadError as err:
                    errors.append(f"Import: {err}")
            if need_export:
                try:
                    export_slots = SlotMap(
                        weekday=match_tiers(weekday.export_tiers, pricing.export_buckets),
                        weekend=match_tiers(weekend.export_tiers, pricing.export_buckets),
                    )
                except ChartReadError as err:
                    errors.append(f"Export: {err}")
        if errors:
            error = "; ".join(errors)
            self._chart_failures += 1
            LOGGER.warning("Could not read the time-of-use windows for %s: %s", plan.code, error)
            if self._chart_failures >= FAILURE_ISSUE_THRESHOLD:
                self._raise_issue(ISSUE_TOU_CHART, {"plan": plan.description})
            return import_slots, export_slots, error
        self._chart_failures = 0
        self._clear_issue(ISSUE_TOU_CHART)
        return import_slots, export_slots, None

    async def _async_chart(self, template: str | None, season: Season | None) -> ChartReading:
        if not template:
            raise ChartReadError("The plan has no timeframe chart")
        if season is None and "SSSSSSSS" in template:
            # Ecotricity may not have published the next season yet. The daily refresh picks it
            # up as soon as it appears in the plan list.
            raise ChartReadError("No published season covers today")
        path = chart_path(template, season.start, season.end) if season else template
        now = dt_util.utcnow()
        cached = self._charts.get(path)
        if cached is not None and now - cached[1] < CHART_MAX_AGE:
            return cached[0]
        data = await self._onboarding.async_get_chart(path)
        reading = await self.hass.async_add_executor_job(read_chart, data)
        # Re-insert so the dict stays ordered oldest first, then drop the oldest beyond the limit.
        self._charts.pop(path, None)
        self._charts[path] = (reading, now)
        while len(self._charts) > MAX_CACHED_CHARTS:
            del self._charts[next(iter(self._charts))]
        return reading

    def _raise_issue(self, issue_id: str, placeholders: dict[str, str] | None = None) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{issue_id}_{self.config_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue_id,
            translation_placeholders=placeholders,
        )

    def _clear_issue(self, issue_id: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"{issue_id}_{self.config_entry.entry_id}")


class ConsumptionCoordinator(DataUpdateCoordinator[ConsumptionSummary]):
    """Import half-hourly portal data into statistics once a day, backfilling on first run."""

    config_entry: EcotricityConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EcotricityConfigEntry,
        portal: PortalClient,
        local_tz: tzinfo,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} consumption",
            update_interval=CONSUMPTION_UPDATE_INTERVAL,
        )
        self._portal = portal
        self._local_tz = local_tz
        # How far each series has been read this session, for series with no statistic yet.
        self._scanned_until: dict[Series, datetime] = {}

    @property
    def icp(self) -> str:
        """Return the configured ICP number."""
        return str(self.config_entry.data[CONF_ICP])

    async def _async_update_data(self) -> ConsumptionSummary:
        try:
            return await self._async_import()
        except AuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except EcotricityError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
                retry_after=RETRY_AFTER_FAILURE.total_seconds(),
            ) from err

    async def _async_import(self) -> ConsumptionSummary:
        indicator = await self._portal.async_get_indicator()
        if indicator.data_start is None or indicator.data_end is None:
            return ConsumptionSummary(indicator=indicator)

        # Only the series the portal holds data for (no export without generation, say).
        held = [
            series
            for series in (IMPORT, EXPORT, CARBON)
            if not indicator.features or series.feature in indicator.features
        ]
        last_points = {
            series: await async_last_point(self.hass, statistic_id(self.icp, series))
            for series in held
        }

        data_end = indicator.data_end
        last_day_start = self._local_midnight(data_end - timedelta(seconds=1))
        # Continue from the earliest last-imported hour, so a series added later (or one that
        # fell behind) catches up. Hours already imported are skipped. A series with no
        # statistic yet is read from the start, unless this session has already read it to the
        # end and found nothing, so a series that never has readings isn't re-read every day.
        resume = min(
            (
                point.start + timedelta(hours=1)
                if point is not None
                else self._scanned_until.get(series, indicator.data_start)
                for series, point in last_points.items()
            ),
            default=indicator.data_start,
        )
        # Always fetch the last complete day too, for the daily summary sensors.
        fetch_start = max(indicator.data_start, min(resume, last_day_start))

        hours_imported = 0
        last_day: list[ConsumptionData] = []
        chunk_start = fetch_start
        while chunk_start < data_end:
            chunk_end = min(chunk_start + CONSUMPTION_CHUNK, data_end)
            data = await self._portal.async_get_half_hourly(chunk_start, chunk_end)
            for series, readings in (
                (IMPORT, data.imported_kwh),
                (EXPORT, data.exported_kwh),
                (CARBON, data.carbon_savings_kg),
            ):
                if series not in last_points or not readings:
                    continue
                last_points[series], added = async_import_hours(
                    self.hass, self.icp, series, hourly_totals(readings), last_points[series]
                )
                if series is IMPORT:
                    hours_imported += added
            if chunk_end > last_day_start:
                last_day.append(data)
            chunk_start = chunk_end
            if chunk_start < data_end:
                await asyncio.sleep(BACKFILL_PAUSE_SECONDS)
        for series in held:
            self._scanned_until[series] = data_end

        if hours_imported:
            LOGGER.debug("Imported %s hours of consumption", hours_imported)
        return self._summarise(indicator, last_day, last_day_start, data_end, hours_imported)

    def _local_midnight(self, instant: datetime) -> datetime:
        local = instant.astimezone(self._local_tz)
        return datetime.combine(local.date(), datetime.min.time(), self._local_tz)

    def _summarise(
        self,
        indicator: PortalIndicator,
        chunks: Sequence[ConsumptionData],
        day_start: datetime,
        day_end: datetime,
        hours_imported: int,
    ) -> ConsumptionSummary:
        def total(readings: Sequence[IntervalReading]) -> float | None:
            values = [r.value for r in readings if day_start <= r.start < day_end]
            return round(sum(values), 3) if values else None

        imported = [r for chunk in chunks for r in chunk.imported_kwh]
        exported = [r for chunk in chunks for r in chunk.exported_kwh]
        carbon = [r for chunk in chunks for r in chunk.carbon_savings_kg]
        return ConsumptionSummary(
            indicator=indicator,
            day=day_start.astimezone(self._local_tz).date(),
            imported_kwh=total(imported),
            exported_kwh=total(exported),
            carbon_savings_kg=total(carbon),
            hours_imported=hours_imported,
        )

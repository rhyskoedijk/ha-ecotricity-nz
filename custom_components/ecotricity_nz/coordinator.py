"""Data update coordinators for Ecotricity NZ."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
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
from .api.timeframes import ChartReadError, ChartReading, read_chart
from .const import (
    BACKFILL_PAUSE_SECONDS,
    CONF_ICP,
    CONF_PLAN_ID,
    CONSUMPTION_CHUNK,
    CONSUMPTION_RECHECK_INTERVAL,
    CONSUMPTION_SETTLE_PERIOD,
    CONSUMPTION_UPDATE_INTERVAL,
    DOMAIN,
    FAILURE_ISSUE_THRESHOLD,
    ISSUE_ATTESTATION,
    ISSUE_PLAN_NOT_FOUND,
    ISSUE_TOU_CHART,
    LOGGER,
    PRICING_CHECK_INTERVAL,
    PRICING_FETCH_INTERVAL,
    PRICING_RETRY_INTERVAL,
    PRICING_RETRY_MAX_INTERVAL,
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
from .store import PricingSnapshot, PricingStore
from .tariff import (
    Alignment,
    DayPricing,
    PlanSlots,
    bucket_names,
    chart_seasons,
    controlled_prices,
    first_day,
    plan_needs_season,
    plan_slots,
    price_day,
    price_side,
    season_charts,
    season_for,
    season_slots,
    side_alignment,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from homeassistant.core import HomeAssistant


@dataclass(slots=True)
class EcotricityRuntimeData:
    """Objects shared by the platforms of a config entry."""

    pricing: PricingCoordinator
    consumption: ConsumptionCoordinator | None
    local_tz: tzinfo


type EcotricityConfigEntry = ConfigEntry[EcotricityRuntimeData]


@dataclass(frozen=True, slots=True)
class PlanPeriods:
    """The periods the plan uses in today's and every upcoming season with readable charts.

    Entities are created from these, so they don't come and go with the seasons.
    """

    buckets: frozenset[str] = frozenset()
    """Every overall period's bucket, for import, export and controlled load."""
    supplier: frozenset[str] = frozenset()
    """Ecotricity's period buckets on the sides that are split."""
    network: frozenset[str] = frozenset()
    """The network's period buckets on the sides that are split."""
    import_alignment: Alignment = Alignment.SUPPLIER
    export_alignment: Alignment = Alignment.SUPPLIER
    complete: bool = True
    """False when a later season's charts couldn't be read, so its periods may be missing."""


@dataclass(frozen=True, slots=True)
class PricingData:
    """Pricing state for one ICP on one day."""

    icp: IcpDetails
    plans: PlanList
    plan: Plan
    day: date
    season: Season | None
    pricing: DayPricing
    periods: PlanPeriods
    names: dict[str, str]
    """The plan's bucket names (see `bucket_names`), worked out once rather than per entity."""
    fetched_at: datetime
    """When the pricing was last fetched from Ecotricity."""
    tou_error: str | None = None
    missing_charts: tuple[str, ...] = ()
    """Sides given their one network rate all day because their charts are missing."""


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
    """Work out today's prices and time-of-use windows from pricing fetched from Ecotricity.

    Everything fetched is saved (see store.py), and each day's prices and windows are worked out
    from the saved copy, so they keep working across restarts and without an internet connection.
    Rates and seasons carry their own dates, and the next season's charts are downloaded ahead of
    time once it's published, so a new season applies from its first day without a fetch.

    Pricing is fetched again only when Ecotricity might have changed it (see `_fetch_reason`). The
    coordinator checks once a day while fetches are working, and backs off from an hour to eight
    hours between attempts while they fail. Just after each NZ midnight it works out the new day
    from the saved pricing, without checking or fetching.
    """

    config_entry: EcotricityConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EcotricityConfigEntry,
        onboarding: OnboardingClient,
        local_tz: tzinfo,
        store: PricingStore,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} pricing",
            update_interval=PRICING_CHECK_INTERVAL,
        )
        self._onboarding = onboarding
        self._local_tz = local_tz
        self._store = store
        self._snapshot: PricingSnapshot | None = None
        self._loaded = False
        # The last day priced, so a check that prices it twice (to decide whether to fetch, then
        # for the entities) only works it out once.
        self._priced: tuple[PricingSnapshot, date, PricingData] | None = None
        self._fetch_requested = False
        self._fetch_error: str | None = None
        self._fetch_failures = 0
        self._attestation_failures = 0
        self._chart_failures = 0
        self._unsub_midnight: CALLBACK_TYPE | None = None

    @property
    def icp(self) -> str:
        """Return the configured ICP number."""
        return str(self.config_entry.data[CONF_ICP])

    async def async_fetch_now(self) -> bool:
        """Fetch pricing from Ecotricity now, whatever the schedule, and say whether it worked."""
        self._fetch_requested = True
        await self.async_refresh()
        return self.last_update_success and self._fetch_error is None

    @callback
    def async_start_midnight_refresh(self) -> None:
        """Work out the new day's prices just after each NZ midnight, from the saved pricing."""
        tomorrow = dt_util.now(self._local_tz).date() + timedelta(days=1)
        midnight = datetime.combine(tomorrow, time(0, 0, 5), self._local_tz)

        @callback
        def _refresh(_now: datetime) -> None:
            self._unsub_midnight = None
            self._async_new_day()
            self.async_start_midnight_refresh()

        self._unsub_midnight = async_track_point_in_utc_time(
            self.hass, _refresh, dt_util.as_utc(midnight)
        )

    @callback
    def _async_new_day(self) -> None:
        """Update the entities for the new day.

        Not a coordinator refresh, which would move the daily check to midnight for everyone.
        """
        if self._snapshot is None:  # pragma: no cover - the first refresh always leaves one
            return
        self.data = self._price_day(self._snapshot, dt_util.now(self._local_tz).date())
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Cancel the midnight refresh."""
        if self._unsub_midnight is not None:
            self._unsub_midnight()
            self._unsub_midnight = None
        await super().async_shutdown()

    async def _async_update_data(self) -> PricingData:
        if not self._loaded:
            self._loaded = True
            saved = await self._store.async_load()
            if saved is not None and saved.icp.icp == self.icp:
                self._snapshot = saved
        now = dt_util.utcnow()
        day = now.astimezone(self._local_tz).date()
        reason = self._fetch_reason(now, day)
        if reason is None:
            # Nothing is due, so whatever failed before no longer matters.
            self._reset_backoff()
        else:
            LOGGER.debug("Fetching pricing from Ecotricity: %s", reason)
            fetched = await self._async_fetch(now, day)
            if fetched is None and self._snapshot is not None:
                return self._price_day(self._snapshot, day)
            if fetched is None:  # pragma: no cover - _async_fetch raises without a saved copy
                raise UpdateFailed(translation_domain=DOMAIN, translation_key="update_failed")
            data = self._price_day(fetched, day)
            # Charts are only downloaded when pricing is fetched, so count unreadable ones per
            # successful fetch rather than per check.
            if data.tou_error is None:
                self._chart_failures = 0
            else:
                self._chart_failures += 1
                LOGGER.warning(
                    "Could not read the time-of-use windows for %s: %s",
                    data.plan.code,
                    data.tou_error,
                )
                if self._chart_failures >= FAILURE_ISSUE_THRESHOLD:
                    self._raise_issue(ISSUE_TOU_CHART, {"plan": data.plan.description})
            return data
        if self._snapshot is None:  # pragma: no cover - a fetch is always due without one
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="update_failed")
        return self._price_day(self._snapshot, day)

    def _fetch_reason(self, now: datetime, day: date) -> str | None:  # noqa: PLR0911 - one return per reason
        """Return why pricing should be fetched now, or None if the saved copy will do.

        Seasons start on the 1st, but Ecotricity can publish or change a season, or change a
        rate, at any time. Until a fetch succeeds, the reason stays true, so it's tried again at
        each check.
        """
        snapshot = self._snapshot
        if snapshot is None:
            return "no saved pricing"
        if self._fetch_requested:
            return "requested"
        plan = self._select_plan(snapshot.plans)
        if snapshot.charts_plan_id != plan.id:
            return "a different plan was chosen"
        if now - snapshot.fetched_at >= PRICING_FETCH_INTERVAL:
            return "weekly update"
        if snapshot.fetched_at.astimezone(self._local_tz).date() >= day:
            return None
        # The rest are checked at most once a day.
        if day.day == 1:
            return "start of the month"
        season = season_for(plan, day)
        if season is not None and season.end == day:
            return "last day of the season"
        tomorrow = day + timedelta(days=1)
        if plan_needs_season(plan) and (season is None or season_for(plan, tomorrow) is None):
            return "next season not published yet"
        data = self._price_day(snapshot, day)
        if data.tou_error is not None or data.missing_charts:
            return "time-of-use windows unavailable"
        return None

    async def _async_fetch(self, now: datetime, day: date) -> PricingSnapshot | None:
        """Fetch everything, save it and return it, or return None to carry on with the saved copy.

        Without a saved copy to fall back on, a failure fails the update.
        """
        self._fetch_requested = False
        try:
            icp = await self._onboarding.async_get_icp(self.icp)
            plans = await self._onboarding.async_get_plans(icp)
        except AttestationError as err:
            self._attestation_failures += 1
            if self._attestation_failures >= FAILURE_ISSUE_THRESHOLD:
                self._raise_issue(ISSUE_ATTESTATION)
            self._fetch_failed(err, "attestation_failed", None)
            return None
        except EcotricityError as err:
            self._fetch_failed(err, "update_failed", {"error": str(err)})
            return None
        self._attestation_failures = 0
        self._clear_issue(ISSUE_ATTESTATION)

        titles = await self._async_plan_titles()
        plans = plans.with_titles(titles)
        plan = self._select_plan(plans)
        snapshot = PricingSnapshot(
            icp=icp,
            plans=plans,
            fetched_at=now,
            plan_titles=titles,
            charts=await self._async_charts(plan, day),
            charts_plan_id=plan.id,
        )
        self._snapshot = snapshot
        self._reset_backoff()
        await self._store.async_save(snapshot)
        return snapshot

    def _fetch_failed(
        self, err: EcotricityError, translation_key: str, placeholders: dict[str, str] | None
    ) -> None:
        """Back off, and fall back to the saved pricing or fail the update if there isn't any."""
        self._fetch_failures += 1
        retry = min(
            PRICING_RETRY_INTERVAL * 2 ** (self._fetch_failures - 1), PRICING_RETRY_MAX_INTERVAL
        )
        self.update_interval = retry
        if self._snapshot is None:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders=placeholders,
                retry_after=retry.total_seconds(),
            ) from err
        # Warn once, not at every retry while the connection is down.
        log = LOGGER.debug if self._fetch_error is not None else LOGGER.warning
        log(
            "Could not fetch pricing from Ecotricity, so the pricing saved %s is used: %s",
            self._snapshot.fetched_at.astimezone(self._local_tz).isoformat(timespec="minutes"),
            err,
        )
        self._fetch_error = str(err)

    def _reset_backoff(self) -> None:
        """Go back to checking once a day."""
        self._fetch_error = None
        self._fetch_failures = 0
        self.update_interval = PRICING_CHECK_INTERVAL

    async def _async_plan_titles(self) -> dict[str, str]:
        """Return customer-facing plan names, keeping the last good set if the fetch fails."""
        try:
            return await self._onboarding.async_get_plan_titles()
        except EcotricityError as err:
            LOGGER.debug("Could not fetch plan names, keeping the previous ones: %s", err)
        return dict(self._snapshot.plan_titles) if self._snapshot else {}

    async def _async_charts(self, plan: Plan, day: date) -> dict[str, ChartReading]:
        """Download and read the charts for this season and every published season after it.

        This season's and the next are downloaded at every fetch; later ones only until they've
        been read and explained once, since they're only needed to know which periods the plan
        uses. A chart that can't be downloaded or read keeps its saved reading, if there is one.
        """
        saved = self._snapshot.charts if self._snapshot else {}
        refresh = {
            path
            for season in chart_seasons(plan, day, upcoming=False)
            for path in season_charts(plan, season)
        }
        charts: dict[str, ChartReading] = {}
        for season in chart_seasons(plan, day, upcoming=True):
            paths = [path for path in season_charts(plan, season) if path not in charts]
            if not refresh.intersection(paths) and _explained(saved, plan, season, day):
                charts.update((path, saved[path]) for path in paths)
                continue
            for path in paths:
                try:
                    data = await self._onboarding.async_get_chart(path)
                    charts[path] = await self.hass.async_add_executor_job(read_chart, data)
                except EcotricityError as err:
                    LOGGER.debug("Could not read the chart %s: %s", path, err)
                    if path in saved:
                        charts[path] = saved[path]
        return charts

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

    def _price_day(self, snapshot: PricingSnapshot, day: date) -> PricingData:
        """Work out a day's prices and windows from saved pricing. Makes no requests."""
        plan = self._select_plan(snapshot.plans)
        if self._priced is not None:
            priced_snapshot, priced_day, data = self._priced
            if priced_snapshot is snapshot and priced_day == day and data.plan is plan:
                if data.tou_error is None:
                    self._clear_issue(ISSUE_TOU_CHART)
                return data
        season = season_for(plan, day)
        slots = plan_slots(snapshot.charts, plan, day)
        for error in slots.missing:
            LOGGER.debug("Taking one network rate to apply all day without its chart: %s", error)
        for error in slots.later_errors:
            LOGGER.debug("Leaving out a season whose charts can't be read: %s", error)
        alignments = (
            side_alignment(plan, day, slots.seasons(export=False), export=False),
            side_alignment(plan, day, slots.seasons(export=True), export=True),
        )
        pricing = price_day(plan, day, slots.today[False], slots.today[True], alignments)
        tou_error = "; ".join(slots.errors) or None
        if tou_error is None:
            self._clear_issue(ISSUE_TOU_CHART)
        data = PricingData(
            icp=snapshot.icp,
            plans=snapshot.plans,
            plan=plan,
            day=day,
            season=season,
            pricing=pricing,
            periods=_plan_periods(plan, day, slots, pricing, alignments),
            names=bucket_names(plan),
            fetched_at=snapshot.fetched_at,
            tou_error=tou_error,
            missing_charts=slots.missing,
        )
        self._priced = (snapshot, day, data)
        return data

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


def _explained(
    charts: Mapping[str, ChartReading], plan: Plan, season: Season | None, day: date
) -> bool:
    """Return whether a season's saved charts are all there and the plan's rates explain them.

    With every chart there, none is missing, so `season_slots` has nothing to report.
    """
    if any(path not in charts for path in season_charts(plan, season)):
        return False
    start = day if season is None else first_day(plan, season, day)
    if start is None:
        # The season no longer applies, so its charts aren't needed.
        return True
    try:
        for export in (False, True):
            season_slots(charts, plan, season, start, export=export)
    except ChartReadError:
        return False
    return True


def _plan_periods(
    plan: Plan,
    day: date,
    slots: PlanSlots,
    pricing: DayPricing,
    alignments: tuple[Alignment, Alignment],
) -> PlanPeriods:
    """Return the periods the plan uses today or in any later season with readable charts.

    Controlled load needs no charts, so its periods come from every later season's rates. A side
    whose charts can't be read today has the periods its rates give (`rate_buckets`), so its
    sensors stay.
    """
    buckets: set[str] = set(pricing.rate_buckets)
    for side in (pricing.import_side, pricing.export_side):
        if side is not None:
            buckets.update(side.buckets)
    starts = {day} | {
        start
        for item in plan.seasons
        if item.end >= day and (start := first_day(plan, item, day)) is not None
    }
    for start in starts:
        buckets.update(controlled_prices(plan, start)[0])
    supplier: set[str] = set()
    network: set[str] = set()
    for export, alignment in ((False, alignments[0]), (True, alignments[1])):
        for later in slots.later[export]:
            buckets.update(price_side(later, alignment).buckets)
        if alignment is Alignment.SPLIT:
            for season in slots.seasons(export=export):
                supplier.update(slot.supplier for slot in season.all)
                network.update(slot.network for slot in season.all)
    return PlanPeriods(
        buckets=frozenset(buckets),
        supplier=frozenset(supplier),
        network=frozenset(network),
        import_alignment=alignments[0],
        export_alignment=alignments[1],
        complete=not slots.later_errors,
    )


class ConsumptionCoordinator(DataUpdateCoordinator[ConsumptionSummary]):
    """Import half-hourly portal data into statistics once a day, backfilling on first run.

    The portal takes in new meter data once a day, at a time that isn't known. When a daily import
    finds nothing new, the portal is checked again every few hours with a single request, and
    the next import runs as soon as new data appears. That moves the daily import to just after
    the portal's own, without needing to know when that is. If the portal's timing turns out to be
    consistent, a fixed time could replace this (issue #6).
    """

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
        self._import_requested = False
        # Set while waiting for the portal's next data, with the latest data and when it was read.
        self._waiting_since: tuple[datetime | None, datetime] | None = None

    @property
    def icp(self) -> str:
        """Return the configured ICP number."""
        return str(self.config_entry.data[CONF_ICP])

    async def async_fetch_now(self) -> bool:
        """Import from the portal now, whatever the schedule, and say whether it worked."""
        self._import_requested = True
        await self.async_refresh()
        return self.last_update_success

    async def _async_update_data(self) -> ConsumptionSummary:
        requested, self._import_requested = self._import_requested, False
        try:
            indicator = await self._portal.async_get_indicator()
            if not requested and self.data is not None and self._still_waiting(indicator):
                return replace(self.data, indicator=indicator, hours_imported=0)
            summary = await self._async_import(indicator)
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
        if summary.hours_imported:
            self._waiting_since = None
            self.update_interval = CONSUMPTION_UPDATE_INTERVAL
        else:
            self._waiting_since = (indicator.data_end, dt_util.utcnow())
            self.update_interval = CONSUMPTION_RECHECK_INTERVAL
        return summary

    def _still_waiting(self, indicator: PortalIndicator) -> bool:
        """Return True while the portal has nothing new since the last import found nothing.

        A full import still runs once a day, so gaps the portal fills in later are picked up.
        """
        if self._waiting_since is None:
            return False
        data_end, since = self._waiting_since
        return (
            indicator.data_end == data_end
            and dt_util.utcnow() - since < CONSUMPTION_UPDATE_INTERVAL
        )

    async def _async_import(self, indicator: PortalIndicator) -> ConsumptionSummary:
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
        # Gaps older than this are imported as they stand rather than waited on.
        settled_before = data_end - CONSUMPTION_SETTLE_PERIOD
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
        # Always fetch the last complete day too, for the daily summary sensors. Requests start
        # and end at NZ midnight: a response starting partway through the hour repeated when
        # daylight saving ends can't be placed on the right occurrence of it, and a chunk ending
        # mid-hour would split that hour's readings. Hours already imported are skipped.
        fetch_start = max(indicator.data_start, self._local_midnight(min(resume, last_day_start)))

        hours_imported = 0
        last_day: list[ConsumptionData] = []
        chunk_start = fetch_start
        while chunk_start < data_end:
            chunk_end = min(self._local_midnight(chunk_start, CONSUMPTION_CHUNK.days), data_end)
            data = await self._portal.async_get_half_hourly(chunk_start, chunk_end)
            for series, readings in (
                (IMPORT, data.imported_kwh),
                (EXPORT, data.exported_kwh),
                (CARBON, data.carbon_savings_kg),
            ):
                if series not in last_points or not readings:
                    continue
                last_points[series], added = async_import_hours(
                    self.hass,
                    self.icp,
                    series,
                    hourly_totals(readings, settled_before),
                    last_points[series],
                    settled_before=settled_before,
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

    def _local_midnight(self, instant: datetime, days_later: int = 0) -> datetime:
        """Return the NZ midnight starting the instant's day, or a number of days after it."""
        day = instant.astimezone(self._local_tz).date() + timedelta(days=days_later)
        return datetime.combine(day, time.min, self._local_tz)

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

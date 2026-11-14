"""Data update coordinators for Ecotricity NZ."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, tzinfo
from functools import partial
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
    ISSUE_NETWORK_CATEGORY,
    ISSUE_NETWORK_CATEGORY_UNPRICED,
    ISSUE_PLAN_NOT_FOUND,
    ISSUE_TOU_CHART,
    LOGGER,
    PRICING_CHECK_INTERVAL,
    PRICING_FETCH_INTERVAL,
    PRICING_MISSING_CHART_RETRIES,
    PRICING_RETRY_INTERVAL,
    PRICING_RETRY_MAX_INTERVAL,
    RETRY_AFTER_FAILURE,
)
from .costs import CostKeys, DayCosts, cost_keys, hourly_costs
from .statistics import (
    CARBON,
    EXPORT,
    IMPORT,
    Series,
    StatisticPoint,
    async_import_hours,
    async_last_point,
    cost_series,
    hourly_totals,
    statistic_id,
)
from .store import PricingSnapshot, PricingStore
from .tariff import (
    Alignment,
    DayPricing,
    DaySlots,
    PlanDay,
    billed_plan,
    bucket_names,
    chart_seasons,
    controlled_prices,
    daily_charges,
    ended_seasons,
    first_day,
    network_category,
    plan_needs_season,
    price_plan_day,
    price_side,
    rate_periods,
    season_charts,
    season_explained,
    season_for,
    season_slots,
    side_rates,
    without_export,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from homeassistant.core import HomeAssistant

_NEW_PLAN = "a different plan was chosen"


@dataclass(slots=True)
class EcotricityRuntimeData:
    """Objects shared by the platforms of a config entry."""

    pricing: PricingCoordinator
    consumption: ConsumptionCoordinator | None
    local_tz: tzinfo


type EcotricityConfigEntry = ConfigEntry[EcotricityRuntimeData]


@dataclass(frozen=True, slots=True)
class PlanPeriods:
    """The periods the plan uses in any published season, today's, upcoming or ended.

    Upcoming seasons count once their charts can be read, and ended ones from their rates.
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
    """Sides, today or in a later season, given their one network rate all day because their
    charts are missing, or don't match the plan's rates."""
    later_errors: tuple[str, ...] = ()
    """Why a later season's charts couldn't be used, so its periods may be missing."""
    chart_gaps: tuple[str, ...] = ()
    """The side and season of each of `missing_charts` (see `PlanSlots.gaps`)."""


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
    hours between attempts while they fail. At each half-hour it updates the sensors that change
    with the time of day, and at NZ midnight it first works out the new day from the saved
    pricing, without checking or fetching.
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
        # Set when a scheduled fetch was due at setup, which was left for after startup.
        self._fetch_deferred = False
        self._fetch_error: str | None = None
        self._fetch_failures = 0
        self._attestation_failures = 0
        self._chart_failures = 0
        self._unsub_half_hour: CALLBACK_TYPE | None = None
        self._half_hour_listeners: list[CALLBACK_TYPE] = []
        # Past days priced for the cost statistics, for the snapshot and plan they came from.
        self._costed: tuple[PricingSnapshot, str, dict[date, DayCosts | None]] | None = None

    @property
    def icp(self) -> str:
        """Return the configured ICP number."""
        return str(self.config_entry.data[CONF_ICP])

    async def async_fetch_now(self) -> bool:
        """Fetch pricing from Ecotricity now, whatever the schedule, and say whether it worked."""
        self._fetch_requested = True
        await self.async_refresh()
        return self.last_update_success and self._fetch_error is None

    async def async_fetch_deferred(self) -> None:
        """Run the fetch that was due at setup, if there was one (see `_async_update_data`)."""
        if self._fetch_deferred:
            self._fetch_deferred = False
            await self.async_refresh()

    def day_costs(self, day: date) -> DayCosts | None:
        """Return what a day's usage is priced with, from the saved pricing. Makes no requests.

        None when no pricing is saved, or no published season covers the day: rates without
        dates of their own apply to every day, so only the seasons tell which days the saved
        pricing is for. A side whose charts can't be read has no half-hours; one given its one
        network rate all day is priced that way, as the sensors show it.
        """
        pricer = self.cost_pricer()
        return None if pricer is None else pricer(day)

    def cost_pricer(self) -> Callable[[date], DayCosts | None] | None:
        """Return `day_costs` for the pricing saved now, which can run in the executor.

        The plan is chosen here, on the event loop, as choosing it can raise a repair issue. What
        it returns only reads the saved pricing. None when no pricing is saved.
        """
        snapshot = self._snapshot
        if snapshot is None:  # pragma: no cover - the first refresh always leaves one
            return None
        plan = self._cost_plan(snapshot)
        if self._costed is None or self._costed[:2] != (snapshot, plan.id):
            self._costed = (snapshot, plan.id, {})
        priced = self._costed[2]

        def price(day: date) -> DayCosts | None:
            if day not in priced:
                priced[day] = _day_costs(snapshot.charts, plan, day)
            return priced[day]

        return price

    def cost_pricing(self) -> tuple[Plan, IcpDetails] | None:
        """Return the plan usage is priced with and the ICP, or None with no saved pricing."""
        if self._snapshot is None:  # pragma: no cover - the first refresh always leaves one
            return None
        return self._cost_plan(self._snapshot), self._snapshot.icp

    def cost_keys(self) -> CostKeys | None:
        """Return the cost statistic keys the plan's rates give, or None with no saved pricing."""
        if self._snapshot is None:  # pragma: no cover - the first refresh always leaves one
            return None
        return cost_keys(self._cost_plan(self._snapshot))

    def earliest_priced_day(self, until: date) -> date | None:
        """Return the first day, up to `until`, whose import the saved pricing can price."""
        if self._snapshot is None:  # pragma: no cover - the first refresh always leaves one
            return None
        plan = self._cost_plan(self._snapshot)
        starts = sorted({item.start for item in plan.seasons if item.start <= until})
        return next(
            (
                day
                for day in starts
                if (costs := self.day_costs(day)) is not None and costs.import_slots is not None
            ),
            None,
        )

    @property
    def import_loss_factor(self) -> float:
        """Return the ICP's consumption loss factor, which bills apply to Ecotricity's import."""
        icp = self._snapshot.icp if self._snapshot else None
        return (icp.loss_factor_consumption if icp else None) or 1.0

    @property
    def export_loss_factor(self) -> float:
        """Return the ICP's generation loss factor, applied to Ecotricity's export the same way."""
        icp = self._snapshot.icp if self._snapshot else None
        return (icp.loss_factor_generation if icp else None) or 1.0

    def _cost_plan(self, snapshot: PricingSnapshot) -> Plan:
        """Return the plan usage is priced with: the one the sensors price."""
        return _pricing_plan(self._select_plan(snapshot.plans), snapshot.plans, snapshot.icp)

    @callback
    def async_add_half_hour_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call `update` at each half-hour, and return how to stop.

        For sensors that change with the time of day. At NZ midnight, every listener is updated
        with the new day instead.
        """
        self._half_hour_listeners.append(update)
        return partial(self._half_hour_listeners.remove, update)

    @callback
    def async_start_half_hour_updates(self) -> None:
        """Update the half-hour listeners at each half-hour, and the new day at NZ midnight.

        NZ midnight is always on a UTC hour, so one timer does both, and a sensor never shows
        yesterday's pricing at midnight.
        """
        now = dt_util.utcnow()
        boundary = now.replace(minute=30 if now.minute < 30 else 0, second=0, microsecond=0)  # noqa: PLR2004 - minutes in half an hour
        if boundary <= now:
            boundary += timedelta(hours=1)

        @callback
        def _update(_now: datetime) -> None:
            self._unsub_half_hour = None
            if self.data is not None and self.data.day != dt_util.now(self._local_tz).date():
                self._async_new_day()
            else:
                for update in list(self._half_hour_listeners):
                    update()
            self.async_start_half_hour_updates()

        self._unsub_half_hour = async_track_point_in_utc_time(self.hass, _update, boundary)

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
        """Cancel the half-hour updates."""
        if self._unsub_half_hour is not None:
            self._unsub_half_hour()
            self._unsub_half_hour = None
        await super().async_shutdown()

    async def _async_update_data(self) -> PricingData:
        now = dt_util.utcnow()
        day = now.astimezone(self._local_tz).date()
        if not self._loaded and await self._async_load(now, day) and self._snapshot is not None:
            return self._price_day(self._snapshot, day)
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
                    self._raise_issue(ISSUE_TOU_CHART, {"plan": data.plan.name})
            if fetched.missing_chart_fetches == PRICING_MISSING_CHART_RETRIES + 1:
                LOGGER.warning(
                    "The charts for %s are still missing or don't match its rates, so its "
                    "network rate is taken to apply all day until the next scheduled fetch: %s",
                    data.plan.code,
                    "; ".join(data.missing_charts),
                )
            return data
        if self._snapshot is None:  # pragma: no cover - a fetch is always due without one
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="update_failed")
        return self._price_day(self._snapshot, day)

    async def _async_load(self, now: datetime, day: date) -> bool:
        """Load the saved pricing, and return True if a fetch due now is left for after startup.

        A scheduled fetch can take a while, so setup uses the saved pricing and the fetch runs
        once Home Assistant has started (`async_fetch_deferred`). Without saved pricing for the
        chosen plan there's nothing to start with, so that's fetched straight away.
        """
        self._loaded = True
        saved = await self._store.async_load()
        if saved is None or saved.icp.icp != self.icp:
            return False
        self._snapshot = saved
        self._fetch_deferred = self._fetch_reason(now, day) not in {None, _NEW_PLAN}
        return self._fetch_deferred

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
            return _NEW_PLAN
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
        return self._chart_reason(snapshot, day)

    def _chart_reason(self, snapshot: PricingSnapshot, day: date) -> str | None:
        """Return why the charts should be fetched again, or None if they'll do."""
        data = self._price_day(snapshot, day)
        if data.tou_error is not None:
            return "time-of-use windows unavailable"
        # Prices still work while charts are missing, so they're only retried daily for a while,
        # and no repair issue is raised. The count is saved, so a restart doesn't reset it.
        if data.missing_charts and snapshot.missing_chart_fetches <= PRICING_MISSING_CHART_RETRIES:
            return "charts missing"
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
            charts=await self._async_charts(_pricing_plan(plan, plans, icp), day),
            charts_plan_id=plan.id,
        )
        data = self._price_day(snapshot, day)
        if data.missing_charts:
            # Counted per plan and gap, so choosing another plan starts again, and so does a gap
            # the last fetch didn't have (a later season's missing chart that now prices today).
            # Gaps are keyed by side and season, so a chart that goes from missing to not
            # matching its rates carries on counting.
            saved = self._snapshot
            previous = (
                saved.missing_chart_fetches
                if saved
                and saved.charts_plan_id == plan.id
                and set(data.chart_gaps) <= set(saved.chart_gaps)
                else 0
            )
            snapshot = replace(
                snapshot,
                chart_gaps=data.chart_gaps,
                missing_chart_fetches=previous + 1,
            )
            # The count doesn't change the prices, so they needn't be worked out again.
            self._priced = (snapshot, day, data)
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
        """Download and read the charts for every published season.

        This season's and the next are downloaded at every fetch; later ones only until they've
        been read and explained once, since they're only needed to know which periods the plan
        uses. Seasons that have ended are downloaded until they've been read once, to price past
        usage for the cost statistics. A chart that can't be downloaded or read keeps its saved
        reading, if there is one.
        """
        saved = self._snapshot.charts if self._snapshot else {}
        refresh = {
            path
            for season in chart_seasons(plan, day, upcoming=False)
            for path in season_charts(plan, season)
        }
        charts: dict[str, ChartReading] = {}
        for season in [*chart_seasons(plan, day, upcoming=True), *ended_seasons(plan, day)]:
            paths = [path for path in season_charts(plan, season) if path not in charts]
            if not refresh.intersection(paths) and season_explained(saved, plan, season, day):
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
        pricing_plan = _pricing_plan(plan, snapshot.plans, snapshot.icp)
        self._check_network_category(plan, pricing_plan, snapshot.icp)
        priced = price_plan_day(snapshot.charts, pricing_plan, day)
        slots = priced.slots
        for error in slots.missing:
            LOGGER.debug(
                "Taking one network rate to apply all day, as its chart can't be used: %s", error
            )
        for error in slots.later_errors:
            LOGGER.debug("Leaving out a season whose charts can't be read: %s", error)
        tou_error = "; ".join(slots.errors) or None
        if tou_error is None:
            self._clear_issue(ISSUE_TOU_CHART)
        data = PricingData(
            icp=snapshot.icp,
            plans=snapshot.plans,
            plan=plan,
            day=day,
            season=season,
            pricing=priced.pricing,
            periods=_plan_periods(pricing_plan, day, priced),
            names=bucket_names(plan),
            fetched_at=snapshot.fetched_at,
            tou_error=tou_error,
            missing_charts=slots.missing,
            later_errors=slots.later_errors,
            chart_gaps=slots.gaps,
        )
        self._priced = (snapshot, day, data)
        return data

    def _check_network_category(self, plan: Plan, pricing_plan: Plan, icp: IcpDetails) -> None:
        """Raise a repair issue when the plan is published for another network price category.

        The network charges the ICP's registered category, so the plan is priced with that
        category's rates (see `billed_plan`) and its advertised network prices are wrong until
        the ICP is moved.
        """
        advertised = network_category(plan)
        if advertised is None or advertised == icp.price_category:
            self._clear_issue(ISSUE_NETWORK_CATEGORY)
            self._clear_issue(ISSUE_NETWORK_CATEGORY_UNPRICED)
            return
        placeholders = {
            "plan": plan.name,
            "plan_category": advertised,
            "icp_category": icp.price_category,
        }
        if pricing_plan.chart_network_rates is not None:
            self._clear_issue(ISSUE_NETWORK_CATEGORY_UNPRICED)
            self._raise_issue(ISSUE_NETWORK_CATEGORY, {**placeholders, "icp": icp.icp})
        else:
            LOGGER.warning(
                "%s is published with %s network rates, and none of the listed plans has the "
                "ICP's %s rates, so it's priced with the %s rates",
                plan.code,
                advertised,
                icp.price_category,
                advertised,
            )
            self._clear_issue(ISSUE_NETWORK_CATEGORY)
            self._raise_issue(ISSUE_NETWORK_CATEGORY_UNPRICED, placeholders)

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


def _plan_periods(plan: Plan, day: date, priced: PlanDay) -> PlanPeriods:
    """Return the periods the plan uses today, in later seasons with readable charts, or before.

    Controlled load needs no charts, so its periods come from every season's rates. A side whose
    charts can't be read today has the periods its rates give (`rate_buckets`), so its sensors
    stay. So does a period only an ended season had (a winter-only peak, say): its periods come
    from its rates, and its sensors don't go when it ends only to come back next year.
    """
    slots, alignments, pricing = priced.slots, priced.alignments, priced.pricing
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
    buckets.update(_ended_periods(plan, day, alignments))
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


def _ended_periods(plan: Plan, day: date, alignments: tuple[Alignment, Alignment]) -> set[str]:
    """Return the periods the rates give in every published season that ended before `day`.

    Its charts may not have been downloaded (they're only kept for the cost statistics), so the
    rates are used.
    """
    buckets: set[str] = set()
    for item in plan.seasons:
        if item.end >= day or (start := first_day(plan, item, item.start)) is None:
            continue
        buckets.update(controlled_prices(plan, start)[0])
        for export, alignment in ((False, alignments[0]), (True, alignments[1])):
            rates = side_rates(plan, start, export=export)
            if not rates.is_empty:
                buckets.update(rate_periods(rates, alignment))
    return buckets


def _day_costs(charts: Mapping[str, ChartReading], plan: Plan, day: date) -> DayCosts | None:
    """Return what a day's usage is priced with, or None if no published season covers it."""
    season = season_for(plan, day)
    if season is None:
        return None
    sides: list[DaySlots | None] = []
    for export in (False, True):
        try:
            slots, _missing = season_slots(charts, plan, season, day, export=export)
        except ChartReadError as err:
            LOGGER.debug("Can't price %s %s: %s", "export" if export else "import", day, err)
            slots = None
        sides.append(slots)
    return DayCosts(sides[0], sides[1], daily_charges(plan, day))


def _pricing_plan(plan: Plan, plans: PlanList, icp: IcpDetails) -> Plan:
    """Return the plan as it's priced for the ICP.

    Its network rates are the ICP's registered price category's, which the network charges, and
    it has no export rates if the ICP can't export.
    """
    billed = billed_plan(plan, plans.all_plans, icp.price_category)
    return billed if icp.has_export else without_export(billed)


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
        pricing: PricingCoordinator,
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
        self._pricing = pricing
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
        cost_points: dict[str, StatisticPoint | None] = {}
        keys, cost_resume = await self._async_cost_resume(
            last_points, cost_points, indicator.data_start, data_end
        )
        if cost_resume is not None:
            resume = min(resume, cost_resume)
        # Always fetch the last complete day too, for the daily summary sensors. Requests start
        # and end at NZ midnight: a response starting partway through the hour repeated when
        # daylight saving ends can't be placed on the right occurrence of it, and a chunk ending
        # mid-hour would split that hour's readings. Hours already imported are skipped.
        fetch_start = max(indicator.data_start, self._local_midnight(min(resume, last_day_start)))

        hours_imported = 0
        seen: set[Series] = set()
        last_day: list[ConsumptionData] = []
        chunk_start = fetch_start
        while chunk_start < data_end:
            chunk_end = min(self._local_midnight(chunk_start, CONSUMPTION_CHUNK.days), data_end)
            data = await self._portal.async_get_half_hourly(chunk_start, chunk_end)
            hours_imported += self._import_energy(data, last_points, settled_before, seen)
            if keys is not None:
                await self._async_import_costs(
                    data, keys, cost_points, settled_before, export=EXPORT in last_points
                )
            if chunk_end > last_day_start:
                last_day.append(data)
            chunk_start = chunk_end
            if chunk_start < data_end:
                await asyncio.sleep(BACKFILL_PAUSE_SECONDS)
        for series in held:
            if last_points[series] is not None:
                self._scanned_until.pop(series, None)
            else:
                # Readings that weren't imported were all in hours the portal may still fill.
                self._scanned_until[series] = settled_before if series in seen else data_end

        if hours_imported:
            LOGGER.debug("Imported %s hours of consumption", hours_imported)
        return self._summarise(indicator, last_day, last_day_start, data_end, hours_imported)

    def _import_energy(
        self,
        data: ConsumptionData,
        last_points: dict[Series, StatisticPoint | None],
        settled_before: datetime,
        seen: set[Series],
    ) -> int:
        """Add a chunk's readings to each held series' statistic, and return the import hours.

        Each series with readings in the chunk is added to `seen`.
        """
        hours_imported = 0
        for series, readings in (
            (IMPORT, data.imported_kwh),
            (EXPORT, data.exported_kwh),
            (CARBON, data.carbon_savings_kg),
        ):
            if series not in last_points or not readings:
                continue
            seen.add(series)
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
        return hours_imported

    async def _async_cost_resume(
        self,
        last_points: dict[Series, StatisticPoint | None],
        points: dict[str, StatisticPoint | None],
        data_start: datetime,
        data_end: datetime,
    ) -> tuple[CostKeys | None, datetime | None]:
        """Return the cost statistic keys and where they carry on from.

        Both are None when nothing can be priced: no import series, no saved pricing, or no day
        it can price. Every cost statistic is a checkpoint, so they carry on from the one
        furthest behind, and one that fell behind (or is new) catches up. One without any hours
        yet starts at the first day the saved pricing can price, rather than reading the
        portal's whole history again, unless its usage series has been read to the end this
        session without any readings (see `_scanned_until`), so it isn't read again every day.
        Their last points go in `points`.
        """
        keys = self._pricing.cost_keys() if IMPORT in last_points else None
        if keys is None:
            return None, None
        sides = [(IMPORT, keys.statistic_keys(export=False))]
        if EXPORT in last_points:
            sides.append((EXPORT, keys.statistic_keys(export=True)))
        first_start: datetime | None = None
        starts: list[datetime] = []
        for usage, side_keys in sides:
            for key in sorted(side_keys):
                point = await async_last_point(self.hass, statistic_id(self.icp, cost_series(key)))
                points[key] = point
                if point is not None:
                    starts.append(point.start + timedelta(hours=1))
                    continue
                if last_points.get(usage) is None and usage in self._scanned_until:
                    continue
                if first_start is None:
                    last_day = data_end.astimezone(self._local_tz).date()
                    if (first := self._pricing.earliest_priced_day(last_day)) is None:
                        continue
                    first_start = max(data_start, self._day_start(first))
                starts.append(first_start)
        if not starts:
            return None, None
        return keys, min(starts)

    async def _async_import_costs(
        self,
        data: ConsumptionData,
        keys: CostKeys,
        points: dict[str, StatisticPoint | None],
        settled_before: datetime,
        *,
        export: bool,
    ) -> None:
        """Price a chunk's readings and add each part's hours to its cost statistic.

        Pricing a month of half-hours takes long enough to hold up the event loop on slower
        hardware, so it runs in the executor.
        """
        day_costs = self._pricing.cost_pricer()
        if day_costs is None:  # pragma: no cover - `keys` needs saved pricing
            return
        costs = await self.hass.async_add_executor_job(
            partial(
                hourly_costs,
                data.imported_kwh,
                data.exported_kwh if export else (),
                day_costs,
                keys,
                tz=self._local_tz,
                import_loss_factor=self._pricing.import_loss_factor,
                export_loss_factor=self._pricing.export_loss_factor,
                settled_before=settled_before,
            )
        )
        for key, hours in costs.items():
            series = cost_series(key)
            if key not in points:  # pragma: no cover - every key comes from `cost_keys`
                # Never write a statistic without its last point, or its sum would restart.
                points[key] = await async_last_point(self.hass, statistic_id(self.icp, series))
            points[key], _added = async_import_hours(
                self.hass, self.icp, series, hours, points[key], settled_before=settled_before
            )

    def _day_start(self, day: date) -> datetime:
        """Return the NZ midnight starting a day."""
        return datetime.combine(day, time.min, self._local_tz)

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

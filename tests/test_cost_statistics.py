"""Tests for the hourly cost statistics worked out from portal readings and saved pricing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ecotricity_nz import coordinator as coordinator_module
from custom_components.ecotricity_nz.api import (
    ConsumptionData,
    EcotricityConnectionError,
    IntervalReading,
    Plan,
)
from custom_components.ecotricity_nz.api.parsers import parse_values
from custom_components.ecotricity_nz.const import (
    CONF_PLAN_ID,
    DOMAIN,
    GST_FACTOR,
    ISSUE_TOU_CHART,
)
from custom_components.ecotricity_nz.coordinator import PricingCoordinator
from custom_components.ecotricity_nz.costs import (
    DAILY_CHARGE_TOTAL,
    EXPORT_GST,
    EXPORT_TOTAL,
    GST,
    IMPORT_COST_TOTAL,
    LINE_ENERGY_SUFFIX,
    TOTAL_ELECTRICITY_COMPENSATION,
    TOTAL_ELECTRICITY_COST,
    TOTAL_ELECTRICITY_NET_BILLED,
    cost_keys,
    daily_key,
    export_key,
    import_key,
    is_line_energy,
)
from custom_components.ecotricity_nz.statistics import (
    COST_UNIT,
    EXPORT,
    IMPORT,
    cost_series,
    statistic_id,
)
from custom_components.ecotricity_nz.tariff import (
    ComponentKind,
    DaySlots,
    billed_plan,
    daily_charges,
    line_key,
    rebill,
    season_for,
    side_rates,
)
from tests.conftest import (
    ANYTIME_PLAN,
    NZ,
    TEST_ICP,
    TIME_OF_USE_PLAN,
    WINTER_PEAK,
    chart_file,
    default_plan_where,
    explained_chart,
    find_plan,
    load_json_fixture,
    needs_network_category_change,
    plan_id,
    season_chart_name,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

# The day the portal fixture has readings for, in the plan's first season.
PORTAL_DAY = date(2026, 4, 5)
# Friday 16 October 2026, once the portal fixture's season has ended.
AFTER_PORTAL_SEASON = datetime(2026, 10, 15, 20, 15, tzinfo=UTC)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def _sums(hass: HomeAssistant) -> dict[str, float]:
    """Return the running sum of every cost statistic and bill line's kWh written, by its key."""
    metadata = await get_instance(hass).async_add_executor_job(get_metadata, hass)
    prefix = f"{DOMAIN}:{TEST_ICP.lower()}_"
    sums: dict[str, float] = {}
    for stat_id, (_, meta) in metadata.items():
        key = stat_id.removeprefix(prefix)
        if meta["unit_of_measurement"] != COST_UNIT and not is_line_energy(key):
            continue
        result = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, stat_id, True, {"sum"}
        )
        sums[key] = float(result[stat_id][0]["sum"] or 0.0)
    return sums


def _expected(
    plan: Plan, day: date, loss_factor: float, billed: Plan | None = None
) -> dict[str, float]:
    """Return what the portal fixture's day costs, part by part, from the plan's own charts.

    Each half-hour is priced with its rates as the season's chart explains them. Ecotricity's
    import rates are charged on usage plus losses; GST is added to import, daily charges and export.
    With `billed`, the plan as it's billed (see `billed_plan`), its network rates are used instead
    of the chart's.
    """
    season = season_for(plan, day)
    assert season is not None
    chart = chart_file(season_chart_name(plan, season, weekend=day.weekday() >= 5))
    sides = explained_chart(chart)
    values = parse_values(load_json_fixture("ecotricity/portal_values_dst_end.json"), NZ)
    totals: dict[str, float] = defaultdict(float)
    for export, readings in ((False, values.imported_kwh), (True, values.exported_kwh)):
        _rates, slots = sides[export]
        if billed is not None:
            slots = rebill(DaySlots(slots, slots), side_rates(billed, day, export=export)).weekday
        for reading in readings:
            local = reading.start.astimezone(NZ)
            for part in slots[local.hour * 2 + local.minute // 30].components:
                losses = not export and part.kind is ComponentKind.SUPPLIER
                key = export_key(part) if export else import_key(part)
                cost = reading.value * part.price * (loss_factor if losses else 1) / 100
                totals[key] += cost
                # A free rate isn't a bill line, so its kWh aren't counted.
                if part.price and (line := line_key(part, export=export)) is not None:
                    totals[line] += cost
                    totals[line + LINE_ENERGY_SUFFIX] += reading.value
    energy = sum(value for key, value in totals.items() if key.startswith("import_cost_"))
    # Every line the plan has gets a statistic, zero where it never applied.
    plan = billed or plan
    keys = cost_keys(plan)
    for line in keys.import_lines | keys.export_lines:
        totals[line] += 0.0
        totals[line + LINE_ENERGY_SUFFIX] += 0.0
    daily = 0.0
    for charge in daily_charges(plan, day):
        totals[daily_key(charge)] += charge.price / 100
        daily += charge.price / 100
    exported = sum(
        value for key, value in totals.items() if key.startswith("export_") and key != EXPORT_TOTAL
    )
    totals[IMPORT_COST_TOTAL] = energy
    totals[DAILY_CHARGE_TOTAL] = daily
    totals[GST] = (energy + daily) * (GST_FACTOR - 1)
    totals[TOTAL_ELECTRICITY_COST] = (energy + daily) * GST_FACTOR
    totals[EXPORT_TOTAL] = exported
    totals[EXPORT_GST] = exported * (GST_FACTOR - 1)
    totals[TOTAL_ELECTRICITY_COMPENSATION] = -exported * GST_FACTOR
    totals[TOTAL_ELECTRICITY_NET_BILLED] = totals[TOTAL_ELECTRICITY_COST] + exported * GST_FACTOR
    return dict(totals)


def _loss_factor(entry: MockConfigEntry) -> float:
    return entry.runtime_data.pricing.loss_factor


@pytest.mark.usefixtures("mock_onboarding", "mock_portal")
async def test_usage_is_priced_into_hourly_cost_statistics(
    hass: HomeAssistant, portal_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, portal_config_entry)

    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, _loss_factor(portal_config_entry))
    sums = await _sums(hass)
    assert {key for key, value in expected.items() if value} <= sums.keys() <= expected.keys()
    for key, value in expected.items():
        assert sums.get(key, 0.0) == pytest.approx(value), key
    # A credit stays negative, so it's drawn below zero in a stacked chart, except in the Energy
    # dashboard's compensation, which counts money received as positive.
    assert sums[EXPORT_TOTAL] < 0
    assert sums[TOTAL_ELECTRICITY_COMPENSATION] > 0

    metadata = await get_instance(hass).async_add_executor_job(get_metadata, hass)
    network = statistic_id(TEST_ICP, cost_series("import_cost_network"))
    assert metadata[network][1]["name"] == f"Ecotricity {TEST_ICP} import cost (network)"
    assert metadata[network][1]["unit_class"] is None

    # A second run adds nothing: every hour is already priced.
    await portal_config_entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    assert await _sums(hass) == sums


@pytest.mark.usefixtures("mock_onboarding")
async def test_portal_without_an_import_series_writes_no_costs(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Costs and daily charges go with import, so without its series nothing can be priced, but
    # the series the portal does hold are still imported.
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator, features=frozenset({"POWER_ACTIVE_SUPPLY_CONSUMPTION"})
    )
    await _setup(hass, portal_config_entry)

    assert await _sums(hass) == {}
    exported = statistic_id(TEST_ICP, EXPORT)
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, exported, True, {"sum"}
    )
    assert result[exported]


@pytest.mark.usefixtures("mock_portal")
async def test_plan_for_another_category_is_costed_with_the_icps_network_rates(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # The network bills the ICP's registered category, not the one the plan was published with.
    freezer.move_to(WINTER_PEAK)
    plans = mock_onboarding.async_get_plans.return_value
    plan = default_plan_where(
        lambda plan: (
            needs_network_category_change(plan)
            and plan.timeframe_chart_weekday is not None
            and any(season.contains(PORTAL_DAY) for season in plan.seasons)
        )
    )
    billed = billed_plan(plan, plans.all_plans, plan.price_category)
    portal_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        portal_config_entry, options={**portal_config_entry.options, CONF_PLAN_ID: plan.id}
    )
    assert await hass.config_entries.async_setup(portal_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    loss_factor = _loss_factor(portal_config_entry)
    expected = _expected(plan, PORTAL_DAY, loss_factor, billed)
    sums = await _sums(hass)
    for key, value in expected.items():
        assert sums.get(key, 0.0) == pytest.approx(value), key
    advertised = _expected(plan, PORTAL_DAY, loss_factor)
    network = "import_cost_network"
    assert sums[network] != pytest.approx(advertised[network])


@pytest.mark.usefixtures("mock_portal")
async def test_ended_seasons_charts_are_downloaded_once_to_price_past_usage(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(AFTER_PORTAL_SEASON)
    await _setup(hass, portal_config_entry)

    plan = find_plan(ANYTIME_PLAN)
    ended = season_for(plan, PORTAL_DAY)
    assert ended is not None
    assert ended.end < AFTER_PORTAL_SEASON.astimezone(NZ).date()
    ended_charts = {season_chart_name(plan, ended, weekend=weekend) for weekend in (False, True)}

    def downloads() -> list[str]:
        return [
            path.rsplit("/", 1)[-1]
            for (path,) in (call.args for call in mock_onboarding.async_get_chart.await_args_list)
            if path.rsplit("/", 1)[-1] in ended_charts
        ]

    assert sorted(downloads()) == sorted(ended_charts)
    expected = _expected(plan, PORTAL_DAY, _loss_factor(portal_config_entry))
    sums = await _sums(hass)
    for key, value in expected.items():
        assert sums.get(key, 0.0) == pytest.approx(value), key

    # Once read, an ended season's charts are kept rather than downloaded again.
    assert await portal_config_entry.runtime_data.pricing.async_fetch_now()
    assert len(downloads()) == len(ended_charts)


@pytest.mark.usefixtures("mock_portal")
async def test_ended_seasons_chart_that_cant_be_downloaded_is_not_retried_daily(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(AFTER_PORTAL_SEASON)
    plan = find_plan(ANYTIME_PLAN)
    ended = season_for(plan, PORTAL_DAY)
    assert ended is not None
    ended_charts = {season_chart_name(plan, ended, weekend=weekend) for weekend in (False, True)}
    serve = mock_onboarding.async_get_chart.side_effect

    async def without_ended(path: str) -> bytes:
        if path.rsplit("/", 1)[-1] in ended_charts:
            raise EcotricityConnectionError(f"Chart {path} returned HTTP 404")
        return serve(path)

    mock_onboarding.async_get_chart.side_effect = without_ended
    await _setup(hass, portal_config_entry)

    # Today's prices don't depend on it, so it's no chart error, gap or repair issue.
    pricing = portal_config_entry.runtime_data.pricing
    assert pricing.data.tou_error is None
    assert pricing.data.missing_charts == ()
    issue_id = f"{ISSUE_TOU_CHART}_{portal_config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    # Nor is it a reason to fetch: the next day's check makes no requests.
    fetches = mock_onboarding.async_get_plans.await_count
    freezer.tick(timedelta(days=1))
    await pricing.async_refresh()
    assert mock_onboarding.async_get_plans.await_count == fetches


@pytest.mark.usefixtures("mock_portal")
async def test_icp_without_an_export_register_has_no_export_costs(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    icp = mock_onboarding.async_get_icp.return_value
    mock_onboarding.async_get_icp.return_value = replace(
        icp, channels=tuple(channel for channel in icp.channels if channel.register_code != "EG")
    )
    await _setup(hass, portal_config_entry)

    sums = await _sums(hass)
    assert TOTAL_ELECTRICITY_COST in sums
    assert not [key for key in sums if key.startswith("export_")]


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.review("TEST-15")
async def test_costs_start_once_the_usage_can_be_priced(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # The portal has history from before the first day the saved pricing covers.
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator, data_start=datetime(2026, 3, 1, 0, 0, tzinfo=NZ)
    )

    # Usage is imported while none of it can be priced, so there are no costs yet.
    with patch.object(PricingCoordinator, "day_costs", return_value=None):
        await _setup(hass, portal_config_entry)
    assert await _sums(hass) == {}
    energy = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id(TEST_ICP, IMPORT), True, {"sum"}
    )
    assert energy

    # Once it can, costs are worked out from the first day the pricing covers, not the start of
    # the portal's history.
    mock_portal.async_get_half_hourly.reset_mock()
    consumption = portal_config_entry.runtime_data.consumption
    assert await consumption.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    plan = portal_config_entry.runtime_data.pricing.data.plan
    first = min(season.start for season in plan.seasons)
    start, _ = mock_portal.async_get_half_hourly.await_args_list[0].args
    assert start == datetime.combine(first, time.min, NZ)
    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, _loss_factor(portal_config_entry))
    sums = await _sums(hass)
    assert sums[TOTAL_ELECTRICITY_COST] == pytest.approx(expected[TOTAL_ELECTRICITY_COST])


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.review("TEST-15")
async def test_cost_statistic_that_fell_behind_the_others_catches_up(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # The portal's latest day comes after the day with readings, so that day isn't fetched
    # again for the daily sensors. The net billed total stops partway through it while every
    # other part is written.
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator, data_end=datetime.combine(PORTAL_DAY + timedelta(days=2), time.min, NZ)
    )
    stalled = datetime.combine(PORTAL_DAY, time(12, 0), NZ).astimezone(UTC)
    price = coordinator_module.hourly_costs

    def stalling(*args: Any, **kwargs: Any) -> dict[str, dict[datetime, float]]:
        costs = price(*args, **kwargs)
        net = costs[TOTAL_ELECTRICITY_NET_BILLED]
        costs[TOTAL_ELECTRICITY_NET_BILLED] = {hour: v for hour, v in net.items() if hour < stalled}
        return costs

    with patch.object(coordinator_module, "hourly_costs", side_effect=stalling):
        await _setup(hass, portal_config_entry)
    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, _loss_factor(portal_config_entry))
    behind = await _sums(hass)
    assert behind[TOTAL_ELECTRICITY_NET_BILLED] != pytest.approx(
        expected[TOTAL_ELECTRICITY_NET_BILLED]
    )

    # The next sync carries on from the statistic furthest behind, so it fills in the rest of
    # the day, and the parts already written aren't added twice.
    consumption = portal_config_entry.runtime_data.consumption
    assert await consumption.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    sums = await _sums(hass)
    for key, value in expected.items():
        assert sums.get(key, 0.0) == pytest.approx(value), key


def _day_readings(day: date, kwh: float = 0.5) -> tuple[IntervalReading, ...]:
    """Return a reading for every half-hour of an NZ day, stepping in UTC across DST changes."""
    start = datetime.combine(day, time.min, NZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, NZ).astimezone(UTC)
    readings = []
    while start < end:
        readings.append(IntervalReading(start, start + timedelta(minutes=30), kwh))
        start += timedelta(minutes=30)
    return tuple(readings)


def _serve(mock_portal: MagicMock, *days: date) -> None:
    """Have the portal hold import readings for these NZ days, and nothing else."""
    readings = tuple(reading for day in days for reading in _day_readings(day))
    first = datetime.combine(min(days), time.min, NZ)
    last = datetime.combine(max(days) + timedelta(days=1), time.min, NZ)
    mock_portal.async_get_indicator.return_value = replace(
        mock_portal.async_get_indicator.return_value, data_start=first, data_end=last
    )

    async def half_hourly(start: datetime, end: datetime) -> ConsumptionData:
        return ConsumptionData(
            imported_kwh=tuple(reading for reading in readings if start <= reading.start < end)
        )

    mock_portal.async_get_half_hourly.side_effect = half_hourly


async def _hours(hass: HomeAssistant, key: str, start: datetime) -> list[Any]:
    """Return each hour of a statistic from `start`, with its change."""
    stat_id = statistic_id(TEST_ICP, IMPORT if key == IMPORT.key else cost_series(key))
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {stat_id}, "hour", None, {"change"}
    )
    return list(rows.get(stat_id, []))


def _daily_total(plan: Plan, day: date) -> float:
    return sum(charge.price for charge in daily_charges(plan, day)) / 100


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.review("TEST-15")
async def test_usage_before_the_pricing_year_gets_no_costs(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A first backfill reads the portal's whole history, which starts before any published
    # season. Those days' usage is imported, but they can't be priced.
    freezer.move_to(WINTER_PEAK)
    plan = find_plan(ANYTIME_PLAN)
    first = min(season.start for season in plan.seasons)
    before = first - timedelta(days=1)
    _serve(mock_portal, before, first)

    await _setup(hass, portal_config_entry)

    since = datetime.combine(before, time.min, NZ).astimezone(UTC)
    energy = await _hours(hass, IMPORT.key, since)
    assert datetime.fromtimestamp(energy[0]["start"], UTC) == since
    costs = await _hours(hass, TOTAL_ELECTRICITY_COST, since)
    priced_from = datetime.combine(first, time.min, NZ).astimezone(UTC)
    assert datetime.fromtimestamp(costs[0]["start"], UTC) == priced_from
    daily = await _hours(hass, DAILY_CHARGE_TOTAL, since)
    assert sum(row["change"] for row in daily) == pytest.approx(_daily_total(plan, first))


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.review("TEST-02")
async def test_day_daylight_saving_starts_is_imported_and_priced_in_23_hours(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    day = date(2026, 9, 27)
    freezer.move_to(datetime(2026, 9, 29, 20, 0, tzinfo=UTC))
    _serve(mock_portal, day)

    await _setup(hass, portal_config_entry)

    since = datetime.combine(day, time.min, NZ).astimezone(UTC)
    energy = await _hours(hass, IMPORT.key, since)
    assert len(energy) == 23
    assert sum(row["change"] for row in energy) == pytest.approx(46 * 0.5)
    daily = await _hours(hass, DAILY_CHARGE_TOTAL, since)
    assert len(daily) == 23
    assert sum(row["change"] for row in daily) == pytest.approx(
        _daily_total(find_plan(ANYTIME_PLAN), day)
    )


_ONBOARDING_CALLS = ("async_get_icp", "async_get_plans", "async_get_plan_titles", "async_get_chart")


@pytest.mark.usefixtures("mock_portal")
@pytest.mark.review("TEST-13")
async def test_costs_are_worked_out_from_saved_pricing_with_no_requests(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    # Nothing to import while the entry sets up.
    mock_portal.async_get_indicator.return_value = replace(
        indicator, data_start=None, data_end=None
    )
    await _setup(hass, portal_config_entry)
    assert await _sums(hass) == {}

    # Offline: a request to the onboarding site would fail.
    calls = {name: getattr(mock_onboarding, name).await_count for name in _ONBOARDING_CALLS}
    for name in _ONBOARDING_CALLS:
        getattr(mock_onboarding, name).side_effect = EcotricityConnectionError("offline")
    mock_portal.async_get_indicator.return_value = indicator
    assert await portal_config_entry.runtime_data.consumption.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    assert {name: getattr(mock_onboarding, name).await_count for name in _ONBOARDING_CALLS} == calls
    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, _loss_factor(portal_config_entry))
    assert (await _sums(hass))[TOTAL_ELECTRICITY_COST] == pytest.approx(
        expected[TOTAL_ELECTRICITY_COST]
    )


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.review("TEST-12")
async def test_choosing_another_plan_prices_later_usage_with_it(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Hours already priced keep their costs; only later usage uses the new plan.
    day = date(2026, 6, 15)
    freezer.move_to(datetime(2026, 6, 17, 20, 0, tzinfo=UTC))
    _serve(mock_portal, day)
    await _setup(hass, portal_config_entry)
    before = await _sums(hass)
    assert before[TOTAL_ELECTRICITY_COST]

    new_plan = find_plan(TIME_OF_USE_PLAN)
    hass.config_entries.async_update_entry(
        portal_config_entry,
        options={**portal_config_entry.options, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)},
    )
    # As the options flow does when it saves.
    assert await hass.config_entries.async_reload(portal_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    # Reloaded with the new plan, the day already priced isn't priced again. Lines only the new
    # plan has are new statistics, which catch up from the first day the pricing covers.
    reloaded = await _sums(hass)
    assert {key: reloaded[key] for key in before} == before

    later = day + timedelta(days=1)
    _serve(mock_portal, day, later)
    assert await portal_config_entry.runtime_data.consumption.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    # The next day's usage, 0.5 kWh every half-hour, is priced at the new plan's rates.
    season = season_for(new_plan, later)
    assert season is not None
    _rates, slots = explained_chart(chart_file(season_chart_name(new_plan, season)))[False]
    loss = _loss_factor(portal_config_entry)
    supplier = sum(
        0.5 * part.price * loss / 100
        for slot in slots
        for part in slot.components
        if part.kind is ComponentKind.SUPPLIER
    )
    after = await _sums(hass)
    assert after["import_cost_supplier"] - before["import_cost_supplier"] == pytest.approx(supplier)

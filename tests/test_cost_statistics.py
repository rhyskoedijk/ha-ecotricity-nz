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
    statistic_during_period,
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
    LINE_ENERGY_SUFFIX,
    TOTAL_ELECTRICITY_COMPENSATION,
    TOTAL_ELECTRICITY_COST,
    cost_keys,
    daily_key,
    is_line_energy,
    levy_key,
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
    without_export,
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
    plan: Plan, day: date, loss_factors: tuple[float, float], billed: Plan | None = None
) -> dict[str, float]:
    """Return what the portal fixture's day costs, part by part, from the plan's own charts.

    Each half-hour is priced with its rates as the season's chart explains them. Ecotricity's
    rates are charged on the kWh plus losses, with `loss_factors` (import's, then export's); GST is
    added to import, daily charges and export.
    With `billed`, the plan as it's billed (see `billed_plan`), its network rates are used instead
    of the chart's.
    """
    charged = {False: 0.0, True: 0.0}
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
                losses = loss_factors[export] if part.kind is ComponentKind.SUPPLIER else 1.0
                cost = reading.value * part.price * losses / 100
                charged[export] += cost
                if part.kind is ComponentKind.LEVY:
                    if not export:
                        totals[levy_key(part)] += cost
                        totals[levy_key(part) + LINE_ENERGY_SUFFIX] += reading.value
                # A free rate isn't a bill line, so its kWh aren't counted.
                elif part.price and (line := line_key(part, export=export)) is not None:
                    totals[line] += cost
                    totals[line + LINE_ENERGY_SUFFIX] += reading.value
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
    totals[TOTAL_ELECTRICITY_COST] = (charged[False] + daily) * GST_FACTOR
    totals[TOTAL_ELECTRICITY_COMPENSATION] = -charged[True] * GST_FACTOR
    return dict(totals)


def _lines(sums: dict[str, float], prefix: str) -> float:
    """Return what the bill lines starting with a prefix, such as line_import_network_, add to."""
    return sum(
        value for key, value in sums.items() if key.startswith(prefix) and not is_line_energy(key)
    )


def _loss_factors(entry: MockConfigEntry) -> tuple[float, float]:
    pricing = entry.runtime_data.pricing
    return pricing.import_loss_factor, pricing.export_loss_factor


@pytest.mark.usefixtures("mock_onboarding", "mock_portal")
async def test_usage_is_priced_into_hourly_cost_statistics(
    hass: HomeAssistant, portal_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, portal_config_entry)

    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, _loss_factors(portal_config_entry))
    sums = await _sums(hass)
    assert {key for key, value in expected.items() if value} <= sums.keys() <= expected.keys()
    for key, value in expected.items():
        assert sums.get(key, 0.0) == pytest.approx(value), key
    # A credit stays negative, so it's drawn below zero in a stacked chart, except in the Energy
    # dashboard's compensation, which counts money received as positive.
    assert _lines(sums, "line_export_") < 0
    assert sums[TOTAL_ELECTRICITY_COMPENSATION] > 0

    metadata = await get_instance(hass).async_add_executor_job(get_metadata, hass)
    levy = statistic_id(TEST_ICP, cost_series("line_import_ea_levy"))
    assert metadata[levy][1]["name"] == f"Ecotricity {TEST_ICP} line (import ea levy)"
    assert metadata[levy][1]["unit_class"] is None

    # A second run adds nothing: every hour is already priced.
    await portal_config_entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    assert await _sums(hass) == sums


@pytest.mark.usefixtures("mock_portal")
async def test_export_is_credited_with_the_icps_generation_losses(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Like the consumption loss factor on import, a generation loss factor other than 1 applies
    # to Ecotricity's export rates.
    freezer.move_to(WINTER_PEAK)
    icp = mock_onboarding.async_get_icp.return_value
    mock_onboarding.async_get_icp.return_value = replace(icp, loss_factor_generation=1.02)
    await _setup(hass, portal_config_entry)

    loss_factors = _loss_factors(portal_config_entry)
    assert loss_factors[True] == 1.02
    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, loss_factors)
    without = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, (loss_factors[False], 1.0))
    assert expected[TOTAL_ELECTRICITY_COMPENSATION] != pytest.approx(
        without[TOTAL_ELECTRICITY_COMPENSATION]
    )
    sums = await _sums(hass)
    for key, value in expected.items():
        assert sums.get(key, 0.0) == pytest.approx(value), key


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

    loss_factors = _loss_factors(portal_config_entry)
    expected = _expected(plan, PORTAL_DAY, loss_factors, billed)
    sums = await _sums(hass)
    for key, value in expected.items():
        assert sums.get(key, 0.0) == pytest.approx(value), key
    advertised = _expected(plan, PORTAL_DAY, loss_factors)
    network = "line_import_network_"
    assert _lines(sums, network) != pytest.approx(_lines(advertised, network))


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
    expected = _expected(plan, PORTAL_DAY, _loss_factors(portal_config_entry))
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
    assert TOTAL_ELECTRICITY_COMPENSATION not in sums
    assert not [key for key in sums if key.startswith("line_export_")]


@pytest.mark.usefixtures("mock_onboarding")
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
    with patch.object(PricingCoordinator, "cost_pricer", return_value=lambda _day: None):
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
    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, _loss_factors(portal_config_entry))
    sums = await _sums(hass)
    assert sums[TOTAL_ELECTRICITY_COST] == pytest.approx(expected[TOTAL_ELECTRICITY_COST])


@pytest.mark.usefixtures("mock_onboarding")
async def test_charts_are_explained_once_for_the_days_of_a_season_with_the_same_rates(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Every day of a season with the same rates has the same half-hours, so a month of usage
    # doesn't explain the charts again for each day.
    days = [date(2026, 6, 1) + timedelta(days=offset) for offset in range(30)]
    freezer.move_to(datetime(2026, 7, 2, 20, 0, tzinfo=UTC))
    _serve(mock_portal, *days)
    plan = find_plan(ANYTIME_PLAN)
    assert {season_for(plan, day) for day in days} == {season_for(plan, days[0])}
    with patch.object(
        coordinator_module, "season_slots", wraps=coordinator_module.season_slots
    ) as explain:
        portal_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(portal_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)

    assert await _sums(hass)
    # Once for each side, whichever day of the season comes first (finding where costs start
    # prices the season's first day).
    assert explain.call_count == 2


@pytest.mark.usefixtures("mock_onboarding")
async def test_cost_statistic_that_fell_behind_the_others_catches_up(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # The portal's latest day comes after the day with readings, so that day isn't fetched
    # again for the daily sensors. The total cost stops partway through it while every other part
    # is written.
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator, data_end=datetime.combine(PORTAL_DAY + timedelta(days=2), time.min, NZ)
    )
    stalled = datetime.combine(PORTAL_DAY, time(12, 0), NZ).astimezone(UTC)
    price = coordinator_module.hourly_costs

    def stalling(*args: Any, **kwargs: Any) -> dict[str, dict[datetime, float]]:
        costs = price(*args, **kwargs)
        total = costs[TOTAL_ELECTRICITY_COST]
        costs[TOTAL_ELECTRICITY_COST] = {hour: v for hour, v in total.items() if hour < stalled}
        return costs

    with patch.object(coordinator_module, "hourly_costs", side_effect=stalling):
        await _setup(hass, portal_config_entry)
    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, _loss_factors(portal_config_entry))
    behind = await _sums(hass)
    assert behind[TOTAL_ELECTRICITY_COST] != pytest.approx(expected[TOTAL_ELECTRICITY_COST])

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


def _serve(mock_portal: MagicMock, *days: date, export: bool = False) -> None:
    """Have the portal hold import readings for these NZ days, and export ones if asked."""
    readings = tuple(reading for day in days for reading in _day_readings(day))
    exported = readings if export else ()
    first = datetime.combine(min(days), time.min, NZ)
    last = datetime.combine(max(days) + timedelta(days=1), time.min, NZ)
    mock_portal.async_get_indicator.return_value = replace(
        mock_portal.async_get_indicator.return_value, data_start=first, data_end=last
    )

    async def half_hourly(start: datetime, end: datetime) -> ConsumptionData:
        return ConsumptionData(
            imported_kwh=tuple(reading for reading in readings if start <= reading.start < end),
            exported_kwh=tuple(reading for reading in exported if start <= reading.start < end),
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


async def _daily_charged(hass: HomeAssistant, plan: Plan, day: date, start: datetime) -> float:
    """Return what the daily charge items' statistics have charged from `start`."""
    charged = 0.0
    for key in sorted({daily_key(charge) for charge in daily_charges(plan, day)}):
        charged += sum(row["change"] for row in await _hours(hass, key, start))
    return charged


@pytest.mark.usefixtures("mock_onboarding")
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
    assert await _daily_charged(hass, plan, first, since) == pytest.approx(
        _daily_total(plan, first)
    )


@pytest.mark.usefixtures("mock_onboarding")
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
    assert len(await _hours(hass, TOTAL_ELECTRICITY_COST, since)) == 23
    plan = find_plan(ANYTIME_PLAN)
    assert await _daily_charged(hass, plan, day, since) == pytest.approx(_daily_total(plan, day))


_ONBOARDING_CALLS = ("async_get_icp", "async_get_plans", "async_get_plan_titles", "async_get_chart")


@pytest.mark.usefixtures("mock_portal")
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
    expected = _expected(find_plan(ANYTIME_PLAN), PORTAL_DAY, _loss_factors(portal_config_entry))
    assert (await _sums(hass))[TOTAL_ELECTRICITY_COST] == pytest.approx(
        expected[TOTAL_ELECTRICITY_COST]
    )


@pytest.mark.usefixtures("mock_onboarding")
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
    # Reloaded with the new plan, the day already priced isn't priced again, not even by the
    # lines only the new plan has.
    reloaded = await _sums(hass)
    assert {key: reloaded[key] for key in before} == before
    assert not {key: total for key, total in reloaded.items() if key not in before and total}

    later = day + timedelta(days=1)
    _serve(mock_portal, day, later)
    assert await portal_config_entry.runtime_data.consumption.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    # The next day's usage, 0.5 kWh every half-hour, is priced at the new plan's rates.
    season = season_for(new_plan, later)
    assert season is not None
    _rates, slots = explained_chart(chart_file(season_chart_name(new_plan, season)))[False]
    loss, _export_loss = _loss_factors(portal_config_entry)
    supplier = sum(
        0.5 * part.price * loss / 100
        for slot in slots
        for part in slot.components
        if part.kind is ComponentKind.SUPPLIER
    )
    since = datetime.combine(later, time.min, NZ).astimezone(UTC)
    charged = 0.0
    for key in await _sums(hass):
        if key.startswith("line_import_supplier_") and not is_line_energy(key):
            charged += sum(row["change"] for row in await _hours(hass, key, since))
    assert charged == pytest.approx(supplier)
    # The new plan's lines charge nothing for the day the old plan already priced.
    first = datetime.combine(day, time.min, NZ).astimezone(UTC)
    for key in set(await _sums(hass)) - set(before):
        assert not [
            row
            for row in await _hours(hass, key, first)
            if row["start"] < since.timestamp() and row["change"]
        ], key


async def _choose_plan(hass: HomeAssistant, entry: MockConfigEntry, role: str) -> None:
    """Choose another plan in the options, which reloads the entry as the options flow does."""
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PLAN_ID: plan_id(role)}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def _sync(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await entry.runtime_data.consumption.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


def _plan_keys(entry: MockConfigEntry) -> set[str]:
    keys = entry.runtime_data.pricing.cost_keys()
    assert keys is not None
    return set(keys.statistic_keys(export=False) | keys.statistic_keys(export=True))


async def _change(hass: HomeAssistant, key: str, start: datetime, end: datetime) -> float:
    stat_id = statistic_id(TEST_ICP, cost_series(key))
    result = await get_instance(hass).async_add_executor_job(
        statistic_during_period, hass, start, end, stat_id, {"change"}, None
    )
    return float(result["change"] or 0.0)


@pytest.mark.usefixtures("mock_onboarding")
async def test_choosing_an_earlier_plan_again_doesnt_cost_hours_another_plan_costed(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A day each with one plan, another plan, then the first again. The first plan's own
    # statistics already have hours, but they carry on after the other plan's day, not into it.
    first = date(2026, 6, 15)
    second, third = first + timedelta(days=1), first + timedelta(days=2)
    freezer.move_to(datetime(2026, 6, 19, 20, 0, tzinfo=UTC))
    _serve(mock_portal, first)
    await _setup(hass, portal_config_entry)
    earlier = _plan_keys(portal_config_entry)

    await _choose_plan(hass, portal_config_entry, TIME_OF_USE_PLAN)
    _serve(mock_portal, first, second)
    await _sync(hass, portal_config_entry)
    only_earlier = earlier - _plan_keys(portal_config_entry)

    await _choose_plan(hass, portal_config_entry, ANYTIME_PLAN)
    _serve(mock_portal, first, second, third)
    await _sync(hass, portal_config_entry)

    starts = [datetime.combine(day, time.min, NZ).astimezone(UTC) for day in (first, second, third)]
    end = starts[2] + timedelta(days=1)
    charged = {}
    for key in sorted(only_earlier):
        assert await _change(hass, key, starts[1], starts[2]) == 0.0, key
        # The same usage on a day of the same season costs the same, from wherever it starts.
        charged[key] = await _change(hass, key, starts[0], starts[1])
        assert await _change(hass, key, starts[2], end) == pytest.approx(charged[key]), key
    assert any(charged.values())


@pytest.mark.usefixtures("mock_onboarding")
async def test_choosing_a_plan_with_export_rates_doesnt_cost_earlier_export(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # The first plan has no export rates, so the export side has no costed hours to say when
    # the plan changed. Export before the change still isn't costed with the new plan.
    first = date(2026, 6, 15)
    second = first + timedelta(days=1)
    freezer.move_to(datetime(2026, 6, 18, 20, 0, tzinfo=UTC))
    _serve(mock_portal, first, export=True)
    pricing_plan = coordinator_module._pricing_plan

    def no_export_rates(plan: Plan, plans: Any, icp: Any) -> Plan:
        priced = pricing_plan(plan, plans, icp)
        return without_export(priced) if plan.id == plan_id(ANYTIME_PLAN) else priced

    with patch.object(coordinator_module, "_pricing_plan", side_effect=no_export_rates):
        await _setup(hass, portal_config_entry)
    sums = await _sums(hass)
    assert sums[TOTAL_ELECTRICITY_COST]
    assert not sums.get(TOTAL_ELECTRICITY_COMPENSATION)

    await _choose_plan(hass, portal_config_entry, TIME_OF_USE_PLAN)
    _serve(mock_portal, first, second, export=True)
    await _sync(hass, portal_config_entry)

    starts = [datetime.combine(day, time.min, NZ).astimezone(UTC) for day in (first, second)]
    end = starts[1] + timedelta(days=1)
    assert await _change(hass, TOTAL_ELECTRICITY_COMPENSATION, starts[0], starts[1]) == 0.0
    assert await _change(hass, TOTAL_ELECTRICITY_COMPENSATION, starts[1], end) > 0.0


@pytest.mark.usefixtures("mock_onboarding")
async def test_choosing_a_plan_with_export_rates_again_doesnt_cost_export_another_plan_had_none_for(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A day with a plan with export rates, one with a plan without them, then the first again.
    # The first plan's export statistics stopped before the other plan's day, but they carry on
    # after it, not into it.
    first = date(2026, 6, 15)
    second, third = first + timedelta(days=1), first + timedelta(days=2)
    freezer.move_to(datetime(2026, 6, 19, 20, 0, tzinfo=UTC))
    pricing_plan = coordinator_module._pricing_plan

    def no_export_rates(plan: Plan, plans: Any, icp: Any) -> Plan:
        priced = pricing_plan(plan, plans, icp)
        return without_export(priced) if plan.id == plan_id(ANYTIME_PLAN) else priced

    portal_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        portal_config_entry,
        options={**portal_config_entry.options, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)},
    )
    _serve(mock_portal, first, export=True)
    with patch.object(coordinator_module, "_pricing_plan", side_effect=no_export_rates):
        assert await hass.config_entries.async_setup(portal_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)

        await _choose_plan(hass, portal_config_entry, ANYTIME_PLAN)
        _serve(mock_portal, first, second, export=True)
        await _sync(hass, portal_config_entry)

        await _choose_plan(hass, portal_config_entry, TIME_OF_USE_PLAN)
        _serve(mock_portal, first, second, third, export=True)
        await _sync(hass, portal_config_entry)

    starts = [datetime.combine(day, time.min, NZ).astimezone(UTC) for day in (first, second, third)]
    end = starts[2] + timedelta(days=1)
    credited = await _change(hass, TOTAL_ELECTRICITY_COMPENSATION, starts[0], starts[1])
    assert credited > 0.0
    assert await _change(hass, TOTAL_ELECTRICITY_COMPENSATION, starts[1], starts[2]) == 0.0
    assert await _change(hass, TOTAL_ELECTRICITY_COMPENSATION, starts[2], end) == pytest.approx(
        credited
    )


@pytest.mark.usefixtures("mock_onboarding")
async def test_hours_a_rate_isnt_charged_in_are_left_out_without_changing_any_ranges_cost(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # On a time-of-use plan each of Ecotricity's rates applies only in some hours. Those hours
    # get no row, but the recorder works out a range's change from the row before it, so a range
    # starting at any hour must still cost what its hours do.
    day = date(2026, 6, 15)
    freezer.move_to(datetime(2026, 6, 17, 20, 0, tzinfo=UTC))
    _serve(mock_portal, day)
    portal_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        portal_config_entry,
        options={**portal_config_entry.options, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)},
    )
    assert await hass.config_entries.async_setup(portal_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    plan = find_plan(TIME_OF_USE_PLAN)
    season = season_for(plan, day)
    assert season is not None
    _rates, slots = explained_chart(chart_file(season_chart_name(plan, season)))[False]
    loss, _export_loss = _loss_factors(portal_config_entry)
    # Each supplier line's cost in every hour of the day, zero where it doesn't apply.
    start = datetime.combine(day, time.min, NZ).astimezone(UTC)
    hours = [start + timedelta(hours=hour) for hour in range(24)]
    expected: dict[str, dict[datetime, float]] = defaultdict(lambda: dict.fromkeys(hours, 0.0))
    for index, slot in enumerate(slots):
        for part in slot.components:
            line = line_key(part, export=False)
            if part.kind is ComponentKind.SUPPLIER and part.price and line is not None:
                expected[line][hours[index // 2]] += 0.5 * part.price * loss / 100
    assert len(expected) > 1
    end = start + timedelta(days=1)
    for line, costs in expected.items():
        stat_id = statistic_id(TEST_ICP, cost_series(line))
        rows = await _hours(hass, line, start)
        assert len(rows) < len(hours), line
        for hour in hours:
            result = await get_instance(hass).async_add_executor_job(
                statistic_during_period, hass, hour, end, stat_id, {"change"}, None
            )
            remaining = sum(cost for when, cost in costs.items() if when >= hour)
            assert result["change"] == pytest.approx(remaining, abs=1e-6), (line, hour)

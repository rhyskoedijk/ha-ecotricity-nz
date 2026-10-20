"""Tests for setting up the integration, its sensors and the consumption import."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.image import DATA_COMPONENT as IMAGE_DATA_COMPONENT
from homeassistant.components.recorder.statistics import get_last_statistics, get_metadata
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ecotricity_nz import coordinator as coordinator_module
from custom_components.ecotricity_nz import tariff as tariff_module
from custom_components.ecotricity_nz.api import (
    AttestationError,
    AuthError,
    EcotricityConnectionError,
    Plan,
    PlanList,
    Rate,
    RateUnit,
    Season,
)
from custom_components.ecotricity_nz.api.parsers import parse_icp
from custom_components.ecotricity_nz.api.timeframes import (
    ChartReading,
    LegendEntry,
    read_chart,
    shown_price,
)
from custom_components.ecotricity_nz.const import (
    CONF_INCLUDE_GST,
    CONF_PLAN_ID,
    CONSUMPTION_RECHECK_INTERVAL,
    CONSUMPTION_UPDATE_INTERVAL,
    DOMAIN,
    FAILURE_ISSUE_THRESHOLD,
    GST_FACTOR,
    PRICING_FETCH_INTERVAL,
    PRICING_RETRY_INTERVAL,
    RETRY_AFTER_FAILURE,
)
from custom_components.ecotricity_nz.statistics import CARBON, EXPORT, IMPORT, statistic_id
from custom_components.ecotricity_nz.tariff import (
    day_slots,
    plan_alignment,
    price_side,
    side_rates,
)
from tests.conftest import (
    ANYTIME_PLAN,
    DEFAULT_NETWORK,
    DEFAULT_PRICE_CATEGORY,
    NZ,
    SOLAR_PLAN,
    TEST_ICP,
    TIME_OF_USE_PLAN,
    WINTER_PEAK,
    chart_file,
    chart_name,
    chart_reading,
    extra_network_codes,
    find_plan,
    find_plan_where,
    has_extra_network_buckets,
    has_overlapping_seasons,
    import_rates,
    load_network_fixture,
    plan_id,
    stored_charts,
)

PREFIX = f"sensor.icp_{TEST_ICP.lower()}"


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, options: dict[str, object] | None = None
) -> None:
    entry.add_to_hass(hass)
    if options is not None:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def _state(hass: HomeAssistant, key: str) -> str:
    state = hass.states.get(f"{PREFIX}_{key}")
    assert state is not None, key
    return state.state


@pytest.mark.usefixtures("mock_onboarding")
async def test_pricing_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "plan") == "Low ecoSAVER"
    assert _state(hass, "pricing_season") == "Seasonal Rates 04/2026 - 09/2026"
    assert _state(hass, "current_import_period") == "peak"
    # Only the periods this plan uses are options.
    assert _extra(hass, "current_import_period")["options"] == ["peak", "off_peak"]
    assert _extra(hass, "current_export_period")["options"] == ["peak", "off_peak"]
    # Winter peak: 37.3476c + GST. Export: 13.74c anytime + 2c network peak, credited as
    # published because GST isn't added to export credits.
    assert float(_state(hass, "current_import_price")) == pytest.approx(0.4295)
    assert float(_state(hass, "current_export_price")) == pytest.approx(-0.1574)
    assert float(_state(hass, "daily_charge")) == pytest.approx(1.7825)

    plan = hass.states.get(f"{PREFIX}_plan")
    assert plan is not None
    assert plan.attributes["internal_name"] == "Low Off Peak"

    # 11:05am is off peak, and the price changes at the half-hour boundary without a poll.
    freezer.move_to(WINTER_PEAK + timedelta(hours=2, minutes=50))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _state(hass, "current_import_period") == "off_peak"
    assert float(_state(hass, "current_import_price")) == pytest.approx(0.2765)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.usefixtures("mock_onboarding")
async def test_gst_option(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(
        hass, config_entry, {CONF_INCLUDE_GST: False, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)}
    )

    assert float(_state(hass, "current_import_price")) == pytest.approx(0.3735)
    # Export credits are paid as published either way.
    assert float(_state(hass, "current_export_price")) == pytest.approx(-0.1574)


@pytest.mark.usefixtures("mock_onboarding")
async def test_options_flow_reloads(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_INCLUDE_GST: False, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.options[CONF_INCLUDE_GST] is False
    assert float(_state(hass, "current_import_price")) == pytest.approx(0.3735)


async def test_flat_plan_needs_no_chart(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Low User outside winter: one import price all day, so no chart is needed.
    freezer.move_to(datetime(2026, 10, 15, 0, 0, tzinfo=UTC))
    serve = mock_onboarding.async_get_chart.side_effect
    mock_onboarding.async_get_chart.side_effect = EcotricityConnectionError("down")
    await _setup(hass, config_entry, {CONF_PLAN_ID: plan_id(ANYTIME_PLAN)})

    assert _state(hass, "plan") == "Low ecoANYTIME"
    assert float(_state(hass, "current_import_price")) == pytest.approx(0.2825)
    coordinator = config_entry.runtime_data.pricing
    assert coordinator.data.tou_error is None

    # The network's one rate is taken to apply all day, which the chart might not agree with, so
    # it's fetched again at the next day's check until the chart is read.
    assert coordinator.data.missing_charts
    calls = mock_onboarding.async_get_icp.await_count
    freezer.tick(timedelta(days=1))
    await coordinator.async_refresh()
    assert mock_onboarding.async_get_icp.await_count == calls + 1

    mock_onboarding.async_get_chart.side_effect = serve
    freezer.tick(timedelta(days=1))
    await coordinator.async_refresh()
    assert not coordinator.data.missing_charts
    calls = mock_onboarding.async_get_icp.await_count
    freezer.tick(timedelta(days=1))
    await coordinator.async_refresh()
    assert mock_onboarding.async_get_icp.await_count == calls


@pytest.mark.usefixtures("mock_onboarding")
async def test_consumption_imports_statistics(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, portal_config_entry)
    await async_wait_recording_done(hass)

    assert _state(hass, "plan") == "Low ecoANYTIME"
    plan = hass.states.get(f"{PREFIX}_plan")
    assert plan is not None
    assert plan.attributes["internal_name"] == "Low User"

    # The DST-end fixture day has 50 half-hours: 0.50, 0.51, ... 0.99 kWh.
    assert float(_state(hass, "latest_day_imported")) == pytest.approx(37.25)
    # Export repeats 0, 0.1, 0.2, 0.3: twelve full cycles plus 0 and 0.1.
    assert float(_state(hass, "latest_day_exported")) == pytest.approx(7.3)
    assert float(_state(hass, "latest_day_carbon_savings")) == pytest.approx(2.5)

    stats = await _last_stat(hass, statistic_id(TEST_ICP, IMPORT))
    assert stats["sum"] == pytest.approx(37.25)
    assert datetime.fromtimestamp(stats["start"], UTC) == datetime(2026, 4, 5, 11, 0, tzinfo=UTC)
    # The daily sensors point at the statistics holding the full history.
    assert _extra(hass, "latest_day_imported")["statistic_id"] == statistic_id(TEST_ICP, IMPORT)
    assert _extra(hass, "latest_day_exported")["statistic_id"] == statistic_id(TEST_ICP, EXPORT)
    assert _extra(hass, "latest_day_carbon_savings")["statistic_id"] == (
        "ecotricity_nz:0000123456ab1c2_carbon_savings"
    )

    # The IDs match the statistic names and the daily sensors: imported and exported.
    assert statistic_id(TEST_ICP, IMPORT) == "ecotricity_nz:0000123456ab1c2_energy_imported"
    assert statistic_id(TEST_ICP, EXPORT) == "ecotricity_nz:0000123456ab1c2_energy_exported"

    # Carbon savings are a statistic too: 0.05 kg every half-hour.
    assert statistic_id(TEST_ICP, CARBON) == "ecotricity_nz:0000123456ab1c2_carbon_savings"
    carbon = await _last_stat(hass, statistic_id(TEST_ICP, CARBON))
    assert carbon["sum"] == pytest.approx(2.5)

    # The names match the daily sensors, which is how they're listed in the Energy dashboard.
    metadata = await get_instance(hass).async_add_executor_job(get_metadata, hass)
    assert {stat_id: meta["name"] for stat_id, (_, meta) in metadata.items()} == {
        statistic_id(TEST_ICP, IMPORT): f"Ecotricity {TEST_ICP} imported",
        statistic_id(TEST_ICP, EXPORT): f"Ecotricity {TEST_ICP} exported",
        statistic_id(TEST_ICP, CARBON): f"Ecotricity {TEST_ICP} carbon savings",
    }

    # A second run adds nothing: every complete hour is already imported.
    await portal_config_entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    stats = await _last_stat(hass, statistic_id(TEST_ICP, IMPORT))
    assert stats["sum"] == pytest.approx(37.25)
    assert portal_config_entry.runtime_data.consumption.data.hours_imported == 0


async def _last_stat(hass: HomeAssistant, stat_id: str) -> dict[str, float]:
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    return dict(result[stat_id][0])  # type: ignore[arg-type]


async def test_unlisted_plan_falls_back_and_raises_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    # A plan Ecotricity no longer lists for this ICP.
    await _setup(hass, config_entry, {CONF_PLAN_ID: "9999"})

    plans: PlanList = mock_onboarding.async_get_plans.return_value
    titles = mock_onboarding.async_get_plan_titles.return_value
    assert _state(hass, "plan") == plans.with_titles(titles).default.name
    issue_id = f"plan_not_found_{config_entry.entry_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id)

    # Choosing a listed plan again clears it.
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_INCLUDE_GST: True, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)}
    )
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not issue_registry.async_get_issue(DOMAIN, issue_id)


@pytest.mark.parametrize("error", [AttestationError("blocked"), EcotricityConnectionError("down")])
async def test_setup_retries_on_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    error: Exception,
) -> None:
    mock_onboarding.async_get_icp.side_effect = error
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_options_flow_while_setup_is_retrying(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_onboarding: MagicMock
) -> None:
    mock_onboarding.async_get_icp.side_effect = EcotricityConnectionError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    # Plans aren't known, so only the GST option is offered, and the chosen plan is kept.
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert list(result["data_schema"].schema) == [CONF_INCLUDE_GST]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_INCLUDE_GST: False}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.options == {
        CONF_INCLUDE_GST: False,
        CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN),
    }


async def test_repeated_attestation_failures_raise_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.pricing
    issue_id = f"attestation_failed_{config_entry.entry_id}"

    mock_onboarding.async_get_icp.side_effect = AttestationError("blocked")
    for _ in range(FAILURE_ISSUE_THRESHOLD):
        assert not await coordinator.async_fetch_now()
    assert issue_registry.async_get_issue(DOMAIN, issue_id)
    # The saved pricing carries on meanwhile.
    assert coordinator.last_update_success
    assert _state(hass, "current_import_price") != "unavailable"

    mock_onboarding.async_get_icp.side_effect = None
    assert await coordinator.async_fetch_now()
    assert not issue_registry.async_get_issue(DOMAIN, issue_id)


async def test_unreadable_chart_leaves_price_unavailable(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    mock_onboarding.async_get_chart.side_effect = None
    mock_onboarding.async_get_chart.return_value = b"not a png"
    await _setup(hass, config_entry)

    assert _state(hass, "current_import_price") == "unavailable"
    assert _state(hass, "current_import_period") == "unavailable"
    # The daily charge doesn't need the chart.
    assert float(_state(hass, "daily_charge")) == pytest.approx(1.7825)

    # Counted per fetch, not per check, since charts are only downloaded when pricing is fetched.
    coordinator = config_entry.runtime_data.pricing
    issue_id = f"tou_chart_unreadable_{config_entry.entry_id}"
    for _ in range(FAILURE_ISSUE_THRESHOLD):
        await coordinator.async_refresh()
    assert not issue_registry.async_get_issue(DOMAIN, issue_id)
    for _ in range(FAILURE_ISSUE_THRESHOLD - 1):
        await coordinator.async_fetch_now()
    assert issue_registry.async_get_issue(DOMAIN, issue_id)


async def test_unreadable_chart_prices_periods_from_the_rates(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A new install whose charts can't be read still has a price sensor for each period, priced
    # from the plan's rates as the readable chart would price it.
    freezer.move_to(WINTER_PEAK)
    mock_onboarding.async_get_chart.side_effect = None
    mock_onboarding.async_get_chart.return_value = b"not a png"
    await _setup(hass, config_entry)

    plan = find_plan(TIME_OF_USE_PLAN)
    day = WINTER_PEAK.astimezone(NZ).date()
    chart = chart_reading(chart_file(chart_name(plan, day)))
    slots = day_slots(side_rates(plan, day, export=False), chart, chart, export=False)
    expected = price_side(slots, plan_alignment([slots])).buckets
    assert len(expected) > 1
    for bucket, cents in expected.items():
        price = await _enabled_price(hass, config_entry, entity_registry, bucket.lower())
        assert float(price) == pytest.approx(cents * GST_FACTOR / 100, abs=1e-4)
    assert _state(hass, "current_import_price") == "unavailable"


@pytest.mark.usefixtures("mock_onboarding")
async def test_price_sensors_are_kept_while_the_chart_cannot_be_read(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Which periods the plan has isn't known until the chart is read, so none are removed.
    freezer.move_to(WINTER_PEAK)
    mock_onboarding.async_get_chart.side_effect = None
    mock_onboarding.async_get_chart.return_value = b"not a png"
    config_entry.add_to_hass(hass)
    kept = entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{TEST_ICP}_price_charge_3", config_entry=config_entry
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get(kept.entity_id) is not None
    # Its entity is still provided, rather than left in the registry as no longer provided.
    assert hass.states.get(kept.entity_id) is not None


@pytest.mark.usefixtures("mock_onboarding")
async def test_chart_templates_without_season_dates(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A plan can name one fixed chart rather than a template with the season's dates.
    freezer.move_to(WINTER_PEAK)
    plan = find_plan(TIME_OF_USE_PLAN)
    season = next(item for item in plan.seasons if item.contains(WINTER_PEAK.date()))

    def fixed(template: str | None) -> str:
        assert template is not None
        return template.replace("SSSSSSSS", season.start.strftime("%Y%m%d")).replace(
            "EEEEEEEE", season.end.strftime("%Y%m%d")
        )

    fixed_plan = replace(
        plan,
        timeframe_chart_weekday=fixed(plan.timeframe_chart_weekday),
        timeframe_chart_weekend=fixed(plan.timeframe_chart_weekend),
    )
    mock_onboarding.async_get_plans.return_value = PlanList(default=fixed_plan)
    await _setup(hass, config_entry)

    assert config_entry.runtime_data.pricing.data.tou_error is None
    assert _state(hass, "current_import_price") != "unavailable"


async def test_fixed_chart_plan_has_later_seasons_controlled_periods(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # With one fixed chart, today's is the only chart to explain, once for each side. A later
    # season's controlled rate needs no chart, so it still has its sensor from now.
    freezer.move_to(WINTER_PEAK)
    plan = find_plan(TIME_OF_USE_PLAN)
    season = next(item for item in plan.seasons if item.contains(WINTER_PEAK.date()))
    later = next(item for item in plan.seasons if item.start > season.end)

    def fixed(template: str | None) -> str:
        assert template is not None
        return template.replace("SSSSSSSS", season.start.strftime("%Y%m%d")).replace(
            "EEEEEEEE", season.end.strftime("%Y%m%d")
        )

    night = Rate(
        id="c1",
        description="Controlled night",
        charge="CHARGE_LOW",
        register_type="CN",
        unit=RateUnit.PER_KWH,
        raw_unit="cents/kWh",
        price=9.0,
        is_network=False,
        valid_start=later.start,
        valid_end=later.end,
    )
    fixed_plan = replace(
        plan,
        timeframe_chart_weekday=fixed(plan.timeframe_chart_weekday),
        timeframe_chart_weekend=fixed(plan.timeframe_chart_weekend),
        retail_rates=(*plan.retail_rates, night),
    )
    mock_onboarding.async_get_plans.return_value = PlanList(default=fixed_plan)
    with patch.object(
        tariff_module, "season_slots", wraps=tariff_module.season_slots
    ) as season_slots:
        await _setup(hass, config_entry, {CONF_PLAN_ID: fixed_plan.id})

    assert season_slots.call_count == 2
    assert config_entry.runtime_data.pricing.data.tou_error is None
    assert await _enabled_price(hass, config_entry, entity_registry, "cn_charge_low") == "unknown"


@pytest.mark.usefixtures("mock_onboarding")
async def test_refreshes_after_nz_midnight(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # 30 September is the last day of the winter season; 1 October starts the next one.
    freezer.move_to(datetime(2026, 9, 30, 10, 0, tzinfo=UTC))  # 11pm NZDT
    await _setup(hass, config_entry)
    assert _state(hass, "pricing_season") == "Seasonal Rates 04/2026 - 09/2026"
    calls = mock_onboarding.async_get_icp.await_count

    freezer.move_to(datetime(2026, 9, 30, 11, 1, tzinfo=UTC))  # just after NZ midnight
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    # The new season comes from the saved pricing. Fetching waits for the daily check.
    assert mock_onboarding.async_get_icp.await_count == calls
    assert _state(hass, "pricing_season") == "Seasonal Rates 10/2026 - 12/2026"


@pytest.mark.usefixtures("mock_onboarding")
async def test_portal_without_data_yet(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = type(indicator)(
        indicator_id="ind", display_name=TEST_ICP
    )
    await _setup(hass, portal_config_entry)

    assert _state(hass, "latest_day_imported") == "unknown"
    mock_portal.async_get_half_hourly.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "reauth"),
    [(AuthError("no"), True), (EcotricityConnectionError("down"), False)],
)
@pytest.mark.usefixtures("mock_onboarding")
async def test_consumption_errors(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
    error: Exception,
    reauth: bool,
) -> None:
    freezer.move_to(WINTER_PEAK)
    mock_portal.async_get_indicator.side_effect = error
    await _setup(hass, portal_config_entry)

    consumption = portal_config_entry.runtime_data.consumption
    assert consumption is not None
    assert not consumption.last_update_success
    assert _state(hass, "latest_day_imported") == "unavailable"
    flows = [flow["context"]["source"] for flow in hass.config_entries.flow.async_progress()]
    assert ("reauth" in flows) is reauth


async def test_pricing_retries_soon_after_a_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed fetch keeps the saved pricing, and is tried again an hour later."""
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    fetched = _state(hass, "last_pricing_update")

    # The weekly fetch fails.
    mock_onboarding.async_get_icp.side_effect = EcotricityConnectionError("down")
    freezer.tick(PRICING_FETCH_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    calls = mock_onboarding.async_get_icp.await_count
    assert calls > 1
    assert _state(hass, "current_import_price") != "unavailable"
    assert _state(hass, "last_pricing_update") == fetched

    mock_onboarding.async_get_icp.side_effect = None
    freezer.tick(PRICING_RETRY_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_onboarding.async_get_icp.await_count == calls + 1
    assert _state(hass, "last_pricing_update") != fetched


@pytest.mark.usefixtures("mock_onboarding")
async def test_consumption_retries_soon_after_a_failure(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed import, including the first backfill, is retried well before the next day."""
    freezer.move_to(WINTER_PEAK)
    mock_portal.async_get_indicator.side_effect = EcotricityConnectionError("down")
    await _setup(hass, portal_config_entry)
    consumption = portal_config_entry.runtime_data.consumption
    assert consumption is not None
    assert not consumption.last_update_success

    mock_portal.async_get_indicator.side_effect = None
    freezer.tick(RETRY_AFTER_FAILURE + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert consumption.last_update_success


NEW_SEASON = Season(name="Seasonal Rates", start=date(2027, 4, 1), end=date(2027, 9, 30))
NEW_SEASON_CHART = b"new season chart"


def _with_new_season(plans: PlanList) -> PlanList:
    """Publish an extra season, starting 1 April 2027, that adds shoulder rates.

    Both Ecotricity and the network charge a shoulder rate in it, from noon to midnight.
    """
    plan = plans.find(plan_id(TIME_OF_USE_PLAN))
    assert plan is not None

    def shoulder(description: str, price: float, *, network: bool) -> Rate:
        return Rate(
            id=f"900{int(network)}",
            description=description,
            charge="CHARGE_3",
            register_type=None,
            unit=RateUnit.PER_KWH,
            raw_unit="cents/kWh",
            price=price,
            is_network=network,
            season=NEW_SEASON.name,
            valid_start=NEW_SEASON.start,
            valid_end=NEW_SEASON.end,
        )

    updated = replace(
        plan,
        retail_rates=(*plan.retail_rates, shoulder("Shoulder Usage", 18.5, network=False)),
        network_rates=(*plan.network_rates, shoulder("Uncontrolled - Shoulder", 8.5, network=True)),
        seasons=(*plan.seasons, NEW_SEASON),
    )
    return PlanList(
        default=updated if plans.default is plan else plans.default,
        alternative=updated if plans.alternative is plan else plans.alternative,
        others=tuple(updated if other is plan else other for other in plans.others),
    )


def _new_season_reading(plan: Plan) -> ChartReading:
    """Return the new season's legend: off-peak to noon, then shoulder."""
    rates = side_rates(plan, NEW_SEASON.start, export=False)
    prices = {
        combo.supplier: shown_price(combo.price)
        for combo in rates.candidates()
        if combo.network == "CHARGE_3"
    }
    return ChartReading(
        import_entries=(
            LegendEntry(0, 720, prices["CHARGE_LOW"]),
            LegendEntry(720, 1440, prices["CHARGE_3"]),
        ),
        export_entries=(LegendEntry(0, 1440, "-13.74"),),
    )


@pytest.fixture
def new_season_charts(mock_onboarding: MagicMock) -> Generator[None]:
    """Serve a chart for the new season, as Ecotricity does once it's published."""
    serve = mock_onboarding.async_get_chart.side_effect
    plan = _with_new_season(mock_onboarding.async_get_plans.return_value).find(
        plan_id(TIME_OF_USE_PLAN)
    )
    assert plan is not None

    async def chart(path: str) -> bytes:
        return NEW_SEASON_CHART if "-20270401-" in path else serve(path)

    def read(data: bytes) -> ChartReading:
        return _new_season_reading(plan) if data == NEW_SEASON_CHART else read_chart(data)

    mock_onboarding.async_get_chart.side_effect = chart
    with patch.object(coordinator_module, "read_chart", side_effect=read):
        yield


@pytest.mark.usefixtures("new_season_charts")
async def test_new_season_adds_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    unique_id = f"{TEST_ICP}_price_charge_3"
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None
    assert "shoulder" not in _extra(hass, "current_import_period")["options"]

    # Ecotricity publishes next year's seasons part way through this one.
    mock_onboarding.async_get_plans.return_value = _with_new_season(
        mock_onboarding.async_get_plans.return_value
    )
    await config_entry.runtime_data.pricing.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id) is not None
    # The period sensor can show the new period from now on, named from Ecotricity's rate.
    assert "shoulder" in _extra(hass, "current_import_period")["options"]
    season = hass.states.get(f"{PREFIX}_pricing_season")
    assert season is not None
    assert "2027-04-01/2027-09-30" in season.attributes["published"]
    assert season.attributes["end"] == date(2026, 9, 30)


@pytest.mark.usefixtures("new_season_charts")
async def test_prices_the_plan_no_longer_has_are_removed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    plans: PlanList = mock_onboarding.async_get_plans.return_value
    mock_onboarding.async_get_plans.return_value = _with_new_season(plans)
    await _setup(hass, config_entry)
    shoulder = f"{TEST_ICP}_price_charge_3"
    peak = f"{TEST_ICP}_price_charge_high"
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, shoulder) is not None

    # Ecotricity withdraws the season with the shoulder rate.
    mock_onboarding.async_get_plans.return_value = plans
    await config_entry.runtime_data.pricing.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get_entity_id("sensor", DOMAIN, shoulder) is None
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, peak) is not None
    assert "shoulder" not in _extra(hass, "current_import_period")["options"]

    # If it comes back, so does its sensor.
    mock_onboarding.async_get_plans.return_value = _with_new_season(plans)
    await config_entry.runtime_data.pricing.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get_entity_id("sensor", DOMAIN, shoulder) is not None


@pytest.mark.usefixtures("mock_onboarding")
async def test_price_sensors_are_kept_while_a_later_seasons_chart_cannot_be_read(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Today's chart reads, but a later season's doesn't, so a period only that season has isn't
    # known yet. Its sensor is kept rather than removed.
    freezer.move_to(WINTER_PEAK)
    plans: PlanList = mock_onboarding.async_get_plans.return_value
    mock_onboarding.async_get_plans.return_value = _with_new_season(plans)
    serve = mock_onboarding.async_get_chart.side_effect

    def chart(path: str) -> bytes:
        if "-20270401-" in path:
            raise EcotricityConnectionError("down")
        return serve(path)

    mock_onboarding.async_get_chart.side_effect = chart
    config_entry.add_to_hass(hass)
    shoulder = entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{TEST_ICP}_price_charge_3", config_entry=config_entry
    )
    split = entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{TEST_ICP}_current_supplier_import_period", config_entry=config_entry
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    data = config_entry.runtime_data.pricing.data
    assert data.tou_error is None
    assert not data.periods.complete
    # Both are still provided: the shoulder price has no value today, and the supplier period
    # follows today's chart.
    assert entity_registry.async_get(shoulder.entity_id) is not None
    shoulder_state = hass.states.get(shoulder.entity_id)
    assert shoulder_state is not None
    assert shoulder_state.state == "unknown"
    split_state = hass.states.get(split.entity_id)
    assert split_state is not None
    assert split_state.state == _state(hass, "current_import_period")

    # Once every season left can be read, a period none of them has is removed as usual.
    mock_onboarding.async_get_plans.return_value = plans
    await config_entry.runtime_data.pricing.async_fetch_now()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.runtime_data.pricing.data.periods.complete
    assert entity_registry.async_get(shoulder.entity_id) is None
    assert entity_registry.async_get(split.entity_id) is None


@pytest.mark.usefixtures("mock_onboarding")
async def test_stale_price_sensors_are_removed_at_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Left over from a plan chosen before, with a price this plan doesn't have.
    freezer.move_to(WINTER_PEAK)
    config_entry.add_to_hass(hass)
    stale = entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{TEST_ICP}_price_charge_3", config_entry=config_entry
    )
    other = entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{TEST_ICP}_pricing_season", config_entry=config_entry
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get(stale.entity_id) is None
    assert entity_registry.async_get(other.entity_id) is not None


@pytest.mark.usefixtures("new_season_charts")
async def test_later_seasons_charts_are_downloaded_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Every published season's charts are read, so the plan's periods are known for the year.
    # Seasons after the next are only downloaded until they've been read once.
    freezer.move_to(WINTER_PEAK)
    mock_onboarding.async_get_plans.return_value = _with_new_season(
        mock_onboarding.async_get_plans.return_value
    )
    await _setup(hass, config_entry)

    def later_season_downloads() -> int:
        return sum(
            "-20270401-" in call.args[0] for call in mock_onboarding.async_get_chart.call_args_list
        )

    assert later_season_downloads() == 2
    freezer.tick(PRICING_FETCH_INTERVAL)
    await config_entry.runtime_data.pricing.async_refresh()
    assert mock_onboarding.async_get_icp.await_count == 2
    assert later_season_downloads() == 2


async def test_later_seasons_chart_is_downloaded_again_until_its_prices_are_explained(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A later season's chart reads, but its prices don't match the plan's rates (it was drawn
    # before the rates were published, say), so it's downloaded again at each fetch.
    freezer.move_to(WINTER_PEAK)
    mock_onboarding.async_get_plans.return_value = _with_new_season(
        mock_onboarding.async_get_plans.return_value
    )
    serve = mock_onboarding.async_get_chart.side_effect
    unexplained = ChartReading(
        import_entries=(LegendEntry(0, 1440, "99.99"),),
        export_entries=(LegendEntry(0, 1440, "-13.74"),),
    )

    async def chart(path: str) -> bytes:
        return NEW_SEASON_CHART if "-20270401-" in path else serve(path)

    def read(data: bytes) -> ChartReading:
        return unexplained if data == NEW_SEASON_CHART else read_chart(data)

    def later_season_downloads() -> int:
        return sum(
            "-20270401-" in call.args[0] for call in mock_onboarding.async_get_chart.call_args_list
        )

    mock_onboarding.async_get_chart.side_effect = chart
    with patch.object(coordinator_module, "read_chart", side_effect=read):
        await _setup(hass, config_entry)
        coordinator = config_entry.runtime_data.pricing
        assert not coordinator.data.periods.complete
        assert later_season_downloads() == 2

        freezer.tick(PRICING_FETCH_INTERVAL)
        await coordinator.async_refresh()
        assert later_season_downloads() == 4


async def test_pricing_is_fetched_weekly(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.pricing
    icp_calls = mock_onboarding.async_get_icp.await_count
    chart_calls = mock_onboarding.async_get_chart.await_count
    assert chart_calls > 0

    # Checks within the week work from the saved pricing, across midnights too.
    for _ in range(6):
        freezer.tick(timedelta(days=1))
        await coordinator.async_refresh()
    assert mock_onboarding.async_get_icp.await_count == icp_calls
    assert mock_onboarding.async_get_chart.await_count == chart_calls

    # A week after the last fetch, everything is fetched again, charts included.
    freezer.tick(timedelta(days=1))
    await coordinator.async_refresh()
    assert mock_onboarding.async_get_icp.await_count == icp_calls + 1
    assert mock_onboarding.async_get_chart.await_count == 2 * chart_calls


@pytest.mark.parametrize(
    ("supplier", "network", "price"),
    [
        # Ecotricity's credit outweighs the network's charge: a credit overall.
        (-10.0, 2.0, -0.08),
        # The network's charge outweighs Ecotricity's credit: a cost overall.
        (-1.0, 2.0, 0.01),
        # A credit too small to show, shown as 0 rather than -0.
        (-0.001, 0.0, 0.0),
    ],
)
async def test_export_price_keeps_its_sign(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
    supplier: float,
    network: float,
    price: float,
) -> None:
    # A credit is negative and a cost positive, in the state and in each part, with no GST.
    freezer.move_to(WINTER_PEAK)
    plans: PlanList = mock_onboarding.async_get_plans.return_value
    rates = [
        Rate(
            id=f"900{index}",
            description=description,
            charge="CHARGE_24",
            register_type="EG",
            unit=RateUnit.PER_KWH,
            raw_unit="cents/kWh",
            price=cents,
            is_network=is_network,
        )
        for index, (description, cents, is_network) in enumerate(
            (("Export", supplier, False), ("Network export", network, True))
        )
    ]
    # The plan's own export rates are swapped for these.
    plan = replace(
        plans.default,
        retail_rates=(
            *(rate for rate in plans.default.retail_rates if not rate.is_export),
            rates[0],
        ),
        network_rates=(
            *(rate for rate in plans.default.network_rates if not rate.is_export),
            rates[1],
        ),
    )
    mock_onboarding.async_get_plans.return_value = PlanList(default=plan)
    await _setup(hass, config_entry)

    state = _state(hass, "current_export_price")
    assert float(state) == pytest.approx(price)
    assert state != "-0.0"
    # Zero parts add nothing, so they're left out.
    expected = {
        key: pytest.approx(cents / 100, abs=1e-4)
        for key, cents in (("supplier_rate", supplier), ("network_rate", network))
        if cents
    }
    parts = _extra(hass, "current_export_price")
    parts.pop("period")
    assert parts == expected


async def test_code_without_a_fixed_period_is_named_from_the_plan(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    plans: PlanList = mock_onboarding.async_get_plans.return_value
    unnamed = Rate(
        id="9002",
        description="Network usage",
        charge="CHARGE_9",
        register_type=None,
        unit=RateUnit.PER_KWH,
        raw_unit="cents/kWh",
        price=12.0,
        is_network=False,
    )
    plan = replace(plans.default, retail_rates=(unnamed,), network_rates=())
    mock_onboarding.async_get_plans.return_value = PlanList(default=plan)
    await _setup(hass, config_entry)

    # The period comes from the rate's description, without "usage".
    assert _state(hass, "current_import_period") == "network"
    assert _extra(hass, "current_import_period")["windows"] == ["00:00-24:00 network"]
    assert _extra(hass, "current_import_period")["options"] == ["network"]
    assert _extra(hass, "current_import_price")["period"] == "network"
    assert float(_state(hass, "current_import_price")) == pytest.approx(0.138)


def _extra_buckets_plan(config_entry: MockConfigEntry) -> tuple[Plan, date, dict[str, str]]:
    """Return a fixture plan whose network charges for more periods than peak and off-peak.

    Each extra charge code's rates are described as "Period A Usage", "Period B Usage" and so on,
    so the test knows the names to expect whichever network the plan is from. Descriptions don't
    change prices, so the plan's own charts still fit. Also returns a day with a stored chart, and
    the name given to each extra code.
    """
    plan = find_plan_where(has_extra_network_buckets)
    day = stored_charts(plan)[0][0]
    names = {
        code: f"Period {chr(ord('A') + index)}"
        for index, code in enumerate(extra_network_codes(plan, day))
    }

    def renamed(rates: tuple[Rate, ...]) -> tuple[Rate, ...]:
        return tuple(
            replace(rate, description=f"{names[rate.charge]} Usage")
            if rate.charge in names
            else rate
            for rate in rates
        )

    plan = replace(
        plan,
        id=str(config_entry.options[CONF_PLAN_ID]),
        retail_rates=renamed(plan.retail_rates),
        network_rates=renamed(plan.network_rates),
    )
    return plan, day, names


def _key(name: str) -> str:
    return name.lower().replace(" ", "_")


async def test_price_sensors_are_named_from_the_plan(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    plan, day, names = _extra_buckets_plan(config_entry)
    freezer.move_to(datetime.combine(day, time(8, 15), NZ))
    mock_onboarding.async_get_plans.return_value = PlanList(default=plan)
    await _setup(hass, config_entry)

    entities = {
        entry.unique_id.removeprefix(f"{TEST_ICP}_"): entry.entity_id.removeprefix(f"{PREFIX}_")
        for entry in er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
        if "_price_" in entry.unique_id and "_price_eg_" not in entry.unique_id
    }
    # Fixed codes keep their fixed names; the others take the plan's own descriptions.
    fixed = {"CHARGE_HIGH": "peak", "CHARGE_LOW": "off_peak"}
    codes = {rate.charge for rate in import_rates(plan.network_rates, day)}
    periods = {fixed[code] for code in codes if code in fixed} | {
        _key(name) for name in names.values()
    }
    expected = {code: f"{fixed[code]}_import_price" for code in codes if code in fixed}
    expected |= {code: f"{_key(name)}_import_price" for code, name in names.items()}
    assert entities == {f"price_{code.lower()}": entity for code, entity in expected.items()}
    assert set(_extra(hass, "current_import_period")["options"]) == periods


async def test_price_sensor_names_follow_the_plan(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    plan, day, names = _extra_buckets_plan(config_entry)
    code, name = next(iter(names.items()))
    freezer.move_to(datetime.combine(day, time(8, 15), NZ))
    mock_onboarding.async_get_plans.return_value = PlanList(default=plan)
    await _setup(hass, config_entry)
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{TEST_ICP}_price_{code.lower()}"
    )
    assert entity_id is not None
    entity_registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(entity_id)
    assert state is not None
    # Names are shown in sentence case.
    assert state.attributes["friendly_name"].endswith(f"{name.capitalize()} import price")

    # Named prices are broken down like any other: the anytime energy rate, the period's network
    # rate and the levy, each plus GST.
    anytime = sum(rate.price for rate in import_rates(plan.retail_rates, day))
    network = sum(
        rate.price for rate in import_rates(plan.network_rates, day) if rate.charge == code
    )
    levies = [
        rate
        for rate in plan.rates
        if rate.is_levy and rate.unit is RateUnit.PER_KWH and rate.applies_on(day)
    ]
    gst = 1.15 / 100
    total = anytime + network + sum(levy.price for levy in levies)
    assert float(state.state) == pytest.approx(total * gst, abs=0.0001)
    assert _extra(hass, f"{_key(name)}_import_price") == {
        "supplier_rate": pytest.approx(anytime * gst, abs=0.0001),
        "network_rate": pytest.approx(network * gst, abs=0.0001),
        **{_key(levy.description): pytest.approx(levy.price * gst, abs=0.0001) for levy in levies},
    }

    # Ecotricity renames the rate, then drops its description altogether.
    number = code.removeprefix("CHARGE_")
    for description, renamed in (("Renamed Usage", "Renamed"), ("", f"Charge {number}")):
        rates = tuple(
            replace(rate, description=description) if rate.charge == code else rate
            for rate in plan.network_rates
        )
        retail = tuple(
            replace(rate, description=description) if rate.charge == code else rate
            for rate in plan.retail_rates
        )
        mock_onboarding.async_get_plans.return_value = PlanList(
            default=replace(plan, retail_rates=retail, network_rates=rates)
        )
        await config_entry.runtime_data.pricing.async_fetch_now()
        await hass.async_block_till_done(wait_background_tasks=True)

        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["friendly_name"].endswith(f"{renamed} import price")


_STANDARD_ATTRIBUTES = {
    "friendly_name",
    "unit_of_measurement",
    "icon",
    "device_class",
    "state_class",
}


def _extra(hass: HomeAssistant, key: str) -> dict[str, Any]:
    """Return a sensor's own attributes, without Home Assistant's standard ones."""
    state = hass.states.get(f"{PREFIX}_{key}")
    assert state is not None, key
    return {k: v for k, v in state.attributes.items() if k not in _STANDARD_ATTRIBUTES}


@pytest.mark.usefixtures("mock_onboarding")
async def test_price_breakdown_attributes(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    # Low Off Peak, winter peak: every part has its own peak rate.
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)

    attributes = _extra(hass, "current_import_price")
    assert attributes.pop("period") == "peak"
    parts = attributes
    assert parts == {
        "supplier_rate": pytest.approx(0.224),
        "network_rate": pytest.approx(0.2032),
        "ea_levy": pytest.approx(0.0023),
    }
    assert sum(parts.values()) == pytest.approx(
        float(_state(hass, "current_import_price")), abs=0.0002
    )

    # Export credits are negative, like the sensor state, and have no GST added.
    export = _extra(hass, "current_export_price")
    assert export.pop("period") == "peak"
    assert export == {
        "supplier_rate": pytest.approx(-0.1374),
        "network_rate": pytest.approx(-0.02),
    }
    assert sum(export.values()) == pytest.approx(float(_state(hass, "current_export_price")))


@pytest.mark.usefixtures("mock_onboarding")
async def test_breakdown_keys_dont_depend_on_the_period(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    # Low User outside winter: an anytime supplier rate on top of the off-peak network rate.
    freezer.move_to(datetime(2026, 10, 15, 0, 0, tzinfo=UTC))
    await _setup(hass, config_entry, {CONF_PLAN_ID: plan_id(ANYTIME_PLAN)})

    assert set(_extra(hass, "current_import_price")) == {
        "period",
        "supplier_rate",
        "network_rate",
        "ea_levy",
    }


@pytest.mark.usefixtures("mock_onboarding")
async def test_daily_charge_components_are_attributes_only(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)

    daily = [
        entry.unique_id
        for entry in er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
        if entry.unique_id.startswith(f"{TEST_ICP}_daily")
    ]
    assert daily == [f"{TEST_ICP}_daily_charge"]

    state = hass.states.get(f"{PREFIX}_daily_charge")
    assert state is not None
    # Low Off Peak: 30c admin, 35c metering and 90c network a day, plus GST.
    assert _extra(hass, "daily_charge") == {
        "supplier_rate": pytest.approx(0.345),
        "metering_rate": pytest.approx(0.4025),
        "network_rate": pytest.approx(1.035),
    }
    assert sum(_extra(hass, "daily_charge").values()) == pytest.approx(float(state.state))
    entry = entity_registry.async_get(f"{PREFIX}_daily_charge")
    assert entry is not None
    assert entry.options["sensor"]["suggested_display_precision"] == 4


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.usefixtures("new_season_charts")
async def test_period_that_doesnt_apply_today_has_no_price(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A shoulder period that starts next season has its price sensor already, with no value
    # until then rather than another period's price.
    freezer.move_to(WINTER_PEAK)
    mock_onboarding.async_get_plans.return_value = _with_new_season(
        mock_onboarding.async_get_plans.return_value
    )
    await _setup(hass, config_entry)
    entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, f"{TEST_ICP}_price_charge_3")
    assert entity_id is not None
    entity_registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _state(hass, "shoulder_import_price") == "unknown"
    assert _extra(hass, "shoulder_import_price") == {}


async def test_plan_names_fall_back_when_mappings_fail(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    assert _state(hass, "plan") == "Low ecoSAVER"

    # A failed fetch keeps the names from the last good one.
    mock_onboarding.async_get_plan_titles.side_effect = EcotricityConnectionError("down")
    await config_entry.runtime_data.pricing.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _state(hass, "plan") == "Low ecoSAVER"


async def test_plan_names_missing_from_the_start(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    mock_onboarding.async_get_plan_titles.side_effect = EcotricityConnectionError("down")
    await _setup(hass, config_entry)

    assert _state(hass, "plan") == "Low Off Peak"


@pytest.mark.usefixtures("mock_onboarding", "mock_portal")
async def test_network_and_sync_sensors(
    hass: HomeAssistant, portal_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, portal_config_entry)
    icp = parse_icp(load_network_fixture(DEFAULT_NETWORK, "icp.json"))

    assert _state(hass, "network") == icp.network_name
    assert _extra(hass, "network") == {"code": DEFAULT_NETWORK}
    # Ecotricity bills its energy rate on usage times the loss factor; export has none here.
    assert _state(hass, "network_import_loss_factor") == str(icp.loss_factor_consumption)
    assert _state(hass, "network_export_loss_factor") == str(icp.loss_factor_generation)
    assert _state(hass, "pricing_category") == icp.price_category_description
    assert _extra(hass, "pricing_category") == {"code": DEFAULT_PRICE_CATEGORY}
    assert _state(hass, "latest_day_of_usage") == "2026-04-05"
    assert _state(hass, "last_meter_data_update") == "2026-04-05T15:06:00+00:00"


@pytest.mark.usefixtures("mock_onboarding")
async def test_registry_details(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    # Enabled by default.
    meter = entity_registry.async_get(f"{PREFIX}_meter")
    assert meter is not None
    assert meter.disabled_by is None

    assert _extra(hass, "meter") == {
        "type": "HHR",
        "smart": True,
        "certification_expiry": date(2030, 6, 1),
    }
    assert hass.states.get(f"{PREFIX}_meter_certification_expiry") is None


@pytest.mark.parametrize("capacity", [None, 0.0])
async def test_generation_capacity_sensor_left_out_when_not_recorded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    capacity: float | None,
) -> None:
    # Some networks record zero even for ICPs with generation, so the sensor is left out rather than
    # shown, and one left from an earlier setup is removed.
    freezer.move_to(WINTER_PEAK)
    icp = mock_onboarding.async_get_icp.return_value
    mock_onboarding.async_get_icp.return_value = replace(icp, generation_capacity_kw=capacity)
    config_entry.add_to_hass(hass)
    stale = entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{TEST_ICP}_registered_generation_capacity", config_entry=config_entry
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get(stale.entity_id) is None
    assert hass.states.get(f"{PREFIX}_registered_generation_capacity") is None
    assert hass.states.get(f"{PREFIX}_icp") is not None


async def test_generation_capacity_sensor_shows_recorded_capacity(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    icp = mock_onboarding.async_get_icp.return_value
    mock_onboarding.async_get_icp.return_value = replace(icp, generation_capacity_kw=5.5)
    await _setup(hass, config_entry)

    assert float(_state(hass, "registered_generation_capacity")) == 5.5


@pytest.mark.usefixtures("mock_onboarding")
async def test_schedule_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    # Monday 8:15am in winter, on Low Off Peak: peak until 11am.
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)

    assert _state(hass, "next_import_period_change") == "2026-06-14T23:00:00+00:00"
    assert _extra(hass, "next_import_period_change") == {
        "period": "off_peak",
        "price": pytest.approx(0.2765),
    }
    # Export has the same windows on this plan: 15.74c peak, 13.74c off-peak.
    assert _state(hass, "next_export_period_change") == "2026-06-14T23:00:00+00:00"
    assert _extra(hass, "next_export_period_change") == {
        "period": "off_peak",
        "price": pytest.approx(-0.1374),
    }
    assert _extra(hass, "current_import_period")["windows"] == [
        "00:00-07:00 off_peak",
        "07:00-11:00 peak",
        "11:00-17:00 off_peak",
        "17:00-21:00 peak",
        "21:00-24:00 off_peak",
    ]
    assert _state(hass, "next_pricing_season_change") == "2026-10-01"
    assert _state(hass, "pricing_season") == "Seasonal Rates 04/2026 - 09/2026"


@pytest.mark.usefixtures("mock_onboarding")
async def test_no_next_period_change_on_a_flat_plan(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    # Low User outside winter: one period all day, every day, until the season ends.
    freezer.move_to(datetime(2026, 10, 15, 0, 0, tzinfo=UTC))
    await _setup(hass, config_entry, {CONF_PLAN_ID: plan_id(ANYTIME_PLAN)})

    assert _state(hass, "next_import_period_change") == "unknown"
    assert _extra(hass, "current_import_period")["windows"] == ["00:00-24:00 off_peak"]
    assert _state(hass, "next_pricing_season_change") == "2027-01-01"


@pytest.mark.usefixtures("mock_onboarding")
async def test_time_of_use_chart_image(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    entity_id = f"image.icp_{TEST_ICP.lower()}_current_time_of_use_chart"
    # The unique ID is the one from before the image was renamed, so existing entities keep it.
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    assert entry.unique_id == f"{TEST_ICP}_time_of_use_chart"
    image = hass.data[IMAGE_DATA_COMPONENT].get_entity(entity_id)
    assert image is not None
    base = "https://onboarding.ecotricity.co.nz/timeframeimages_ecotricity/"
    plan = find_plan(TIME_OF_USE_PLAN)
    winter = date(2026, 6, 15)
    # Monday, GST on: the GST-inclusive weekday chart.
    assert image.image_url == base + chart_name(plan, winter, include_gst=True)
    first_update = image.image_last_updated

    # Saturday, after the midnight refresh: the weekend chart.
    freezer.move_to(datetime(2026, 6, 19, 12, 1, tzinfo=UTC))
    await config_entry.runtime_data.pricing.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert image.image_url == base + chart_name(plan, winter, weekend=True, include_gst=True)
    assert image.image_last_updated != first_update

    # GST off: the GST-exclusive chart.
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_INCLUDE_GST: False, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)}
    )
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    image = hass.data[IMAGE_DATA_COMPONENT].get_entity(entity_id)
    assert image is not None
    assert image.image_url == base + chart_name(plan, winter, weekend=True)


@pytest.mark.usefixtures("mock_onboarding")
async def test_time_of_use_chart_image_without_a_season(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """With no published season for today, there's no chart URL to point at."""
    # Monday 3 May 2027, after the last published season ends.
    freezer.move_to(datetime(2027, 5, 2, 21, 0, tzinfo=UTC))
    await _setup(hass, config_entry)
    entity_id = f"image.icp_{TEST_ICP.lower()}_current_time_of_use_chart"
    image = hass.data[IMAGE_DATA_COMPONENT].get_entity(entity_id)
    assert image is not None
    assert image.image_url is None


async def test_time_of_use_chart_image_stays_on_the_onboarding_host(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A chart template without a leading slash can't turn the rest of the URL into a host."""
    freezer.move_to(WINTER_PEAK)
    plans = mock_onboarding.async_get_plans.return_value

    def unrooted(plan: Plan) -> Plan:
        weekday = plan.timeframe_chart_weekday
        return replace(plan, timeframe_chart_weekday=weekday and weekday.lstrip("/"))

    mock_onboarding.async_get_plans.return_value = replace(
        plans,
        default=unrooted(plans.default),
        alternative=plans.alternative and unrooted(plans.alternative),
        others=tuple(unrooted(plan) for plan in plans.others),
    )
    await _setup(hass, config_entry)
    entity_id = f"image.icp_{TEST_ICP.lower()}_current_time_of_use_chart"
    image = hass.data[IMAGE_DATA_COMPONENT].get_entity(entity_id)
    assert image is not None
    assert image.image_url == (
        "https://onboarding.ecotricity.co.nz/timeframeimages_ecotricity/"
        + chart_name(find_plan(TIME_OF_USE_PLAN), date(2026, 6, 15), include_gst=True)
    )


@pytest.mark.usefixtures("mock_onboarding")
async def test_credentials_never_logged(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    caplog.set_level("DEBUG")
    # A failing portal makes the integration log its errors.
    mock_portal.async_get_indicator.side_effect = EcotricityConnectionError("down")
    await _setup(hass, portal_config_entry)
    await portal_config_entry.runtime_data.consumption.async_refresh()

    own = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.ecotricity_nz")
    )
    assert "Error fetching" in own
    # The password appears nowhere in the log. The account number and ICP don't appear in
    # anything the integration logs (Home Assistant itself logs entity IDs, which contain the ICP).
    assert "secret" not in caplog.text
    for value in ("123456", TEST_ICP, TEST_ICP.lower()):
        assert value not in own


@pytest.mark.usefixtures("mock_onboarding")
async def test_import_and_export_periods_are_separate(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    # Low ecoSOLAR in spring: one import price all day, peak and off-peak export prices.
    # Monday 15 October 2026, 8:15am NZDT, is in the weekday export peak.
    freezer.move_to(datetime(2026, 10, 14, 19, 15, tzinfo=UTC))
    await _setup(hass, config_entry, {CONF_PLAN_ID: plan_id(SOLAR_PLAN)})

    # Import is one price all day, so its period never changes this season.
    assert _state(hass, "current_import_period") == "off_peak"
    assert _extra(hass, "current_import_period")["windows"] == ["00:00-24:00 off_peak"]
    assert _state(hass, "next_import_period_change") == "unknown"

    # Export has its own peak windows.
    assert _state(hass, "current_export_period") == "peak"
    assert _extra(hass, "current_export_period")["windows"] == [
        "00:00-07:00 off_peak",
        "07:00-11:00 peak",
        "11:00-17:00 off_peak",
        "17:00-21:00 peak",
        "21:00-24:00 off_peak",
    ]
    assert float(_state(hass, "current_export_price")) == pytest.approx(-0.21)
    # 11am NZDT, when export drops to the off-peak rate.
    assert _state(hass, "next_export_period_change") == "2026-10-14T22:00:00+00:00"
    assert _extra(hass, "next_export_period_change") == {
        "period": "off_peak",
        "price": pytest.approx(-0.16),
    }


@pytest.mark.usefixtures("mock_portal")
async def test_entity_categories(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    icp = mock_onboarding.async_get_icp.return_value
    mock_onboarding.async_get_icp.return_value = replace(icp, generation_capacity_kw=5.5)
    await _setup(hass, portal_config_entry)

    diagnostic = {
        entry.unique_id.removeprefix(f"{TEST_ICP}_")
        for entry in er.async_entries_for_config_entry(
            entity_registry, portal_config_entry.entry_id
        )
        if entry.entity_category is EntityCategory.DIAGNOSTIC
    }
    assert diagnostic == {
        "icp",
        "network",
        "network_import_loss_factor",
        "network_export_loss_factor",
        "pricing_category",
        "meter",
        "registered_generation_capacity",
        "plan",
        "pricing_season",
        "last_meter_data_update",
        "last_pricing_update",
    }


def test_daily_charge_keys() -> None:
    from custom_components.ecotricity_nz.sensor import _daily_charge_key  # noqa: PLC0415
    from custom_components.ecotricity_nz.tariff import DailyCharge  # noqa: PLC0415

    assert _daily_charge_key(DailyCharge("Daily Network Charges", 90, is_network=True)) == (
        "network_rate"
    )
    assert _daily_charge_key(DailyCharge("Metering Fee", 35, is_network=False)) == ("metering_rate")
    assert (
        _daily_charge_key(
            DailyCharge("Admin & climate positive certification", 30, is_network=False)
        )
        == "supplier_rate"
    )
    # A kind of charge not seen before keeps its own key.
    assert _daily_charge_key(DailyCharge("Smart Home Bundle", 20, is_network=False)) == (
        "smart_home_bundle"
    )


async def _enabled_price(
    hass: HomeAssistant, entry: MockConfigEntry, entity_registry: er.EntityRegistry, key: str
) -> str:
    """Enable a period price sensor, which is disabled by default, and return its state."""
    entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, f"{TEST_ICP}_price_{key}")
    assert entity_id is not None, key
    entity_registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state.state


async def test_controlled_load(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(datetime(2026, 10, 15, 0, 0, tzinfo=UTC))
    plans: PlanList = mock_onboarding.async_get_plans.return_value
    low_user = plans.find(plan_id(ANYTIME_PLAN))
    assert low_user is not None
    controlled = [
        Rate(
            id=f"c{index}",
            description="Controlled",
            charge="CHARGE_24",
            register_type="CN",
            unit=RateUnit.PER_KWH,
            raw_unit="cents/kWh",
            price=price,
            is_network=network,
        )
        for index, (price, network) in enumerate(((12.0, False), (3.0, True)))
    ]
    with_controlled = replace(
        low_user,
        retail_rates=(*low_user.retail_rates, controlled[0]),
        network_rates=(*low_user.network_rates, controlled[1]),
    )
    mock_onboarding.async_get_plans.return_value = PlanList(default=with_controlled)
    await _setup(hass, config_entry, {CONF_PLAN_ID: plan_id(ANYTIME_PLAN)})

    # (12c + 3c + 0.2c EA levy) plus GST.
    assert float(_state(hass, "current_controlled_price")) == pytest.approx(0.1748)
    assert _extra(hass, "current_controlled_price") == {
        "supplier_rate": pytest.approx(0.138),
        "network_rate": pytest.approx(0.0345),
        "ea_levy": pytest.approx(0.0023),
    }
    # Import prices are unaffected.
    assert float(_state(hass, "current_import_price")) == pytest.approx(0.2825)
    # So is the controlled period's own price sensor, once enabled.
    assert float(await _enabled_price(hass, config_entry, entity_registry, "cn_charge_24")) == (
        pytest.approx(0.1748)
    )


async def test_controlled_period_from_a_later_season_has_its_sensor(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A controlled night rate that only starts next season. Controlled load needs no chart, so
    # its sensor is there from now, with no value until the rate applies.
    freezer.move_to(datetime(2026, 10, 15, 0, 0, tzinfo=UTC))
    # Every published season, including those whose charts the fixtures leave out.
    low_user = find_plan(ANYTIME_PLAN)
    later = next(season for season in low_user.seasons if season.start > date(2026, 10, 15))
    night = Rate(
        id="c1",
        description="Controlled night",
        charge="CHARGE_LOW",
        register_type="CN",
        unit=RateUnit.PER_KWH,
        raw_unit="cents/kWh",
        price=9.0,
        is_network=False,
        valid_start=later.start,
        valid_end=later.end,
    )
    mock_onboarding.async_get_plans.return_value = PlanList(
        default=replace(low_user, retail_rates=(*low_user.retail_rates, night))
    )
    await _setup(hass, config_entry, {CONF_PLAN_ID: plan_id(ANYTIME_PLAN)})

    assert _state(hass, "current_controlled_price") == "unknown"
    assert await _enabled_price(hass, config_entry, entity_registry, "cn_charge_low") == "unknown"


async def test_outer_season_is_priced_from_when_it_applies_again(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Inside a season that sits within a longer one, such as "Seasonal Rates" within "Standard
    # Rates", the longer one next applies when the inner one ends. A controlled rate that only
    # starts then is part of the plan, so it has its sensor from now.
    plan = find_plan_where(has_overlapping_seasons)
    inner = min(plan.seasons, key=lambda season: season.end - season.start)
    outer = max(plan.seasons, key=lambda season: season.end - season.start)
    freezer.move_to(datetime.combine(inner.start + timedelta(days=14), time(), UTC))
    night = Rate(
        id="c1",
        description="Controlled night",
        charge="CHARGE_LOW",
        register_type="CN",
        unit=RateUnit.PER_KWH,
        raw_unit="cents/kWh",
        price=9.0,
        is_network=False,
        valid_start=inner.end + timedelta(days=1),
        valid_end=outer.end,
    )
    mock_onboarding.async_get_plans.return_value = PlanList(
        default=replace(plan, retail_rates=(*plan.retail_rates, night))
    )
    await _setup(hass, config_entry, {CONF_PLAN_ID: plan.id})

    assert config_entry.runtime_data.pricing.data.season == inner
    assert await _enabled_price(hass, config_entry, entity_registry, "cn_charge_low") == "unknown"


@pytest.mark.usefixtures("mock_onboarding")
async def test_no_controlled_sensors_without_controlled_load(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)

    assert hass.states.get(f"{PREFIX}_current_controlled_price") is None
    assert not [
        entry
        for entry in er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
        if "_cn_" in entry.unique_id
    ]


@pytest.mark.usefixtures("mock_onboarding")
async def test_new_series_catches_up_without_duplicates(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Import and export were imported before carbon existed.
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator,
        features=frozenset(
            {"POWER_ACTIVE_PURCHASE_CONSUMPTION", "POWER_ACTIVE_SUPPLY_CONSUMPTION"}
        ),
    )
    await _setup(hass, portal_config_entry)
    await async_wait_recording_done(hass)
    assert not await _stats(hass, statistic_id(TEST_ICP, CARBON))

    # The portal now holds carbon too: the next sync backfills it in full.
    mock_portal.async_get_indicator.return_value = replace(
        indicator,
        features=frozenset(
            {
                "POWER_ACTIVE_PURCHASE_CONSUMPTION",
                "POWER_ACTIVE_SUPPLY_CONSUMPTION",
                "EMISSION|POWER_ACTIVE_PURCHASE_CONSUMPTION|Carbon Savings",
            }
        ),
    )
    await portal_config_entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    assert (await _last_stat(hass, statistic_id(TEST_ICP, CARBON)))["sum"] == pytest.approx(2.5)
    # Import wasn't imported twice.
    assert (await _last_stat(hass, statistic_id(TEST_ICP, IMPORT)))["sum"] == pytest.approx(37.25)
    assert portal_config_entry.runtime_data.consumption.data.hours_imported == 0


@pytest.mark.usefixtures("mock_onboarding")
async def test_series_the_portal_lacks_is_skipped(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # No generation: the portal holds no export series.
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator,
        features=frozenset(
            {
                "POWER_ACTIVE_PURCHASE_CONSUMPTION",
                "EMISSION|POWER_ACTIVE_PURCHASE_CONSUMPTION|Carbon Savings",
            }
        ),
    )
    await _setup(hass, portal_config_entry)
    await async_wait_recording_done(hass)
    assert not await _stats(hass, statistic_id(TEST_ICP, EXPORT))
    calls = mock_portal.async_get_half_hourly.await_count

    # The missing export series doesn't make every sync re-read the whole history: with
    # everything imported, only the latest day is fetched again for the daily sensors.
    await portal_config_entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    start, _ = mock_portal.async_get_half_hourly.await_args.args
    assert mock_portal.async_get_half_hourly.await_count == calls + 1
    assert start == datetime(2026, 4, 5, 0, 0, tzinfo=NZ)


async def _stats(hass: HomeAssistant, stat_id: str) -> list[dict[str, float]]:
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    return result.get(stat_id, [])


@pytest.mark.usefixtures("mock_onboarding")
async def test_series_listed_without_readings_is_not_reread(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # The portal lists an export series with a data range but never returns readings for it.
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator,
        data_start=datetime(2026, 3, 1, 0, 0, tzinfo=NZ),
        features=frozenset(
            {"POWER_ACTIVE_PURCHASE_CONSUMPTION", "POWER_ACTIVE_SUPPLY_CONSUMPTION"}
        ),
    )
    half_hourly = mock_portal.async_get_half_hourly.side_effect

    async def without_export(start: datetime, end: datetime) -> Any:
        return replace(await half_hourly(start, end), exported_kwh=())

    mock_portal.async_get_half_hourly.side_effect = without_export
    await _setup(hass, portal_config_entry)
    await async_wait_recording_done(hass)
    assert not await _stats(hass, statistic_id(TEST_ICP, EXPORT))
    first_start, first_end = mock_portal.async_get_half_hourly.await_args_list[0].args
    assert first_start == datetime(2026, 3, 1, 0, 0, tzinfo=NZ)
    assert first_end == datetime(2026, 4, 1, 0, 0, tzinfo=NZ)
    calls = mock_portal.async_get_half_hourly.await_count

    # The next sync only fetches the latest day, not the whole history again.
    await portal_config_entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    start, _ = mock_portal.async_get_half_hourly.await_args.args
    assert mock_portal.async_get_half_hourly.await_count == calls + 1
    assert start == datetime(2026, 4, 5, 0, 0, tzinfo=NZ)


@pytest.mark.usefixtures("mock_onboarding")
async def test_export_chart_mismatch_keeps_import_windows(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    # Low ecoSOLAR in winter has time-of-use import and export prices. An export price the plan's
    # rates can't explain leaves import working.
    freezer.move_to(WINTER_PEAK)

    def read(data: bytes) -> ChartReading:
        reading = read_chart(data)
        wrong = tuple(replace(entry, text="-99.99") for entry in reading.export_entries)
        return replace(reading, export_entries=wrong)

    with patch.object(coordinator_module, "read_chart", side_effect=read):
        await _setup(hass, config_entry, {CONF_PLAN_ID: plan_id(SOLAR_PLAN)})

    assert _state(hass, "current_import_period") == "peak"
    assert _state(hass, "current_export_period") == "unavailable"
    assert "Export:" in _extra(hass, "current_import_price")["tou_error"]


def _import_legend(plan: Plan, day: date, *windows: tuple[int, int, str, str]) -> tuple:
    """Return import legend entries for (start hour, end hour, supplier, network) windows."""
    rates = side_rates(plan, day, export=False)
    prices = {
        (combo.supplier, combo.network): shown_price(combo.price)
        for combo in rates.candidates()
        if combo.has_network
    }
    return tuple(
        LegendEntry(start * 60, end * 60, prices[supplier, network])
        for start, end, supplier, network in windows
    )


async def test_periods_that_dont_line_up_get_their_own_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Ecotricity's peak runs 7am to 11am but the network's 9am to 1pm, so 8:15am is Ecotricity's
    # peak at the network's off-peak rate.
    freezer.move_to(WINTER_PEAK)
    plan = find_plan(TIME_OF_USE_PLAN)
    day = WINTER_PEAK.astimezone(NZ).date()
    high, low = "CHARGE_HIGH", "CHARGE_LOW"
    mixed = _import_legend(
        plan,
        day,
        (0, 7, low, low),
        (7, 9, high, low),
        (9, 11, high, high),
        (11, 13, low, high),
        (13, 24, low, low),
    )
    lined_up = [True]

    def read(data: bytes) -> ChartReading:
        reading = read_chart(data)
        return reading if lined_up[0] else replace(reading, import_entries=mixed)

    lined_up[0] = False
    with patch.object(coordinator_module, "read_chart", side_effect=read):
        await _setup(hass, config_entry)
        supplier = f"{TEST_ICP}_current_supplier_import_period"
        network = f"{TEST_ICP}_current_network_import_period"
        assert entity_registry.async_get_entity_id("sensor", DOMAIN, supplier) is not None
        assert entity_registry.async_get_entity_id("sensor", DOMAIN, network) is not None
        assert _state(hass, "current_supplier_import_period") == "peak"
        assert _state(hass, "current_network_import_period") == "off_peak"
        # The overall period is the one whose own price is nearest: Ecotricity's peak at the
        # network's off-peak rate is much nearer off-peak.
        assert _state(hass, "current_import_period") == "off_peak"
        rates = side_rates(plan, day, export=False)
        price = next(
            combo.price
            for combo in rates.candidates()
            if (combo.supplier, combo.network) == (high, low)
        )
        assert float(_state(hass, "current_import_price")) == pytest.approx(
            price * 1.15 / 100, abs=0.0001
        )
        assert _extra(hass, "current_supplier_import_period")["windows"] == [
            "00:00-07:00 off_peak",
            "07:00-11:00 peak",
            "11:00-24:00 off_peak",
        ]
        assert _extra(hass, "current_network_import_period")["windows"] == [
            "00:00-09:00 off_peak",
            "09:00-13:00 peak",
            "13:00-24:00 off_peak",
        ]
        # The overall windows change wherever the price does.
        assert len(_extra(hass, "current_import_period")["windows"]) == 5
        # Export lines up, so it keeps one period sensor.
        assert (
            entity_registry.async_get_entity_id(
                "sensor", DOMAIN, f"{TEST_ICP}_current_supplier_export_period"
            )
            is None
        )

        # Once the periods line up again, the extra sensors go.
        lined_up[0] = True
        await config_entry.runtime_data.pricing.async_fetch_now()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, supplier) is None
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, network) is None


def _drop_import_reading(mock_portal: MagicMock, missing: datetime) -> list[bool]:
    """Leave one half-hour out of the portal's import readings while the flag is set."""
    fetch = mock_portal.async_get_half_hourly.side_effect
    dropping = [True]

    async def half_hourly(start: datetime, end: datetime) -> Any:
        data = await fetch(start, end)
        if not dropping[0]:
            return data
        kept = tuple(r for r in data.imported_kwh if r.start != missing)
        return replace(data, imported_kwh=kept)

    mock_portal.async_get_half_hourly.side_effect = half_hourly
    return dropping


@pytest.mark.usefixtures("mock_onboarding")
async def test_recent_gap_holds_back_the_import_until_filled(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A missing half-hour mid-day isn't skipped over, so it's imported once the portal has it."""
    freezer.move_to(WINTER_PEAK)
    missing = datetime(2026, 4, 5, 10, 30, tzinfo=NZ).astimezone(UTC)
    dropping = _drop_import_reading(mock_portal, missing)
    await _setup(hass, portal_config_entry)
    await async_wait_recording_done(hass)

    # Nothing from the incomplete hour onwards is imported yet.
    stats = await _last_stat(hass, statistic_id(TEST_ICP, IMPORT))
    assert datetime.fromtimestamp(stats["start"], UTC) == missing - timedelta(minutes=90)

    # The portal fills the gap: the rest of the day follows, and the total is complete.
    dropping[0] = False
    await portal_config_entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    stats = await _last_stat(hass, statistic_id(TEST_ICP, IMPORT))
    assert stats["sum"] == pytest.approx(37.25)
    assert datetime.fromtimestamp(stats["start"], UTC) == datetime(2026, 4, 5, 11, 0, tzinfo=UTC)


@pytest.mark.usefixtures("mock_onboarding")
async def test_requests_start_at_nz_midnight(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Resuming within the repeated daylight-saving hour still fetches from the day's start.

    A response starting partway through that hour can't be placed on the right occurrence of it.
    """
    freezer.move_to(WINTER_PEAK)
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator, data_end=datetime(2026, 4, 7, 0, 0, tzinfo=NZ)
    )
    # 14:30 UTC is the second 02:30 that night, so the import stops after the first 02:00 hour.
    dropping = _drop_import_reading(mock_portal, datetime(2026, 4, 4, 14, 30, tzinfo=UTC))
    await _setup(hass, portal_config_entry)
    await async_wait_recording_done(hass)
    stats = await _last_stat(hass, statistic_id(TEST_ICP, IMPORT))
    assert datetime.fromtimestamp(stats["start"], UTC) == datetime(2026, 4, 4, 13, 0, tzinfo=UTC)

    dropping[0] = False
    calls = mock_portal.async_get_half_hourly.await_count
    await portal_config_entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    requests = mock_portal.async_get_half_hourly.await_args_list[calls:]
    assert requests[0].args[0] == datetime(2026, 4, 5, 0, 0, tzinfo=NZ)
    for request in requests:
        for bound in request.args:
            assert bound.astimezone(NZ).time() == time(0, 0)
    stats = await _last_stat(hass, statistic_id(TEST_ICP, IMPORT))
    assert stats["sum"] == pytest.approx(37.25)
    assert datetime.fromtimestamp(stats["start"], UTC) == datetime(2026, 4, 5, 11, 0, tzinfo=UTC)


@pytest.mark.usefixtures("mock_onboarding")
async def test_settled_gap_is_imported_as_it_stands(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An old gap the portal never fills doesn't stop later hours being imported."""
    freezer.move_to(WINTER_PEAK)
    missing = datetime(2026, 4, 5, 10, 30, tzinfo=NZ).astimezone(UTC)
    _drop_import_reading(mock_portal, missing)
    with patch.object(coordinator_module, "CONSUMPTION_SETTLE_PERIOD", timedelta(hours=6)):
        await _setup(hass, portal_config_entry)
        await async_wait_recording_done(hass)

    # Counting the repeated daylight-saving hour, 10:30 is the 24th reading (0.73 kWh).
    stats = await _last_stat(hass, statistic_id(TEST_ICP, IMPORT))
    assert stats["sum"] == pytest.approx(37.25 - 0.73)
    assert datetime.fromtimestamp(stats["start"], UTC) == datetime(2026, 4, 5, 11, 0, tzinfo=UTC)


@pytest.mark.usefixtures("mock_onboarding")
async def test_consumption_rechecks_until_new_data_arrives(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """When an import finds nothing new, the portal is checked again in a few hours."""
    freezer.move_to(WINTER_PEAK)
    # The portal has only part of the day so far.
    cutoff = datetime(2026, 4, 5, 12, 0, tzinfo=NZ)
    fetch = mock_portal.async_get_half_hourly.side_effect

    async def partial(start: datetime, end: datetime) -> Any:
        data = await fetch(start, end)
        if cutoff is None:
            return data
        return replace(
            data,
            imported_kwh=tuple(r for r in data.imported_kwh if r.start < cutoff),
            exported_kwh=tuple(r for r in data.exported_kwh if r.start < cutoff),
            carbon_savings_kg=tuple(r for r in data.carbon_savings_kg if r.start < cutoff),
        )

    mock_portal.async_get_half_hourly.side_effect = partial
    await _setup(hass, portal_config_entry)
    consumption = portal_config_entry.runtime_data.consumption
    assert consumption is not None
    assert consumption.data.hours_imported > 0
    assert consumption.update_interval == CONSUMPTION_UPDATE_INTERVAL

    # The next daily import finds nothing new, so it checks again sooner.
    await consumption.async_refresh()
    assert consumption.data.hours_imported == 0
    assert consumption.update_interval == CONSUMPTION_RECHECK_INTERVAL

    # Rechecks only ask the portal how much data it has.
    reads = mock_portal.async_get_half_hourly.await_count
    indicators = mock_portal.async_get_indicator.await_count
    freezer.tick(CONSUMPTION_RECHECK_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_portal.async_get_indicator.await_count == indicators + 1
    assert mock_portal.async_get_half_hourly.await_count == reads
    assert _state(hass, "latest_day_imported") != "unavailable"

    # New data appears: it's imported straight away, and the daily schedule resumes from then.
    cutoff = None
    indicator = mock_portal.async_get_indicator.return_value
    mock_portal.async_get_indicator.return_value = replace(
        indicator, data_end=datetime(2026, 4, 7, 0, 0, tzinfo=NZ)
    )
    freezer.tick(CONSUMPTION_RECHECK_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_portal.async_get_half_hourly.await_count > reads
    assert consumption.data.hours_imported > 0
    assert consumption.update_interval == CONSUMPTION_UPDATE_INTERVAL


@pytest.mark.usefixtures("mock_onboarding")
async def test_consumption_reads_again_daily_while_waiting(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    mock_portal: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Even with no new data, a full read runs daily, in case the portal has filled a gap."""
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, portal_config_entry)
    consumption = portal_config_entry.runtime_data.consumption
    assert consumption is not None
    await consumption.async_refresh()
    assert consumption.update_interval == CONSUMPTION_RECHECK_INTERVAL
    reads = mock_portal.async_get_half_hourly.await_count

    await consumption.async_refresh()
    assert mock_portal.async_get_half_hourly.await_count == reads

    # The refresh action reads in full whenever it's used.
    assert await consumption.async_fetch_now()
    assert mock_portal.async_get_half_hourly.await_count == reads + 1

    freezer.tick(CONSUMPTION_UPDATE_INTERVAL)
    await consumption.async_refresh()
    assert mock_portal.async_get_half_hourly.await_count == reads + 2

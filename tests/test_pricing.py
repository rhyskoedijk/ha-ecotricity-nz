"""Tests for when pricing is fetched, and for working from the saved copy."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ecotricity_nz.api import EcotricityConnectionError
from custom_components.ecotricity_nz.api.parsers import (
    parse_icp,
    parse_plan_list,
    parse_plan_titles,
)
from custom_components.ecotricity_nz.api.timeframes import read_chart
from custom_components.ecotricity_nz.const import (
    CONF_INCLUDE_GST,
    CONF_PLAN_ID,
    PRICING_CHECK_INTERVAL,
    PRICING_FETCH_INTERVAL,
    PRICING_RETRY_INTERVAL,
    PRICING_RETRY_MAX_INTERVAL,
)
from custom_components.ecotricity_nz.store import (
    PricingSnapshot,
    _snapshot,
    _snapshot_dict,
)
from tests.conftest import CHARTS, NZ, TEST_ICP, WINTER_PEAK, load_json_fixture

PREFIX = f"sensor.icp_{TEST_ICP.lower()}"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _reload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _move_to(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _state(hass: HomeAssistant, key: str) -> str:
    state = hass.states.get(f"{PREFIX}_{key}")
    assert state is not None, key
    return state.state


def _go_offline(onboarding: MagicMock) -> None:
    error = EcotricityConnectionError("offline")
    for name in ("async_get_icp", "async_get_plans", "async_get_plan_titles", "async_get_chart"):
        getattr(onboarding, name).side_effect = error


def _storage_key(entry: MockConfigEntry) -> str:
    return f"ecotricity_nz.{entry.entry_id}.pricing"


async def test_restart_works_offline(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    price = _state(hass, "current_import_price")
    fetched = _state(hass, "last_pricing_update")
    calls = mock_onboarding.async_get_icp.await_count

    # A restart within the week doesn't fetch anything.
    await _reload(hass, config_entry)
    assert mock_onboarding.async_get_icp.await_count == calls

    # Nor does it need to: with no connection at all, everything still works.
    _go_offline(mock_onboarding)
    await _reload(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "current_import_price") == price
    assert _state(hass, "current_import_period") == "peak"
    assert _state(hass, "last_pricing_update") == fetched


async def test_next_season_applies_offline(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A week before the winter season ends, with the next one already published.
    freezer.move_to(datetime(2026, 9, 23, 12, 0, tzinfo=NZ))
    await _setup(hass, config_entry)
    _go_offline(mock_onboarding)

    # The fetches due on the season's last day and on the 1st fail, but its charts were
    # downloaded ahead of time, so the new season works from its first minute.
    await _move_to(hass, freezer, datetime(2026, 10, 1, 0, 0, 10, tzinfo=NZ))
    assert _state(hass, "pricing_season") == "Seasonal Rates 10/2026 - 12/2026"
    assert _state(hass, "current_import_period") == "off_peak"
    assert _state(hass, "current_import_price") != "unavailable"
    assert config_entry.runtime_data.pricing.data.tou_error is None


@pytest.mark.parametrize(
    ("start", "later", "fetched"),
    [
        # Set up at noon, so the daily check runs at noon. Nothing is due within the week.
        (datetime(2026, 6, 15, 12, 0), datetime(2026, 6, 18, 12, 1), False),
        # The 1st of the month, at that day's check rather than at midnight.
        (datetime(2026, 6, 29, 12, 0), datetime(2026, 7, 1, 11, 0), False),
        (datetime(2026, 6, 29, 12, 0), datetime(2026, 7, 1, 12, 1), True),
        # The last day of the season. Daylight saving starts on the 27th, and the check runs every
        # 24 hours, so it moves to 1pm.
        (datetime(2026, 9, 25, 12, 0), datetime(2026, 9, 30, 12, 59), False),
        (datetime(2026, 9, 25, 12, 0), datetime(2026, 9, 30, 13, 1), True),
        # No published season covers today: try daily until one does.
        (datetime(2027, 4, 2, 12, 0), datetime(2027, 4, 3, 11, 0), False),
        (datetime(2027, 4, 2, 12, 0), datetime(2027, 4, 3, 12, 1), True),
        # A week after the last fetch.
        (datetime(2026, 6, 15, 12, 0), datetime(2026, 6, 22, 12, 1), True),
    ],
)
async def test_fetch_schedule(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
    start: datetime,
    later: datetime,
    fetched: bool,
) -> None:
    freezer.move_to(start.replace(tzinfo=NZ))
    await _setup(hass, config_entry)
    calls = mock_onboarding.async_get_icp.await_count

    # Step through the daily checks and the midnight updates in between.
    when = start.replace(tzinfo=NZ)
    while when < later.replace(tzinfo=NZ):
        when = min(when + timedelta(minutes=30), later.replace(tzinfo=NZ))
        await _move_to(hass, freezer, when)

    assert (mock_onboarding.async_get_icp.await_count > calls) is fetched


async def test_unpublished_season_is_fetched_once_a_day(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(datetime(2027, 4, 2, 12, 0, tzinfo=NZ))
    await _setup(hass, config_entry)
    assert _state(hass, "current_import_price") == "unavailable"
    calls = mock_onboarding.async_get_icp.await_count
    coordinator = config_entry.runtime_data.pricing

    # Each check sees the same missing season, but only the first of the day fetches.
    await _move_to(hass, freezer, datetime(2027, 4, 3, 9, 0, tzinfo=NZ))
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert mock_onboarding.async_get_icp.await_count == calls + 1


async def test_unreadable_windows_are_fetched_again_next_day(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    mock_onboarding.async_get_chart.side_effect = EcotricityConnectionError("down")
    await _setup(hass, config_entry)
    assert _state(hass, "current_import_price") == "unavailable"
    calls = mock_onboarding.async_get_chart.await_count

    mock_onboarding.async_get_chart.side_effect = None
    mock_onboarding.async_get_chart.return_value = (
        CHARTS / "CKHK-RTOU24-CKHK_OFPK_LOW_260401_T0-RESIDENTIAL-20260401-20260930-WD-NOGST.png"
    ).read_bytes()
    # Set up at 8:15am, so the next day's check runs then.
    await _move_to(hass, freezer, datetime(2026, 6, 16, 8, 16, tzinfo=NZ))
    assert mock_onboarding.async_get_chart.await_count > calls
    assert _state(hass, "current_import_price") != "unavailable"


async def test_choosing_another_plan_fetches(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    calls = mock_onboarding.async_get_icp.await_count

    # Its charts weren't downloaded with the last fetch, so they're fetched now.
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_INCLUDE_GST: True, CONF_PLAN_ID: "3001"}
    )
    await _reload(hass, config_entry)
    assert mock_onboarding.async_get_icp.await_count == calls + 1
    assert config_entry.runtime_data.pricing.data.plan.id == "3001"
    assert _state(hass, "current_import_price") != "unavailable"


async def test_saved_pricing_for_another_icp_is_ignored(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    snapshot = _fixture_snapshot()
    data = _snapshot_dict(snapshot)
    data["icp"]["icp"] = "9999999999ZZ9Z9"
    hass_storage[_storage_key(config_entry)] = {"version": 1, "key": "x", "data": data}

    await _setup(hass, config_entry)
    mock_onboarding.async_get_icp.assert_awaited()
    assert _state(hass, "icp") == TEST_ICP


async def test_damaged_saved_pricing_is_fetched_again(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(WINTER_PEAK)
    hass_storage[_storage_key(config_entry)] = {"version": 1, "key": "x", "data": {"icp": {}}}

    await _setup(hass, config_entry)
    assert "Ignoring the saved pricing" in caplog.text
    mock_onboarding.async_get_icp.assert_awaited()
    assert _state(hass, "current_import_price") != "unavailable"


@pytest.mark.usefixtures("mock_onboarding")
async def test_saved_pricing_is_deleted_with_the_entry(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    await hass.async_block_till_done()
    assert _storage_key(config_entry) in hass_storage

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _storage_key(config_entry) not in hass_storage


async def test_offline_warning_is_logged_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    _go_offline(mock_onboarding)
    coordinator = config_entry.runtime_data.pricing

    for _ in range(3):
        assert not await coordinator.async_fetch_now()
    warnings = [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and "Could not fetch pricing" in record.message
    ]
    assert len(warnings) == 1


def _fixture_snapshot() -> PricingSnapshot:
    chart = (
        CHARTS / "CKHK-RTOU24-CKHK_OFPK_LOW_260401_T0-RESIDENTIAL-20260401-20260930-WD-NOGST.png"
    )
    titles = parse_plan_titles(load_json_fixture("plan_mappings.json"))
    return PricingSnapshot(
        icp=parse_icp(load_json_fixture("icp.json")),
        plans=parse_plan_list(load_json_fixture("plan_list.json")).with_titles(titles),
        fetched_at=datetime(2026, 6, 14, 20, 15, tzinfo=UTC),
        plan_titles=titles,
        charts={"/chart.png": read_chart(chart.read_bytes())},
        charts_plan_id="3003",
    )


def test_snapshot_round_trip() -> None:
    snapshot = _fixture_snapshot()
    # Through JSON, as Home Assistant's store saves it. Only the unused personal details differ.
    saved = json.loads(json.dumps(_snapshot_dict(snapshot)))
    assert _snapshot(saved) == replace(snapshot, icp=replace(snapshot.icp, address="", region=""))


@pytest.mark.usefixtures("mock_onboarding")
async def test_saved_file_leaves_out_unused_personal_details(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    icp = parse_icp(load_json_fixture("icp.json"))
    assert icp.address
    assert icp.region
    await _setup(hass, config_entry)

    saved = json.dumps(hass_storage[_storage_key(config_entry)])
    assert icp.address not in saved
    assert f'"{icp.region}"' not in saved
    # What the sensors show is kept.
    assert TEST_ICP in saved
    for serial in icp.meter_serial_numbers:
        assert serial in saved


def test_snapshot_needs_a_time_zone() -> None:
    saved = _snapshot_dict(_fixture_snapshot())
    saved["fetched_at"] = "2026-06-14T20:15:00"
    with pytest.raises(ValueError, match="time zone"):
        _snapshot(saved)


async def test_failed_chart_download_keeps_the_saved_reading(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.pricing

    # Everything else is fetched, but the charts can't be downloaded this time.
    mock_onboarding.async_get_chart.side_effect = EcotricityConnectionError("down")
    assert await coordinator.async_fetch_now()
    assert coordinator.data.tou_error is None
    assert _state(hass, "current_import_period") == "peak"


async def test_failed_fetches_back_off(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.pricing
    assert coordinator.update_interval == PRICING_CHECK_INTERVAL

    # Each failure in a row doubles the wait, up to the maximum.
    _go_offline(mock_onboarding)
    freezer.tick(PRICING_FETCH_INTERVAL)
    waits = []
    for _ in range(6):
        await coordinator.async_refresh()
        assert coordinator.update_interval is not None
        waits.append(coordinator.update_interval)
    assert waits == [timedelta(hours=hours) for hours in (1, 2, 4, 8, 8, 8)]
    assert waits[-1] == PRICING_RETRY_MAX_INTERVAL
    assert _state(hass, "current_import_price") != "unavailable"

    # A fetch that works goes back to the daily check.
    mock_onboarding.async_get_icp.side_effect = None
    mock_onboarding.async_get_plans.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.update_interval == PRICING_CHECK_INTERVAL


async def test_failed_refresh_action_doesnt_keep_backing_off(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(WINTER_PEAK)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.pricing

    # A requested fetch fails, but nothing else is due, so the next check settles back to daily.
    mock_onboarding.async_get_icp.side_effect = EcotricityConnectionError("down")
    assert not await coordinator.async_fetch_now()
    assert coordinator.update_interval == PRICING_RETRY_INTERVAL
    await coordinator.async_refresh()
    assert coordinator.update_interval == PRICING_CHECK_INTERVAL

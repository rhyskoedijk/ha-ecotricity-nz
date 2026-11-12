"""Tests for the bill card's websocket command."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ecotricity_nz.api import Rate, RateUnit
from custom_components.ecotricity_nz.const import (
    CONF_ICP,
    CONF_INCLUDE_GST_IMPORT,
    CONF_PLAN_ID,
    DOMAIN,
    GST_FACTOR,
)
from custom_components.ecotricity_nz.coordinator import PricingCoordinator
from custom_components.ecotricity_nz.costs import LINE_ENERGY_SUFFIX, daily_key, import_key
from custom_components.ecotricity_nz.statistics import IMPORT, cost_series, statistic_id
from custom_components.ecotricity_nz.tariff import (
    ComponentKind,
    DailyCharge,
    bucket_name,
    bucket_names,
    bucket_period,
    component_key,
    daily_charge_key,
    line_key,
    rate_component,
)
from custom_components.ecotricity_nz.websocket_api import BillSection, bill_lines
from tests.conftest import (
    NZ,
    SOLAR_PLAN,
    TEST_ICP,
    TIME_OF_USE_PLAN,
    WINTER_PEAK,
    find_plan,
    find_plan_where,
    has_capacity_charge,
    plan_id,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import WebSocketGenerator

# The day the portal fixture has readings for.
PORTAL_DAY = date(2026, 4, 5)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _bill(hass_ws_client: WebSocketGenerator, hass: HomeAssistant, **data: Any) -> Any:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/bill", **data})
    return await client.receive_json()


def _stat(key: str) -> str:
    return statistic_id(TEST_ICP, cost_series(key))


@pytest.mark.freeze_time(WINTER_PEAK)
async def test_bill_lists_every_rate_as_a_line_in_its_section(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_onboarding: MagicMock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    await _setup(hass, config_entry)

    response = await _bill(hass_ws_client, hass)
    assert response["success"]
    result = response["result"]
    icp = mock_onboarding.async_get_icp.return_value
    assert result["icp"] == TEST_ICP
    assert result["network_name"] == icp.network_name
    assert result["loss_factor"] == icp.loss_factor_consumption
    assert result["gst_rate"] == pytest.approx(GST_FACTOR - 1)
    # Sections come in the bill's order.
    order = [section["section"] for section in result["sections"]]
    assert order == [section for section in BillSection if section in order]

    lines = {
        line["key"]: (section["section"], line)
        for section in result["sections"]
        for line in section["lines"]
    }
    plan = find_plan(TIME_OF_USE_PLAN)
    names = bucket_names(plan)
    for rate in plan.rates:
        if not rate.price or rate.is_controlled or (rate.is_export and rate.is_levy):
            continue
        part = rate_component(rate)
        if rate.unit is RateUnit.PER_DAY:
            charge = DailyCharge(rate.description, rate.price, is_network=rate.is_network)
            section, line = lines[daily_key(charge)]
            assert section == ("network" if rate.is_network else "other")
            assert line["usage_statistic_id"] is None
            # Named by who charges it, as the daily charge sensor splits it.
            assert line["kind"] == "daily"
            assert line["part"] == daily_charge_key(charge).removesuffix("_rate")
        elif part.kind is ComponentKind.LEVY:
            section, line = lines[import_key(part)]
            assert section == "other"
            assert line["usage_statistic_id"] == statistic_id(TEST_ICP, IMPORT)
            assert (line["kind"], line["part"]) == ("levy", component_key(part))
        else:
            key = line_key(part, export=rate.is_export)
            assert key is not None
            section, line = lines[key]
            assert section == ("network" if rate.is_network else "energy")
            assert line["usage_statistic_id"] == _stat(key + LINE_ENERGY_SUFFIX)
            # Ecotricity's import is charged on usage plus losses.
            assert line["losses"] is (not rate.is_network and not rate.is_export)
            # Named by side and period, like the price sensors, not by the plan's wording.
            assert line["kind"] == "usage"
            assert line["part"] == ("export" if rate.is_export else "import")
            assert line["period"] == bucket_period(part.bucket, names)
            assert line["name"] == bucket_name(part.bucket, names)
        # GST is charged on the bill's total, not line by line.
        assert "gst" not in line
        assert line["statistic_id"] == _stat(line["key"])


def test_bill_lists_daily_charges_then_import_then_export_in_each_section() -> None:
    plan = find_plan(SOLAR_PLAN)
    lines = bill_lines(plan)
    sections = list(BillSection)
    positions = [(sections.index(line.section), line.order) for line in lines]
    assert positions == sorted(positions)
    assert any(line.key.startswith("line_export_") for line in lines)
    # Each line appears once, however many seasons list its rate.
    assert len({line.key for line in lines}) == len(lines)


def test_rates_in_one_bucket_share_one_line_named_by_its_period() -> None:
    plan = find_plan(TIME_OF_USE_PLAN)
    rate = next(
        rate
        for rate in plan.retail_rates
        if rate.unit is RateUnit.PER_KWH and rate.price and not rate.is_levy
    )
    extra = replace(rate, description="Usage Adjustment", price=1.0)
    plan = replace(plan, retail_rates=(*plan.retail_rates, extra))

    key = line_key(rate_component(rate), export=rate.is_export)
    lines = [line for line in bill_lines(plan) if line.key == key]
    assert len(lines) == 1
    assert lines[0].period == bucket_period(rate_component(rate).bucket, bucket_names(plan))


def test_bill_has_no_line_for_capacity_charges_or_export_levies() -> None:
    # A capacity charge is per kVA of the connection, which isn't published, and levies are
    # only charged on import, so neither can be a line.
    plan = find_plan_where(has_capacity_charge)
    capacity = [rate for rate in plan.rates if rate.unit is RateUnit.OTHER and rate.price]
    assert capacity
    export_levy = Rate(
        description="EA Levy",
        charge="CHARGE_24",
        register_type="EG",
        unit=RateUnit.PER_KWH,
        price=0.2,
        is_network=False,
    )
    lines = bill_lines(replace(plan, retail_rates=(*plan.retail_rates, export_levy)))

    names = {line.name for line in lines}
    assert not names & {rate.description for rate in capacity}
    assert not [line for line in lines if line.kind == "levy" and line.part != "ea_levy"]
    assert len([line for line in lines if line.kind == "levy"]) == len(
        {rate.description for rate in plan.rates if rate.is_levy and not rate.is_export}
    )


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_bill_has_no_export_lines_without_an_export_register(
    hass: HomeAssistant,
    mock_onboarding: MagicMock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    icp = mock_onboarding.async_get_icp.return_value
    mock_onboarding.async_get_icp.return_value = replace(
        icp, channels=tuple(channel for channel in icp.channels if channel.register_code != "EG")
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Ecotricity {TEST_ICP}",
        unique_id=TEST_ICP,
        data={CONF_ICP: TEST_ICP},
        options={CONF_INCLUDE_GST_IMPORT: True, CONF_PLAN_ID: plan_id(SOLAR_PLAN)},
    )
    await _setup(hass, entry)

    result = (await _bill(hass_ws_client, hass, config_entry_id=entry.entry_id))["result"]
    keys = [line["key"] for section in result["sections"] for line in section["lines"]]
    assert keys
    assert not [key for key in keys if key.startswith("line_export_")]


async def test_bill_needs_a_loaded_entry(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    response = await _bill(hass_ws_client, hass)
    assert not response["success"]
    assert response["error"]["code"] == "not_found"


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_bill_needs_an_entry_named_when_several_are_loaded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    await _setup(hass, config_entry)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Ecotricity 0000654321AB1C2",
        unique_id="0000654321AB1C2",
        data={CONF_ICP: "0000654321AB1C2"},
        options=dict(config_entry.options),
    )
    await _setup(hass, other)

    # The error says how to choose one.
    several = (await _bill(hass_ws_client, hass))["error"]
    assert several["code"] == "not_found"
    assert "icp option" in several["message"]
    named = await _bill(hass_ws_client, hass, config_entry_id=config_entry.entry_id)
    assert named["success"]
    unknown = await _bill(hass_ws_client, hass, config_entry_id="unknown")
    assert unknown["error"]["code"] == "not_found"


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_bill_is_chosen_by_icp_when_several_are_loaded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    await _setup(hass, config_entry)
    other_icp = "0000654321AB1C2"
    other = MockConfigEntry(
        domain=DOMAIN,
        title=f"Ecotricity {other_icp}",
        unique_id=other_icp,
        data={CONF_ICP: other_icp},
        options=dict(config_entry.options),
    )
    await _setup(hass, other)

    # The ICP as it's written anywhere: on a bill, or in lower case as in entity IDs.
    for icp in (TEST_ICP, other_icp.lower(), f" {other_icp} "):
        assert (await _bill(hass_ws_client, hass, icp=icp))["success"], icp
    days = await _bill(
        hass_ws_client,
        hass,
        type=f"{DOMAIN}/bill_days",
        icp=other_icp.lower(),
        start_time=WINTER_PEAK.isoformat(),
        end_time=WINTER_PEAK.isoformat(),
    )
    assert days["success"]

    unknown = (await _bill(hass_ws_client, hass, icp="0000999999AB1C2"))["error"]
    assert unknown["code"] == "not_found"
    assert "icp" in unknown["message"]
    # Both given must agree.
    mismatch = await _bill(
        hass_ws_client, hass, icp=other_icp, config_entry_id=config_entry.entry_id
    )
    assert mismatch["error"]["code"] == "not_found"


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_bill_waits_for_pricing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    await _setup(hass, config_entry)

    with patch.object(PricingCoordinator, "cost_pricing", return_value=None):
        response = await _bill(hass_ws_client, hass)
    assert response["error"]["code"] == "not_ready"


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_bill_has_gst_whatever_the_entrys_option(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Ecotricity {TEST_ICP}",
        unique_id=TEST_ICP,
        data={CONF_ICP: TEST_ICP},
        options={CONF_INCLUDE_GST_IMPORT: False, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)},
    )
    await _setup(hass, entry)

    # The option is for the sensors; the bill adds GST at the end like Ecotricity's does.
    result = (await _bill(hass_ws_client, hass))["result"]
    assert result["gst_rate"] == pytest.approx(GST_FACTOR - 1)


@pytest.mark.usefixtures("mock_onboarding", "mock_portal")
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_bill_counts_only_the_days_charged(
    hass: HomeAssistant,
    portal_config_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    await _setup(hass, portal_config_entry)
    await async_wait_recording_done(hass)

    # The portal has one day of usage, when daylight saving ends (25 hours). A week around it
    # carries one day of daily charges, as the cost statistics spread them.
    start = datetime.combine(PORTAL_DAY - timedelta(days=3), time.min, NZ)
    end = datetime.combine(PORTAL_DAY + timedelta(days=4), time.min, NZ)
    response = await _bill(
        hass_ws_client,
        hass,
        type=f"{DOMAIN}/bill_days",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
    )
    assert response["success"]
    assert response["result"]["days"] == pytest.approx(1)

    # A range before any usage has none.
    before = await _bill(
        hass_ws_client,
        hass,
        type=f"{DOMAIN}/bill_days",
        start_time=(start - timedelta(days=30)).isoformat(),
        end_time=start.isoformat(),
    )
    assert before["result"]["days"] == 0


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_bill_days_needs_a_valid_range_and_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    await _setup(hass, config_entry)

    invalid = await _bill(
        hass_ws_client, hass, type=f"{DOMAIN}/bill_days", start_time="soon", end_time="later"
    )
    assert invalid["error"]["code"] == "invalid_format"
    unknown = await _bill(
        hass_ws_client,
        hass,
        type=f"{DOMAIN}/bill_days",
        config_entry_id="unknown",
        start_time=WINTER_PEAK.isoformat(),
        end_time=WINTER_PEAK.isoformat(),
    )
    assert unknown["error"]["code"] == "not_found"


@pytest.mark.usefixtures("mock_onboarding")
@pytest.mark.freeze_time(WINTER_PEAK)
async def test_icps_lists_every_loaded_icp_for_the_card_editor(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    assert (await _bill(hass_ws_client, hass, type=f"{DOMAIN}/icps"))["result"] == {"icps": []}

    await _setup(hass, config_entry)
    result = (await _bill(hass_ws_client, hass, type=f"{DOMAIN}/icps"))["result"]
    assert result == {"icps": [{"icp": TEST_ICP, "title": config_entry.title}]}

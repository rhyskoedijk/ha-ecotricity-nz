"""Shared fixtures for the Ecotricity NZ tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterable
from dataclasses import replace
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecotricity_nz.api import (
    EcotricityConnectionError,
    Plan,
    PlanList,
    PortalIndicator,
    Rate,
    RateUnit,
    Season,
)
from custom_components.ecotricity_nz.api.parsers import (
    parse_icp,
    parse_plan_list,
    parse_plan_titles,
    parse_values,
)
from custom_components.ecotricity_nz.api.timeframes import (
    ChartReading,
    needs_season,
    read_chart,
    shown_price,
)
from custom_components.ecotricity_nz.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_ICP,
    CONF_INCLUDE_GST,
    CONF_PASSWORD,
    CONF_PLAN_ID,
    DOMAIN,
)
from custom_components.ecotricity_nz.tariff import (
    ANYTIME,
    SideRates,
    Slot,
    explain,
    first_day,
    period_for_bucket,
    season_for,
    side_rates,
)

FIXTURES = Path(__file__).parent / "fixtures"
# One folder per network, named by its distributor code, holding icp.json where the network has a
# test ICP, and one folder per price category with that category's plan_list.json and charts/.
# Only charts that test something no other chart does are kept (docs/development.md).
NETWORKS = FIXTURES / "networks"
# The network and price category most tests run against: the test ICP's.
DEFAULT_NETWORK = "CKHK"
DEFAULT_PRICE_CATEGORY = "RLUTOU"
TEST_ICP = "0000123456AB1C2"
NZ = ZoneInfo("Pacific/Auckland")
# Plans in the default fixture, by the kind of pricing a test needs. They're found by their internal
# description, which stays the same when Ecotricity republishes a plan with a new ID or code.
# Peak and off-peak retail import prices:
TIME_OF_USE_PLAN = "Low Off Peak"
# One retail import price, so one import price all day outside winter:
ANYTIME_PLAN = "Low User"
# One retail import price, with peak and off-peak export credits:
SOLAR_PLAN = "LowBATTERY User"


def load_fixture(name: str) -> str:
    """Return a fixture file's text, by its path under tests/fixtures."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_fixture(name: str) -> Any:
    """Return a parsed JSON fixture, by its path under tests/fixtures."""
    return json.loads(load_fixture(name))


def load_network_fixture(network: str, name: str) -> Any:
    """Return a parsed JSON fixture from a network's folder, such as its icp.json."""
    return json.loads((NETWORKS / network / name).read_text(encoding="utf-8"))


def load_plan_list(network: str, category: str) -> Any:
    """Return the parsed plan list fixture for a network's price category."""
    return load_network_fixture(network, f"{category}/plan_list.json")


def find_plan(
    description: str, network: str = DEFAULT_NETWORK, category: str = DEFAULT_PRICE_CATEGORY
) -> Plan:
    """Return a fixture plan by its internal description, such as TIME_OF_USE_PLAN."""
    plans = parse_plan_list(load_plan_list(network, category)).all_plans
    return next(plan for plan in plans if plan.description == description)


def plan_id(description: str) -> str:
    """Return the ID of a plan in the default fixture, for a config entry's options."""
    return find_plan(description).id


def fixture_plans() -> list[Plan]:
    """Return every plan in every network's fixtures, in a stable order."""
    return [
        plan
        for network, category in price_categories()
        for plan in sorted(
            parse_plan_list(load_plan_list(network, category)).all_plans,
            key=lambda plan: plan.code,
        )
    ]


def find_plan_where(scenario: Callable[[Plan], bool]) -> Plan:
    """Return the first fixture plan, from any network, that has a pricing scenario.

    Tests describe the situation they need rather than naming a network or plan, so the same test
    covers whichever network has it, and keeps working as fixtures are added or refreshed.
    """
    plan = next((plan for plan in fixture_plans() if scenario(plan)), None)
    assert plan is not None, f"No fixture plan has the {scenario.__name__} scenario"
    return plan


def import_rates(rates: Iterable[Rate], day: date) -> list[Rate]:
    """Return the per-kWh import rates (not export, controlled load or levies) valid on a day."""
    return [
        rate
        for rate in rates
        if rate.unit is RateUnit.PER_KWH
        and rate.applies_on(day)
        and not (rate.is_export or rate.is_controlled or rate.is_levy)
    ]


def extra_network_codes(plan: Plan, day: date) -> list[str]:
    """Return the network's import charge codes beyond anytime, peak and off-peak, on a day."""
    return sorted(
        {
            rate.charge
            for rate in import_rates(plan.network_rates, day)
            if rate.charge and period_for_bucket(rate.charge) is None
        }
    )


def has_capacity_charge(plan: Plan) -> bool:
    """A fixed network charge per kVA of the connection's capacity, not per day."""
    return any(rate.unit is RateUnit.OTHER and rate.price for rate in plan.network_rates)


def has_extra_network_buckets(plan: Plan) -> bool:
    """One anytime retail import rate, and network rates in more periods than peak and off-peak.

    Some networks charge for periods such as shoulder, weekend or super off-peak under their own
    numbered charge codes, while the plan's own energy rate is the same all day.
    """
    return any(
        extra_network_codes(plan, season.start)
        and {rate.charge for rate in import_rates(plan.retail_rates, season.start)} == {ANYTIME}
        for season in plan.seasons
    )


def has_supplier_periods_and_anytime_network(plan: Plan) -> bool:
    """Retail import rates in more than one period, and only an anytime network import rate.

    The network's rate applies all day, but only the chart places Ecotricity's periods.
    """
    for season in plan.seasons:
        rates = side_rates(plan, season.start, export=False)
        network = import_rates(plan.network_rates, season.start)
        if len(rates.supplier) > 1 and not rates.network and network:
            return True
    return False


def chart_name(plan: Plan, day: date, *, weekend: bool = False, include_gst: bool = False) -> str:
    """Return the file name of a plan's chart for the season that applies on a day."""
    season = season_for(plan, day)
    assert season is not None
    return season_chart_name(plan, season, weekend=weekend, include_gst=include_gst)


def season_chart_name(
    plan: Plan, season: Season, *, weekend: bool = False, include_gst: bool = False
) -> str:
    """Return the file name of a plan's chart for a season.

    Built from the plan's own chart template, the way Ecotricity names its charts, so tests don't
    hard-code plan codes and season dates.
    """
    template = plan.timeframe_chart_weekend if weekend else plan.timeframe_chart_weekday
    assert template is not None
    name = (
        template.rsplit("/", 1)[-1]
        .replace("SSSSSSSS", season.start.strftime("%Y%m%d"))
        .replace("EEEEEEEE", season.end.strftime("%Y%m%d"))
    )
    return name if include_gst else name.replace(".png", "-NOGST.png")


def stored_charts(plan: Plan) -> list[tuple[date, Path]]:
    """Return the first day each season with a stored chart applies, and the chart.

    Only charts that test something no other chart does are kept, so most seasons have none.
    """
    charts = []
    for season in sorted(plan.seasons, key=lambda season: season.start):
        day = first_day(plan, season, season.start)
        for weekend in (False, True):
            chart = chart_file(season_chart_name(plan, season, weekend=weekend))
            if day is not None and chart.exists():
                charts.append((day, chart))
    return charts


def chart_plan(chart: Path) -> tuple[Plan, date]:
    """Return the plan a fixture chart belongs to, and the first day of its season."""
    # Charts are named {network}-{category}-{plan}-{customer}-{start}-{end}-{WD|WE}-NOGST.png,
    # and a category can have hyphens of its own, so it's taken from the folder.
    network, category = chart.parents[2].name, chart.parents[1].name
    code, _customer, start, *_ = chart.stem.removeprefix(f"{network}-{category}-").split("-")
    raw = json.loads((chart.parents[1] / "plan_list.json").read_text(encoding="utf-8"))
    plan = next((plan for plan in parse_plan_list(raw).all_plans if plan.code == code), None)
    assert plan is not None, f"{code} isn't in {network}/{category}/plan_list.json"
    return plan, datetime.strptime(start, "%Y%m%d").replace(tzinfo=UTC).date()


@cache
def chart_reading(chart: Path) -> ChartReading:
    """Return a fixture chart's reading, read once per test run."""
    return read_chart(chart.read_bytes())


def explained_chart(chart: Path) -> dict[bool, tuple[SideRates, tuple[Slot, ...]]]:
    """Return each side of a fixture chart (keyed by whether it's export), explained by its plan."""
    plan, day = chart_plan(chart)
    reading = chart_reading(chart)
    sides: dict[bool, tuple[SideRates, tuple[Slot, ...]]] = {}
    for export, entries in ((False, reading.import_entries), (True, reading.export_entries)):
        rates = side_rates(plan, day, export=export)
        if not rates.is_empty:
            sides[export] = (rates, explain(entries, rates))
    return sides


def find_chart_where(scenario: Callable[[Path], bool]) -> Path:
    """Return the first fixture chart, from any network, that shows a pricing scenario."""
    chart = next((chart for chart in chart_files() if scenario(chart)), None)
    assert chart is not None, f"No fixture chart has the {scenario.__name__} scenario"
    return chart


def has_mixed_periods(chart: Path) -> bool:
    """Half-hours where Ecotricity's period and the network's aren't the same period.

    The price is then one period's supplier rate plus another period's network rate.
    """
    return any(
        ANYTIME
        not in (slot.supplier.removeprefix(rates.prefix), slot.network.removeprefix(rates.prefix))
        and slot.supplier != slot.network
        for rates, slots in explained_chart(chart).values()
        for slot in slots
    )


def has_hours_without_a_network_rate(chart: Path) -> bool:
    """A network that publishes rates for some periods only, and charges nothing in the others."""
    return any(
        rates.network and slot.network == rates.prefix + ANYTIME
        for rates, slots in explained_chart(chart).values()
        for slot in slots
    )


def has_clipped_price(chart: Path) -> bool:
    """A legend line so long that its last price runs off the image."""
    reading = chart_reading(chart)
    return any(entry.clipped for entry in (*reading.import_entries, *reading.export_entries))


def has_clipped_price_needing_its_bar(chart: Path) -> bool:
    """A cut-off price whose remaining digits fit more than one of the plan's prices."""
    reading = chart_reading(chart)
    for export, (rates, _slots) in explained_chart(chart).items():
        prices = {shown_price(combo.price) for combo in rates.candidates()}
        entries = reading.export_entries if export else reading.import_entries
        for entry in entries:
            if (
                entry.clipped
                and len({price for price in prices if price.startswith(entry.text)}) > 1
            ):
                return True
    return False


def has_overlapping_seasons(plan: Plan) -> bool:
    """Seasons that overlap, such as "Standard Rates" all year and a winter season inside it."""
    return any(
        a is not b and a.start <= b.end and b.start <= a.end
        for a in plan.seasons
        for b in plan.seasons
    )


def network_codes() -> list[str]:
    """Return every network with fixtures."""
    return sorted(path.name for path in NETWORKS.iterdir() if path.is_dir())


def price_categories() -> list[tuple[str, str]]:
    """Return every (network, price category) pair with a plan list fixture."""
    return sorted(
        (path.parent.parent.name, path.parent.name) for path in NETWORKS.glob("*/*/plan_list.json")
    )


def chart_file(name: str) -> Path:
    """Return a fixture chart by its file name, which starts with its network and price category."""
    network = name.split("-", 1)[0]
    # A category can have hyphens of its own, so match the name against the network's folders.
    categories = [
        path.name
        for path in (NETWORKS / network).iterdir()
        if name.startswith(f"{network}-{path.name}-")
    ]
    assert categories, f"{name} isn't in any of {network}'s price category folders"
    return NETWORKS / network / max(categories, key=len) / "charts" / name


def chart_files() -> list[Path]:
    """Return every fixture chart, for every network and price category."""
    return sorted(NETWORKS.glob("*/*/charts/*.png"))


@pytest.fixture(autouse=True)
def auto_setup(recorder_mock: Any, enable_custom_integrations: None) -> None:
    """Run every test with a recorder (a dependency) and the integration loadable.

    `recorder_mock` must be resolved before `hass`, so it comes first here.
    """


@pytest.fixture(autouse=True)
def no_backfill_pause() -> Generator[None]:
    """Skip the pause between backfill requests."""
    with patch("custom_components.ecotricity_nz.coordinator.BACKFILL_PAUSE_SECONDS", 0):
        yield


def _chart_bytes(path: str) -> bytes:
    """Serve fixture charts. Where a weekend chart isn't captured, the weekday chart stands in."""
    chart = chart_file(path.rsplit("/", 1)[-1])
    if not chart.exists():
        chart = chart.with_name(chart.name.replace("-WE-", "-WD-"))
    if not chart.exists():
        raise EcotricityConnectionError(f"Chart {path} returned HTTP 404")
    return chart.read_bytes()


def _captured_seasons(plans: PlanList) -> PlanList:
    """Return the plans with only the seasons whose weekday chart is in the fixtures.

    Ecotricity publishes a chart for every season it lists, but only charts that test something
    are kept. Leaving out the rest keeps the mock site as consistent as the real one, so a season
    whose chart can't be read is only ever one a test sets up.
    """

    @cache
    def captured(plan: Plan) -> Plan:
        if not plan.timeframe_chart_weekday or not needs_season(plan.timeframe_chart_weekday):
            return plan
        seasons = tuple(
            season
            for season in plan.seasons
            if chart_file(season_chart_name(plan, season)).exists()
        )
        return replace(plan, seasons=seasons)

    return PlanList(
        default=captured(plans.default),
        alternative=captured(plans.alternative) if plans.alternative else None,
        others=tuple(captured(plan) for plan in plans.others),
    )


@pytest.fixture
def mock_onboarding() -> Generator[MagicMock]:
    """Patch the onboarding client everywhere it's created."""
    client = MagicMock()
    client.async_get_icp = AsyncMock(
        return_value=parse_icp(load_network_fixture(DEFAULT_NETWORK, "icp.json"))
    )
    client.async_get_plans = AsyncMock(
        return_value=_captured_seasons(
            parse_plan_list(load_plan_list(DEFAULT_NETWORK, DEFAULT_PRICE_CATEGORY))
        )
    )
    client.async_get_chart = AsyncMock(side_effect=_chart_bytes)
    client.async_get_plan_titles = AsyncMock(
        return_value=parse_plan_titles(load_json_fixture("plan_mappings.json"))
    )
    with (
        patch("custom_components.ecotricity_nz.OnboardingClient", return_value=client),
        patch("custom_components.ecotricity_nz.config_flow.OnboardingClient", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_portal() -> Generator[MagicMock]:
    """Patch the portal client everywhere it's created."""
    client = MagicMock()
    client.async_login = AsyncMock()
    client.async_get_indicator = AsyncMock(
        return_value=PortalIndicator(
            indicator_id="ind",
            display_name=TEST_ICP,
            last_import=datetime(2026, 4, 6, 3, 6, tzinfo=NZ),
            data_start=datetime(2026, 4, 5, 0, 0, tzinfo=NZ),
            data_end=datetime(2026, 4, 6, 0, 0, tzinfo=NZ),
        )
    )
    values = parse_values(load_json_fixture("portal_values_dst_end.json"), NZ)

    async def half_hourly(start: datetime, end: datetime) -> Any:
        return type(values)(
            imported_kwh=tuple(r for r in values.imported_kwh if start <= r.start < end),
            exported_kwh=tuple(r for r in values.exported_kwh if start <= r.start < end),
            carbon_savings_kg=tuple(r for r in values.carbon_savings_kg if start <= r.start < end),
        )

    client.async_get_half_hourly = AsyncMock(side_effect=half_hourly)
    with (
        patch("custom_components.ecotricity_nz.PortalClient", return_value=client),
        patch("custom_components.ecotricity_nz.config_flow.PortalClient", return_value=client),
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A config entry without portal credentials, on the Low Off Peak plan."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Ecotricity {TEST_ICP}",
        unique_id=TEST_ICP,
        data={CONF_ICP: TEST_ICP},
        options={CONF_INCLUDE_GST: True, CONF_PLAN_ID: plan_id(TIME_OF_USE_PLAN)},
    )


@pytest.fixture
def portal_config_entry() -> MockConfigEntry:
    """A config entry with portal credentials."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Ecotricity {TEST_ICP}",
        unique_id=TEST_ICP,
        data={CONF_ICP: TEST_ICP, CONF_ACCOUNT_NUMBER: "123456", CONF_PASSWORD: "secret"},
        options={CONF_INCLUDE_GST: True, CONF_PLAN_ID: plan_id(ANYTIME_PLAN)},
    )


# Monday 15 June 2026, 8:15am NZST: winter peak on the default fixture's time-of-use plans.
WINTER_PEAK = datetime(2026, 6, 14, 20, 15, tzinfo=UTC)

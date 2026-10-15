"""Checks every network's fixtures against each other.

Each network's folder under tests/fixtures/networks holds a folder per price category, with that
category's plan list and the timeframe charts for its plans, saved as Ecotricity publishes them.
Adding a price category's folder is enough for these tests to cover it: every chart is read and
matched against the prices worked out from that category's own plans, for the season and day type
in the chart's file name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from custom_components.ecotricity_nz.api.parsers import parse_plan_list
from custom_components.ecotricity_nz.api.timeframes import SLOTS_PER_DAY, match_tiers, read_chart
from custom_components.ecotricity_nz.tariff import bucket_names, bucket_period, price_day
from tests.conftest import (
    NETWORKS,
    chart_files,
    chart_plan,
    load_json_fixture,
    load_plan_list,
    network_codes,
    price_categories,
)

if TYPE_CHECKING:
    from custom_components.ecotricity_nz.api import Plan


STRINGS = Path(__file__).parents[1] / "custom_components" / "ecotricity_nz" / "strings.json"


def _plans(network: str, category: str) -> tuple[Plan, ...]:
    return parse_plan_list(load_plan_list(network, category)).all_plans


@pytest.mark.parametrize("network", network_codes())
def test_network_folder(network: str) -> None:
    # The folder is named by a real distributor code, and holds at least one price category.
    assert network in load_json_fixture("network_price_categories.json")["networks"]
    assert any(category_network == network for category_network, _ in price_categories())


@pytest.mark.parametrize(("network", "category"), price_categories())
def test_price_category_folder(network: str, category: str) -> None:
    # The folder holds only that network's and price category's plans and charts.
    plans = _plans(network, category)
    assert {plan.network for plan in plans} == {network}
    content = load_plan_list(network, category)["content"][0]
    raw_plans = [content["default"], content["alternative"], *content["plan_list"]]
    assert {plan["price_category"] for plan in raw_plans if plan} == {category}
    rate_networks = {
        rate["network"] for plan in raw_plans if plan for rate in plan["network_rates"]
    }
    assert rate_networks == {network}
    for chart in (NETWORKS / network / category / "charts").glob("*.png"):
        assert chart.name.startswith(f"{network}-{category}-")


@pytest.mark.parametrize("chart", chart_files(), ids=lambda path: path.stem)
def test_chart_matches_its_plan(chart: Path) -> None:
    plan, day = chart_plan(chart)
    pricing = price_day(plan, day)
    reading = read_chart(chart.read_bytes())

    imports = match_tiers(
        reading.import_tiers,
        pricing.import_buckets,
        other_tiers=reading.export_tiers,
        other_prices=pricing.export_buckets,
    )
    assert len(imports) == SLOTS_PER_DAY
    if reading.export_tiers:
        exports = match_tiers(
            reading.export_tiers,
            pricing.export_buckets,
            other_tiers=reading.import_tiers,
            other_prices=pricing.import_buckets,
        )
        assert len(exports) == SLOTS_PER_DAY


@pytest.mark.parametrize(("network", "category"), price_categories())
def test_every_period_is_translated(network: str, category: str) -> None:
    # Periods seen on real plans get a friendly name, so the period sensors' states and options
    # don't mix translated names with raw keys like super_off_peak.
    sensors = json.loads(STRINGS.read_text(encoding="utf-8"))["entity"]["sensor"]
    for plan in _plans(network, category):
        names = bucket_names(plan)
        for season in plan.seasons:
            pricing = price_day(plan, season.start)
            for key, buckets in (
                ("current_import_period", pricing.import_buckets),
                ("current_export_period", pricing.export_buckets),
            ):
                translated = sensors[key]["state"]
                for bucket in buckets:
                    period = bucket_period(bucket, names)
                    assert period in translated, f"{plan.code}: {period} has no {key} state"

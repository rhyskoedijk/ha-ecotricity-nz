"""Checks every network's fixtures against each other.

Each network's folder under tests/fixtures/networks holds a folder per price category, with that
category's plan list and the timeframe charts worth testing, saved as Ecotricity publishes them.
Every chart's legend is read and each of its prices explained by the plan's own rates, for the
season in the chart's file name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from custom_components.ecotricity_nz.api.models import RateUnit
from custom_components.ecotricity_nz.api.parsers import parse_plan_list
from custom_components.ecotricity_nz.api.timeframes import (
    MINUTES_PER_DAY,
    SLOTS_PER_DAY,
    shown_price,
)
from custom_components.ecotricity_nz.tariff import (
    CONTROLLED_PREFIX,
    EXPORT_PREFIX,
    bucket_names,
    bucket_period,
    resolve_bucket,
)
from tests.conftest import (
    NETWORKS,
    chart_files,
    chart_reading,
    explained_chart,
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
def test_chart_prices_match_its_plan(chart: Path) -> None:
    # Every window on the legend is explained by one of the plan's supplier rates and one of its
    # network rates (plus anytime rates and levies), rounded as the legend rounds them.
    reading = chart_reading(chart)
    sides = explained_chart(chart)
    assert False in sides
    for export, (_rates, slots) in sides.items():
        entries = reading.export_entries if export else reading.import_entries
        assert entries[0].start == 0
        assert entries[-1].end == MINUTES_PER_DAY
        assert len(slots) == SLOTS_PER_DAY
        for entry in entries:
            for slot in entry.slots:
                shown = shown_price(slots[slot].price)
                assert shown.startswith(entry.text) if entry.clipped else shown == entry.text


@pytest.mark.parametrize(("network", "category"), price_categories())
def test_every_period_is_translated(network: str, category: str) -> None:
    # Periods seen on real plans get a friendly name, so the period sensors' states and options
    # don't mix translated names with raw keys like super_off_peak.
    sensors = json.loads(STRINGS.read_text(encoding="utf-8"))["entity"]["sensor"]
    for plan in _plans(network, category):
        names = bucket_names(plan)
        for rate in plan.rates:
            bucket = resolve_bucket(rate)
            if rate.unit is not RateUnit.PER_KWH or rate.is_levy:
                continue
            if bucket.startswith(CONTROLLED_PREFIX):
                continue
            direction = "export" if bucket.startswith(EXPORT_PREFIX) else "import"
            period = bucket_period(bucket, names)
            for party in ("", "supplier_", "network_"):
                key = f"current_{party}{direction}_period"
                assert period in sensors[key]["state"], f"{plan.code}: {period} has no {key} state"

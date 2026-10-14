"""Checks every network's fixtures against each other.

Each folder under tests/fixtures/networks holds one network's plan list and the timeframe charts
for its plans, saved as Ecotricity publishes them. Adding a network's folder is enough for these
tests to cover it: every chart is read and matched against the prices worked out from that
network's own plans, for the season and day type in the chart's file name.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from custom_components.ecotricity_nz.api.parsers import parse_plan_list
from custom_components.ecotricity_nz.api.timeframes import SLOTS_PER_DAY, match_tiers, read_chart
from custom_components.ecotricity_nz.tariff import price_day
from tests.conftest import (
    NETWORKS,
    chart_files,
    load_json_fixture,
    load_network_fixture,
    network_codes,
)

if TYPE_CHECKING:
    from pathlib import Path

    from custom_components.ecotricity_nz.api import Plan


def _plans(network: str) -> tuple[Plan, ...]:
    return parse_plan_list(load_network_fixture(network, "plan_list.json")).all_plans


@pytest.mark.parametrize("network", network_codes())
def test_network_folder(network: str) -> None:
    # The folder is named by a real distributor code, and holds only that network's plans.
    assert network in load_json_fixture("network_codes.json")
    assert {plan.network for plan in _plans(network)} == {network}
    for chart in (NETWORKS / network / "charts").glob("*.png"):
        assert chart.name.startswith(f"{network}-")


@pytest.mark.parametrize("chart", chart_files(), ids=lambda path: path.stem)
def test_chart_matches_its_plan(chart: Path) -> None:
    # Charts are named {network}-{category}-{plan}-{customer}-{start}-{end}-{WD|WE}-NOGST.png.
    network, _category, code, _customer, start, _end, _day_type, _gst = chart.stem.split("-")
    plan = next((plan for plan in _plans(network) if plan.code == code), None)
    assert plan is not None, f"{code} isn't in {network}'s plan_list.json"
    pricing = price_day(plan, datetime.strptime(start, "%Y%m%d").date())  # noqa: DTZ007 - a date
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

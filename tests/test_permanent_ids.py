"""Entity unique IDs, statistic IDs and saved versions never change without a migration.

`tests/fixtures/permanent_ids.json` lists every entity key (the unique ID is `{icp}_{key}`),
every statistic key (`ecotricity_nz:{icp}_{key}`) and the versions of the config entry and the
saved snapshot. A key that's renamed or removed loses users' history, so the test fails until
the list is changed on purpose. A new key fails it too, until it's added, so the list stays
complete without anyone having to remember it.

Entity keys come from the code's `key=` literals and from setting up the default fixture's
solar plan, which has import and export sensors. Keys built from a plan's rates are checked
with a plan made up here that has one rate of every kind, so they don't depend on what
Ecotricity publishes.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecotricity_nz.api.models import Plan, Rate, RateUnit
from custom_components.ecotricity_nz.config_flow import EcotricityConfigFlow
from custom_components.ecotricity_nz.const import CONF_PLAN_ID
from custom_components.ecotricity_nz.costs import cost_keys
from custom_components.ecotricity_nz.sensor import _PRICE_KEY_PREFIX, _SPLIT_PERIOD_KEYS
from custom_components.ecotricity_nz.statistics import CARBON, EXPORT, IMPORT
from custom_components.ecotricity_nz.store import STORAGE_VERSION
from custom_components.ecotricity_nz.tariff import resolve_bucket, slug

from .conftest import SOLAR_PLAN, TEST_ICP, WINTER_PEAK, plan_id

PACKAGE = Path(__file__).parents[1] / "custom_components/ecotricity_nz"
RECORDED = Path(__file__).parent / "fixtures/permanent_ids.json"


def _rate(
    description: str,
    charge: str | None,
    price: float,
    *,
    is_network: bool,
    register: str | None = None,
    unit: RateUnit = RateUnit.PER_KWH,
) -> Rate:
    return Rate(description, charge, register, unit, price, is_network=is_network)


PLAN = Plan(
    id="plan",
    code="PLAN",
    description="Every kind of rate",
    network="NETWORK",
    price_category="CATEGORY",
    retail_rates=(
        _rate("Peak Usage", "CHARGE_HIGH", 20.0, is_network=False),
        _rate("Shoulder Usage", "CHARGE_4", 15.0, is_network=False),
        _rate("Electricity Authority Levy", None, 0.1, is_network=False),
        _rate("Export", "CHARGE_24", -12.0, register="EG", is_network=False),
        _rate("Admin Fee", None, 20.0, unit=RateUnit.PER_DAY, is_network=False),
        _rate("Metering Charge", None, 15.0, unit=RateUnit.PER_DAY, is_network=False),
        _rate("Low User Charge", None, 2.0, unit=RateUnit.PER_DAY, is_network=False),
    ),
    network_rates=(
        _rate("Off Peak Usage", "CHARGE_LOW", 5.0, is_network=True),
        _rate("Export Peak", "CHARGE_HIGH", 1.0, register="EG", is_network=True),
        _rate("Daily Fixed Charge", None, 30.0, unit=RateUnit.PER_DAY, is_network=True),
    ),
    seasons=(),
)


async def _entity_keys(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PLAN_ID: plan_id(SOLAR_PLAN)}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registered = {
        registry_entry.unique_id.removeprefix(f"{TEST_ICP}_")
        for registry_entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    literals = {
        node.value.value
        for path in sorted(PACKAGE.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.keyword)
        and node.arg == "key"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    # Price sensors' keys depend on the plan, so they come from the made-up one instead.
    fixed = {key for key in registered if not key.startswith(_PRICE_KEY_PREFIX)}
    prices = {
        f"{_PRICE_KEY_PREFIX}{slug(resolve_bucket(rate))}"
        for rate in PLAN.rates
        if rate.unit is RateUnit.PER_KWH and not rate.is_levy
    }
    return fixed | literals | set(_SPLIT_PERIOD_KEYS.values()) | prices


def _statistic_keys() -> set[str]:
    keys = cost_keys(PLAN)
    series = {IMPORT.key, EXPORT.key, CARBON.key}
    return series | keys.statistic_keys(export=False) | keys.statistic_keys(export=True)


@pytest.mark.usefixtures("mock_onboarding", "mock_portal")
async def test_entity_and_statistic_keys_never_change(
    hass: HomeAssistant, portal_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(WINTER_PEAK)
    recorded = json.loads(RECORDED.read_text(encoding="utf-8"))
    entity_keys = await _entity_keys(hass, portal_config_entry)

    for name, current in (("entity_keys", entity_keys), ("statistic_keys", _statistic_keys())):
        gone = sorted(set(recorded[name]) - current)
        assert gone == [], f"{name} renamed or removed, which loses history without a migration"
        new = sorted(current - set(recorded[name]))
        assert new == [], f"new {name}: add them to {RECORDED.name}"


def test_saved_versions_only_change_with_a_migration() -> None:
    recorded = json.loads(RECORDED.read_text(encoding="utf-8"))
    current = {
        "config_entry_version": EcotricityConfigFlow.VERSION,
        "storage_version": STORAGE_VERSION,
    }

    # A new config entry version needs `async_migrate_entry`, and a new storage version must
    # read or deliberately discard the old one (`_SnapshotStore`). Update the file after that.
    assert {key: recorded[key] for key in current} == current

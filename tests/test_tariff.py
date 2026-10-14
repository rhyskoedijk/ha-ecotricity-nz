"""Tests for turning plan rates into bucket prices.

Expected prices are the legend values printed on Ecotricity's own timeframe charts, so these tests
check the integration against the website rather than against itself.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ecotricity_nz.api import Plan, Rate, RateUnit
from custom_components.ecotricity_nz.api.parsers import parse_plan_list
from custom_components.ecotricity_nz.api.timeframes import ChartTier, match_tiers, read_chart
from custom_components.ecotricity_nz.tariff import (
    SLOTS_PER_DAY,
    SlotMap,
    TouPeriod,
    next_bucket_change,
    period_for_bucket,
    price_day,
    resolve_bucket,
    season_for,
)
from tests.conftest import CHARTS, load_json_fixture

NZ = ZoneInfo("Pacific/Auckland")
WINTER = date(2026, 6, 15)
SPRING = date(2026, 10, 15)


def _plan(code: str) -> Plan:
    plans = parse_plan_list(load_json_fixture("plan_list.json"))
    return next(plan for plan in plans.all_plans if plan.code == code)


@pytest.mark.parametrize(
    ("code", "day", "imports", "exports"),
    [
        # Low Off Peak, spring: off-peak network charged in peak hours outside winter.
        (
            "CKHK_OFPK_LOW_260401_T0",
            SPRING,
            {"CHARGE_LOW": 24.04, "CHARGE_HIGH": 25.35},
            {"EG_CHARGE_LOW": -13.74},
        ),
        # LowBATTERY User, winter: two import and two export tiers.
        (
            "BATT_SPECIAL_LOW_260401_T0",
            WINTER,
            {"CHARGE_LOW": 23.87, "CHARGE_HIGH": 35.87},
            {"EG_CHARGE_LOW": -16.0, "EG_CHARGE_HIGH": -23.0},
        ),
        # Low User, spring: anytime energy folded into the single network period.
        ("CKHK_ANY_LOW_260401_T0", SPRING, {"CHARGE_LOW": 24.57}, {"EG_CHARGE_LOW": -13.74}),
        # Low User, winter: anytime export plus the winter network export credit.
        (
            "CKHK_ANY_LOW_260401_T0",
            WINTER,
            {"CHARGE_LOW": 24.57, "CHARGE_HIGH": 36.57},
            {"EG_CHARGE_LOW": -13.74, "EG_CHARGE_HIGH": -15.74},
        ),
    ],
)
def test_prices_match_website(
    code: str, day: date, imports: dict[str, float], exports: dict[str, float]
) -> None:
    pricing = price_day(_plan(code), day)

    assert pricing.import_buckets == pytest.approx(imports, abs=0.006)
    assert pricing.export_buckets == pytest.approx(exports, abs=0.006)


def test_daily_charges() -> None:
    pricing = price_day(_plan("CKHK_OFPK_LOW_260401_T0"), SPRING)

    assert pricing.daily_total == pytest.approx(155.0)
    assert any(charge.is_network for charge in pricing.daily_charges)
    # The Low plans carry a 0.2c EA levy (the Eco plans 0.16c), shown on the site as $0.0020.
    assert pricing.levy == pytest.approx(0.2)


def test_season_for() -> None:
    plan = _plan("CKHK_OFPK_LOW_260401_T0")

    season = season_for(plan, SPRING)
    assert season is not None
    assert season.start == date(2026, 10, 1)
    assert season_for(plan, date(2030, 1, 1)) is None


def _rate(**kwargs: object) -> Rate:
    defaults: dict[str, object] = {
        "id": "1",
        "description": "",
        "charge": None,
        "register_type": None,
        "unit": RateUnit.PER_KWH,
        "raw_unit": "cents/kWh",
        "price": 1.0,
        "is_network": False,
    }
    return Rate(**(defaults | kwargs))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("rate", "bucket"),
    [
        (_rate(charge="charge_high"), "CHARGE_HIGH"),
        (_rate(charge="CHARGE_24", register_type="EG"), "EG_CHARGE_24"),
        (_rate(description="Uncontrolled - Off Peak"), "CHARGE_LOW"),
        (_rate(description="Off-Peak Usage"), "CHARGE_LOW"),
        (_rate(description="Super Off Peak"), "super off peak"),
        (_rate(description="Super Off-Peak"), "super off-peak"),
        (_rate(description="Super-off-peak"), "super-off-peak"),
        (_rate(description="Super Off Peak Usage"), "super off peak usage"),
        (_rate(description="Holiday"), "holiday"),
        (_rate(description="Night"), "night"),
        (_rate(description="On Peak Export", register_type="EG"), "EG_CHARGE_HIGH"),
        (_rate(description="Peak Usage"), "CHARGE_HIGH"),
        (_rate(description="Shoulder"), "CHARGE_3"),
        (_rate(description="Anytime usage"), "CHARGE_24"),
        (_rate(description="Controlled"), "CN_CHARGE_24"),
        (_rate(charge="CHARGE_24", register_type="CN"), "CN_CHARGE_24"),
        (_rate(description="Controlled - Night", charge="CHARGE_LOW"), "CN_CHARGE_LOW"),
        (_rate(description="Night boost"), "night boost"),
        (_rate(description="Night boost", register_type="EG"), "EG_night boost"),
        (_rate(description="", unit=RateUnit.OTHER), "UNKNOWN"),
    ],
)
def test_resolve_bucket(rate: Rate, bucket: str) -> None:
    assert resolve_bucket(rate) == bucket


def test_periods() -> None:
    assert period_for_bucket("EG_CHARGE_HIGH") is TouPeriod.PEAK
    assert period_for_bucket("CHARGE_3") is TouPeriod.SHOULDER
    assert period_for_bucket("controlled") is None


def test_slot_map() -> None:
    weekday = tuple("peak" if 14 <= slot < 22 else "off" for slot in range(48))
    slots = SlotMap(weekday=weekday, weekend=("off",) * 48)

    assert slots.bucket_at(datetime(2026, 6, 15, 7, 0)) == "peak"
    assert slots.bucket_at(datetime(2026, 6, 15, 6, 59)) == "off"
    assert slots.bucket_at(datetime(2026, 6, 13, 8, 0)) == "off"
    assert SlotMap.flat("x").bucket_at(datetime(2026, 1, 1, 23, 45)) == "x"


def _with_controlled(plan: Plan) -> Plan:
    """Add a controlled-load supplier rate and network rate (12c and 3c) to a plan."""
    controlled = (
        _rate(description="Controlled", charge="CHARGE_24", register_type="CN", price=12.0),
        _rate(
            description="Controlled",
            charge="CHARGE_24",
            register_type="CN",
            price=3.0,
            is_network=True,
        ),
    )
    return replace(
        plan,
        retail_rates=(*plan.retail_rates, controlled[0]),
        network_rates=(*plan.network_rates, controlled[1]),
    )


def test_controlled_rates_are_priced_separately() -> None:
    plan = _plan("CKHK_ANY_LOW_260401_T0")
    without = price_day(plan, SPRING)
    pricing = price_day(_with_controlled(plan), SPRING)

    # Import and export are unchanged; controlled load gets its own price, levy included.
    assert pricing.import_buckets == without.import_buckets
    assert pricing.export_buckets == without.export_buckets
    assert pricing.controlled_buckets == {"CN_CHARGE_24": pytest.approx(15.2)}
    assert without.controlled_buckets == {}
    kinds = [part.kind.value for part in pricing.components["CN_CHARGE_24"]]
    assert kinds == ["supplier", "network", "levy"]
    assert period_for_bucket("CN_CHARGE_24") is TouPeriod.ANYTIME


def test_shoulder_rates() -> None:
    # A network with a shoulder tier, and an anytime supplier rate folded into all three.
    plan = replace(
        _plan("CKHK_ANY_LOW_260401_T0"),
        retail_rates=(_rate(description="Anytime usage", charge="CHARGE_24", price=18.0),),
        network_rates=(
            _rate(description="Off Peak", charge="CHARGE_LOW", price=2.0, is_network=True),
            _rate(description="Shoulder", charge="CHARGE_3", price=6.0, is_network=True),
            _rate(description="Peak", charge="CHARGE_HIGH", price=12.0, is_network=True),
        ),
    )
    pricing = price_day(plan, SPRING)

    assert pricing.import_buckets == {
        "CHARGE_LOW": pytest.approx(20.0),
        "CHARGE_3": pytest.approx(24.0),
        "CHARGE_HIGH": pytest.approx(30.0),
    }
    assert period_for_bucket("CHARGE_3") is TouPeriod.SHOULDER


def test_anytime_rate_merges_into_additional_network_buckets() -> None:
    plan = replace(
        _plan("CKHK_ANY_LOW_260401_T0"),
        network="ORON",
        price_category="RES",
        retail_rates=(
            _rate(description="Anytime usage", charge="CHARGE_24", price=18.3168),
            _rate(description="EA Levy", charge="CHARGE_24", price=0.16),
            _rate(
                description="Anytime Export",
                charge="CHARGE_24",
                register_type="EG",
                price=-13.74,
            ),
        ),
        network_rates=(
            _rate(
                description="Super Off Peak Usage",
                charge="CHARGE_4",
                price=0.0,
                is_network=True,
            ),
            _rate(
                description="Weekend Usage",
                charge="CHARGE_5",
                price=3.636,
                is_network=True,
            ),
        ),
    )
    pricing = price_day(plan, date(2026, 5, 1))

    assert pricing.import_buckets == pytest.approx({"CHARGE_4": 18.4768, "CHARGE_5": 22.1128})
    assert pricing.export_buckets == {"EG_CHARGE_24": -13.74}

    chart = read_chart((CHARTS / "ORON-RTOU24-TIERED-WE-NOGST.png").read_bytes())
    imports = match_tiers(
        chart.import_tiers,
        pricing.import_buckets,
        other_tiers=chart.export_tiers,
        other_prices=pricing.export_buckets,
    )
    exports = match_tiers(
        chart.export_tiers,
        pricing.export_buckets,
        other_tiers=chart.import_tiers,
        other_prices=pricing.import_buckets,
    )

    assert imports[:6] == ("CHARGE_5",) * 6
    assert imports[6:10] == ("CHARGE_4",) * 4
    assert imports[10:] == ("CHARGE_5",) * 38
    assert exports == ("EG_CHARGE_24",) * 48


def test_named_network_buckets_keep_their_identity_and_match_chart_tiers() -> None:
    categories = (
        ("Holiday", 1.0),
        ("Night", 2.0),
        ("Super Off Peak", 3.0),
    )
    plan = replace(
        _plan("CKHK_ANY_LOW_260401_T0"),
        retail_rates=(_rate(description="Anytime usage", charge="CHARGE_24", price=18.0),),
        network_rates=tuple(
            _rate(
                description=name,
                price=network_rate,
                is_network=True,
            )
            for name, network_rate in categories
        ),
    )
    pricing = price_day(plan, SPRING)

    assert pricing.import_buckets == {
        "holiday": 19.0,
        "night": 20.0,
        "super off peak": 21.0,
    }

    slots_by_category = (frozenset(range(16)), frozenset(range(16, 32)), frozenset(range(32, 48)))
    chart_tiers = tuple(
        ChartTier(
            colour=(0, 0, 255 - index * 40),
            height=price * 5,
            slots=slots,
        )
        for index, (slots, price) in enumerate(
            zip(slots_by_category, pricing.import_buckets.values(), strict=True)
        )
    )

    assert match_tiers(chart_tiers, pricing.import_buckets) == (
        ("holiday",) * 16 + ("night",) * 16 + ("super off peak",) * 16
    )


def _peak_slots(*peak: int) -> SlotMap:
    slots = tuple("CHARGE_HIGH" if slot in peak else "CHARGE_LOW" for slot in range(SLOTS_PER_DAY))
    return SlotMap(weekday=slots, weekend=slots)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 4, 4, 13, 45, tzinfo=UTC),
        # The same instant as a local time: stepping must not use wall-clock arithmetic.
        datetime(2026, 4, 5, 2, 45, tzinfo=NZ),
    ],
)
def test_next_bucket_change_in_repeated_hour(now: datetime) -> None:
    # Peak from 02:00 to 02:30 happens twice the night daylight saving ends. At the first 02:45
    # the next change is the second 02:00, not the following night.
    assert next_bucket_change(_peak_slots(4), now, NZ) == (
        datetime(2026, 4, 4, 14, 0, tzinfo=UTC),
        "CHARGE_HIGH",
    )


def test_next_bucket_change_across_skipped_hour() -> None:
    # Peak from 02:00 to 04:00 starts at 03:00 the night daylight saving starts.
    now = datetime(2026, 9, 27, 1, 15, tzinfo=NZ)
    assert next_bucket_change(_peak_slots(4, 5, 6, 7), now, NZ) == (
        datetime(2026, 9, 27, 3, 0, tzinfo=NZ),
        "CHARGE_HIGH",
    )
    # And ends at 04:00, an hour later rather than two.
    assert next_bucket_change(
        _peak_slots(4, 5, 6, 7), datetime(2026, 9, 27, 3, 0, tzinfo=NZ), NZ
    ) == (datetime(2026, 9, 27, 4, 0, tzinfo=NZ), "CHARGE_LOW")

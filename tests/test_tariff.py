"""Tests for turning plan rates into bucket prices.

Expected prices are the legend values printed on Ecotricity's own timeframe charts, so these tests
check the integration against the website rather than against itself.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.ecotricity_nz.api import Plan, Rate, RateUnit
from custom_components.ecotricity_nz.api.timeframes import (
    ChartReadError,
    ChartTier,
    match_tiers,
    read_chart,
)
from custom_components.ecotricity_nz.tariff import (
    SLOTS_PER_DAY,
    SlotMap,
    TouPeriod,
    bucket_name,
    bucket_names,
    bucket_period,
    next_bucket_change,
    period_for_bucket,
    price_day,
    resolve_bucket,
    season_for,
)
from tests.conftest import (
    ANYTIME,
    ANYTIME_PLAN,
    SOLAR_PLAN,
    TIME_OF_USE_PLAN,
    extra_network_codes,
    find_plan,
    find_plan_where,
    has_anytime_network_rate,
    has_extra_network_buckets,
    import_rates,
    stored_charts,
)

NZ = ZoneInfo("Pacific/Auckland")
WINTER = date(2026, 6, 15)
SPRING = date(2026, 10, 15)


@pytest.mark.parametrize(
    ("plan", "day", "imports", "exports"),
    [
        # Low Off Peak, spring: off-peak network charged in peak hours outside winter.
        (
            TIME_OF_USE_PLAN,
            SPRING,
            {"CHARGE_LOW": 24.04, "CHARGE_HIGH": 25.35},
            {"EG_CHARGE_LOW": -13.74},
        ),
        # LowBATTERY User, winter: two import and two export tiers.
        (
            SOLAR_PLAN,
            WINTER,
            {"CHARGE_LOW": 23.87, "CHARGE_HIGH": 35.87},
            {"EG_CHARGE_LOW": -16.0, "EG_CHARGE_HIGH": -23.0},
        ),
        # Low User, spring: anytime energy folded into the single network period.
        (ANYTIME_PLAN, SPRING, {"CHARGE_LOW": 24.57}, {"EG_CHARGE_LOW": -13.74}),
        # Low User, winter: anytime export plus the winter network export credit.
        (
            ANYTIME_PLAN,
            WINTER,
            {"CHARGE_LOW": 24.57, "CHARGE_HIGH": 36.57},
            {"EG_CHARGE_LOW": -13.74, "EG_CHARGE_HIGH": -15.74},
        ),
    ],
)
def test_prices_match_website(
    plan: str, day: date, imports: dict[str, float], exports: dict[str, float]
) -> None:
    pricing = price_day(find_plan(plan), day)

    assert pricing.import_buckets == pytest.approx(imports, abs=0.006)
    assert pricing.export_buckets == pytest.approx(exports, abs=0.006)


def test_daily_charges() -> None:
    pricing = price_day(find_plan(TIME_OF_USE_PLAN), SPRING)

    assert pricing.daily_total == pytest.approx(155.0)
    assert any(charge.is_network for charge in pricing.daily_charges)
    # The Low plans carry a 0.2c EA levy (the Eco plans 0.16c), shown on the site as $0.0020.
    assert pricing.levy == pytest.approx(0.2)


def test_season_for() -> None:
    plan = find_plan(TIME_OF_USE_PLAN)

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
        (_rate(description="Network usage"), "network usage"),
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
    # Only these three codes have a period that's the same on every plan.
    assert period_for_bucket("CHARGE_24") is TouPeriod.ANYTIME
    assert period_for_bucket("EG_CHARGE_HIGH") is TouPeriod.PEAK
    assert period_for_bucket("CN_CHARGE_LOW") is TouPeriod.OFF_PEAK
    assert period_for_bucket("CHARGE_3") is None
    assert period_for_bucket("controlled") is None


def test_numbered_network_buckets_are_named_from_the_plan() -> None:
    # A network that charges for more periods than peak and off-peak, under numbered charge codes.
    plan = replace(
        find_plan(ANYTIME_PLAN),
        retail_rates=(_rate(description="Anytime usage", charge="CHARGE_24"),),
        network_rates=tuple(
            _rate(description=description, charge=charge, is_network=True)
            for charge, description in (
                ("CHARGE_LOW", "Off Peak Usage"),
                ("CHARGE_3", "Shoulder Usage"),
                ("CHARGE_4", "Super Off Peak Usage"),
                ("CHARGE_5", "Weekend Usage"),
            )
        ),
    )
    names = bucket_names(plan)

    # "Usage" is dropped, and fixed periods aren't named from the plan at all.
    assert names == {"CHARGE_3": "Shoulder", "CHARGE_4": "Super off peak", "CHARGE_5": "Weekend"}
    assert [bucket_period(bucket, names) for bucket in ("CHARGE_LOW", "CHARGE_4", "CHARGE_5")] == [
        "off_peak",
        "super_off_peak",
        "weekend",
    ]
    assert bucket_period("EG_CHARGE_HIGH", names) == "peak"


@pytest.mark.parametrize(
    ("rates", "bucket", "name", "period"),
    [
        # Retail wording is preferred over the network's.
        (
            [
                _rate(description="Uncontrolled - Shoulder", charge="CHARGE_3", is_network=True),
                _rate(description="Shoulder Usage", charge="CHARGE_3"),
            ],
            "CHARGE_3",
            "Shoulder",
            "shoulder",
        ),
        # Other characters become underscores in the period.
        (
            [_rate(description="Off-Peak Plus (Night)", charge="CHARGE_4")],
            "CHARGE_4",
            "Off-peak plus (night)",
            "off_peak_plus_night",
        ),
        # "Export" is dropped from export rates too.
        (
            [_rate(description="Super Off Peak Export", charge="CHARGE_4", register_type="EG")],
            "EG_CHARGE_4",
            "Super off peak",
            "super_off_peak",
        ),
        # No description, or one that's only "Usage": named after the code.
        ([_rate(description="", charge="CHARGE_6")], "CHARGE_6", "Charge 6", "charge_6"),
        ([_rate(description="Usage", charge="CHARGE_7")], "CHARGE_7", "Charge 7", "charge_7"),
        # A name that would clash with a fixed period is named after the code instead.
        ([_rate(description="Peak Usage", charge="CHARGE_3")], "CHARGE_3", "Charge 3", "charge_3"),
        # Two codes with the same name: only the first keeps it.
        (
            [
                _rate(description="Night", charge="CHARGE_4"),
                _rate(description="Night", charge="CHARGE_5"),
            ],
            "CHARGE_5",
            "Charge 5",
            "charge_5",
        ),
    ],
)
def test_bucket_name_fallbacks(rates: list[Rate], bucket: str, name: str, period: str) -> None:
    plan = replace(
        find_plan(ANYTIME_PLAN),
        retail_rates=tuple(rate for rate in rates if not rate.is_network),
        network_rates=tuple(rate for rate in rates if rate.is_network),
    )
    names = bucket_names(plan)

    assert bucket_name(bucket, names) == name
    assert bucket_period(bucket, names) == period


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
    plan = find_plan(ANYTIME_PLAN)
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
        find_plan(ANYTIME_PLAN),
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
    assert bucket_period("CHARGE_3", bucket_names(plan)) == "shoulder"


def _read_imports(chart: Path, imports: dict[str, float], exports: dict[str, float]) -> tuple:
    reading = read_chart(chart.read_bytes())
    return match_tiers(
        reading.import_tiers,
        imports,
        other_tiers=reading.export_tiers,
        other_prices=exports,
    )


def _levy(plan: Plan, day: date) -> float:
    return sum(
        rate.price
        for rate in plan.rates
        if rate.is_levy and rate.unit is RateUnit.PER_KWH and rate.applies_on(day)
    )


def test_anytime_rate_merges_into_extra_network_buckets() -> None:
    # The plan has one anytime energy rate, but the network charges for more periods than peak and
    # off-peak (shoulder, weekend, super off-peak and so on) under numbered charge codes. The site
    # adds the anytime rate to every one of those periods, as it does to peak and off-peak.
    plan = find_plan_where(has_extra_network_buckets)
    charts = stored_charts(plan)
    assert charts

    for day, chart in charts:
        pricing = price_day(plan, day)
        anytime = sum(rate.price for rate in import_rates(plan.retail_rates, day))
        for code in extra_network_codes(plan, day):
            network = sum(
                rate.price for rate in import_rates(plan.network_rates, day) if rate.charge == code
            )
            assert pricing.import_buckets[code] == pytest.approx(
                anytime + network + _levy(plan, day)
            )
        # With those prices, the chart's bar heights fit.
        imports = _read_imports(chart, pricing.import_buckets, pricing.export_buckets)
        assert len(imports) == SLOTS_PER_DAY


def test_extra_network_buckets_without_the_anytime_rate_dont_fit_the_chart() -> None:
    # Without the anytime rate, those periods are only their network rate plus the levy, which can
    # be close to nothing. The charts' bar heights don't fit those prices, so they're rejected
    # rather than read as the wrong periods.
    plan = find_plan_where(has_extra_network_buckets)
    for day, chart in stored_charts(plan):
        pricing = price_day(plan, day)
        anytime = sum(rate.price for rate in import_rates(plan.retail_rates, day))
        extra = extra_network_codes(plan, day)
        unmerged = {
            bucket: price - anytime if bucket in extra else price
            for bucket, price in pricing.import_buckets.items()
        }
        with pytest.raises(ChartReadError):
            _read_imports(chart, unmerged, pricing.export_buckets)


def test_anytime_network_rate_is_added_to_every_period() -> None:
    # The network charges something at all hours (such as a transmission charge) as an anytime
    # network rate, next to its peak and off-peak rates. The site adds it to every period's price
    # instead of showing it as an anytime price of its own.
    plan = find_plan_where(has_anytime_network_rate)
    readable = []
    for day, chart in stored_charts(plan):
        pricing = price_day(plan, day)
        anytime = [rate for rate in import_rates(plan.network_rates, day) if rate.charge == ANYTIME]
        assert ANYTIME not in pricing.import_buckets
        for bucket in pricing.import_buckets:
            parts = {(part.description, part.price) for part in pricing.components[bucket]}
            assert {(rate.description, rate.price) for rate in anytime} <= parts, bucket
        try:
            _read_imports(chart, pricing.import_buckets, pricing.export_buckets)
        except ChartReadError:
            continue  # The chart has another scenario this plan doesn't price yet.
        readable.append((day, pricing, chart, sum(rate.price for rate in anytime)))
    assert readable

    # Charged as a price of its own instead, the other prices are short by it, and the import
    # and export bars don't fit them at one scale.
    for day, pricing, chart, anytime in readable:
        separate = {bucket: price - anytime for bucket, price in pricing.import_buckets.items()}
        separate[ANYTIME] = anytime + _levy(plan, day)
        with pytest.raises(ChartReadError):
            _read_imports(chart, separate, pricing.export_buckets)


def test_named_network_buckets_keep_their_identity_and_match_chart_tiers() -> None:
    categories = (
        ("Network usage A", 1.0),
        ("Network usage B", 2.0),
        ("Network usage C", 3.0),
    )
    plan = replace(
        find_plan(ANYTIME_PLAN),
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
        "network usage a": 19.0,
        "network usage b": 20.0,
        "network usage c": 21.0,
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
        ("network usage a",) * 16 + ("network usage b",) * 16 + ("network usage c",) * 16
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

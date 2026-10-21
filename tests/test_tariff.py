"""Tests for working out every half-hour's price from a plan's rates and its chart legends.

Most tests use rates and legend windows they make up, so each scenario is clear on its own. The
fixture tests check the same scenarios against real charts, wherever a network shows them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.ecotricity_nz.api import Plan, Rate, RateUnit, Season
from custom_components.ecotricity_nz.api.timeframes import (
    MINUTES_PER_DAY,
    SLOTS_PER_DAY,
    ChartReadError,
    ChartReading,
    LegendEntry,
    shown_price,
)
from custom_components.ecotricity_nz.tariff import (
    Alignment,
    DaySlots,
    SideRates,
    Slot,
    TouPeriod,
    bucket_name,
    bucket_names,
    bucket_period,
    chart_seasons,
    day_slots,
    day_windows,
    explain,
    first_day,
    next_change,
    next_season_change,
    period_for_bucket,
    plan_alignment,
    plan_slots,
    price_day,
    price_side,
    rate_periods,
    rates_alignment,
    resolve_bucket,
    season_charts,
    season_explained,
    season_for,
    season_slots,
    side_rates,
)
from tests.conftest import (
    ANYTIME_PLAN,
    SOLAR_PLAN,
    TIME_OF_USE_PLAN,
    chart_file,
    chart_files,
    chart_name,
    chart_reading,
    explained_chart,
    find_chart_where,
    find_plan,
    find_plan_where,
    has_capacity_charge,
    has_clipped_price_needing_its_bar,
    has_hours_without_a_network_rate,
    has_mixed_periods,
    has_overlapping_seasons,
)

NZ = ZoneInfo("Pacific/Auckland")
WINTER = date(2026, 6, 15)
SPRING = date(2026, 10, 15)


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


def _plan(*rates: Rate) -> Plan:
    """Return a plan with only the given rates (plus nothing else: no levy, no daily charges)."""
    return replace(
        find_plan(ANYTIME_PLAN),
        retail_rates=tuple(rate for rate in rates if not rate.is_network),
        network_rates=tuple(rate for rate in rates if rate.is_network),
    )


def _supplier(charge: str, price: float, **kwargs: object) -> Rate:
    return _rate(description=f"{charge} usage", charge=charge, price=price, **kwargs)


def _network(charge: str, price: float, **kwargs: object) -> Rate:
    return _rate(
        description=f"{charge} usage", charge=charge, price=price, is_network=True, **kwargs
    )


def _entries(*windows: tuple[int, int, str]) -> tuple[LegendEntry, ...]:
    """Return legend entries from (start hour, end hour, price text) windows."""
    return tuple(LegendEntry(start * 60, end * 60, text) for start, end, text in windows)


# Ecotricity's peak is 7am to 11am; the network's is 9am to 1pm. Neither has other hours.
SUPPLIER_PEAK_RATES = (_supplier("CHARGE_HIGH", 30.0), _supplier("CHARGE_LOW", 20.0))
NETWORK_PEAK_RATES = (_network("CHARGE_HIGH", 10.0), _network("CHARGE_LOW", 2.0))
MIXED_LEGEND = _entries(
    (0, 7, "22.00"),
    (7, 9, "32.00"),  # Ecotricity's peak, the network's off-peak
    (9, 11, "40.00"),  # both peak
    (11, 13, "30.00"),  # Ecotricity's off-peak, the network's peak
    (13, 24, "22.00"),
)


def _mixed_slots() -> tuple[Slot, ...]:
    rates = side_rates(_plan(*SUPPLIER_PEAK_RATES, *NETWORK_PEAK_RATES), SPRING, export=False)
    return explain(MIXED_LEGEND, rates)


def test_prices_match_website() -> None:
    # The default fixture's Low Off Peak weekday chart in winter, priced from its own legend.
    plan = find_plan(TIME_OF_USE_PLAN)
    readings = [
        chart_reading(chart_file(chart_name(plan, WINTER, weekend=w))) for w in (False, True)
    ]
    slots = day_slots(side_rates(plan, WINTER, export=False), *readings, export=False)
    pricing = price_day(plan, WINTER, slots, None)

    assert pricing.import_side is not None
    assert pricing.import_side.buckets == pytest.approx(
        {"CHARGE_LOW": 24.04, "CHARGE_HIGH": 37.35}, abs=0.006
    )


def test_daily_charges() -> None:
    pricing = price_day(find_plan(TIME_OF_USE_PLAN), SPRING, None, None)

    assert pricing.daily_total == pytest.approx(155.0)
    assert any(charge.is_network for charge in pricing.daily_charges)
    assert pricing.has_export


def test_daily_charges_leave_out_capacity_charges() -> None:
    # Charged per kVA, which nothing published gives, so there's no daily amount to add.
    plan = find_plan_where(has_capacity_charge)
    day = plan.seasons[0].start

    pricing = price_day(plan, day, None, None)

    per_day = [
        rate for rate in plan.rates if rate.applies_on(day) and rate.unit is RateUnit.PER_DAY
    ]
    assert pricing.daily_total == pytest.approx(sum(rate.price for rate in per_day))
    assert [charge.description for charge in pricing.daily_charges] == [
        rate.description for rate in per_day
    ]


def test_season_for() -> None:
    plan = find_plan(TIME_OF_USE_PLAN)

    season = season_for(plan, SPRING)
    assert season is not None
    assert season.start == date(2026, 10, 1)
    assert season_for(plan, date(2030, 1, 1)) is None


def test_any_other_season_wins_over_standard_rates() -> None:
    # A plan can publish "Standard Rates" for the whole year and a season inside it. Ecotricity's
    # site prices a day in both with the other season.
    plan = find_plan_where(has_overlapping_seasons)
    for season in plan.seasons:
        chosen = season_for(plan, season.start)
        assert chosen is not None
        covering = [other for other in plan.seasons if other.contains(season.start)]
        if len(covering) > 1:
            assert chosen.name.lower() != "standard rates"


def test_outer_season_first_applies_when_the_season_inside_it_ends() -> None:
    plan = find_plan_where(has_overlapping_seasons)
    inner = min(plan.seasons, key=lambda season: season.end - season.start)
    outer = max(plan.seasons, key=lambda season: season.end - season.start)
    day = inner.start + timedelta(days=14)

    assert first_day(plan, inner, day) == day
    assert first_day(plan, outer, day) == inner.end + timedelta(days=1)


def test_season_covered_by_another_for_the_rest_of_its_days_no_longer_applies() -> None:
    plan = find_plan_where(has_overlapping_seasons)
    inner = min(plan.seasons, key=lambda season: season.end - season.start)
    outer = max(plan.seasons, key=lambda season: season.end - season.start)
    # The inner season runs to the outer one's end, so the outer one doesn't apply again.
    plan = replace(plan, seasons=(outer, replace(inner, end=outer.end)))

    assert first_day(plan, outer, inner.start + timedelta(days=14)) is None


def test_next_season_change_with_overlapping_seasons() -> None:
    standard = Season("Standard Rates", date(2026, 4, 1), date(2027, 3, 31))
    winter = Season("Seasonal Rates", date(2026, 6, 1), date(2026, 9, 30))
    plan = replace(find_plan(ANYTIME_PLAN), seasons=(standard, winter))

    assert next_season_change(plan, date(2026, 5, 15)) == winter.start
    assert next_season_change(plan, date(2026, 7, 1)) == date(2026, 10, 1)
    assert next_season_change(plan, date(2026, 11, 1)) == date(2027, 4, 1)
    # Before any season, the first one's start.
    assert next_season_change(plan, date(2026, 1, 1)) == standard.start


@pytest.mark.parametrize(
    ("rate", "bucket"),
    [
        (_rate(charge="charge_high"), "CHARGE_HIGH"),
        (_rate(charge="CHARGE_24", register_type="EG"), "EG_CHARGE_24"),
        (_rate(charge="CHARGE_24", register_type="CN"), "CN_CHARGE_24"),
        (_rate(description="Controlled - Night", charge="CHARGE_LOW"), "CN_CHARGE_LOW"),
        # A controlled rate without a code applies whenever the load is on.
        (_rate(description="Controlled"), "CN_CHARGE_24"),
        # Any other rate without a code keeps its description.
        (_rate(description="Night boost"), "NIGHT BOOST"),
        (_rate(description="Night boost", register_type="EG"), "EG_NIGHT BOOST"),
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
    plan = _plan(
        _rate(description="Anytime usage", charge="CHARGE_24"),
        *(
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
    names = bucket_names(_plan(*rates))

    assert bucket_name(bucket, names) == name
    assert bucket_period(bucket, names) == period


def test_price_combines_supplier_and_network_rates_from_different_periods() -> None:
    # Where Ecotricity's and the network's peaks don't line up, a half-hour's price is one
    # period's supplier rate plus the other period's network rate, as bills charge it.
    slots = _mixed_slots()

    assert (slots[16].supplier, slots[16].network, slots[16].price) == (
        "CHARGE_HIGH",
        "CHARGE_LOW",
        32.0,
    )
    assert (slots[20].supplier, slots[20].network) == ("CHARGE_HIGH", "CHARGE_HIGH")
    assert (slots[24].supplier, slots[24].network, slots[24].price) == (
        "CHARGE_LOW",
        "CHARGE_HIGH",
        30.0,
    )
    assert [part.kind.value for part in slots[24].components] == ["supplier", "network"]


def test_fixture_charts_with_mixed_periods() -> None:
    chart = find_chart_where(has_mixed_periods)
    reading = chart_reading(chart)
    for export, (rates, slots) in explained_chart(chart).items():
        entries = reading.export_entries if export else reading.import_entries
        for entry in entries:
            slot = slots[entry.slots[0]]
            assert slot.price == pytest.approx(sum(part.price for part in slot.components))
            assert entry.clipped or shown_price(slot.price) == entry.text
        assert {slot.supplier for slot in slots} <= {*rates.supplier, rates.prefix + "CHARGE_24"}


def test_price_no_combination_explains_is_rejected() -> None:
    rates = side_rates(_plan(*SUPPLIER_PEAK_RATES, *NETWORK_PEAK_RATES), SPRING, export=False)

    with pytest.raises(ChartReadError, match="doesn't match"):
        explain(_entries((0, 12, "22.00"), (12, 24, "25.00")), rates)


def test_hours_without_a_network_rate_are_the_supplier_rate_alone() -> None:
    # A network that only publishes a peak export rate charges nothing in other hours.
    rates = side_rates(
        _plan(
            _supplier("CHARGE_24", -16.0, register_type="EG"),
            _network("CHARGE_HIGH", -5.0, register_type="EG"),
        ),
        SPRING,
        export=True,
    )
    slots = explain(_entries((0, 17, "-16.00"), (17, 21, "-21.00"), (21, 24, "-16.00")), rates)

    assert {slot.network for slot in slots[:34]} == {"EG_CHARGE_24"}
    assert slots[34].network == "EG_CHARGE_HIGH"


def test_fixture_chart_with_hours_without_a_network_rate() -> None:
    chart = find_chart_where(has_hours_without_a_network_rate)
    for rates, slots in explained_chart(chart).values():
        for slot in slots:
            if slot.network == rates.prefix + "CHARGE_24" and rates.network:
                assert not any(part.kind.value == "network" for part in slot.components)


def test_equal_network_rates_prefer_the_supplier_period() -> None:
    # Two network periods at the same rate give the same price, so the one that lines up with
    # Ecotricity's period is taken.
    rates = side_rates(
        _plan(*SUPPLIER_PEAK_RATES, _network("CHARGE_HIGH", 0.0), _network("CHARGE_LOW", 0.0)),
        SPRING,
        export=False,
    )
    slots = explain(_entries((0, 7, "20.00"), (7, 11, "30.00"), (11, 24, "20.00")), rates)

    assert all(slot.network == slot.supplier for slot in slots)


def test_price_fitting_combinations_with_different_exact_prices_is_rejected() -> None:
    # Ecotricity's peak with the network's off-peak, and the other way round, come to 30.548c and
    # 30.552c, which both print as 30.55. Which periods apply can't be told, so it isn't guessed.
    rates = side_rates(
        _plan(
            _supplier("CHARGE_HIGH", 25.0),
            _supplier("CHARGE_LOW", 20.0),
            _network("CHARGE_HIGH", 10.552),
            _network("CHARGE_LOW", 5.548),
        ),
        SPRING,
        export=False,
    )
    assert shown_price(25.0 + 5.548) == shown_price(20.0 + 10.552) == "30.55"

    with pytest.raises(ChartReadError, match="more than one"):
        explain(_entries((0, 12, "25.55"), (12, 24, "30.55")), rates)


@pytest.mark.parametrize(
    ("text", "estimate", "price"),
    [
        # The digits that fit settle it.
        ("-30.1", None, -30.1),
        # They fit two prices, and the bar is clearly nearer one.
        ("-3", -30.0, -30.1),
        ("-3", -35.9, -36.0),
    ],
)
def test_clipped_price_is_settled(text: str, estimate: float | None, price: float) -> None:
    rates = side_rates(
        _plan(
            _supplier("CHARGE_HIGH", -26.0, register_type="EG"),
            _supplier("CHARGE_LOW", -20.1, register_type="EG"),
            _network("CHARGE_HIGH", -10.0, register_type="EG"),
            _network("CHARGE_LOW", 0.0, register_type="EG"),
        ),
        SPRING,
        export=True,
    )
    entries = (
        LegendEntry(0, 720, "-20.10"),
        LegendEntry(720, MINUTES_PER_DAY, text, clipped=True, estimate=estimate),
    )

    assert explain(entries, rates)[-1].price == pytest.approx(price)


@pytest.mark.parametrize("estimate", [None, -33.0])
def test_unsettled_clipped_price_is_rejected(estimate: float | None) -> None:
    # Without a bar, or with one about halfway between two prices, it can't be told.
    rates = side_rates(
        _plan(
            _supplier("CHARGE_HIGH", -26.0, register_type="EG"),
            _supplier("CHARGE_LOW", -20.1, register_type="EG"),
            _network("CHARGE_HIGH", -10.0, register_type="EG"),
            _network("CHARGE_LOW", 0.0, register_type="EG"),
        ),
        SPRING,
        export=True,
    )
    entries = (
        LegendEntry(0, 720, "-20.10"),
        LegendEntry(720, MINUTES_PER_DAY, "-3", clipped=True, estimate=estimate),
    )

    with pytest.raises(ChartReadError, match="more than one"):
        explain(entries, rates)


def test_fixture_clipped_price_is_settled_by_its_bar() -> None:
    chart = find_chart_where(has_clipped_price_needing_its_bar)
    reading = chart_reading(chart)
    sides = explained_chart(chart)
    for export, (_rates, slots) in sides.items():
        entries = reading.export_entries if export else reading.import_entries
        for entry in entries:
            if entry.clipped and entry.estimate is not None:
                assert slots[entry.slots[0]].price == pytest.approx(entry.estimate, abs=0.5)


def test_one_price_all_day_needs_no_chart() -> None:
    plan = _plan(_supplier("CHARGE_24", 25.0), _network("CHARGE_24", 5.0))
    slots, gap = season_slots({}, plan, season_for(plan, SPRING), SPRING, export=False)

    assert slots is not None
    assert {slot.price for slot in slots.all} == {30.0}
    assert gap is None


def _peak_only_network_export() -> SideRates:
    """One Ecotricity export rate, and a network export rate for the peak only."""
    return side_rates(
        _plan(
            _supplier("CHARGE_24", -16.0, register_type="EG"),
            _network("CHARGE_HIGH", -5.0, register_type="EG"),
        ),
        SPRING,
        export=True,
    )


def test_network_rate_for_some_hours_is_read_from_the_chart() -> None:
    reading = ChartReading(
        import_entries=_entries((0, 24, "20.00")),
        export_entries=_entries((0, 17, "-16.00"), (17, 21, "-21.00"), (21, 24, "-16.00")),
    )
    slots = day_slots(_peak_only_network_export(), reading, reading, export=True)

    assert [slot.price for slot in slots.weekday[33:36]] == [-16.0, -21.0, -21.0]
    assert {slot.price for slot in slots.weekend} == {-16.0, -21.0}


SPRING_SEASON = Season("Seasonal Rates", date(2026, 10, 1), date(2026, 12, 31))
SUMMER_SEASON = Season("Seasonal Rates", date(2027, 1, 1), date(2027, 3, 31))
PEAK_ONLY_EXPORT_LEGEND = ChartReading(
    import_entries=(),
    export_entries=_entries((0, 17, "-16.00"), (17, 21, "-21.00"), (21, 24, "-16.00")),
)


def _peak_only_network_export_plan(*seasons: Season) -> Plan:
    """A plan with `_peak_only_network_export`'s rates and a chart for each season."""
    return replace(
        _plan(
            _supplier("CHARGE_24", -16.0, register_type="EG"),
            _network("CHARGE_HIGH", -5.0, register_type="EG"),
        ),
        seasons=seasons,
        timeframe_chart_weekday="/charts/X-SSSSSSSS-EEEEEEEE-WD.png",
        timeframe_chart_weekend="/charts/X-SSSSSSSS-EEEEEEEE-WE.png",
    )


def _charts(plan: Plan, season: Season, reading: ChartReading) -> dict[str, ChartReading]:
    return dict.fromkeys(season_charts(plan, season), reading)


def test_season_charts_are_read_where_one_network_rate_may_apply_all_day() -> None:
    plan = _peak_only_network_export_plan(SPRING_SEASON)
    charts = _charts(plan, SPRING_SEASON, PEAK_ONLY_EXPORT_LEGEND)

    slots, missing = season_slots(charts, plan, SPRING_SEASON, SPRING, export=True)

    assert slots is not None
    assert {slot.price for slot in slots.all} == {-16.0, -21.0}
    assert missing is None


def test_missing_charts_are_reported_where_one_network_rate_is_taken_to_apply_all_day() -> None:
    # The charts might show the rate only applies in some hours, so the gap is reported.
    plan = _peak_only_network_export_plan(SPRING_SEASON)

    slots, missing = season_slots({}, plan, SPRING_SEASON, SPRING, export=True)

    assert slots is not None
    assert {slot.price for slot in slots.all} == {-21.0}
    assert missing == "The plan's charts for the season couldn't be downloaded or read"

    # The same until the season is published, which gives the charts their dates.
    assert season_charts(plan, None) == []
    slots, missing = season_slots({}, plan, None, SPRING, export=True)
    assert slots is not None
    assert missing == "No published season covers the day"


def test_one_network_rate_applies_all_day_when_the_chart_has_no_windows_for_it() -> None:
    plan = _peak_only_network_export_plan(SPRING_SEASON)
    reading = ChartReading(import_entries=_entries((0, 24, "20.00")), export_entries=())

    slots, missing = season_slots(
        _charts(plan, SPRING_SEASON, reading), plan, SPRING_SEASON, SPRING, export=True
    )

    assert slots is not None
    assert {slot.price for slot in slots.all} == {-21.0}
    assert missing == "The chart has no export windows"


def _anytime_plan(*seasons: Season) -> Plan:
    """An anytime plan: one Ecotricity rate, and the network's off-peak rate, which applies all
    day."""
    return replace(
        _plan(_supplier("CHARGE_24", 25.0), _network("CHARGE_LOW", 5.0)),
        seasons=seasons,
        timeframe_chart_weekday="/charts/X-SSSSSSSS-EEEEEEEE-WD.png",
        timeframe_chart_weekend="/charts/X-SSSSSSSS-EEEEEEEE-WE.png",
    )


def test_one_network_rate_applies_all_day_when_a_chart_with_one_price_doesnt_match() -> None:
    # The side's one price may need no chart, so a chart showing another price all day (drawn
    # before a rate change, say) is reported rather than rejecting the side.
    plan = _anytime_plan(SPRING_SEASON)
    stale = ChartReading(import_entries=_entries((0, 24, "99.00")), export_entries=())

    slots, missing = season_slots(
        _charts(plan, SPRING_SEASON, stale), plan, SPRING_SEASON, SPRING, export=False
    )

    assert slots is not None
    assert {slot.price for slot in slots.all} == {30.0}
    assert missing is not None
    assert "doesn't match the plan's rates" in missing


def test_one_network_rate_applies_all_day_when_none_of_the_charts_prices_match() -> None:
    plan = _peak_only_network_export_plan(SPRING_SEASON)
    stale = ChartReading(
        import_entries=(), export_entries=_entries((0, 17, "-99.00"), (17, 24, "-98.00"))
    )

    slots, missing = season_slots(
        _charts(plan, SPRING_SEASON, stale), plan, SPRING_SEASON, SPRING, export=True
    )

    assert slots is not None
    assert {slot.price for slot in slots.all} == {-21.0}
    assert missing is not None


def test_chart_showing_one_network_rate_only_in_some_hours_is_rejected_when_it_doesnt_match() -> (
    None
):
    # The off-peak price matches, so the chart shows the network rate only applies at peak,
    # but the peak price doesn't (the network changed it mid-season, say). Taking the rate to
    # apply all day would price every off-peak half-hour wrongly.
    plan = _peak_only_network_export_plan(SPRING_SEASON)
    changed = ChartReading(
        import_entries=(),
        export_entries=_entries((0, 17, "-16.00"), (17, 21, "-25.00"), (21, 24, "-16.00")),
    )

    with pytest.raises(ChartReadError, match="doesn't match the plan's rates"):
        season_slots(
            _charts(plan, SPRING_SEASON, changed), plan, SPRING_SEASON, SPRING, export=True
        )


def test_season_is_explained_only_when_its_charts_match_the_rates() -> None:
    plan = _peak_only_network_export_plan(SPRING_SEASON)
    unmatched = ChartReading(import_entries=(), export_entries=_entries((0, 24, "-99.00")))

    assert season_explained(
        _charts(plan, SPRING_SEASON, PEAK_ONLY_EXPORT_LEGEND), plan, SPRING_SEASON, SPRING
    )
    assert not season_explained(
        _charts(plan, SPRING_SEASON, unmatched), plan, SPRING_SEASON, SPRING
    )
    assert not season_explained({}, plan, SPRING_SEASON, SPRING)


def test_plan_without_charts_takes_one_network_rate_to_apply_all_day() -> None:
    plan = replace(
        _peak_only_network_export_plan(SPRING_SEASON),
        timeframe_chart_weekday=None,
        timeframe_chart_weekend=None,
    )

    slots, missing = season_slots({}, plan, SPRING_SEASON, SPRING, export=True)

    assert slots is not None
    assert {slot.price for slot in slots.all} == {-21.0}
    assert missing is None


def test_side_with_one_possible_price_needs_no_charts() -> None:
    plan = replace(
        _peak_only_network_export_plan(SPRING_SEASON),
        network_rates=(),
        retail_rates=(_supplier("CHARGE_24", -16.0, register_type="EG"),),
    )

    slots, missing = season_slots({}, plan, SPRING_SEASON, SPRING, export=True)

    assert slots is not None
    assert missing is None


def test_side_that_needs_its_charts_is_rejected_without_them() -> None:
    plan = replace(
        _peak_only_network_export_plan(SPRING_SEASON),
        network_rates=tuple(
            _network(charge, price, register_type="EG")
            for charge, price in (("CHARGE_HIGH", -5.0), ("CHARGE_LOW", -1.0))
        ),
    )

    with pytest.raises(ChartReadError, match="couldn't be downloaded or read"):
        season_slots({}, plan, SPRING_SEASON, SPRING, export=True)


def test_chart_whose_cut_off_price_fits_several_of_the_rates_prices_is_rejected() -> None:
    # A cut-off price that fits both the peak and off-peak price is still one the rates give, so
    # with another price on the chart, the price is shown to vary.
    plan = _peak_only_network_export_plan(SPRING_SEASON)
    cut_off = LegendEntry(0, 17 * 60, "-", clipped=True)
    reading = ChartReading(
        import_entries=(), export_entries=(cut_off, *_entries((17, 24, "-99.00")))
    )

    with pytest.raises(ChartReadError, match="fits more than one"):
        season_slots(
            _charts(plan, SPRING_SEASON, reading), plan, SPRING_SEASON, SPRING, export=True
        )


def test_later_season_whose_chart_isnt_explained_is_left_out() -> None:
    plan = _peak_only_network_export_plan(SPRING_SEASON, SUMMER_SEASON)
    unexplained = ChartReading(
        import_entries=(),
        export_entries=_entries((0, 17, "-16.00"), (17, 21, "-25.00"), (21, 24, "-16.00")),
    )
    charts = _charts(plan, SPRING_SEASON, PEAK_ONLY_EXPORT_LEGEND) | _charts(
        plan, SUMMER_SEASON, unexplained
    )

    slots = plan_slots(charts, plan, SPRING)

    assert slots.today[True] is not None
    assert not slots.errors
    assert slots.later[True] == []
    assert len(slots.later_errors) == 1
    assert "2027-01-01" in slots.later_errors[0]
    assert "doesn't match the plan's rates" in slots.later_errors[0]


def test_later_season_with_missing_charts_takes_one_network_rate_to_apply_all_day() -> None:
    # Priced as today's would be, and reported the same way, so the season still counts towards
    # the plan's periods and doesn't hold back removing sensors the plan no longer has.
    plan = _peak_only_network_export_plan(SPRING_SEASON, SUMMER_SEASON)

    slots = plan_slots(_charts(plan, SPRING_SEASON, PEAK_ONLY_EXPORT_LEGEND), plan, SPRING)

    assert len(slots.later[True]) == 1
    assert {slot.price for slot in slots.later[True][0].all} == {-21.0}
    assert not slots.later_errors
    assert len(slots.missing) == 1
    assert slots.missing[0].startswith("Export from 2027-01-01: ")
    assert slots.gaps == ("Export from 2027-01-01",)


def test_todays_missing_charts_are_reported_separately_from_errors() -> None:
    plan = _peak_only_network_export_plan(SPRING_SEASON)

    slots = plan_slots({}, plan, SPRING)

    assert slots.today[True] is not None
    assert not slots.errors
    assert slots.missing == (
        "Export: The plan's charts for the season couldn't be downloaded or read",
    )
    # Keyed apart from the same season's gap while it was still to come.
    assert slots.gaps == (f"Export today in {SPRING_SEASON.start.isoformat()}",)
    assert slots.seasons(export=True) == [slots.today[True]]


def test_next_season_for_charts_can_be_inside_todays() -> None:
    # Outside the inner season, the next season to apply is the inner one, not the season after
    # the outer one ends.
    standard = Season("Standard Rates", date(2026, 4, 1), date(2027, 3, 31))
    winter = Season("Seasonal Rates", date(2026, 6, 1), date(2026, 9, 30))
    plan = replace(_peak_only_network_export_plan(), seasons=(standard, winter))

    assert chart_seasons(plan, date(2026, 5, 15), upcoming=False) == [standard, winter]
    assert chart_seasons(plan, date(2026, 7, 1), upcoming=False) == [winter, standard]
    assert chart_seasons(plan, date(2026, 11, 1), upcoming=False) == [standard]
    assert chart_seasons(plan, date(2026, 5, 15), upcoming=True) == [standard, winter]


def test_plan_with_one_set_of_charts_has_no_seasons_for_them() -> None:
    plan = replace(
        _peak_only_network_export_plan(SPRING_SEASON),
        timeframe_chart_weekday="/charts/X-WD.png",
        timeframe_chart_weekend="/charts/X-WE.png",
    )

    assert chart_seasons(plan, SPRING, upcoming=False) == [None]
    assert chart_seasons(plan, SPRING, upcoming=True) == [None]
    assert season_charts(plan, None) == ["/charts/X-WD-NOGST.png", "/charts/X-WE-NOGST.png"]


def test_weekday_and_weekend_sharing_a_chart_download_it_once() -> None:
    plan = replace(
        _peak_only_network_export_plan(SPRING_SEASON),
        timeframe_chart_weekday="/charts/X.png",
        timeframe_chart_weekend="/charts/X.png",
    )

    assert season_charts(plan, None) == ["/charts/X-NOGST.png"]


def test_fixture_charts_agree_where_one_network_rate_is_taken_to_apply_all_day() -> None:
    # Without a chart, a side whose network has one time-of-use rate gets that rate all day;
    # every fixture chart of such a side shows that price all day too.
    checked = 0
    for chart in chart_files():
        for rates, slots in explained_chart(chart).values():
            if rates.flat() is None and (likely := rates.likely_flat()) is not None:
                assert {slot.price for slot in slots} == {likely.price}, chart.name
                checked += 1
    assert checked


def _rate_prices(rates: SideRates, alignment: Alignment) -> dict[str, float | None]:
    return {
        bucket: None if parts is None else sum(part.price for part in parts)
        for bucket, parts in rate_periods(rates, alignment).items()
    }


def test_rates_alone_price_periods_that_line_up() -> None:
    rates = side_rates(_plan(*SUPPLIER_PEAK_RATES, *NETWORK_PEAK_RATES), SPRING, export=False)

    assert rates_alignment(rates) is Alignment.SUPPLIER
    assert _rate_prices(rates, Alignment.SUPPLIER) == {"CHARGE_HIGH": 40.0, "CHARGE_LOW": 22.0}


def test_rates_alone_price_the_network_periods_with_one_supplier_rate() -> None:
    rates = side_rates(
        _plan(_supplier("CHARGE_24", 20.0), *NETWORK_PEAK_RATES), SPRING, export=False
    )

    assert rates_alignment(rates) is Alignment.NETWORK
    assert _rate_prices(rates, Alignment.NETWORK) == {"CHARGE_HIGH": 30.0, "CHARGE_LOW": 22.0}


def test_rates_alone_leave_a_period_only_one_party_has_unpriced() -> None:
    # Which of Ecotricity's rates goes with the network's shoulder only the chart can tell.
    rates = side_rates(
        _plan(*SUPPLIER_PEAK_RATES, *NETWORK_PEAK_RATES, _network("CHARGE_3", 5.0)),
        SPRING,
        export=False,
    )

    assert _rate_prices(rates, Alignment.SPLIT) == {
        "CHARGE_3": None,
        "CHARGE_HIGH": 40.0,
        "CHARGE_LOW": 22.0,
    }


def test_missing_chart_is_rejected() -> None:
    rates = side_rates(_plan(*SUPPLIER_PEAK_RATES), SPRING, export=False)

    with pytest.raises(ChartReadError, match="couldn't be downloaded"):
        day_slots(rates, None, None, export=False)


def test_chart_without_export_windows_is_rejected() -> None:
    rates = side_rates(
        _plan(
            _supplier("CHARGE_HIGH", -21.0, register_type="EG"),
            _supplier("CHARGE_LOW", -16.0, register_type="EG"),
        ),
        SPRING,
        export=True,
    )
    reading = ChartReading(import_entries=_entries((0, 24, "20.00")), export_entries=())

    with pytest.raises(ChartReadError, match="no export windows"):
        day_slots(rates, reading, reading, export=True)


def _day(slots: tuple[Slot, ...]) -> DaySlots:
    return DaySlots(weekday=slots, weekend=slots)


def test_periods_that_line_up_follow_ecotricity() -> None:
    rates = side_rates(_plan(*SUPPLIER_PEAK_RATES, *NETWORK_PEAK_RATES), SPRING, export=False)
    slots = explain(_entries((0, 7, "22.00"), (7, 11, "40.00"), (11, 24, "22.00")), rates)

    assert plan_alignment([_day(slots)]) is Alignment.SUPPLIER
    priced = price_side(_day(slots), Alignment.SUPPLIER)
    assert priced.buckets == {"CHARGE_HIGH": 40.0, "CHARGE_LOW": 22.0}


def test_one_supplier_rate_follows_the_network() -> None:
    # Battery plans' import: one Ecotricity rate, the network's peak and off-peak.
    rates = side_rates(
        _plan(_supplier("CHARGE_24", 20.0), *NETWORK_PEAK_RATES), SPRING, export=False
    )
    slots = explain(_entries((0, 9, "22.00"), (9, 13, "30.00"), (13, 24, "22.00")), rates)

    assert plan_alignment([_day(slots)]) is Alignment.NETWORK
    priced = price_side(_day(slots), Alignment.NETWORK)
    assert {slot.bucket for slot in priced.slots.all} == {"CHARGE_HIGH", "CHARGE_LOW"}


def test_periods_that_dont_line_up_are_split() -> None:
    slots = _mixed_slots()

    assert plan_alignment([_day(slots)]) is Alignment.SPLIT
    priced = price_side(_day(slots), Alignment.SPLIT)
    # Each period's own price is where both are in it.
    assert priced.buckets == {"CHARGE_HIGH": 40.0, "CHARGE_LOW": 22.0}
    # A mixed half-hour takes the period whose own price is nearest: 32c is nearer peak's 40c
    # than off-peak's 22c, but 30c is nearer off-peak.
    labels = [
        (start, end, slot.bucket, slot.price)
        for start, end, slot in day_windows(priced.slots.weekday)
    ]
    assert labels == [
        ("00:00", "07:00", "CHARGE_LOW", 22.0),
        ("07:00", "09:00", "CHARGE_HIGH", 32.0),
        ("09:00", "11:00", "CHARGE_HIGH", 40.0),
        ("11:00", "13:00", "CHARGE_LOW", 30.0),
        ("13:00", "24:00", "CHARGE_LOW", 22.0),
    ]
    # Ecotricity's and the network's own windows ignore the price.
    assert [window[:2] for window in day_windows(priced.slots.weekday, "supplier")] == [
        ("00:00", "07:00"),
        ("07:00", "11:00"),
        ("11:00", "24:00"),
    ]


def test_split_is_decided_across_seasons() -> None:
    # A season where the network has no peak lines up; one where it does doesn't.
    rates = side_rates(
        _plan(*SUPPLIER_PEAK_RATES, _network("CHARGE_LOW", 2.0)), SPRING, export=False
    )
    aligned = explain(_entries((0, 7, "22.00"), (7, 11, "32.00"), (11, 24, "22.00")), rates)

    assert plan_alignment([_day(aligned)]) is Alignment.SUPPLIER
    assert plan_alignment([_day(aligned), _day(_mixed_slots())]) is Alignment.SPLIT


def test_split_side_with_one_price_all_day_in_a_season() -> None:
    # A side that's split in other seasons can have a season with one anytime price, where
    # neither Ecotricity nor the network has periods; it's labelled anytime.
    plan = _plan(_supplier("CHARGE_24", 20.0), _network("CHARGE_24", 2.0))
    flat, _gap = season_slots({}, plan, season_for(plan, SPRING), SPRING, export=False)
    assert flat is not None
    priced = price_side(flat, Alignment.SPLIT)

    assert {slot.bucket for slot in priced.slots.all} == {"CHARGE_24"}
    assert priced.buckets == {"CHARGE_24": 22.0}


def test_controlled_rates_are_priced_separately() -> None:
    plan = _plan(
        _rate(description="Controlled", charge="CHARGE_24", register_type="CN", price=12.0),
        _rate(
            description="Controlled",
            charge="CHARGE_24",
            register_type="CN",
            price=3.0,
            is_network=True,
        ),
        _rate(description="Controlled night", charge="CHARGE_LOW", register_type="CN", price=8.0),
        _rate(description="EA Levy", charge="CHARGE_24", price=0.2),
    )
    pricing = price_day(plan, SPRING, None, None)

    # An anytime controlled rate applies alongside the other controlled period, levy included.
    assert pricing.controlled_buckets == {"CN_CHARGE_LOW": pytest.approx(23.2)}
    kinds = [part.kind.value for part in pricing.controlled_components["CN_CHARGE_LOW"]]
    assert kinds == ["supplier", "supplier", "network", "levy"]


def test_levy_on_the_controlled_register_is_added_once() -> None:
    plan = _plan(
        _rate(description="Controlled", charge="CHARGE_24", register_type="CN", price=12.0),
        _rate(description="EA Levy", charge="CHARGE_24", register_type="CN", price=0.2),
    )
    pricing = price_day(plan, SPRING, None, None)

    assert pricing.controlled_buckets == {"CN_CHARGE_24": pytest.approx(12.2)}
    kinds = [part.kind.value for part in pricing.controlled_components["CN_CHARGE_24"]]
    assert kinds == ["supplier", "levy"]


def test_levy_on_the_export_register_is_left_out_of_export_prices() -> None:
    # Levies are charged on imported energy, never taken off export credits.
    plan = _plan(
        _supplier("CHARGE_24", -16.0, register_type="EG"),
        _rate(description="EA Levy", charge="CHARGE_24", register_type="EG", price=0.2),
    )
    flat = side_rates(plan, SPRING, export=True).flat()

    assert flat is not None
    assert flat.price == pytest.approx(-16.0)
    assert [part.kind.value for part in flat.components] == ["supplier"]


def _peak_slots(*peak: int) -> DaySlots:
    off_peak = Slot(20.0, "CHARGE_LOW", "CHARGE_LOW", (), bucket="CHARGE_LOW")
    peak_slot = Slot(30.0, "CHARGE_HIGH", "CHARGE_HIGH", (), bucket="CHARGE_HIGH")
    slots = tuple(peak_slot if index in peak else off_peak for index in range(SLOTS_PER_DAY))
    return DaySlots(weekday=slots, weekend=slots)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 4, 4, 13, 45, tzinfo=UTC),
        # The same instant as a local time: stepping must not use wall-clock arithmetic.
        datetime(2026, 4, 5, 2, 45, tzinfo=NZ),
    ],
)
def test_next_change_in_repeated_hour(now: datetime) -> None:
    # Peak from 02:00 to 02:30 happens twice the night daylight saving ends. At the first 02:45
    # the next change is the second 02:00, not the following night.
    change = next_change(_peak_slots(4), now, NZ)
    assert change is not None
    assert change[0] == datetime(2026, 4, 4, 14, 0, tzinfo=UTC)
    assert change[1].bucket == "CHARGE_HIGH"


def test_next_change_across_skipped_hour() -> None:
    # Peak from 02:00 to 04:00 starts at 03:00 the night daylight saving starts.
    slots = _peak_slots(4, 5, 6, 7)
    change = next_change(slots, datetime(2026, 9, 27, 1, 15, tzinfo=NZ), NZ)
    assert change is not None
    assert change[0] == datetime(2026, 9, 27, 3, 0, tzinfo=NZ)
    # And ends at 04:00, an hour later rather than two.
    change = next_change(slots, datetime(2026, 9, 27, 3, 0, tzinfo=NZ), NZ)
    assert change is not None
    assert (change[0], change[1].bucket) == (datetime(2026, 9, 27, 4, 0, tzinfo=NZ), "CHARGE_LOW")


def test_no_change_before_the_season_ends() -> None:
    slots = _peak_slots()
    now = datetime(2026, 9, 27, 1, 15, tzinfo=NZ)
    assert next_change(slots, now, NZ) is None
    assert (
        next_change(_peak_slots(40), now, NZ, until=datetime(2026, 9, 27, 12, 0, tzinfo=NZ)) is None
    )


def test_solar_plan_export_prices() -> None:
    # The default fixture's battery plan: Ecotricity's export peak and off-peak, with the
    # network's export rates, all from its own winter charts.
    plan = find_plan(SOLAR_PLAN)
    readings = [
        chart_reading(chart_file(chart_name(plan, WINTER, weekend=w))) for w in (False, True)
    ]
    slots = day_slots(side_rates(plan, WINTER, export=True), *readings, export=True)
    priced = price_side(slots, plan_alignment([slots]))

    for slot in priced.slots.all:
        assert slot.price == pytest.approx(sum(part.price for part in slot.components))
        assert slot.price < 0

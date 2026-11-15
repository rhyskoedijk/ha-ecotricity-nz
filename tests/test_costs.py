"""Tests for pricing usage into hourly costs."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

import pytest

from custom_components.ecotricity_nz.api import IntervalReading, RateUnit
from custom_components.ecotricity_nz.costs import (
    EXPORT_TOTALS,
    IMPORT_TOTALS,
    LINE_ENERGY_SUFFIX,
    TOTAL_ELECTRICITY_COMPENSATION,
    TOTAL_ELECTRICITY_COST,
    CostKeys,
    DayCosts,
    charged_days,
    cost_keys,
    cost_label,
    hourly_costs,
    is_line_energy,
    levy_key,
)
from custom_components.ecotricity_nz.tariff import (
    ComponentKind,
    DailyCharge,
    DaySlots,
    PriceComponent,
    Slot,
    line_key,
    rate_component,
)
from tests.conftest import (
    ANYTIME_PLAN,
    NZ,
    chart_plan,
    explained_chart,
    find_chart_where,
    find_plan,
    find_plan_where,
    has_capacity_charge,
    has_mixed_periods,
)

HALF_HOUR = timedelta(minutes=30)
SUPPLIER = PriceComponent(ComponentKind.SUPPLIER, "Uncontrolled Usage", 20.0, "CHARGE_24")
NETWORK = PriceComponent(ComponentKind.NETWORK, "Uncontrolled Usage", 10.0, "CHARGE_24")
LEVY = PriceComponent(ComponentKind.LEVY, "EA Levy", 0.1)
EXPORT_CREDIT = PriceComponent(ComponentKind.SUPPLIER, "Export", -12.0, "EG_CHARGE_24")
EXPORT_FEE = PriceComponent(ComponentKind.NETWORK, "Export Fee", 1.5, "EG_CHARGE_24")
DAILY = (
    DailyCharge("Daily Network Charges", 96.0, is_network=True),
    DailyCharge("Metering Fee", 48.0, is_network=False),
)


def _line(part: PriceComponent, *, export: bool = False) -> str:
    line = line_key(part, export=export)
    assert line is not None
    return line


SUPPLIER_LINE = _line(SUPPLIER)
NETWORK_LINE = _line(NETWORK)
CREDIT_LINE = _line(EXPORT_CREDIT, export=True)
FEE_LINE = _line(EXPORT_FEE, export=True)
KEYS = CostKeys(
    import_keys=IMPORT_TOTALS
    | frozenset(
        {
            "line_import_ea_levy",
            "line_import_ea_levy_energy",
            "daily_charge_network",
            "daily_charge_metering",
        }
    ),
    export_keys=EXPORT_TOTALS,
    import_lines=frozenset({SUPPLIER_LINE, NETWORK_LINE}),
    export_lines=frozenset({CREDIT_LINE, FEE_LINE}),
)
# The portal's latest data is well after the test days, so every gap counts as settled.
SETTLED = datetime(2000, 1, 1, tzinfo=UTC)


def _all_day(*components: PriceComponent) -> DaySlots:
    slot = Slot(
        price=sum(part.price for part in components), supplier="", network="", components=components
    )
    return DaySlots(weekday=(slot,) * 48, weekend=(slot,) * 48)


def _readings(day: date, kwh: float = 1.0) -> list[IntervalReading]:
    """Return a reading for every half-hour of an NZ day, stepping in UTC across DST changes."""
    start = datetime.combine(day, time.min, NZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, NZ).astimezone(UTC)
    readings = []
    while start < end:
        readings.append(IntervalReading(start, start + HALF_HOUR, kwh))
        start += HALF_HOUR
    return readings


def _costs(
    imported: list[IntervalReading],
    exported: list[IntervalReading] | None = None,
    days: dict[date, DayCosts] | None = None,
    *,
    import_loss_factor: float = 1.0,
    export_loss_factor: float = 1.0,
    settled_before: datetime = SETTLED,
) -> dict[str, dict[datetime, float]]:
    priced = days or {}
    return hourly_costs(
        imported,
        exported or [],
        priced.get,
        KEYS,
        tz=NZ,
        import_loss_factor=import_loss_factor,
        export_loss_factor=export_loss_factor,
        settled_before=settled_before,
    )


DAY = date(2026, 6, 15)
PRICED = {DAY: DayCosts(_all_day(SUPPLIER, NETWORK, LEVY), _all_day(EXPORT_CREDIT), DAILY)}


def test_each_hour_is_split_into_supplier_network_and_levy_costs() -> None:
    costs = _costs(_readings(DAY), days=PRICED)

    hour = datetime.combine(DAY, time(8), NZ).astimezone(UTC)
    # Two half-hours of 1 kWh each, in NZD.
    assert costs[SUPPLIER_LINE][hour] == pytest.approx(2 * SUPPLIER.price / 100)
    assert costs[NETWORK_LINE][hour] == pytest.approx(2 * NETWORK.price / 100)
    assert costs["line_import_ea_levy"][hour] == pytest.approx(2 * LEVY.price / 100)
    assert len(costs[SUPPLIER_LINE]) == 24


def test_levy_is_charged_on_the_kwh_of_the_hours_priced_only() -> None:
    # The next day isn't priced, so its usage isn't charged the levy and isn't counted as the
    # levy's kWh either.
    other = DAY + timedelta(days=1)
    costs = _costs(_readings(DAY) + _readings(other, kwh=5.0), days=PRICED)

    energy = costs["line_import_ea_levy_energy"]
    assert sum(energy.values()) == pytest.approx(48.0)
    assert sum(costs["line_import_ea_levy"].values()) == pytest.approx(48 * LEVY.price / 100)


def test_two_levy_rates_with_one_key_count_the_usage_once() -> None:
    # Both descriptions give the same levy key, so they're one line, charged on the usage once.
    other = PriceComponent(ComponentKind.LEVY, "Electricity Authority Levy", 0.06)
    assert levy_key(other) == levy_key(LEVY)
    priced = {DAY: DayCosts(_all_day(SUPPLIER, LEVY, other), None, ())}
    costs = _costs(_readings(DAY), days=priced)

    key = levy_key(LEVY)
    assert sum(costs[key + LINE_ENERGY_SUFFIX].values()) == pytest.approx(48.0)
    assert sum(costs[key].values()) == pytest.approx(48 * (LEVY.price + other.price) / 100)


def test_consumption_loss_factor_applies_to_supplier_import_only() -> None:
    without = _costs(_readings(DAY), _readings(DAY), PRICED)
    with_losses = _costs(_readings(DAY), _readings(DAY), PRICED, import_loss_factor=1.05)

    hour = datetime.combine(DAY, time(8), NZ).astimezone(UTC)
    assert with_losses[SUPPLIER_LINE][hour] == pytest.approx(without[SUPPLIER_LINE][hour] * 1.05)
    # The losses are charged on top of the usage, which stays as it was.
    energy = SUPPLIER_LINE + LINE_ENERGY_SUFFIX
    assert with_losses[energy][hour] == pytest.approx(without[energy][hour])
    for key in (NETWORK_LINE, "line_import_ea_levy", CREDIT_LINE):
        assert with_losses[key][hour] == pytest.approx(without[key][hour])


def test_generation_loss_factor_applies_to_supplier_export_only() -> None:
    # Like import, Ecotricity's export rates are charged on the export plus losses, and the
    # network's on the export alone.
    priced = {DAY: DayCosts(_all_day(SUPPLIER), _all_day(EXPORT_CREDIT, EXPORT_FEE), ())}
    without = _costs(_readings(DAY), _readings(DAY), priced)
    with_losses = _costs(_readings(DAY), _readings(DAY), priced, export_loss_factor=1.02)

    hour = datetime.combine(DAY, time(8), NZ).astimezone(UTC)
    assert with_losses[CREDIT_LINE][hour] == pytest.approx(without[CREDIT_LINE][hour] * 1.02)
    energy = CREDIT_LINE + LINE_ENERGY_SUFFIX
    assert with_losses[energy][hour] == pytest.approx(without[energy][hour])
    for key in (FEE_LINE, SUPPLIER_LINE, TOTAL_ELECTRICITY_COST):
        assert with_losses[key][hour] == pytest.approx(without[key][hour])
    credited = 2 * (EXPORT_CREDIT.price * 1.02 + EXPORT_FEE.price) / 100
    assert with_losses[TOTAL_ELECTRICITY_COMPENSATION][hour] == pytest.approx(-credited * 1.15)


def test_gst_is_added_to_import_daily_charges_and_export() -> None:
    costs = _costs(_readings(DAY), _readings(DAY), PRICED)

    hour = datetime.combine(DAY, time(8), NZ).astimezone(UTC)
    energy = 2 * (SUPPLIER.price + NETWORK.price + LEVY.price) / 100
    daily = sum(charge.price for charge in DAILY) / 24 / 100
    # Everything charged for using electricity, for the Energy dashboard.
    assert costs[TOTAL_ELECTRICITY_COST][hour] == pytest.approx((energy + daily) * 1.15)
    # Export lines exclude GST, like import's. Bills charge GST on the net total, so it adds to
    # the credit, and the import side's cost is the same with or without export.
    credit = 2 * EXPORT_CREDIT.price / 100
    assert costs[CREDIT_LINE][hour] == pytest.approx(credit)
    assert costs[TOTAL_ELECTRICITY_COMPENSATION][hour] == pytest.approx(-credit * 1.15)
    alone = _costs(_readings(DAY), days=PRICED)
    assert costs[TOTAL_ELECTRICITY_COST] == alone[TOTAL_ELECTRICITY_COST]


def test_lines_levies_and_daily_charges_add_up_to_the_energy_dashboards_totals() -> None:
    # Nothing worked out from the other statistics is stored, so these must give the bill.
    costs = _costs(
        _readings(DAY), _readings(DAY), PRICED, import_loss_factor=1.05, export_loss_factor=1.02
    )

    # Levies, daily charges and lines; a line's or levy's kWh isn't an amount.
    imported = {
        key
        for key in (KEYS.import_keys - IMPORT_TOTALS) | KEYS.import_lines
        if not is_line_energy(key)
    }
    for hour, total in costs[TOTAL_ELECTRICITY_COST].items():
        charged = sum(costs[key][hour] for key in imported)
        assert total == pytest.approx(charged * 1.15)
    for hour, compensation in costs[TOTAL_ELECTRICITY_COMPENSATION].items():
        credited = sum(costs[key][hour] for key in KEYS.export_lines)
        assert compensation == pytest.approx(-credited * 1.15)


def test_export_keeps_its_sign() -> None:
    priced = {DAY: DayCosts(_all_day(SUPPLIER), _all_day(EXPORT_CREDIT, EXPORT_FEE), ())}
    costs = _costs(_readings(DAY), _readings(DAY), priced)

    hour = datetime.combine(DAY, time(8), NZ).astimezone(UTC)
    # A credit is negative, and a network that charges to export is a positive cost.
    assert costs[CREDIT_LINE][hour] == pytest.approx(2 * EXPORT_CREDIT.price / 100)
    assert costs[CREDIT_LINE][hour] < 0
    assert costs[FEE_LINE][hour] == pytest.approx(2 * EXPORT_FEE.price / 100)
    # The Energy dashboard counts money received as positive compensation, GST included.
    assert costs[TOTAL_ELECTRICITY_COMPENSATION][hour] == pytest.approx(
        -2 * (EXPORT_CREDIT.price + EXPORT_FEE.price) * 1.15 / 100
    )


@pytest.mark.parametrize(
    ("day", "hours"),
    [
        pytest.param(date(2026, 9, 27), 23, id="daylight saving starts"),
        pytest.param(date(2026, 4, 5), 25, id="daylight saving ends"),
        pytest.param(DAY, 24, id="ordinary day"),
    ],
)
def test_daily_charges_are_spread_evenly_over_the_hours_of_the_day(day: date, hours: int) -> None:
    priced = {day: DayCosts(_all_day(SUPPLIER), None, DAILY)}
    costs = _costs(_readings(day), days=priced)

    network = costs["daily_charge_network"]
    assert len(network) == hours
    assert len(set(network.values())) == 1
    assert sum(network.values()) == pytest.approx(DAILY[0].price / 100)
    assert sum(costs["daily_charge_metering"].values()) == pytest.approx(DAILY[1].price / 100)


def test_days_without_pricing_are_left_out() -> None:
    other = DAY + timedelta(days=1)
    costs = _costs(_readings(DAY) + _readings(other), days=PRICED)

    assert {hour.astimezone(NZ).date() for hour in costs[TOTAL_ELECTRICITY_COST]} == {DAY}


def test_side_without_pricing_is_left_out() -> None:
    # The day's export prices aren't known, but its import prices are.
    priced = {DAY: DayCosts(_all_day(SUPPLIER), None, DAILY)}
    costs = _costs(_readings(DAY), _readings(DAY), priced)

    assert TOTAL_ELECTRICITY_COST in costs
    assert not (EXPORT_TOTALS | KEYS.export_lines) & costs.keys()


def test_recent_hour_missing_a_half_hour_is_left_out_until_settled() -> None:
    readings = _readings(DAY)[:-1]
    last_hour = datetime.combine(DAY, time(23), NZ).astimezone(UTC)

    recent = _costs(readings, days=PRICED, settled_before=last_hour)
    settled = _costs(readings, days=PRICED, settled_before=last_hour + timedelta(hours=1))

    assert last_hour not in recent[TOTAL_ELECTRICITY_COST]
    assert settled[SUPPLIER_LINE][last_hour] == pytest.approx(SUPPLIER.price / 100)


def test_parts_that_dont_apply_are_zero_in_priced_hours() -> None:
    # The plan has a levy, but today's prices don't include it, so the levy's statistic gets
    # zeros rather than a gap.
    priced = {DAY: DayCosts(_all_day(SUPPLIER, NETWORK), _all_day(EXPORT_CREDIT), ())}
    costs = _costs(_readings(DAY), _readings(DAY), priced)

    assert set(costs["line_import_ea_levy"].values()) == {0.0}
    assert set(costs[FEE_LINE].values()) == {0.0}
    assert len(costs["line_import_ea_levy"]) == 24


def test_price_combines_supplier_and_network_rates_from_different_periods() -> None:
    chart = find_chart_where(has_mixed_periods)
    _plan, day = chart_plan(chart)
    sides = explained_chart(chart)
    slots = {
        export: DaySlots(weekday=explained, weekend=explained)
        for export, (_rates, explained) in sides.items()
    }
    priced = {day: DayCosts(slots[False], slots.get(True), ())}
    readings = _readings(day)
    costs = _costs(readings, days=priced)

    # Each half-hour is charged its own supplier and network rate, whatever their periods.
    expected: dict[datetime, float] = defaultdict(float)
    for reading in readings:
        for part in slots[False].at(reading.start.astimezone(NZ)).components:
            expected[reading.start.replace(minute=0)] += reading.value * part.price / 100
    assert costs[TOTAL_ELECTRICITY_COST] == pytest.approx(
        {hour: cost * 1.15 for hour, cost in expected.items()}
    )


def test_each_rate_is_a_bill_line_charged_on_its_own_usage() -> None:
    # Ecotricity's and the network's periods differ, so a half-hour's usage is on one period's
    # supplier line and another period's network line.
    chart = find_chart_where(has_mixed_periods)
    plan, day = chart_plan(chart)
    explained = explained_chart(chart)[False][1]
    slots = DaySlots(weekday=explained, weekend=explained)
    readings = _readings(day)
    keys = cost_keys(plan)
    costs = hourly_costs(
        readings,
        [],
        {day: DayCosts(slots, None, ())}.get,
        keys,
        tz=NZ,
        import_loss_factor=1.0,
        export_loss_factor=1.0,
        settled_before=SETTLED,
    )

    expected: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    for reading in readings:
        hour = reading.start.replace(minute=0)
        for part in slots.at(reading.start.astimezone(NZ)).components:
            if part.price and (line := line_key(part, export=False)) is not None:
                expected[line][hour] += reading.value * part.price / 100
                expected[line + LINE_ENERGY_SUFFIX][hour] += reading.value
    for key, hours in expected.items():
        assert costs[key] == pytest.approx(dict(hours)), key
    # The lines of each kind charge for all of the usage.
    for kind in (ComponentKind.SUPPLIER, ComponentKind.NETWORK):
        lines = [key for key in keys.import_lines if f"_{kind}_" in key]
        for hour in costs[TOTAL_ELECTRICITY_COST]:
            usage = sum(r.value for r in readings if r.start.replace(minute=0) == hour)
            assert sum(costs[line + LINE_ENERGY_SUFFIX][hour] for line in lines) == pytest.approx(
                usage
            )


def test_two_rates_in_one_bucket_are_one_line_charged_on_the_usage_once() -> None:
    first = PriceComponent(ComponentKind.SUPPLIER, "Usage", 20.0, "CHARGE_24")
    second = PriceComponent(ComponentKind.SUPPLIER, "Usage Adjustment", 5.0, "CHARGE_24")
    line = line_key(first, export=False)
    assert line is not None
    assert line == line_key(second, export=False)
    keys = CostKeys(KEYS.import_keys, frozenset(), import_lines=frozenset({line}))
    costs = hourly_costs(
        _readings(DAY),
        [],
        {DAY: DayCosts(_all_day(first, second), None, ())}.get,
        keys,
        tz=NZ,
        import_loss_factor=1.0,
        export_loss_factor=1.0,
        settled_before=SETTLED,
    )

    hour = datetime.combine(DAY, time(8), NZ).astimezone(UTC)
    assert costs[line][hour] == pytest.approx(2 * 25.0 / 100)
    assert costs[line + LINE_ENERGY_SUFFIX][hour] == pytest.approx(2.0)


def test_levies_and_free_rates_are_not_bill_lines() -> None:
    assert line_key(LEVY, export=False) is None
    plan = find_plan(ANYTIME_PLAN)
    keys = cost_keys(plan)
    lines = keys.import_lines | keys.export_lines
    for rate in plan.rates:
        if rate.unit is not RateUnit.PER_KWH or rate.is_controlled:
            continue
        line = line_key(rate_component(rate), export=rate.is_export)
        assert (line in lines) is bool(rate.price and not rate.is_levy), rate.description
    assert keys.statistic_keys(export=False) >= {
        *keys.import_lines,
        *(line + LINE_ENERGY_SUFFIX for line in keys.import_lines),
    }


def test_cost_keys_come_from_the_plans_rates() -> None:
    plan = find_plan(ANYTIME_PLAN)
    keys = cost_keys(plan)

    for rate in plan.rates:
        if not rate.price or rate.is_controlled or rate.unit is not RateUnit.PER_KWH:
            continue
        part = rate_component(rate)
        if part.kind is ComponentKind.LEVY:
            levy = {levy_key(part), levy_key(part) + LINE_ENERGY_SUFFIX}
            assert (levy <= keys.import_keys) is not rate.is_export
        else:
            assert line_key(part, export=rate.is_export) in keys.import_lines | keys.export_lines
    assert keys.import_keys >= IMPORT_TOTALS
    assert keys.export_lines
    assert keys.export_keys == EXPORT_TOTALS
    assert any(key.startswith("daily_charge_") for key in keys.import_keys)


def test_capacity_charges_have_no_cost_statistic() -> None:
    # Charged per kVA of the connection, which isn't published, so they can't be priced.
    plan = find_plan_where(has_capacity_charge)
    keys = cost_keys(plan)
    capacity = [rate for rate in plan.rates if rate.unit is RateUnit.OTHER and rate.price]
    assert capacity
    for rate in capacity:
        daily = "daily_charge_" + rate.description.lower().replace(" ", "_")
        assert not any(key.startswith(daily) for key in keys.statistic_keys(export=False))


def test_daily_charge_priced_at_nothing_has_no_statistic() -> None:
    free = DailyCharge("Free Admin Fee", 0.0, is_network=False)
    priced = {DAY: DayCosts(_all_day(SUPPLIER), None, (*DAILY, free))}
    costs = _costs(_readings(DAY), days=priced)

    assert "daily_charge_supplier" not in costs
    charged = sum(
        sum(costs[key].values()) for key in ("daily_charge_network", "daily_charge_metering")
    )
    assert charged == pytest.approx(sum(charge.price for charge in DAILY) / 100)


def test_export_without_export_statistics_is_left_out() -> None:
    # Export rates priced at nothing give no export statistics, though the half-hours are priced.
    free = PriceComponent(ComponentKind.SUPPLIER, "Export", 0.0)
    priced = {DAY: DayCosts(_all_day(SUPPLIER), _all_day(free), ())}
    keys = CostKeys(
        import_keys=KEYS.import_keys, export_keys=frozenset(), import_lines=KEYS.import_lines
    )
    costs = hourly_costs(
        _readings(DAY),
        _readings(DAY),
        priced.get,
        keys,
        tz=NZ,
        import_loss_factor=1.0,
        export_loss_factor=1.0,
        settled_before=SETTLED,
    )

    assert not (EXPORT_TOTALS | KEYS.export_lines) & costs.keys()
    assert TOTAL_ELECTRICITY_COST in costs


def test_cost_labels() -> None:
    assert cost_label("line_import_ea_levy") == "line (import ea levy)"
    assert cost_label("daily_charge_metering") == "daily charge (metering)"
    assert cost_label(TOTAL_ELECTRICITY_COST) == "total electricity cost"
    assert cost_label(TOTAL_ELECTRICITY_COMPENSATION) == "total electricity compensation"
    assert cost_label("line_import_network_charge_high") == "line (import network charge high)"
    energy = "line_import_network_charge_high" + LINE_ENERGY_SUFFIX
    assert is_line_energy(energy)
    assert cost_label(energy) == "line (import network charge high) energy"


def test_key_that_isnt_a_cost_statistic_has_no_label() -> None:
    with pytest.raises(ValueError, match="Not a cost statistic key"):
        cost_label("energy_imported")


def _hours(day: date) -> list[datetime]:
    """Return the start of every hour of an NZ day, in UTC."""
    return sorted({reading.start.replace(minute=0) for reading in _readings(day)})


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 6, 15),
        # Daylight saving ends, and the day has 25 hours.
        date(2026, 4, 5),
        # Daylight saving starts, and the day has 23 hours.
        date(2026, 9, 27),
    ],
)
def test_a_whole_day_carries_one_days_charges(day: date) -> None:
    assert charged_days(_hours(day), NZ) == pytest.approx(1)


def test_days_charged_count_only_the_hours_priced() -> None:
    # A range of three days with only the first two priced, and half of the third.
    first = date(2026, 6, 15)
    hours = (
        _hours(first) + _hours(first + timedelta(days=1)) + _hours(first + timedelta(days=2))[:12]
    )
    assert charged_days(hours, NZ) == pytest.approx(2.5)
    assert charged_days([], NZ) == 0

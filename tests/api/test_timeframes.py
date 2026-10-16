"""Tests for reading time-of-use windows from timeframe charts."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from custom_components.ecotricity_nz.api.timeframes import (
    SLOTS_PER_DAY,
    ChartReadError,
    ChartTier,
    chart_path,
    match_tiers,
    needs_season,
    read_chart,
)
from custom_components.ecotricity_nz.tariff import price_day
from tests.conftest import (
    ANYTIME_PLAN,
    SOLAR_PLAN,
    TIME_OF_USE_PLAN,
    chart_file,
    chart_files,
    chart_name,
    chart_plan,
    find_plan,
)

WINTER = date(2026, 6, 15)
SPRING = date(2026, 10, 15)

# Weekday peak on the fixture charts: 7am to 11am and 5pm to 9pm. Other networks differ, which is
# why windows are read from each plan's own charts rather than assumed.
PEAK_SLOTS = frozenset(range(14, 22)) | frozenset(range(34, 42))
ALL_SLOTS = frozenset(range(SLOTS_PER_DAY))


def _chart(plan: str, day: date) -> bytes:
    """Return a default fixture plan's weekday chart, for the season containing a day."""
    return chart_file(chart_name(find_plan(plan), day)).read_bytes()


def _expected(peak: str, off_peak: str) -> tuple[str, ...]:
    return tuple(peak if slot in PEAK_SLOTS else off_peak for slot in range(SLOTS_PER_DAY))


def test_chart_path() -> None:
    template = "/timeframeimages_ecotricity/X-SSSSSSSS-EEEEEEEE-WD.png"
    assert (
        chart_path(template, date(2026, 10, 1), date(2026, 12, 31))
        == "/timeframeimages_ecotricity/X-20261001-20261231-WD-NOGST.png"
    )
    assert chart_path("/a-NOGST.png", date(2026, 1, 1), date(2026, 1, 2)) == "/a-NOGST.png"


def test_needs_season() -> None:
    assert needs_season("/X-SSSSSSSS-EEEEEEEE-WD.png")
    assert not needs_season("/X-20261001-20261231-WD.png")


def test_read_two_tier_import_and_export() -> None:
    reading = read_chart(_chart(SOLAR_PLAN, WINTER))

    assert len(reading.import_tiers) == 2
    assert len(reading.export_tiers) == 2
    peak = max(reading.import_tiers, key=lambda tier: tier.height)
    assert peak.slots == PEAK_SLOTS

    # Legend: import 23.87c off peak, 35.87c peak; export 16c off peak, 23c peak.
    imports = match_tiers(reading.import_tiers, {"CHARGE_LOW": 23.87, "CHARGE_HIGH": 35.87})
    assert imports == _expected("CHARGE_HIGH", "CHARGE_LOW")
    exports = match_tiers(reading.export_tiers, {"EG_CHARGE_LOW": -16.0, "EG_CHARGE_HIGH": -23.0})
    assert exports == _expected("EG_CHARGE_HIGH", "EG_CHARGE_LOW")


def test_read_close_prices() -> None:
    # Legend: 24.04c off peak, 25.35c peak. Only 5% apart, so heights must be precise.
    reading = read_chart(_chart(TIME_OF_USE_PLAN, SPRING))

    imports = match_tiers(reading.import_tiers, {"CHARGE_LOW": 24.04, "CHARGE_HIGH": 25.35})
    assert imports == _expected("CHARGE_HIGH", "CHARGE_LOW")
    [export] = reading.export_tiers
    assert export.slots == ALL_SLOTS


def test_read_flat_chart_with_unused_bucket() -> None:
    # Anytime plan outside winter: one tier, even though the plan also has a peak price.
    reading = read_chart(_chart(ANYTIME_PLAN, SPRING))

    [tier] = reading.import_tiers
    assert tier.slots == ALL_SLOTS
    import_prices = {"CHARGE_HIGH": 30.0, "CHARGE_24": 24.57}
    # One tier fits either price, so the export bars on the same axis (legend -13.74c) settle it.
    with pytest.raises(ChartReadError, match="more than one"):
        match_tiers(reading.import_tiers, import_prices)
    assert match_tiers(
        reading.import_tiers,
        import_prices,
        other_tiers=reading.export_tiers,
        other_prices={"EG_CHARGE_24": -13.74},
    ) == (("CHARGE_24",) * SLOTS_PER_DAY)


def _shared_shade(chart: Path) -> tuple[ChartTier, ChartTier] | None:
    tiers = read_chart(chart.read_bytes()).import_tiers
    return next(
        (
            (first, second)
            for index, first in enumerate(tiers)
            for second in tiers[index + 1 :]
            if first.colour == second.colour
        ),
        None,
    )


def test_read_shade_shared_by_two_prices() -> None:
    # Some charts draw two close prices, such as off-peak and super off-peak, in the same shade,
    # so only their heights tell them apart.
    chart, pair = next(
        ((chart, pair) for chart in chart_files() if (pair := _shared_shade(chart))), (None, None)
    )
    assert chart is not None, "No fixture chart draws two prices in one shade"
    assert pair is not None
    first, second = pair

    # They're separate tiers, of different heights, for different half-hours.
    assert first.height != second.height
    assert not first.slots & second.slots
    # Matched against the plan's prices, they're different prices.
    plan, day = chart_plan(chart)
    pricing = price_day(plan, day)
    reading = read_chart(chart.read_bytes())
    slots = match_tiers(
        reading.import_tiers,
        pricing.import_buckets,
        other_tiers=reading.export_tiers,
        other_prices=pricing.export_buckets,
    )
    assert slots[min(first.slots)] != slots[min(second.slots)]


def test_single_tier_is_not_assumed_cheapest() -> None:
    # 30c, 22c and 15c all fit one tier on their own; the export side pins the scale to 10px/c.
    tier = ChartTier(colour=(0, 0, 255), height=300, slots=frozenset(range(SLOTS_PER_DAY)))
    export = ChartTier(colour=(255, 128, 0), height=100, slots=frozenset(range(SLOTS_PER_DAY)))
    prices = {"CHARGE_LOW": 15.0, "CHARGE_3": 22.0, "CHARGE_HIGH": 30.0}

    with pytest.raises(ChartReadError, match="more than one"):
        match_tiers((tier,), prices)
    assert match_tiers(
        (tier,), prices, other_tiers=(export,), other_prices={"EG_CHARGE_24": -10.0}
    ) == (("CHARGE_HIGH",) * SLOTS_PER_DAY)


def test_ambiguous_ratio_rejected() -> None:
    # Heights 2:1 against 40c, 20c and 10c fit peak and shoulder as well as shoulder and off-peak.
    tall = ChartTier(colour=(0, 0, 255), height=200, slots=frozenset(range(24)))
    short = ChartTier(colour=(0, 0, 128), height=100, slots=frozenset(range(24, SLOTS_PER_DAY)))
    prices = {"CHARGE_LOW": 10.0, "CHARGE_3": 20.0, "CHARGE_HIGH": 40.0}
    export = ChartTier(colour=(255, 128, 0), height=50, slots=frozenset(range(SLOTS_PER_DAY)))

    with pytest.raises(ChartReadError, match="more than one"):
        match_tiers((tall, short), prices)
    # An export side that fits both scales doesn't help.
    with pytest.raises(ChartReadError, match="more than one"):
        match_tiers(
            (tall, short),
            prices,
            other_tiers=(export,),
            other_prices={"EG_CHARGE_LOW": -5.0, "EG_CHARGE_HIGH": -10.0},
        )
    # Prices without export bars don't either.
    with pytest.raises(ChartReadError, match="more than one"):
        match_tiers((tall, short), prices, other_prices={"EG_CHARGE_24": -5.0})
    assert (
        match_tiers(
            (tall, short), prices, other_tiers=(export,), other_prices={"EG_CHARGE_24": -5.0}
        )
        == ("CHARGE_3",) * 24 + ("CHARGE_LOW",) * 24
    )


def test_near_zero_price_doesnt_fit_every_tier() -> None:
    # A price close to zero, such as a 0c network rate plus the levy, can't anchor the scale: it
    # would make the scale hundreds of pixels per cent, where every bar is within a fraction of a
    # cent of that price. Bars of clearly different heights must not all read as one price.
    tiers = (
        ChartTier(colour=(180, 218, 252), height=100.0, slots=frozenset(range(24))),
        ChartTier(colour=(4, 78, 140), height=187.0, slots=frozenset(range(24, SLOTS_PER_DAY))),
    )

    with pytest.raises(ChartReadError, match="don't match"):
        match_tiers(tiers, {"CHARGE_4": 0.2, "CHARGE_HIGH": 50.0})
    # The same bars fit sensible prices at a sensible scale.
    assert match_tiers(tiers, {"CHARGE_LOW": 18.2, "CHARGE_HIGH": 34.0}) == (
        ("CHARGE_LOW",) * 24 + ("CHARGE_HIGH",) * 24
    )


_LOW = (180, 218, 252)
_HIGH = (4, 78, 140)
_EXPORT_LOW = (252, 210, 116)
_EXPORT_HIGH = (228, 94, 52)


def _two_tiers(colours: tuple, heights: tuple[float, float]) -> tuple[ChartTier, ...]:
    """Two tiers: the first half of the day, then the second."""
    return (
        ChartTier(colour=colours[0], height=heights[0], slots=frozenset(range(24))),
        ChartTier(colour=colours[1], height=heights[1], slots=frozenset(range(24, SLOTS_PER_DAY))),
    )


def test_prices_wrong_on_one_side_are_rejected() -> None:
    # Import prices that are all short by the same amount (a charge left out, say) can still fit
    # the import bars on their own, at a different scale. Export bars share the axis, so they
    # show the scale is wrong.
    imports = _two_tiers((_LOW, _HIGH), (222.0, 233.0))
    exports = (ChartTier(colour=_EXPORT_LOW, height=113.0, slots=ALL_SLOTS),)
    export_prices = {"EG_CHARGE_LOW": -13.74}

    short = {"CHARGE_LOW": 24.01, "CHARGE_HIGH": 25.39}
    assert match_tiers(imports, short)
    with pytest.raises(ChartReadError, match="one scale"):
        match_tiers(imports, short, other_tiers=exports, other_prices=export_prices)

    full = {"CHARGE_LOW": 27.03, "CHARGE_HIGH": 28.41}
    assert match_tiers(imports, full, other_tiers=exports, other_prices=export_prices) == (
        ("CHARGE_LOW",) * 24 + ("CHARGE_HIGH",) * 24
    )


def test_unreadable_other_side_doesnt_reject_this_side() -> None:
    # Export bars with a price the plan doesn't have can't confirm the scale, but they mustn't
    # stop the import bars, which fit on their own, from being read.
    imports = _two_tiers((_LOW, _HIGH), (136.0, 191.0))
    exports = (
        ChartTier(colour=_EXPORT_LOW, height=84.0, slots=frozenset(range(14))),
        ChartTier(colour=_EXPORT_HIGH, height=142.0, slots=frozenset(range(14, 42))),
        ChartTier(colour=(244, 158, 60), height=112.0, slots=frozenset(range(42, SLOTS_PER_DAY))),
    )
    export_prices = {"EG_CHARGE_LOW": -16.0, "EG_CHARGE_HIGH": -27.0}

    with pytest.raises(ChartReadError):
        match_tiers(exports, export_prices)
    assert match_tiers(
        imports,
        {"CHARGE_LOW": 25.79, "CHARGE_HIGH": 36.26},
        other_tiers=exports,
        other_prices=export_prices,
    ) == (("CHARGE_LOW",) * 24 + ("CHARGE_HIGH",) * 24)


def test_mismatched_prices_rejected() -> None:
    reading = read_chart(_chart(SOLAR_PLAN, WINTER))

    with pytest.raises(ChartReadError):
        match_tiers(reading.import_tiers, {"CHARGE_LOW": 10.0, "CHARGE_HIGH": 50.0})
    with pytest.raises(ChartReadError):
        match_tiers(reading.import_tiers, {"CHARGE_LOW": 0.0})
    with pytest.raises(ChartReadError):
        match_tiers((), {"CHARGE_LOW": 1.0})


def test_partial_tiers_rejected() -> None:
    tier = ChartTier(colour=(0, 0, 255), height=100, slots=frozenset({0, 1}))
    with pytest.raises(ChartReadError):
        match_tiers((tier,), {"CHARGE_24": 20.0})


def test_unreadable_images_rejected() -> None:
    with pytest.raises(ChartReadError):
        read_chart(b"not an image")

    blank = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(blank, format="PNG")
    with pytest.raises(ChartReadError):
        read_chart(blank.getvalue())


# Ecotricity's chart palette, read from a chart legend: light to dark.
IMPORT_SHADES = [
    (179, 217, 255),
    (153, 204, 255),
    (102, 178, 255),
    (76, 153, 204),
    (51, 136, 204),
    (0, 102, 161),
    (0, 79, 136),
]
EXPORT_SHADES = [
    (249, 210, 114),
    (247, 199, 92),
    (248, 184, 74),
    (246, 156, 61),
    (248, 124, 60),
    (225, 95, 50),
]


def _draw_chart(
    imports: list[tuple[tuple[int, int, int], int]],
    export: tuple,
    labels: list[int] | None = None,
) -> bytes:
    """Draw a chart in Ecotricity's layout: 48 bars, import above the axis and export below.

    With `labels`, also draw the y-axis: a grey line with a dark label centred on each given y.
    """
    image = Image.new("RGB", (924, 682), "white")
    draw = ImageDraw.Draw(image)
    zero = 344
    if labels:
        draw.line((61, min(labels), 61, max(labels)), fill=(204, 204, 204))
        for y in labels:
            draw.rectangle((40, y - 4, 55, y + 4), fill=(100, 100, 100))
    for slot, (colour, height) in enumerate(imports):
        left = 64 + slot * 17
        draw.rectangle((left, zero - height, left + 11, zero - 1), fill=colour)
        draw.rectangle((left, zero, left + 11, zero + export[1]), fill=export[0])
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_three_tier_chart_with_shoulder() -> None:
    # Off-peak 20c, shoulder 26c and peak 32c, drawn in the two closest import shades plus dark,
    # at 8px per cent. Shoulder runs 5-7am and 11am-5pm, as on some networks.
    off_peak, shoulder, peak = (
        (IMPORT_SHADES[0], 160),
        (IMPORT_SHADES[1], 208),
        (IMPORT_SHADES[6], 256),
    )
    day = (
        [off_peak] * 10
        + [shoulder] * 4
        + [peak] * 8
        + [shoulder] * 12
        + [peak] * 10
        + [off_peak] * 4
    )
    reading = read_chart(_draw_chart(day, (EXPORT_SHADES[0], 100)))

    assert len(reading.import_tiers) == 3
    slots = match_tiers(
        reading.import_tiers, {"CHARGE_LOW": 20.0, "CHARGE_3": 26.0, "CHARGE_HIGH": 32.0}
    )
    expected = (
        ["CHARGE_LOW"] * 10
        + ["CHARGE_3"] * 4
        + ["CHARGE_HIGH"] * 8
        + ["CHARGE_3"] * 12
        + ["CHARGE_HIGH"] * 10
        + ["CHARGE_LOW"] * 4
    )
    assert slots == tuple(expected)


@pytest.mark.parametrize(("gap", "tiers"), [(0, 1), (2, 1), (3, 2)])
def test_one_shade_splits_by_height(gap: int, tiers: int) -> None:
    # Bars of one price measure the same height, so only a gap over 2px means a second price.
    day = [(IMPORT_SHADES[0], 160)] * 24 + [(IMPORT_SHADES[0], 160 + gap)] * 24
    reading = read_chart(_draw_chart(day, (EXPORT_SHADES[0], 100)))

    assert len(reading.import_tiers) == tiers


def test_closest_export_shades_stay_separate() -> None:
    # The two closest shades in the whole palette.
    first, second = EXPORT_SHADES[1], EXPORT_SHADES[2]
    image = Image.new("RGB", (924, 682), "white")
    draw = ImageDraw.Draw(image)
    for slot in range(SLOTS_PER_DAY):
        left = 64 + slot * 17
        draw.rectangle((left, 244, left + 11, 343), fill=IMPORT_SHADES[2])
        colour, height = (first, 80) if slot < 24 else (second, 120)
        draw.rectangle((left, 344, left + 11, 344 + height), fill=colour)
    out = io.BytesIO()
    image.save(out, format="PNG")

    reading = read_chart(out.getvalue())

    assert len(reading.export_tiers) == 2
    assert match_tiers(reading.export_tiers, {"EG_CHARGE_LOW": -10.0, "EG_CHARGE_HIGH": -15.0}) == (
        ("EG_CHARGE_LOW",) * 24 + ("EG_CHARGE_HIGH",) * 24
    )


@pytest.mark.parametrize(
    ("labels", "spacing"),
    [
        ([98 + 41 * tick for tick in range(10)], 41.0),
        (None, None),
        # Two labels aren't enough to trust, and uneven ones aren't an axis.
        ([200, 300], None),
        ([100, 141, 182, 260, 300], None),
    ],
)
def test_axis_tick_spacing(labels: list[int] | None, spacing: float | None) -> None:
    day = [(IMPORT_SHADES[0], 160)] * SLOTS_PER_DAY
    reading = read_chart(_draw_chart(day, (EXPORT_SHADES[0], 100), labels))

    assert reading.tick_spacing == spacing


def test_axis_step_settles_prices_in_the_same_ratio() -> None:
    # One price on each side, and import and export prices in the same ratio (20c:30c and
    # -10c:-15c), so the bars fit off-peak at 10px per cent and peak at 6.67px per cent equally
    # well. Only the axis tells them apart: its step is a round 5c at one scale, 7.5c at the other.
    imports = (ChartTier(colour=(0, 0, 255), height=200, slots=ALL_SLOTS),)
    exports = (ChartTier(colour=(255, 128, 0), height=100, slots=ALL_SLOTS),)
    import_prices = {"CHARGE_LOW": 20.0, "CHARGE_HIGH": 30.0}
    export_prices = {"EG_CHARGE_LOW": -10.0, "EG_CHARGE_HIGH": -15.0}

    def read(tick_spacing: float | None) -> tuple[str, ...]:
        return match_tiers(
            imports,
            import_prices,
            other_tiers=exports,
            other_prices=export_prices,
            tick_spacing=tick_spacing,
        )

    with pytest.raises(ChartReadError, match="more than one"):
        read(None)
    assert read(50) == ("CHARGE_LOW",) * SLOTS_PER_DAY
    # Labels 200/3 pixels apart make the step 10c at the other scale instead.
    assert read(200 / 3) == ("CHARGE_HIGH",) * SLOTS_PER_DAY
    # An axis that fits neither scale can't settle it.
    with pytest.raises(ChartReadError, match="more than one"):
        read(43)

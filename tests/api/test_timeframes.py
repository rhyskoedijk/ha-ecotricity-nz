"""Tests for reading time-of-use windows from timeframe charts."""

from __future__ import annotations

import io
from datetime import date

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
from tests.conftest import chart_file

PREFIX = "CKHK-RTOU24-"
SUFFIX = "-WD-NOGST.png"
WINTER = "RESIDENTIAL-20260401-20260930"
SPRING = "RESIDENTIAL-20261001-20261231"

# Weekday peak on the fixture charts: 7am to 11am and 5pm to 9pm. Other networks differ, which is
# why windows are read from each plan's own charts rather than assumed.
PEAK_SLOTS = frozenset(range(14, 22)) | frozenset(range(34, 42))
ALL_SLOTS = frozenset(range(SLOTS_PER_DAY))


def _chart(plan: str, season: str) -> bytes:
    return chart_file(f"{PREFIX}{plan}_260401_T0-{season}{SUFFIX}").read_bytes()


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
    reading = read_chart(_chart("BATT_SPECIAL_LOW", WINTER))

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
    reading = read_chart(_chart("CKHK_OFPK_LOW", SPRING))

    imports = match_tiers(reading.import_tiers, {"CHARGE_LOW": 24.04, "CHARGE_HIGH": 25.35})
    assert imports == _expected("CHARGE_HIGH", "CHARGE_LOW")
    [export] = reading.export_tiers
    assert export.slots == ALL_SLOTS


def test_read_flat_chart_with_unused_bucket() -> None:
    # Anytime plan outside winter: one tier, even though the plan also has a peak price.
    reading = read_chart(_chart("CKHK_ANY_LOW", SPRING))

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


def test_read_shade_shared_by_two_prices() -> None:
    # "off-peak" and "super off-peak" price for part of the day are drawn in the same shade, so
    # only their heights tell them apart. Matching against the plan's prices is in test_tariff.py.
    chart = "ORON-URES-BATT_SPECIAL_LOW_260401_T0-RESIDENTIAL-20260901-20270331-WD-NOGST.png"
    reading = read_chart(chart_file(chart).read_bytes())

    assert len(reading.import_tiers) == 4
    cheaper, low = sorted(reading.import_tiers, key=lambda tier: tier.height)[:2]
    assert low.colour == cheaper.colour
    assert cheaper.slots == frozenset(range(6, 10))
    assert low.slots == frozenset(range(6)) | frozenset(range(44, SLOTS_PER_DAY))
    assert low.height / cheaper.height == pytest.approx(19.029 / 18.2, abs=0.01)


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


def test_mismatched_prices_rejected() -> None:
    reading = read_chart(_chart("BATT_SPECIAL_LOW", WINTER))

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


def _draw_chart(imports: list[tuple[tuple[int, int, int], int]], export: tuple) -> bytes:
    """Draw a chart in Ecotricity's layout: 48 bars, import above the axis and export below."""
    image = Image.new("RGB", (924, 682), "white")
    draw = ImageDraw.Draw(image)
    zero = 344
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

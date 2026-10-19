"""Tests for reading time-of-use windows and prices from timeframe chart legends."""

from __future__ import annotations

import io
from datetime import date

import pytest
from PIL import Image, ImageDraw

from custom_components.ecotricity_nz.api.glyphs import GLYPHS
from custom_components.ecotricity_nz.api.timeframes import (
    MINUTES_PER_DAY,
    ChartReadError,
    LegendEntry,
    chart_path,
    glyph_distance,
    needs_season,
    read_chart,
    shown_price,
)
from tests.conftest import (
    TIME_OF_USE_PLAN,
    chart_file,
    chart_name,
    explained_chart,
    find_chart_where,
    find_plan,
    has_clipped_price,
    has_clipped_price_needing_its_bar,
)

WINTER = date(2026, 6, 15)
# The legend starts below the chart's x-axis labels.
LEGEND_TOP = 520


def _chart(*, weekend: bool = False) -> bytes:
    """Return the default time-of-use plan's winter chart."""
    return chart_file(chart_name(find_plan(TIME_OF_USE_PLAN), WINTER, weekend=weekend)).read_bytes()


def _edited(data: bytes, edit: object) -> bytes:
    """Return a chart with an edit applied to its pixels."""
    with Image.open(io.BytesIO(data)) as raw:
        image = raw.convert("RGB")
    edit(image)  # type: ignore[operator]
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


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


def test_legend_is_read_as_the_website_shows_it() -> None:
    # The default fixture's Low Off Peak weekday chart, as its legend reads on the site.
    reading = read_chart(_chart())

    assert [(entry.start, entry.end, entry.text) for entry in reading.import_entries] == [
        (0, 420, "24.04"),
        (420, 660, "37.35"),
        (660, 1020, "24.04"),
        (1020, 1260, "37.35"),
        (1260, MINUTES_PER_DAY, "24.04"),
    ]
    assert [entry.text for entry in reading.export_entries] == [
        "-13.74",
        "-15.74",
        "-13.74",
        "-15.74",
        "-13.74",
    ]
    assert reading.import_entries[1].slots == range(14, 22)
    assert reading.import_entries[1].price == pytest.approx(37.35)


def test_one_window_covers_the_whole_day() -> None:
    reading = read_chart(_chart(weekend=True))

    assert [(entry.start, entry.end) for entry in reading.import_entries] == [(0, MINUTES_PER_DAY)]


def test_small_rendering_changes_still_read() -> None:
    # Lighter text, as a slightly different font weight or anti-aliasing would draw it.
    def lighten(image: Image.Image) -> None:
        pixels = image.load()
        assert pixels is not None
        for y in range(LEGEND_TOP, image.height):
            for x in range(image.width):
                red, green, blue = pixels[x, y]  # type: ignore[misc]
                pixels[x, y] = tuple(
                    255 - (255 - value) * 85 // 100 for value in (red, green, blue)
                )

    assert read_chart(_edited(_chart(), lighten)) == read_chart(_chart())


def test_clipped_price_keeps_its_digits_and_an_estimate() -> None:
    # Long export lines on battery plans run off the image, cutting the last price short. The
    # digits that fit are kept, with an estimate of the price from its bar's height.
    chart = find_chart_where(has_clipped_price)
    reading = read_chart(chart.read_bytes())
    clipped = [
        entry for entry in (*reading.import_entries, *reading.export_entries) if entry.clipped
    ]
    sides = explained_chart(chart)

    assert clipped
    for entry in clipped:
        assert entry.price is None
        assert entry.estimate is not None
        export = entry in reading.export_entries
        price = sides[export][1][entry.slots[0]].price
        assert shown_price(price).startswith(entry.text)
        assert entry.estimate == pytest.approx(price, abs=0.5)


def test_clipped_price_without_bars_has_no_estimate() -> None:
    chart = find_chart_where(has_clipped_price_needing_its_bar)

    def remove_bars(image: Image.Image) -> None:
        ImageDraw.Draw(image).rectangle((0, 40, image.width, LEGEND_TOP - 50), fill="white")

    reading = read_chart(_edited(chart.read_bytes(), remove_bars))
    assert all(entry.estimate is None for entry in reading.export_entries if entry.clipped)


@pytest.mark.parametrize(
    ("cents", "shown"),
    [
        # Rounded with GST to the cent, then converted back: 30.5532c shows as 30.56c, not 30.55c.
        (30.5532, "30.56"),
        (-21.0, "-21.00"),
        (24.0412, "24.04"),
    ],
)
def test_shown_price_rounds_like_the_legend(cents: float, shown: str) -> None:
    assert shown_price(cents) == shown


def test_glyph_templates_are_far_apart() -> None:
    templates = {
        char: tuple(tuple(int(value, 16) * 16 for value in row) for row in rows)
        for char, rows in GLYPHS.items()
    }
    for char, glyph in templates.items():
        assert glyph_distance(glyph, glyph) == 0
        for other, template in templates.items():
            if other != char:
                assert glyph_distance(glyph, template) > 0.3, (char, other)


def test_unreadable_images_rejected() -> None:
    with pytest.raises(ChartReadError):
        read_chart(b"not an image")

    blank = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(blank, format="PNG")
    with pytest.raises(ChartReadError, match="no legend"):
        read_chart(blank.getvalue())


def test_unknown_character_rejected() -> None:
    # A blot in the first price, where a digit should be.
    def blot(image: Image.Image) -> None:
        ImageDraw.Draw(image).rectangle((150, 556, 156, 568), fill="black")

    with pytest.raises(ChartReadError, match="can't be read"):
        read_chart(_edited(_chart(), blot))


def test_missing_window_rejected() -> None:
    # The legend's second line is gone, leaving a gap in the day.
    def erase_line(image: Image.Image) -> None:
        ImageDraw.Draw(image).rectangle((0, 578, image.width, 596), fill="white")

    with pytest.raises(ChartReadError, match="cover the day"):
        read_chart(_edited(_chart(), erase_line))


def test_unexpected_text_rejected() -> None:
    # A stray character between two windows on a line.
    def stray(image: Image.Image) -> None:
        legend = image.crop((10, 555, 20, 571))
        image.paste(legend, (230, 555))

    with pytest.raises(ChartReadError, match="unexpected text"):
        read_chart(_edited(_chart(), stray))


def test_legend_without_import_windows_rejected() -> None:
    # Only the headings are left.
    def erase_windows(image: Image.Image) -> None:
        ImageDraw.Draw(image).rectangle((0, 550, image.width, image.height), fill="white")

    with pytest.raises(ChartReadError, match="no import windows"):
        read_chart(_edited(_chart(), erase_windows))


def test_entry_slots() -> None:
    entry = LegendEntry(start=420, end=570, text="-28.34")
    assert entry.slots == range(14, 19)
    assert LegendEntry(start=0, end=30, text="-2", clipped=True).price is None

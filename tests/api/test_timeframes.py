"""Tests for reading time-of-use windows and prices from timeframe chart legends."""

from __future__ import annotations

import io
from datetime import date

import pytest
from PIL import Image, ImageDraw

from custom_components.ecotricity_nz.api import timeframes
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
    chart_files,
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
    edit(image)  # type: ignore[operator]  # each edit takes the image
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
                red, green, blue = pixels[x, y]  # type: ignore[misc]  # an RGB pixel is a 3-tuple
                pixels[x, y] = tuple(
                    255 - (255 - value) * 85 // 100 for value in (red, green, blue)
                )

    assert read_chart(_edited(_chart(), lighten)) == read_chart(_chart())


@pytest.mark.review("TEST-10")
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


def test_clipped_price_with_unmeasured_bars_has_no_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pale or patterned bars can leave no coloured pixels in the column measured. They give no
    # scale to estimate from, which must not stop the rest of the chart being read.
    chart = find_chart_where(has_clipped_price)
    monkeypatch.setattr(
        timeframes, "_bar_heights", lambda _image: [(0, 0)] * (MINUTES_PER_DAY // 30)
    )

    reading = read_chart(chart.read_bytes())

    entries = (*reading.import_entries, *reading.export_entries)
    assert any(entry.clipped for entry in entries)
    assert all(entry.estimate is None for entry in entries)


@pytest.mark.parametrize(
    ("text", "export", "sign"),
    [
        # The printed digits give the sign: an export price can be a cost, and an import price
        # a credit.
        ("2", True, 1),
        ("-2", False, -1),
        ("-2", True, -1),
        # With none printed, the column's usual sign.
        ("", True, -1),
        ("", False, 1),
    ],
)
def test_clipped_price_estimate_keeps_the_sign_it_was_printed_with(
    monkeypatch: pytest.MonkeyPatch, text: str, *, export: bool, sign: int
) -> None:
    # Bars are drawn by size, so every bar here is 10 pixels per cent.
    half_day = MINUTES_PER_DAY // 2
    slots = MINUTES_PER_DAY // 30
    monkeypatch.setattr(
        timeframes,
        "_bar_heights",
        lambda _image: [(200, 100)] * (slots // 2) + [(25, 25)] * (slots // 2),
    )
    imports = [LegendEntry(0, half_day, "20.00")]
    exports = [LegendEntry(0, half_day, "-10.00")]
    clipped = LegendEntry(half_day, MINUTES_PER_DAY, text, clipped=True)
    (imports if not export else exports).append(clipped)

    imports, exports = timeframes._estimate_clipped(Image.new("RGB", (1, 1)), imports, exports)

    estimate = (exports if export else imports)[-1].estimate
    assert estimate == pytest.approx(sign * 2.5)


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


@pytest.mark.review("CHART-03")
def test_every_legend_character_is_well_inside_the_matching_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A character near the limit, or nearly as close to another template, would misread after a
    # small rendering change. Keep a quarter of the limit clear on both sides. Only characters
    # that match count: the reader also tries the text above the legend, and skips what it
    # can't read there.
    glyphs: set[timeframes._Glyph] = set()
    match = timeframes._match_glyph

    def recording_match(glyph: timeframes._Glyph) -> str:
        char = match(glyph)
        glyphs.add(glyph)
        return char

    monkeypatch.setattr(timeframes, "_match_glyph", recording_match)
    for chart in chart_files():
        read_chart(chart.read_bytes())

    limit = timeframes._MAX_GLYPH_DISTANCE
    assert glyphs
    for glyph in glyphs:
        nearest, second = sorted(
            glyph_distance(glyph, template) for template in timeframes._TEMPLATES.values()
        )[:2]
        assert nearest < limit * 0.75, nearest
        assert second > limit * 1.25, second


@pytest.mark.review("TEST-10")
def test_unreadable_images_rejected() -> None:
    with pytest.raises(ChartReadError):
        read_chart(b"not an image")

    blank = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(blank, format="PNG")
    with pytest.raises(ChartReadError, match="no legend"):
        read_chart(blank.getvalue())


def _png(image: Image.Image) -> bytes:
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


def _ink_below_its_line_top() -> bytes:
    """A mark whose ink starts further below its line's top than a character is tall."""
    image = Image.new("RGB", (200, 60), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 30, 13, 49), fill="black")
    draw.rectangle((100, 5, 103, 49), fill="black")
    return _png(image)


@pytest.mark.review("CHART-02")
@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(_png(Image.new("RGB", (40, 40), "white"))[:60], id="truncated"),
        pytest.param(_png(Image.new("P", (40, 40), 0)), id="palette"),
        pytest.param(_png(Image.new("L", (40, 40), 0)), id="greyscale_all_ink"),
        pytest.param(_ink_below_its_line_top(), id="ink_below_its_line_top"),
    ],
)
def test_any_unreadable_image_raises_chart_read_error(data: bytes) -> None:
    # Anything else would escape the coordinator, which only expects the API's own errors, and
    # make every pricing sensor unavailable instead of keeping the saved reading.
    with pytest.raises(ChartReadError):
        read_chart(data)


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


def test_unreadable_legend_heading_rejected() -> None:
    # Only the export column's first letter is left, which gives no character width.
    def erase_heading(image: Image.Image) -> None:
        ImageDraw.Draw(image).rectangle((482, 528, image.width, 546), fill="white")

    with pytest.raises(ChartReadError, match="headings can't be read"):
        read_chart(_edited(_chart(), erase_heading))


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


def test_image_too_large_to_be_a_chart_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pillow refuses a decompression bomb before decoding it; that's an unreadable chart too.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    image = io.BytesIO()
    Image.new("L", (100, 100), 255).save(image, format="PNG")

    with pytest.raises(ChartReadError, match="not a readable image"):
        read_chart(image.getvalue())


def test_blank_glyphs_are_as_different_as_can_be() -> None:
    assert glyph_distance(((0, 0),), ((0, 0),)) == 1.0


def test_one_cell_line_keeps_its_pitch() -> None:
    runs = [(0, 4), (1, 3)]
    assert timeframes._line_cells(runs, 2, 10) == {0: runs}


def test_pitch_that_doesnt_fit_the_line_is_kept() -> None:
    # A refit more than a tenth away from the heading's pitch is a misread, so it isn't used.
    runs = [(0, 0), (0, 0), (34, 34)]
    assert timeframes._line_cells(runs, 0, 10) == {0: runs[:2], 3: runs[2:]}


def test_text_above_the_legend_is_skipped() -> None:
    # A line of digits above the headings, such as an axis label, isn't the legend.
    def label(image: Image.Image) -> None:
        digits = image.crop((10, 555, 60, 571))
        ImageDraw.Draw(image).rectangle((0, 0, image.width, 30), fill="white")
        image.paste(digits, (10, 5))

    expected = read_chart(_chart())
    assert read_chart(_edited(_chart(), label)) == expected


def test_ink_before_a_columns_first_cell_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ink the cell fit places before a column's first character is left out of its text.
    expected = read_chart(_chart())
    line_cells = timeframes._line_cells

    def with_stray(
        runs: list[tuple[int, int]], origin: float, pitch: float
    ) -> dict[int, list[tuple[int, int]]]:
        cells = line_cells(runs, origin, pitch)
        return {-1: cells[min(cells)], **cells}

    monkeypatch.setattr(timeframes, "_line_cells", with_stray)
    assert read_chart(_chart()) == expected


def test_text_before_a_window_rejected() -> None:
    with pytest.raises(ChartReadError, match="unexpected text"):
        timeframes._entries(["x 12am - 12am : 21.00c"])


def test_chart_without_bars_has_no_bar_heights() -> None:
    assert timeframes._bar_heights(Image.new("RGB", (100, 100), "white")) is None

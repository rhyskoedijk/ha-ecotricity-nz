"""Read time-of-use windows and prices from the legend of Ecotricity's plan timeframe charts.

Ecotricity publishes the hours each rate applies only as a server-rendered PNG per plan, season
and day type (weekday or weekend). No API, page data or public dataset carries them (see
docs/api.md). Under the bars, each chart has a legend in a monospace font listing every import
and export window with its price, for example "7am - 11am : -21.00c": cents excluding GST, as the
GST-inclusive price rounded to the cent and converted back (see `shown_price`).

This module reads that legend by matching each character against the templates in `glyphs.py`.
A window with a price that runs off the right edge of the image (long battery-plan export lines)
keeps the digits that fit, with an estimate of the price from its bar's height, so the plan's
rates can settle which price it is. Turning windows into rates happens in `tariff.py`.

Pillow is a Home Assistant core dependency, so no extra requirement is needed.
"""

from __future__ import annotations

import io
import re
import statistics
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from itertools import pairwise
from typing import TYPE_CHECKING

from PIL import Image, UnidentifiedImageError

from .exceptions import EcotricityResponseError
from .glyphs import GLYPHS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from .models import Season

SLOTS_PER_DAY = 48
SLOT_MINUTES = 30
MINUTES_PER_DAY = 24 * 60
_START_PLACEHOLDER = "SSSSSSSS"
_END_PLACEHOLDER = "EEEEEEEE"
_NO_GST_SUFFIX = "-NOGST.png"
_GST = Decimal("1.15")
_CENT = Decimal("0.01")

# Legend text is dark grey to black on white. Lighter pixels are anti-aliasing at glyph edges.
_INK = 200
# Rows of one text line are at most this far apart; lines are about 10 pixels apart.
_MAX_LINE_GAP = 2
_GLYPH_ROWS = 16
# Characters are matched to the nearest template. On the fixture charts every character is within
# 0.11 of its own template and at least 0.39 from any other, so this accepts small rendering
# changes but never a different character.
_MAX_GLYPH_DISTANCE = 0.2
# The import and export columns are much further apart than words within a column.
_COLUMN_GAP_WIDTHS = 4
# Each column's heading ("Import rate", "Export rate") is this many characters, which gives the
# width of one character from the centres of its first and last.
_HEADING_LENGTH = 11
_HEADINGS = {"I": False, "E": True}
_TIME = r"(\d{1,2})(?::(\d\d))?([ap]m)"
_ENTRY = re.compile(rf"{_TIME} *- *{_TIME} *: *(-?\d+\.\d\d)c")
# A price cut off by the image's right edge, which can only be the line's last entry.
_CLIPPED = re.compile(rf"{_TIME} *- *{_TIME} *: *(-?[\d.]*)$")
# A pixel belongs to an import bar when it's clearly blue, and to an export bar when it's
# clearly orange. Grey axis lines, white background and black text fall in neither.
_HUE_MARGIN = 40
# Light bars are stippled, so a bar's columns can have small gaps.
_MAX_BAR_GAP = 2
# Prices too close to zero to measure a bar against.
_MIN_SCALE_PRICE = 1.0


class ChartReadError(EcotricityResponseError):
    """A timeframe chart could not be read or did not match the plan's prices."""


@dataclass(frozen=True, slots=True)
class LegendEntry:
    """One window from a chart's legend, with its price."""

    start: int
    """Minutes after midnight."""
    end: int
    """Minutes after midnight; 1440 is the midnight that ends the day."""
    text: str
    """The price as printed, in cents excluding GST, for example "-21.00". Cut short if the
    legend line runs off the image."""
    clipped: bool = False
    estimate: float | None = None
    """The price estimated from the bar heights, for a clipped entry where it could be measured."""

    @property
    def price(self) -> float | None:
        """Return the printed price, or None if it's cut short."""
        return None if self.clipped else float(self.text)

    @property
    def slots(self) -> range:
        """Return the half-hour slot indexes the window covers (0 is 00:00 to 00:30)."""
        return range(self.start // SLOT_MINUTES, self.end // SLOT_MINUTES)


@dataclass(frozen=True, slots=True)
class ChartReading:
    """Import and export windows read from one chart's legend."""

    import_entries: tuple[LegendEntry, ...]
    export_entries: tuple[LegendEntry, ...]


def needs_season(template: str) -> bool:
    """Return whether a chart template has season dates to fill in."""
    return _START_PLACEHOLDER in template or _END_PLACEHOLDER in template


def chart_path(
    template: str, start: date | None, end: date | None, *, include_gst: bool = False
) -> str:
    """Return the chart path for a season.

    Charts excluding GST are used for reading, because their legend prices match the API's. The
    GST-inclusive version is for display; note that Ecotricity adds GST to its export rows too.
    """
    path = template
    if start is not None and end is not None:
        path = path.replace(_START_PLACEHOLDER, start.strftime("%Y%m%d")).replace(
            _END_PLACEHOLDER, end.strftime("%Y%m%d")
        )
    path = path.removesuffix(_NO_GST_SUFFIX).removesuffix(".png")
    return path + (".png" if include_gst else _NO_GST_SUFFIX)


def season_chart_path(
    template: str | None, season: Season | None, *, include_gst: bool = False
) -> str:
    """Return a plan's chart for a season. A template without season dates ignores the season.

    Raises ChartReadError if the plan has no chart, or the chart needs a season and there's none
    (Ecotricity may not have published it yet).
    """
    if not template:
        raise ChartReadError("The plan has no timeframe chart")
    if not needs_season(template):
        return chart_path(template, None, None, include_gst=include_gst)
    if season is None:
        raise ChartReadError("No published season covers the day")
    return chart_path(template, season.start, season.end, include_gst=include_gst)


def shown_price(cents: float) -> str:
    """Return a price as a chart's legend prints it, such as "30.56" for 30.5532.

    The legend rounds the GST-inclusive price to the cent and converts that back, rounding again,
    so it can differ from the price rounded directly by a cent.
    """
    with_gst = (Decimal(repr(cents)) * _GST).quantize(_CENT, ROUND_HALF_UP)
    return str((with_gst / _GST).quantize(_CENT, ROUND_HALF_UP))


type _Glyph = tuple[tuple[int, ...], ...]
type _Run = tuple[int, int]

_TEMPLATES: dict[str, _Glyph] = {
    char: tuple(tuple(int(value, 16) * 16 for value in row) for row in rows)
    for char, rows in GLYPHS.items()
}


def glyph_distance(glyph: _Glyph, template: _Glyph) -> float:
    """Return how different two glyphs are, from 0 (identical) to about 1.

    Both are aligned on their left edge and top row. A shift of a pixel either way is also tried,
    since anti-aliasing can move a glyph's first column.
    """
    best = 1.0
    for shift in (0, -1, 1):
        diff = mass = 0
        for y in range(max(len(glyph), len(template))):
            row = glyph[y] if y < len(glyph) else ()
            other = template[y] if y < len(template) else ()
            width = max(len(row), len(other) + shift)
            for x in range(min(0, shift), width):
                a = row[x] if 0 <= x < len(row) else 0
                b = other[x - shift] if 0 <= x - shift < len(other) else 0
                diff += abs(a - b)
                mass += max(a, b)
        if mass:
            best = min(best, diff / mass)
        if best < _MAX_GLYPH_DISTANCE / 4:
            break
    return best


@cache
def _match_glyph(glyph: _Glyph) -> str:
    """Return the character a glyph is, cached because charts repeat the same pixels."""
    char, distance = min(
        ((char, glyph_distance(glyph, template)) for char, template in _TEMPLATES.items()),
        key=lambda item: item[1],
    )
    if distance > _MAX_GLYPH_DISTANCE:
        raise ChartReadError("The chart's legend has a character that can't be read")
    return char


class _Legend:
    """The legend's text lines and characters, read from a greyscale chart."""

    def __init__(self, image: Image.Image) -> None:
        self._width, self._height = image.size
        pixels = image.load()
        assert pixels is not None  # noqa: S101 - load() only returns None for unloaded images
        self._pixels = pixels
        # Where the ink is, as bytes by row and by column, so finding lines and characters is a
        # byte search rather than a loop over every pixel.
        ink = image.point(lambda value: 255 if value < _INK else 0)
        self._by_row = ink.tobytes()
        self._by_column = ink.transpose(Image.Transpose.TRANSPOSE).tobytes()

    def lines(self) -> list[tuple[int, int]]:
        """Return the (top, bottom) rows of each line of text."""
        width = self._width
        rows = [y for y in range(self._height) if any(self._by_row[y * width : (y + 1) * width])]
        lines: list[list[int]] = []
        for y in rows:
            if lines and y - lines[-1][-1] <= _MAX_LINE_GAP:
                lines[-1].append(y)
            else:
                lines.append([y])
        return [(line[0], line[-1]) for line in lines]

    def runs(self, line: tuple[int, int]) -> list[_Run]:
        """Return the (first, last) columns of each stretch of ink along a line."""
        top, bottom = line
        height = self._height
        runs: list[_Run] = []
        start: int | None = None
        for x in range(self._width + 1):
            ink = x < self._width and any(
                self._by_column[x * height + top : x * height + bottom + 1]
            )
            if ink and start is None:
                start = x
            elif not ink and start is not None:
                runs.append((start, x - 1))
                start = None
        return runs

    def glyph(self, runs: Sequence[_Run], top: int) -> _Glyph:
        """Return the pixels of one character, trimmed to its ink, from the line's top row."""
        first = max(min(run[0] for run in runs) - 1, 0)
        last = min(max(run[1] for run in runs) + 1, self._width - 1)
        bottom = min(top + _GLYPH_ROWS, self._height)
        columns = [
            x
            for x in range(first, last + 1)
            if any(int(self._pixels[x, y]) < 255 for y in range(top, bottom))  # type: ignore[arg-type] # noqa: PLR2004 - white
        ]
        rows = tuple(
            tuple(255 - int(self._pixels[x, y]) for x in range(columns[0], columns[-1] + 1))  # type: ignore[arg-type]
            for y in range(top, bottom)
        )
        while rows and not any(rows[-1]):
            rows = rows[:-1]
        return rows


@dataclass(slots=True)
class _Column:
    export: bool
    origin: float
    """The centre of the column's first character."""
    pitch: float
    """The width of one character."""
    lines: list[str]


def _centre(run: _Run) -> float:
    return (run[0] + run[1]) / 2


def _columns(legend: _Legend, heading: tuple[int, int], runs: list[_Run]) -> list[_Column]:
    """Return the legend's columns from its heading line ("Import rate", "Export rate")."""
    width = statistics.median(run[1] - run[0] + 1 for run in runs)
    groups: list[list[_Run]] = [[runs[0]]]
    for previous, run in pairwise(runs):
        if run[0] - previous[1] > _COLUMN_GAP_WIDTHS * width:
            groups.append([run])
        else:
            groups[-1].append(run)
    columns: list[_Column] = []
    for group in groups:
        heading_char = _match_glyph(legend.glyph(group[:1], heading[0]))
        pitch = (_centre(group[-1]) - _centre(group[0])) / (_HEADING_LENGTH - 1)
        if heading_char not in _HEADINGS or pitch <= 0:
            raise ChartReadError("The chart's legend headings can't be read")
        columns.append(_Column(_HEADINGS[heading_char], _centre(group[0]), pitch, []))
    return columns


def _line_cells(runs: list[_Run], origin: float, pitch: float) -> dict[int, list[_Run]]:
    """Group a column's runs of ink into character cells, refining the pitch along the line.

    Some characters ("m", "r", "3") are drawn as more than one run, so runs are grouped by which
    cell their centre falls in rather than taken one per character.
    """
    for _ in range(3):
        cells = [round((_centre(run) - origin) / pitch) for run in runs]
        if len(set(cells)) < 2:  # noqa: PLR2004 - two points make a line
            break
        mean_cell = statistics.fmean(cells)
        mean_x = statistics.fmean(_centre(run) for run in runs)
        spread = sum((cell - mean_cell) ** 2 for cell in cells)
        fitted = (
            sum(
                (cell - mean_cell) * (_centre(run) - mean_x)
                for cell, run in zip(cells, runs, strict=True)
            )
            / spread
        )
        if abs(fitted - pitch) > pitch / 10:
            break
        pitch = fitted
    grouped: dict[int, list[_Run]] = {}
    for run in runs:
        grouped.setdefault(round((_centre(run) - origin) / pitch), []).append(run)
    return grouped


def _read_text(image: Image.Image) -> list[_Column]:
    """Return the legend's text, line by line, for each column."""
    legend = _Legend(image)
    lines = legend.lines()
    for index, line in enumerate(lines):
        runs = legend.runs(line)
        if not runs:
            continue  # pragma: no cover - every line found has ink
        try:
            first = _match_glyph(legend.glyph(runs[:1], line[0]))
        except ChartReadError:
            continue
        if first in _HEADINGS:
            columns = _columns(legend, line, runs)
            entry_lines = lines[index + 1 :]
            break
    else:
        raise ChartReadError("The chart has no legend")

    for top, bottom in entry_lines:
        runs = legend.runs((top, bottom))
        for position, column in enumerate(columns):
            start = column.origin - column.pitch / 2
            end = (
                columns[position + 1].origin - column.pitch / 2
                if position + 1 < len(columns)
                else float("inf")
            )
            own = [run for run in runs if start <= _centre(run) < end]
            if not own:
                continue
            cells = _line_cells(own, column.origin, column.pitch)
            text = [" "] * (max(cells) + 1)
            for cell, cell_runs in cells.items():
                if cell >= 0:
                    text[cell] = _match_glyph(legend.glyph(cell_runs, top))
            column.lines.append("".join(text))
    return columns


def _minutes(hour: str, minute: str | None, meridiem: str) -> int:
    return (int(hour) % 12 + (12 if meridiem == "pm" else 0)) * 60 + int(minute or 0)


def _entries(lines: Sequence[str]) -> list[LegendEntry]:
    """Parse a column's lines into windows, checking that they cover the day once."""
    entries: list[LegendEntry] = []
    for line in lines:
        position = 0
        for match in _ENTRY.finditer(line):
            if line[position : match.start()].strip():
                raise ChartReadError(f"The chart's legend has unexpected text: {line.strip()}")
            groups = match.groups()
            entries.append(LegendEntry(_minutes(*groups[0:3]), _minutes(*groups[3:6]), groups[6]))
            position = match.end()
        rest = line[position:].strip()
        if not rest:
            continue
        if (clipped := _CLIPPED.fullmatch(rest)) is None:
            raise ChartReadError(f"The chart's legend has unexpected text: {line.strip()}")
        groups = clipped.groups()
        entries.append(
            LegendEntry(_minutes(*groups[0:3]), _minutes(*groups[3:6]), groups[6], clipped=True)
        )

    entries = [
        entry if entry.end else LegendEntry(entry.start, MINUTES_PER_DAY, entry.text, entry.clipped)
        for entry in entries
    ]
    entries.sort(key=lambda entry: entry.start)
    if (
        not entries
        or entries[0].start != 0
        or entries[-1].end != MINUTES_PER_DAY
        or any(a.end != b.start for a, b in pairwise(entries))
        or any(entry.start % SLOT_MINUTES or entry.end % SLOT_MINUTES for entry in entries)
        or any(entry.end <= entry.start for entry in entries)
    ):
        raise ChartReadError("The chart's legend windows don't cover the day")
    return entries


def _is_import(pixel: tuple[int, int, int]) -> bool:
    return pixel[2] - pixel[0] > _HUE_MARGIN


def _is_export(pixel: tuple[int, int, int]) -> bool:
    return pixel[0] - pixel[2] > _HUE_MARGIN


def _bar_heights(image: Image.Image) -> list[tuple[int, int]] | None:
    """Return each half-hour's import and export bar heights in pixels, or None if not found.

    The bars are the tallest band of rows with blue or orange in it. The colour key above the
    chart is a separate, thinner band.
    """
    width, height = image.size
    pixels = image.load()
    assert pixels is not None  # noqa: S101 - load() only returns None for unloaded images

    def coloured(x: int, y: int) -> bool:
        pixel: tuple[int, int, int] = pixels[x, y]  # type: ignore[assignment]
        return _is_import(pixel) or _is_export(pixel)

    rows = [y for y in range(height) if any(coloured(x, y) for x in range(0, width, 2))]
    bands: list[list[int]] = []
    for y in rows:
        if bands and y - bands[-1][-1] <= _MAX_BAR_GAP + 1:
            bands[-1].append(y)
        else:
            bands.append([y])
    if not bands:
        return None
    band = max(bands, key=len)
    top, bottom = band[0], band[-1]
    columns = [x for x in range(width) if any(coloured(x, y) for y in range(top, bottom + 1))]
    bars: list[list[int]] = []
    for x in columns:
        if bars and x - bars[-1][-1] <= _MAX_BAR_GAP + 1:
            bars[-1].append(x)
        else:
            bars.append([x])
    if len(bars) != SLOTS_PER_DAY:
        return None
    heights: list[tuple[int, int]] = []
    for bar in bars:
        x = bar[len(bar) // 2]
        column: list[tuple[int, int, int]] = [
            pixels[x, y]  # type: ignore[misc]
            for y in range(top, bottom + 1)
        ]
        imports = [y for y, pixel in enumerate(column) if _is_import(pixel)]
        exports = [y for y, pixel in enumerate(column) if _is_export(pixel)]
        heights.append(
            (
                imports[-1] - imports[0] + 1 if imports else 0,
                exports[-1] - exports[0] + 1 if exports else 0,
            )
        )
    return heights


def _estimate_clipped(
    image: Image.Image, import_entries: list[LegendEntry], export_entries: list[LegendEntry]
) -> tuple[list[LegendEntry], list[LegendEntry]]:
    """Estimate clipped prices from their bars in an RGB chart, scaled by the printed prices."""
    if not any(entry.clipped for entry in (*import_entries, *export_entries)):
        return import_entries, export_entries
    heights = _bar_heights(image)
    if heights is None:
        return import_entries, export_entries

    def bar(entry: LegendEntry, side: int) -> float:
        return statistics.median(heights[slot][side] for slot in entry.slots)

    # A bar with no coloured pixels in the column sampled (pale or patterned bars) wasn't
    # measured, so it gives neither the scale nor an estimate.
    scales = [
        height / abs(price)
        for side, entries in enumerate((import_entries, export_entries))
        for entry in entries
        if (price := entry.price) is not None
        and abs(price) >= _MIN_SCALE_PRICE
        and (height := bar(entry, side))
    ]
    if not scales:
        return import_entries, export_entries
    scale = statistics.median(scales)

    def sign(entry: LegendEntry, side: int) -> int:
        # The printed digits give the sign where they start with one, since an export price can
        # be a cost and an import price a credit. Otherwise it's the column's usual sign.
        if entry.text:
            return -1 if entry.text.startswith("-") else 1
        return -1 if side else 1

    def estimated(entries: list[LegendEntry], side: int) -> list[LegendEntry]:
        return [
            LegendEntry(
                entry.start,
                entry.end,
                entry.text,
                clipped=True,
                estimate=sign(entry, side) * height / scale
                if (height := bar(entry, side))
                else None,
            )
            if entry.clipped
            else entry
            for entry in entries
        ]

    return estimated(import_entries, 0), estimated(export_entries, 1)


def read_chart(data: bytes) -> ChartReading:
    """Read the import and export windows and prices from a timeframe chart PNG."""
    try:
        with Image.open(io.BytesIO(data)) as raw:
            image = raw.convert("RGB")
    except (UnidentifiedImageError, OSError) as err:
        raise ChartReadError(f"Timeframe chart is not a readable image: {err}") from err

    columns = _read_text(image.convert("L"))
    import_lines = [line for column in columns if not column.export for line in column.lines]
    export_lines = [line for column in columns if column.export for line in column.lines]
    if not import_lines:
        raise ChartReadError("The chart's legend has no import windows")
    import_entries = _entries(import_lines)
    export_entries = _entries(export_lines) if export_lines else []
    import_entries, export_entries = _estimate_clipped(image, import_entries, export_entries)
    return ChartReading(import_entries=tuple(import_entries), export_entries=tuple(export_entries))

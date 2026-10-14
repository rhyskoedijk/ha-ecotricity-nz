"""Read time-of-use windows from Ecotricity's plan timeframe charts.

Ecotricity publishes the hours each rate applies only as a server-rendered PNG per plan, season
and day type (weekday or weekend). No API, page data or public dataset carries them (see
docs/api.md). Each chart draws 48 half-hour bars: blue import bars above the axis and orange export
bars below it, with each bar's height proportional to its price and its shade marking its tier.

This module finds the 48 bars, groups them into tiers by colour and height, and measures each tier's
height. A shade usually marks one price, but not always, so bars of one shade with different
heights are separate tiers.
Turning tiers into prices and charge buckets happens in `match_tiers`, which checks the bar heights
against the API's own prices so a misread chart is rejected rather than trusted.

Pillow is a Home Assistant core dependency, so no extra requirement is needed.
"""

from __future__ import annotations

import io
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PIL import Image, UnidentifiedImageError

from .exceptions import EcotricityResponseError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

SLOTS_PER_DAY = 48
_START_PLACEHOLDER = "SSSSSSSS"
_END_PLACEHOLDER = "EEEEEEEE"
_NO_GST_SUFFIX = "-NOGST.png"

# A pixel belongs to an import bar when it is clearly blue, and to an export bar when it is
# clearly orange. Grey axis lines, white background and black text fall in neither.
_HUE_MARGIN = 40
# Light bars are stippled, so runs of bar pixels may have small gaps.
_MAX_RUN_GAP = 3
# Bars are much taller than the legend swatches; ignore runs shorter than this.
_MIN_RUN_LENGTH = 12
# Bar columns contain at least this share of the busiest column's bar pixels.
_BAR_PROFILE_SHARE = 0.5
# Bars whose median colours are closer than this belong to the same tier. Ecotricity's palette
# has 7 import and 6 export shades, the closest two only 23.5 apart (RGB distance), while the
# same shade read from different bars varies by about 4. Halfway keeps every shade separate.
_TIER_COLOUR_TOLERANCE = 12.0
# Bars of one shade can still be different prices: some network charts draw two close prices in
# the same shade, a few pixels apart. Bars of one
# price measure the same height to the pixel, so bars more than this many pixels apart are separate
# tiers.
_TIER_HEIGHT_TOLERANCE = 2


class ChartReadError(EcotricityResponseError):
    """A timeframe chart could not be read or did not match the plan's prices."""


@dataclass(frozen=True, slots=True)
class ChartTier:
    """Bars of one shade and height on a chart."""

    colour: tuple[int, int, int]
    height: float
    """Median bar height in pixels. Proportional to the absolute price."""
    slots: frozenset[int]
    """Half-hour slot indexes (0 is 00:00 to 00:30) drawn in this shade."""


@dataclass(frozen=True, slots=True)
class ChartReading:
    """Import and export tiers read from one chart."""

    import_tiers: tuple[ChartTier, ...]
    export_tiers: tuple[ChartTier, ...]


def needs_season(template: str) -> bool:
    """Return whether a chart template has season dates to fill in."""
    return _START_PLACEHOLDER in template or _END_PLACEHOLDER in template


def chart_path(
    template: str, start: date | None, end: date | None, *, include_gst: bool = False
) -> str:
    """Return the chart path for a season.

    Charts excluding GST are used for reading, because their bar heights match the API's prices.
    The GST-inclusive version is for display; note that Ecotricity adds GST to its export rows too.
    """
    path = template
    if start is not None and end is not None:
        path = path.replace(_START_PLACEHOLDER, start.strftime("%Y%m%d")).replace(
            _END_PLACEHOLDER, end.strftime("%Y%m%d")
        )
    path = path.removesuffix(_NO_GST_SUFFIX).removesuffix(".png")
    return path + (".png" if include_gst else _NO_GST_SUFFIX)


def _is_import(pixel: tuple[int, int, int]) -> bool:
    return pixel[2] - pixel[0] > _HUE_MARGIN


def _is_export(pixel: tuple[int, int, int]) -> bool:
    return pixel[0] - pixel[2] > _HUE_MARGIN


def _is_bar_pixel(pixel: Any) -> int:  # noqa: ANN401 - Pillow pixel access is untyped
    return int(_is_import(pixel) or _is_export(pixel))


def _bar_columns(image: Image.Image) -> list[tuple[int, int]]:
    """Return the (first, last) x of each bar, found from a per-column profile of bar pixels."""
    width, height = image.size
    pixels = image.load()
    assert pixels is not None  # noqa: S101 - load() only returns None for unloaded images
    profile = [sum(_is_bar_pixel(pixels[x, y]) for y in range(height)) for x in range(width)]
    threshold = max(profile, default=0) * _BAR_PROFILE_SHARE
    if threshold <= 0:
        return []
    bars: list[tuple[int, int]] = []
    start: int | None = None
    for x, count in enumerate([*profile, 0]):
        if count >= threshold and start is None:
            start = x
        elif count < threshold and start is not None:
            bars.append((start, x - 1))
            start = None
    return bars


def _longest_run(ys: Sequence[int]) -> tuple[int, int] | None:
    """Return the (top, bottom) of the longest run of y values, bridging small gaps."""
    best: tuple[int, int] | None = None
    run_start: int | None = None
    previous: int | None = None
    for y in [*ys, None]:
        if y is not None and previous is not None and y - previous <= _MAX_RUN_GAP + 1:
            previous = y
            continue
        if (
            run_start is not None
            and previous is not None
            and (best is None or previous - run_start > best[1] - best[0])
        ):
            best = (run_start, previous)
        run_start = previous = y
    if best is None or best[1] - best[0] + 1 < _MIN_RUN_LENGTH:
        return None
    return best


def _measure_bar(
    image: Image.Image, first: int, last: int, *, export: bool
) -> tuple[float, tuple[int, int, int]] | None:
    """Return the height and median colour of a bar's import or export part."""
    pixels = image.load()
    assert pixels is not None  # noqa: S101 - load() only returns None for unloaded images
    centre = (first + last) // 2
    columns = [x for x in (centre - 1, centre, centre + 1) if first <= x <= last]
    test = _is_export if export else _is_import
    hits: dict[int, tuple[int, int, int]] = {}
    for y in range(image.size[1]):
        for x in columns:
            pixel: tuple[int, int, int] = pixels[x, y]  # type: ignore[assignment]
            if test(pixel):
                hits[y] = pixel
                break
    run = _longest_run(sorted(hits))
    if run is None:
        return None
    top, bottom = run
    colours = [hits[y] for y in range(top, bottom + 1) if y in hits]
    colour = (
        int(statistics.median(c[0] for c in colours)),
        int(statistics.median(c[1] for c in colours)),
        int(statistics.median(c[2] for c in colours)),
    )
    return float(bottom - top + 1), colour


def _distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return float(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5)


def _group_tiers(
    bars: Sequence[tuple[float, tuple[int, int, int]] | None],
) -> tuple[ChartTier, ...]:
    """Group bars into tiers by shade and height, so one shade used for two prices stays apart."""
    groups: list[tuple[tuple[int, int, int], list[float], set[int]]] = []
    for slot, bar in enumerate(bars):
        if bar is None:
            continue
        height, colour = bar
        for reference, heights, slots in groups:
            if (
                _distance(reference, colour) <= _TIER_COLOUR_TOLERANCE
                and abs(heights[0] - height) <= _TIER_HEIGHT_TOLERANCE
            ):
                heights.append(height)
                slots.add(slot)
                break
        else:
            groups.append((colour, [height], {slot}))
    return tuple(
        ChartTier(colour=colour, height=statistics.median(heights), slots=frozenset(slots))
        for colour, heights, slots in groups
    )


def read_chart(data: bytes) -> ChartReading:
    """Read the import and export tiers from a timeframe chart PNG."""
    try:
        with Image.open(io.BytesIO(data)) as raw:
            image = raw.convert("RGB")
    except (UnidentifiedImageError, OSError) as err:
        raise ChartReadError(f"Timeframe chart is not a readable image: {err}") from err

    columns = _bar_columns(image)
    if len(columns) != SLOTS_PER_DAY:
        raise ChartReadError(f"Expected {SLOTS_PER_DAY} bars, found {len(columns)}")

    imports = [_measure_bar(image, first, last, export=False) for first, last in columns]
    exports = [_measure_bar(image, first, last, export=True) for first, last in columns]
    if any(bar is None for bar in imports):
        raise ChartReadError("Some half-hours have no import bar")
    if any(bar is not None for bar in exports) and any(bar is None for bar in exports):
        raise ChartReadError("Some half-hours have no export bar")
    return ChartReading(import_tiers=_group_tiers(imports), export_tiers=_group_tiers(exports))


def _fit(
    tiers: Sequence[ChartTier], candidates: Mapping[str, float], scale: float, tolerance: float
) -> tuple[float, dict[ChartTier, str]] | None:
    """Assign each tier to the nearest bucket price at a given pixels-per-cent scale.

    Returns the worst error and the assignment, or None if any tier is further than `tolerance`
    cents from every bucket.
    """
    assignment: dict[ChartTier, str] = {}
    worst = 0.0
    for tier in tiers:
        price = tier.height / scale
        bucket, bucket_price = min(candidates.items(), key=lambda item: abs(item[1] - price))
        error = abs(bucket_price - price)
        if error > tolerance:
            return None
        worst = max(worst, error)
        assignment[tier] = bucket
    return worst, assignment


def _anchor_scales(tiers: Sequence[ChartTier], candidates: Mapping[str, float]) -> list[float]:
    """Return the pixels-per-cent scales that put the tallest tier on each bucket price."""
    if not tiers:
        return []
    tallest = max(tier.height for tier in tiers)
    return [tallest / price for price in sorted(set(candidates.values()))]


def _usage_prices(bucket_prices: Mapping[str, float]) -> dict[str, float]:
    return {bucket: abs(price) for bucket, price in bucket_prices.items() if price}


def match_tiers(
    tiers: Sequence[ChartTier],
    bucket_prices: Mapping[str, float],
    *,
    other_tiers: Sequence[ChartTier] = (),
    other_prices: Mapping[str, float] | None = None,
    tolerance: float = 0.6,
) -> tuple[str, ...]:
    """Return the charge bucket for each half-hour slot.

    Bar height is proportional to the absolute price, but the pixels-per-cent scale differs
    between charts. Each candidate bucket is tried as the anchor for the tallest tier; a scale
    fits when every tier lands within `tolerance` cents of a bucket price. When several buckets
    share a price, the first one in `bucket_prices` order is used.

    A chart with few tiers can fit more than one scale (a single tier fits every price). Import
    and export bars share one axis, so the other side's tiers and prices (`other_tiers` and
    `other_prices`) are used to rule out scales they don't fit. If the slots still can't be
    settled, the chart is rejected rather than guessed.
    """
    if not tiers:
        raise ChartReadError("Chart has no tiers to match")
    candidates = _usage_prices(bucket_prices)
    if not candidates:
        raise ChartReadError("Plan has no usage prices to match against")

    fits = [
        (scale, fit)
        for scale in _anchor_scales(tiers, candidates)
        if (fit := _fit(tiers, candidates, scale, tolerance)) is not None
    ]
    if not fits:
        raise ChartReadError("Chart bar heights don't match the plan's prices")
    if len({frozenset(fit[1].items()) for _, fit in fits}) > 1:
        other_candidates = _usage_prices(other_prices or {})
        scales = _anchor_scales(tiers, candidates) + _anchor_scales(other_tiers, other_candidates)
        fits = [
            (scale, fit)
            for scale in scales
            if other_tiers
            and other_candidates
            and _fit(other_tiers, other_candidates, scale, tolerance) is not None
            and (fit := _fit(tiers, candidates, scale, tolerance)) is not None
        ]
        if len({frozenset(fit[1].items()) for _, fit in fits}) != 1:
            raise ChartReadError("Chart bar heights fit more than one set of the plan's prices")
    best = min((fit for _, fit in fits), key=lambda fit: fit[0])

    slots: list[str | None] = [None] * SLOTS_PER_DAY
    for tier, bucket in best[1].items():
        for slot in tier.slots:
            slots[slot] = bucket
    if any(bucket is None for bucket in slots):
        raise ChartReadError("Some half-hours could not be matched to a price")
    return tuple(bucket for bucket in slots if bucket is not None)

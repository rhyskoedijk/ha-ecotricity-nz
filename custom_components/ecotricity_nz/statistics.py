"""Import half-hourly portal readings into Home Assistant long-term statistics.

Portal data arrives a day or more late, so it can't be recorded as sensor state at the right
time. Instead each half-hourly series is summed into hourly external statistics, which the Energy
dashboard reads with their real timestamps. The last imported statistic doubles as the backfill
checkpoint: every run continues from the hour after it.

Because nothing before the checkpoint is read again, recent hours are only imported once every
earlier hour is complete. An hour with a missing half-hour holds the checkpoint back until the
portal fills it in, or until it's old enough to count as settled, when it's imported as it stands
so a permanent gap can't stop the import.

The same readings are priced into hourly cost statistics, one for each part of the bill (see
costs.py). An hour whose day can't be priced is a gap like any other: a recent one holds the
cost statistics back, and an old one is skipped.

Most cost statistics are zero for much of the day (a peak rate outside its hours, export at
night), so they're sparse: an hour that adds nothing has no row, unless the next hour adds
something or it's the last hour of the import. The recorder works out a change over a range from
the first row at or after the hour before it, so the row before each run of hours that add
something keeps that right, and the last row keeps each statistic's last hour the checkpoint.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy, UnitOfMass
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter, MassConverter

from .api.parsers import FEATURE_CARBON, FEATURE_EXPORT, FEATURE_IMPORT
from .const import DOMAIN
from .costs import (
    COST_PRECISION,
    ENERGY_PRECISION,
    TOTAL_ELECTRICITY_COST,
    cost_label,
    is_line_energy,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.core import HomeAssistant

    from .api import IntervalReading

HOUR = timedelta(hours=1)
READINGS_PER_HOUR = 2
# Prices are published in New Zealand dollars.
COST_UNIT = "NZD"


@dataclass(frozen=True, slots=True)
class StatisticPoint:
    """The last imported hour of a statistic."""

    start: datetime
    sum: float
    row: bool = True
    """False for a start worked out for a statistic with no row at it, which then gets one, with
    nothing added, before its next hours. Without it, a range starting between the statistic's
    real last row and its next hours would lose its first hour."""


@dataclass(frozen=True, slots=True)
class Series:
    """A half-hourly series from the portal, imported as an hourly statistic."""

    key: str
    """The statistic ID suffix, for example "energy_imported"."""
    feature: str | None
    """The portal feature ID the readings come from, or None for a cost worked out from them."""
    label: str
    unit: str
    unit_class: str | None
    precision: int = 4
    """Decimal places the running sum is kept to."""
    sparse: bool = False
    """Whether hours that add nothing are left out, where they can be (see the module's notes)."""


IMPORT = Series(
    "energy_imported",
    FEATURE_IMPORT,
    "imported",
    UnitOfEnergy.KILO_WATT_HOUR,
    EnergyConverter.UNIT_CLASS,
)
EXPORT = Series(
    "energy_exported",
    FEATURE_EXPORT,
    "exported",
    UnitOfEnergy.KILO_WATT_HOUR,
    EnergyConverter.UNIT_CLASS,
)
CARBON = Series(
    "carbon_savings",
    FEATURE_CARBON,
    "carbon savings",
    UnitOfMass.KILOGRAMS,
    MassConverter.UNIT_CLASS,
)


def cost_series(key: str) -> Series:
    """Return the series for one part of the hourly cost, such as "line_import_ea_levy".

    A bill line's kWh (`is_line_energy`) is an energy series instead. Every one is sparse except
    the total cost, whose hours say which hours were priced (see `charged_days`).
    """
    sparse = key != TOTAL_ELECTRICITY_COST
    if is_line_energy(key):
        return Series(
            key,
            None,
            cost_label(key),
            UnitOfEnergy.KILO_WATT_HOUR,
            EnergyConverter.UNIT_CLASS,
            ENERGY_PRECISION,
            sparse=sparse,
        )
    return Series(key, None, cost_label(key), COST_UNIT, None, COST_PRECISION, sparse=sparse)


def statistic_id(icp: str, series: Series) -> str:
    """Return the external statistic ID for one of an ICP's series.

    IDs are lower case, for example ecotricity_nz:0000123456ab1c2_energy_imported.
    """
    return f"{DOMAIN}:{icp.lower()}_{series.key}"


def hourly_totals(
    readings: Iterable[IntervalReading], settled_before: datetime
) -> dict[datetime, float]:
    """Sum half-hourly readings into UTC hours.

    Hours missing a half-hour are left out unless they start before `settled_before`, so a recent
    gap is picked up in full on a later run instead of being recorded short.
    """
    totals: dict[datetime, float] = defaultdict(float)
    counts: dict[datetime, int] = defaultdict(int)
    for reading in readings:
        hour = reading.start.replace(minute=0, second=0, microsecond=0)
        totals[hour] += reading.value
        counts[hour] += 1
    return {
        hour: round(total, 4)
        for hour, total in sorted(totals.items())
        if counts[hour] == READINGS_PER_HOUR or hour < settled_before
    }


async def async_last_point(hass: HomeAssistant, stat_id: str) -> StatisticPoint | None:
    """Return the most recent imported hour of a statistic."""
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        stat_id,
        True,  # noqa: FBT003 - convert_units, passed positionally to the executor
        {"sum"},
    )
    rows = result.get(stat_id)
    if not rows:
        return None
    row = rows[0]
    start = datetime.fromtimestamp(row["start"], tz=UTC)
    return StatisticPoint(start=start, sum=float(row.get("sum") or 0.0))


def async_import_hours(  # noqa: PLR0913 - writing a series needs all of these
    hass: HomeAssistant,
    icp: str,
    series: Series,
    totals: dict[datetime, float],
    last: StatisticPoint | None,
    *,
    settled_before: datetime,
) -> tuple[StatisticPoint | None, int]:
    """Add hourly totals after the last imported hour, continuing its running sum.

    Stops at the first hour missing from `totals` that starts at or after `settled_before`, so
    the checkpoint doesn't move past a recent gap the portal may still fill. A sparse series
    leaves out the hours that add nothing and don't need a row (see the module's notes). Returns
    the new last point and the number of hours added, rows or not.
    """
    running = last.sum if last else 0.0
    hours: list[StatisticData] = []
    newest = last
    for hour, value in sorted(totals.items()):
        if last is not None and hour <= last.start:
            continue
        if newest is not None and hour > max(newest.start + HOUR, settled_before):
            break
        running = round(running + value, series.precision)
        hours.append(StatisticData(start=hour, state=value, sum=running))
        newest = StatisticPoint(start=hour, sum=running)
    rows = [
        row
        for index, row in enumerate(hours)
        if not series.sparse or row["state"] or index == len(hours) - 1 or hours[index + 1]["state"]
    ]
    if rows and last is not None and not last.row:
        rows.insert(0, StatisticData(start=last.start, state=0.0, sum=last.sum))
    if rows:
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Ecotricity {icp} {series.label}",
                source=DOMAIN,
                statistic_id=statistic_id(icp, series),
                unit_class=series.unit_class,
                unit_of_measurement=series.unit,
            ),
            rows,
        )
    return newest, len(hours)

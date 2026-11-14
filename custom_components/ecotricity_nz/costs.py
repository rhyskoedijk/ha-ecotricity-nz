"""Work out what each hour of usage cost, part by part, for the cost statistics.

Each half-hourly reading is priced with the half-hour's rates (see tariff.py), split the way a
bill is: a line for each of Ecotricity's and the network's rates, with the kWh it was charged on,
one for each levy, and each daily charge item spread evenly over the hours of its day. Only what
can't be added up from those is stored besides: the total cost and the export compensation, which
include GST, for the Energy dashboard. Anything else (a side's supplier or network cost, its GST,
the net bill) is a sum of these, so it isn't stored again.

Bills charge Ecotricity's import rates on usage plus line losses, so Ecotricity's import lines
apply the ICP's consumption loss factor. Its export lines apply the generation loss factor the
same way: it's 1 on the ICPs seen so far, but nothing says others' are. The network's rates and
the levies are charged on usage alone.

Amounts keep their sign, so an export credit is negative. Results are in NZD; everything before
that is in cents excluding GST, like the rest of the integration. This module has no Home
Assistant imports.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

from .api.models import RateUnit
from .const import GST_FACTOR
from .tariff import (
    ComponentKind,
    DailyCharge,
    DaySlots,
    component_key,
    daily_charge_key,
    line_key,
    rate_component,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import date, tzinfo

    from .api import IntervalReading, Plan
    from .tariff import PriceComponent

HOUR = timedelta(hours=1)
READINGS_PER_HOUR = 2
CENTS_PER_DOLLAR = 100
# Decimal places of a dollar kept, well past a cent, so a day's share of a daily charge adds up
# to the whole charge.
COST_PRECISION = 8

TOTAL_ELECTRICITY_COST = "total_electricity_cost"
"""Everything charged for using electricity: import, levies, daily charges and GST. For the
Energy dashboard. It has a value in every priced hour, so it also says which hours are priced."""
TOTAL_ELECTRICITY_COMPENSATION = "total_electricity_compensation"
"""Every export amount and its GST with the sign flipped, for the Energy dashboard, which counts
money received as positive and takes it off the cost. The one amount where a credit is
positive."""
IMPORT_TOTALS = frozenset({TOTAL_ELECTRICITY_COST})
"""The import side's statistics worked out from its parts."""
EXPORT_TOTALS = frozenset({TOTAL_ELECTRICITY_COMPENSATION})
"""The export side's statistics worked out from its parts."""

LINE_PREFIX = "line_"
"""Bill lines: one cost statistic per rate of Ecotricity's or the network's (see `line_key`)."""
LINE_ENERGY_SUFFIX = "_energy"
"""Added to a bill line's key for the kWh it was charged on."""
ENERGY_PRECISION = 4
"""Decimal places of a kWh kept, as for the usage statistics."""

_LEVY_PREFIX = "line_import_"
_DAILY_PREFIX = "daily_charge_"
_RATE_SUFFIX = "_rate"
_TOTAL_LABELS = {
    TOTAL_ELECTRICITY_COST: "total electricity cost",
    TOTAL_ELECTRICITY_COMPENSATION: "total electricity compensation",
}


@dataclass(frozen=True, slots=True)
class DayCosts:
    """What a day's usage is priced with."""

    import_slots: DaySlots | None
    """None when the day's import prices aren't known."""
    export_slots: DaySlots | None
    """None when the plan has no export rates, or the day's export prices aren't known."""
    daily_charges: tuple[DailyCharge, ...]


def levy_key(part: PriceComponent) -> str:
    """Return the statistic key of an import levy, such as line_import_ea_levy.

    A levy is one line of the bill, named like the others, but has no kWh of its own: it's
    charged on all of the usage.
    """
    return _LEVY_PREFIX + component_key(part).removesuffix(_RATE_SUFFIX)


def daily_key(charge: DailyCharge) -> str:
    """Return the statistic key of a daily charge item, such as daily_charge_metering."""
    return _DAILY_PREFIX + daily_charge_key(charge).removesuffix(_RATE_SUFFIX)


def is_line_energy(key: str) -> bool:
    """Return whether a statistic key is the kWh of a bill line, rather than an amount."""
    return key.startswith(LINE_PREFIX) and key.endswith(LINE_ENERGY_SUFFIX)


def cost_label(key: str) -> str:
    """Return a statistic's name from its key, for example "line (import ea levy)".

    Raises ValueError for a key `cost_keys` can't give, so a new kind of key can't slip through
    without a name.
    """
    if (name := _TOTAL_LABELS.get(key)) is not None:
        return name
    if key.startswith(LINE_PREFIX):
        line = key.removeprefix(LINE_PREFIX)
        if is_line_energy(key):
            return f"line ({line.removesuffix(LINE_ENERGY_SUFFIX).replace('_', ' ')}) energy"
        return f"line ({line.replace('_', ' ')})"
    if key.startswith(_DAILY_PREFIX):
        return f"daily charge ({key.removeprefix(_DAILY_PREFIX).replace('_', ' ')})"
    raise ValueError(f"Not a cost statistic key: {key}")


@dataclass(frozen=True, slots=True)
class CostKeys:
    """Every statistic key a plan's rates can give, by side."""

    import_keys: frozenset[str]
    """Levies and daily charges, with the import side's totals (`IMPORT_TOTALS`)."""
    export_keys: frozenset[str]
    """The export side's totals (`EXPORT_TOTALS`). Empty when the plan has no export rates."""
    import_lines: frozenset[str] = frozenset()
    """Import bill lines, one per rate (see `line_key`)."""
    export_lines: frozenset[str] = frozenset()
    """Export bill lines, one per rate."""

    def statistic_keys(self, *, export: bool) -> frozenset[str]:
        """Return every statistic key of a side: its parts, totals, lines and lines' kWh."""
        parts, lines = (
            (self.export_keys, self.export_lines)
            if export
            else (self.import_keys, self.import_lines)
        )
        return parts | lines | {line + LINE_ENERGY_SUFFIX for line in lines}


def cost_keys(plan: Plan) -> CostKeys:
    """Return the statistic keys of every non-zero rate the plan has, on any day.

    Every priced hour gets a value for each of these, zero when the part doesn't apply, so a
    part that's only charged in some seasons doesn't leave gaps that hold the import back.
    Controlled-load rates are left out: the portal has no separate controlled-load series.
    """
    import_keys = set(IMPORT_TOTALS)
    lines: tuple[set[str], set[str]] = (set(), set())
    for rate in plan.rates:
        if not rate.price or rate.is_controlled:
            continue
        if rate.unit is RateUnit.PER_DAY:
            import_keys.add(
                daily_key(DailyCharge(rate.description, rate.price, is_network=rate.is_network))
            )
            continue
        if rate.unit is not RateUnit.PER_KWH or (rate.is_export and rate.is_levy):
            # Levies apply to every kWh imported, never to export.
            continue
        part = rate_component(rate)
        if part.kind is ComponentKind.LEVY:
            import_keys.add(levy_key(part))
        elif (line := line_key(part, export=rate.is_export)) is not None:
            lines[rate.is_export].add(line)
    export_keys = EXPORT_TOTALS if lines[True] else frozenset()
    return CostKeys(frozenset(import_keys), export_keys, frozenset(lines[0]), frozenset(lines[1]))


def hourly_costs(  # noqa: PLR0913 - pricing a reading needs all of these
    imported: Iterable[IntervalReading],
    exported: Iterable[IntervalReading],
    day_costs: Callable[[date], DayCosts | None],
    keys: CostKeys,
    *,
    tz: tzinfo,
    import_loss_factor: float,
    export_loss_factor: float,
    settled_before: datetime,
) -> dict[str, dict[datetime, float]]:
    """Return each hour's cost of every part, in NZD, keyed by statistic key and UTC hour.

    Hours are left out when their day's prices aren't known, and, like `hourly_totals`, when a
    half-hour is missing, unless they start before `settled_before`. Daily charges go with
    import: each hour with priced import gets the day's charges divided by the hours in that NZ
    day (23, 24 or 25). GST is added to each side's total, as bills charge it on the net total.
    """
    import_levies, import_lines, import_amounts, import_hours = _price_readings(
        imported, day_costs, tz, export=False, loss_factor=import_loss_factor
    )
    _levies, export_lines, export_amounts, export_hours = _price_readings(
        exported, day_costs, tz, export=True, loss_factor=export_loss_factor
    )
    costs: dict[str, dict[datetime, float]] = defaultdict(dict)

    for hour in _counted(import_hours, settled_before):
        parts = dict.fromkeys(keys.import_keys - IMPORT_TOTALS, 0.0)
        for key, cents in import_levies[hour].items():
            parts[key] = parts.get(key, 0.0) + cents
        energy = import_amounts[hour]
        local_day = hour.astimezone(tz).date()
        charges = day_costs(local_day)
        hours_in_day = _hours_in_day(local_day, tz)
        daily = 0.0
        for charge in charges.daily_charges if charges else ():
            if not charge.price:
                # Like `cost_keys`, an item priced at nothing has no statistic.
                continue
            share = charge.price / hours_in_day
            key = daily_key(charge)
            parts[key] = parts.get(key, 0.0) + share
            daily += share
        parts[TOTAL_ELECTRICITY_COST] = (energy + daily) * GST_FACTOR
        for key, cents in parts.items():
            costs[key][hour] = _nzd(cents)
        _add_lines(costs, hour, keys.import_lines, import_lines[hour])

    for hour in _counted(export_hours, settled_before):
        if keys.export_keys:
            costs[TOTAL_ELECTRICITY_COMPENSATION][hour] = _nzd(-export_amounts[hour] * GST_FACTOR)
        _add_lines(costs, hour, keys.export_lines, export_lines[hour])

    return {key: dict(sorted(hours.items())) for key, hours in costs.items() if hours}


def _add_lines(
    costs: dict[str, dict[datetime, float]],
    hour: datetime,
    lines: frozenset[str],
    values: dict[str, float],
) -> None:
    """Add an hour's bill lines, in NZD, and their kWh, zero for a line that didn't apply."""
    for line in lines:
        costs[line][hour] = _nzd(values.get(line, 0.0))
        energy = line + LINE_ENERGY_SUFFIX
        costs[energy][hour] = round(values.get(energy, 0.0), ENERGY_PRECISION)


def _price_readings(
    readings: Iterable[IntervalReading],
    day_costs: Callable[[date], DayCosts | None],
    tz: tzinfo,
    *,
    export: bool,
    loss_factor: float,
) -> tuple[
    dict[datetime, dict[str, float]],
    dict[datetime, dict[str, float]],
    dict[datetime, float],
    dict[datetime, int],
]:
    """Return each UTC hour's levies, bill lines and total amount, and its priced half-hours.

    Amounts are in cents, excluding GST. A bill line's kWh is under its key with
    `LINE_ENERGY_SUFFIX`: the kWh alone, though Ecotricity's lines include losses.
    """
    levies: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    lines: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    amounts: dict[datetime, float] = defaultdict(float)
    counts: dict[datetime, int] = defaultdict(int)
    for reading in readings:
        local = reading.start.astimezone(tz)
        costs = day_costs(local.date())
        slots = None if costs is None else costs.export_slots if export else costs.import_slots
        if slots is None:
            continue
        hour = reading.start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        counts[hour] += 1
        charged: set[str] = set()
        for part in slots.at(local).components:
            if not part.price:
                continue
            factor = loss_factor if part.kind is ComponentKind.SUPPLIER else 1.0
            cents = reading.value * factor * part.price
            amounts[hour] += cents
            if part.kind is ComponentKind.LEVY:
                levies[hour][levy_key(part)] += cents
            elif (line := line_key(part, export=export)) is not None:
                lines[hour][line] += cents
                charged.add(line)
        # Two rates in one bucket are one line, charged on the usage once.
        for line in charged:
            lines[hour][line + LINE_ENERGY_SUFFIX] += reading.value
    return levies, lines, amounts, counts


def _counted(counts: dict[datetime, int], settled_before: datetime) -> list[datetime]:
    return sorted(
        hour
        for hour, count in counts.items()
        if count == READINGS_PER_HOUR or hour < settled_before
    )


def charged_days(hours: Iterable[datetime], tz: tzinfo) -> float:
    """Return how many days of daily charges a set of priced hours carries.

    `hourly_costs` spreads each day's charges over the hours of its NZ day, and only priced hours
    get a share, so this is each hour's share of its day added up: a whole day counts as one
    (including the 23 and 25 hour days of daylight saving), and a day priced partway through
    counts as the part priced.
    """
    lengths: dict[date, int] = {}
    days = 0.0
    for hour in hours:
        day = hour.astimezone(tz).date()
        if day not in lengths:
            lengths[day] = _hours_in_day(day, tz)
        days += 1 / lengths[day]
    return round(days, 6)


def _hours_in_day(day: date, tz: tzinfo) -> int:
    """Return how many hours an NZ day has: 23 when daylight saving starts, 25 when it ends."""
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC)
    return round((end - start) / HOUR)


def _nzd(cents: float) -> float:
    return round(cents / CENTS_PER_DOLLAR, COST_PRECISION)

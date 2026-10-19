"""Work out the price of every half-hour from a plan's rates and the windows on its charts.

Each chart's legend gives the exact price of every window (see api/timeframes.py). This module
works out which of the plan's rates make up each of those prices:

- Every rate goes into a bucket by its charge code (such as CHARGE_24, CHARGE_HIGH or CHARGE_3),
  prefixed with EG_ for export and CN_ for controlled load.
- A half-hour's price is one of Ecotricity's (supplier) rates plus one of the network's rates,
  plus any anytime rates (CHARGE_24) and, for import, the levies. Ecotricity's and the network's
  time-of-use periods don't always line up, so the two rates can belong to different periods: a
  half-hour can be Ecotricity's peak but the network's off-peak. Bills charge them that way too.
- A legend price is matched against every such combination, rounded the way the legend rounds.
  A price no combination explains means the plan's rates and charts disagree, and the chart is
  rejected rather than guessed.

Each side (import or export) then gets one period per half-hour. When the price only depends on
Ecotricity's period, or only on the network's, that's the period (`Alignment`). When it depends
on both, the side is split: Ecotricity's and the network's periods are kept apart, and the overall
period is the one whose own price is nearest the half-hour's price.

Everything here is in cents excluding GST. This module has no Home Assistant imports.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, timedelta
from enum import StrEnum
from itertools import product
from typing import TYPE_CHECKING

from .api.models import RateUnit
from .api.timeframes import SLOT_MINUTES, SLOTS_PER_DAY, ChartReadError, shown_price

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date, datetime, tzinfo

    from .api.models import Plan, Rate, Season
    from .api.timeframes import ChartReading, LegendEntry

ANYTIME = "CHARGE_24"
EXPORT_PREFIX = "EG_"
CONTROLLED_PREFIX = "CN_"
PREFIXES = (EXPORT_PREFIX, CONTROLLED_PREFIX)
WEEKDAY_COUNT = 5
# A season named this applies all year; Ecotricity's site prefers any other season covering a day.
STANDARD_SEASON = "standard rates"


class TouPeriod(StrEnum):
    """Fixed periods for the charge codes that mean the same on every plan.

    CHARGE_24 is anytime, CHARGE_HIGH is peak and CHARGE_LOW is off-peak, whatever the network.
    Every other code (CHARGE_3, CHARGE_4 and so on) is named from the plan's own rate descriptions
    instead; see `bucket_names`.
    """

    ANYTIME = "anytime"
    PEAK = "peak"
    OFF_PEAK = "off_peak"


_PERIODS = {
    "CHARGE_24": TouPeriod.ANYTIME,
    "CHARGE_HIGH": TouPeriod.PEAK,
    "CHARGE_LOW": TouPeriod.OFF_PEAK,
}
_CHARGE_CODE_PREFIX = "CHARGE_"
# Words dropped from rate descriptions to make bucket names: "Shoulder Usage" becomes "Shoulder".
_NAME_NOISE = re.compile(r"\b(?:usage|export)\b", re.IGNORECASE)
# Prices this close are the same price.
_PRICE_PRECISION = 6


def period_for_bucket(bucket: str) -> TouPeriod | None:
    """Return the fixed period for a bucket code, ignoring any export or controlled prefix."""
    return _PERIODS.get(_unprefixed(bucket))


def bucket_names(plan: Plan) -> dict[str, str]:
    """Return a name for each of the plan's buckets that has no fixed period.

    Names come from the descriptions of the plan's own rates in each bucket, preferring
    Ecotricity's retail wording over the network's, with "usage" and "export" dropped: a
    "Super Off Peak Usage" rate names its bucket "Super off peak". A name that would clash with a
    fixed period or another bucket in the same group is left out, so `bucket_name` falls back to
    the charge code.
    """
    names: dict[str, str] = {}
    for rate in sorted(plan.rates, key=lambda rate: rate.is_network):
        if rate.unit is not RateUnit.PER_KWH or rate.is_levy:
            continue
        bucket = resolve_bucket(rate)
        if bucket in names or period_for_bucket(bucket) is not None:
            continue
        if name := _clean_name(rate.description):
            names[bucket] = name

    taken = {(prefix, period.value) for prefix in ("", *PREFIXES) for period in TouPeriod}
    for bucket, name in list(names.items()):
        key = (_prefix_of(bucket), period_key(name))
        if key in taken:
            del names[bucket]
        else:
            taken.add(key)
    return names


def bucket_name(bucket: str, names: dict[str, str]) -> str:
    """Return the name of a bucket with no fixed period, falling back to its charge code.

    For example "Shoulder" from the plan's descriptions, or "Charge 4" when no description names it.
    """
    if (name := names.get(bucket)) is not None:
        return name
    code = _unprefixed(bucket)
    if code.upper().startswith(_CHARGE_CODE_PREFIX):
        code = "Charge " + code[len(_CHARGE_CODE_PREFIX) :]
    return _sentence_case(code.replace("_", " "))


def bucket_period(bucket: str, names: dict[str, str]) -> str:
    """Return a bucket's period: its fixed one, or a key from its name, like "super_off_peak"."""
    if (period := period_for_bucket(bucket)) is not None:
        return period.value
    return period_key(bucket_name(bucket, names))


def period_key(name: str) -> str:
    """Return a lower-case key for a name, with runs of other characters as underscores."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _clean_name(description: str) -> str:
    # Separators left at either end once a word is dropped, as in "Uncontrolled - Usage".
    name = " ".join(_NAME_NOISE.sub(" ", description).split()).strip(" -:,/")
    return _sentence_case(name)


def _sentence_case(text: str) -> str:
    text = " ".join(text.split())
    return text[:1].upper() + text[1:].lower()


def _prefix_of(bucket: str) -> str:
    return next((prefix for prefix in PREFIXES if bucket.startswith(prefix)), "")


def _unprefixed(bucket: str) -> str:
    for prefix in PREFIXES:
        if bucket.startswith(prefix):
            return bucket.removeprefix(prefix)
    return bucket


def _prefix_for(rate: Rate) -> str:
    if rate.is_export:
        return EXPORT_PREFIX
    if rate.is_controlled:
        return CONTROLLED_PREFIX
    return ""


def resolve_bucket(rate: Rate) -> str:
    """Return the bucket code for a rate: its charge code, with the group's prefix.

    Every published rate has a charge code. A controlled rate without one applies whenever the
    network switches the load on, so it's anytime; any other is bucketed by its description.
    """
    prefix = _prefix_for(rate)
    if code := (rate.charge or "").strip().upper():
        return prefix + code
    if prefix == CONTROLLED_PREFIX:
        return prefix + ANYTIME
    return prefix + (rate.description.strip().upper() or "UNKNOWN")


class ComponentKind(StrEnum):
    """What part of a per-kWh price a component is."""

    SUPPLIER = "supplier"
    """Ecotricity's own energy charge (or credit, for export)."""
    NETWORK = "network"
    """The lines company's charge, passed through."""
    LEVY = "levy"
    """A levy such as the Electricity Authority levy."""


@dataclass(frozen=True, slots=True)
class PriceComponent:
    """One part of a price, in cents excluding GST."""

    kind: ComponentKind
    description: str
    price: float


@dataclass(frozen=True, slots=True)
class DailyCharge:
    """A fixed charge per day."""

    description: str
    price: float
    is_network: bool


class Alignment(StrEnum):
    """Whose time-of-use periods decide a side's price."""

    SUPPLIER = "supplier"
    """The price only depends on Ecotricity's period (or both line up exactly)."""
    NETWORK = "network"
    """The price only depends on the network's period, as on plans with one Ecotricity rate."""
    SPLIT = "split"
    """The price depends on both, which don't line up."""


@dataclass(frozen=True, slots=True)
class Slot:
    """The price of one half-hour on one side, and the rates that make it up."""

    price: float
    """Cents excluding GST. Export credits are negative."""
    supplier: str
    """The bucket of Ecotricity's rate, or the group's anytime bucket when it has no periods."""
    network: str
    """The bucket of the network's rate, or the group's anytime bucket when none applies."""
    components: tuple[PriceComponent, ...]
    bucket: str = ""
    """The overall period's bucket, set by `price_side`."""


@dataclass(frozen=True, slots=True)
class DaySlots:
    """Every half-hour of a weekday and a weekend day on one side."""

    weekday: tuple[Slot, ...]
    weekend: tuple[Slot, ...]

    def day(self, local: datetime) -> tuple[Slot, ...]:
        """Return the slots for a local day."""
        return self.weekday if local.weekday() < WEEKDAY_COUNT else self.weekend

    def at(self, local: datetime) -> Slot:
        """Return the slot for a local time."""
        return self.day(local)[local.hour * 2 + local.minute // SLOT_MINUTES]

    @property
    def all(self) -> tuple[Slot, ...]:
        """Return the weekday and weekend slots together."""
        return self.weekday + self.weekend


@dataclass(frozen=True, slots=True)
class SidePricing:
    """One side's half-hours and per-period prices for a day."""

    slots: DaySlots
    alignment: Alignment
    buckets: dict[str, float]
    """Each overall period's own price."""
    components: dict[str, tuple[PriceComponent, ...]]
    """The parts of each overall period's own price."""


@dataclass(frozen=True, slots=True)
class DayPricing:
    """Prices that apply on one day, in cents excluding GST."""

    import_side: SidePricing | None
    """None when the time-of-use windows couldn't be read."""
    export_side: SidePricing | None
    """None when the plan has no export rates or the windows couldn't be read."""
    controlled_buckets: dict[str, float]
    """Prices for controlled-load registers (such as hot water). Empty for most plans."""
    controlled_components: dict[str, tuple[PriceComponent, ...]]
    daily_charges: tuple[DailyCharge, ...]
    has_export: bool
    """Whether the plan has export rates on the day."""

    @property
    def daily_total(self) -> float:
        """Return the total of all fixed daily charges."""
        return sum(charge.price for charge in self.daily_charges)


@dataclass(frozen=True, slots=True)
class _Candidate:
    supplier: str
    network: str
    components: tuple[PriceComponent, ...]
    has_network: bool
    """False for the fallback with no network rate, where the network has rates for some hours."""

    @property
    def price(self) -> float:
        return sum(part.price for part in self.components)


@dataclass(frozen=True, slots=True)
class SideRates:
    """One side's per-kWh rates on a day, split into the parts a price is made from."""

    prefix: str
    always: tuple[PriceComponent, ...]
    """Anytime rates and levies, in every half-hour's price."""
    supplier: dict[str, tuple[PriceComponent, ...]]
    """Ecotricity's time-of-use rates by bucket."""
    network: dict[str, tuple[PriceComponent, ...]]
    """The network's time-of-use rates by bucket."""

    @property
    def is_empty(self) -> bool:
        """Return whether the side has no rates at all."""
        return not (self.always or self.supplier or self.network)

    def candidates(self) -> list[_Candidate]:
        """Return every combination of one supplier and one network rate, fallbacks last.

        Some networks only publish rates for some periods (a peak export rate, say), and charge
        nothing in the others, so a combination with no network rate comes last.
        """
        anytime = self.prefix + ANYTIME
        supplier = list(self.supplier.items()) or [(anytime, ())]
        network = list(self.network.items()) or [(anytime, ())]
        combos = [
            _Candidate(s_bucket, n_bucket, (*s_parts, *n_parts, *self.always), has_network=True)
            for (s_bucket, s_parts), (n_bucket, n_parts) in product(supplier, network)
        ]
        if self.network:
            combos += [
                _Candidate(s_bucket, anytime, (*s_parts, *self.always), has_network=False)
                for s_bucket, s_parts in supplier
            ]
        return combos

    def flat(self) -> Slot | None:
        """Return the only possible half-hour, when the side has one price all day."""
        combos = [combo for combo in self.candidates() if combo.has_network]
        if len(combos) != 1:
            return None
        return _slot(combos[0])


def side_rates(plan: Plan, day: date, *, export: bool) -> SideRates:
    """Return the import or export per-kWh rates that apply on a day."""
    prefix = EXPORT_PREFIX if export else ""
    rates = [
        rate
        for rate in plan.rates
        if rate.applies_on(day)
        and rate.unit is RateUnit.PER_KWH
        and rate.is_export == export
        and not rate.is_controlled
    ]
    always: list[PriceComponent] = []
    supplier: dict[str, tuple[PriceComponent, ...]] = {}
    network: dict[str, tuple[PriceComponent, ...]] = {}
    for rate in rates:
        part = _component(rate)
        if rate.is_levy:
            # Levies apply to every kWh imported, never to export.
            if not export:
                always.append(part)
            continue
        bucket = resolve_bucket(rate)
        if bucket == prefix + ANYTIME:
            always.append(part)
        else:
            group = network if rate.is_network else supplier
            group[bucket] = (*group.get(bucket, ()), part)
    return SideRates(prefix=prefix, always=tuple(always), supplier=supplier, network=network)


def _component(rate: Rate) -> PriceComponent:
    if rate.is_levy:
        kind = ComponentKind.LEVY
    elif rate.is_network:
        kind = ComponentKind.NETWORK
    else:
        kind = ComponentKind.SUPPLIER
    return PriceComponent(kind, rate.description, rate.price)


def _slot(candidate: _Candidate) -> Slot:
    return Slot(
        price=round(candidate.price, _PRICE_PRECISION),
        supplier=candidate.supplier,
        network=candidate.network,
        components=candidate.components,
    )


def _matches(entry: LegendEntry, candidates: Sequence[_Candidate]) -> list[_Candidate]:
    """Return the combinations whose price the legend entry shows."""
    if not entry.clipped:
        return [combo for combo in candidates if shown_price(combo.price) == entry.text]
    matching = [combo for combo in candidates if shown_price(combo.price).startswith(entry.text)]
    prices = sorted({round(combo.price, 2) for combo in matching})
    if len(prices) <= 1:
        return matching
    if entry.estimate is None:
        return []
    # The digits that fit fit more than one price, so the bar's height settles it, as long as
    # it's clearly nearer one of them.
    estimate = entry.estimate
    nearest = min(prices, key=lambda price: abs(price - estimate))
    others = [price for price in prices if price != nearest]
    if any(abs(price - estimate) < 2 * abs(nearest - estimate) for price in others):
        return []
    return [combo for combo in matching if round(combo.price, 2) == nearest]


def explain(entries: Sequence[LegendEntry], rates: SideRates) -> tuple[Slot, ...]:
    """Return every half-hour's price and rates from a chart's legend entries for one side.

    Raises ChartReadError if a price can't be explained by the plan's rates, or a clipped price
    can't be settled.

    When several combinations give the same price (two network periods at the same rate, say),
    the one where Ecotricity's and the network's periods line up is preferred, then one that
    carries on from the half-hour before.
    """
    candidates = rates.candidates()
    slots: list[Slot] = []
    previous: _Candidate | None = None
    for entry in sorted(entries, key=lambda entry: entry.start):
        matching = _matches(entry, candidates)
        if not matching:
            raise ChartReadError(
                f"The chart's price {entry.text}c from {entry.start // 60:02d}:"
                f"{entry.start % 60:02d} doesn't match the plan's rates"
            )

        def preference(combo: _Candidate, before: _Candidate | None = previous) -> tuple[int, ...]:
            return (
                not combo.has_network,
                combo.supplier != combo.network,
                before is None or combo.network != before.network,
                before is None or combo.supplier != before.supplier,
            )

        chosen = min(matching, key=preference)
        slots.extend(_slot(chosen) for _ in entry.slots)
        previous = chosen
    if len(slots) != SLOTS_PER_DAY:  # pragma: no cover - the reader checks the windows
        raise ChartReadError("The chart's windows don't cover the day")
    return tuple(slots)


def day_slots(
    rates: SideRates, weekday: ChartReading | None, weekend: ChartReading | None, *, export: bool
) -> DaySlots:
    """Return one side's half-hours for a weekday and a weekend day.

    A side with one possible price all day needs no chart. Raises ChartReadError if the charts
    are needed but missing, or can't be explained.
    """
    if (flat := rates.flat()) is not None:
        slots = (flat,) * SLOTS_PER_DAY
        return DaySlots(weekday=slots, weekend=slots)
    if weekday is None or weekend is None:
        raise ChartReadError("The plan's charts for the season couldn't be downloaded or read")

    def side(chart: ChartReading) -> tuple[LegendEntry, ...]:
        entries = chart.export_entries if export else chart.import_entries
        if not entries:
            raise ChartReadError(f"The chart has no {'export' if export else 'import'} windows")
        return entries

    return DaySlots(weekday=explain(side(weekday), rates), weekend=explain(side(weekend), rates))


def _price_key(price: float) -> float:
    return round(price, _PRICE_PRECISION)


def _determines(seasons: Iterable[DaySlots], key: str) -> bool:
    """Return whether one party's period alone decides the price in every season."""
    for slots in seasons:
        prices: dict[str, set[float]] = {}
        for slot in slots.all:
            prices.setdefault(getattr(slot, key), set()).add(_price_key(slot.price))
        if any(len(values) > 1 for values in prices.values()):
            return False
    return True


def plan_alignment(seasons: Sequence[DaySlots]) -> Alignment:
    """Return whose periods decide a side's price, across every season's half-hours.

    Ecotricity's periods are preferred when both line up, unless Ecotricity has one rate all day.
    """
    supplier = _determines(seasons, "supplier")
    supplier_varies = any(
        _unprefixed(slot.supplier) != ANYTIME for slots in seasons for slot in slots.all
    )
    if supplier and (supplier_varies or not _determines(seasons, "network")):
        return Alignment.SUPPLIER
    if _determines(seasons, "network"):
        return Alignment.NETWORK
    return Alignment.SPLIT


def _most_common(slots: Iterable[Slot]) -> Slot | None:
    counts = Counter(slots)
    return counts.most_common(1)[0][0] if counts else None


def _own_slots(slots: DaySlots, alignment: Alignment) -> dict[str, Slot]:
    """Return a slot showing each period's own price.

    Without a split, that's any half-hour in the period. With one, it's a half-hour where both
    Ecotricity and the network are in that period, or failing that, one where either is.
    """
    every = slots.all
    if alignment is not Alignment.SPLIT:
        key = "supplier" if alignment is Alignment.SUPPLIER else "network"
        return {getattr(slot, key): slot for slot in every}
    # Both parties have periods when the side is split, so neither's anytime is a period of its own.
    buckets = ({slot.supplier for slot in every} | {slot.network for slot in every}) - {
        _prefix_of(every[0].supplier) + ANYTIME
    }
    own: dict[str, Slot] = {}
    for bucket in buckets:
        found = _most_common(
            slot for slot in every if slot.supplier == bucket and slot.network == bucket
        ) or _most_common(slot for slot in every if bucket in (slot.supplier, slot.network))
        if found is not None:
            own[bucket] = found
    # A season where neither has periods (one price all day) falls back to Ecotricity's.
    return own or {slot.supplier: slot for slot in every}


def price_side(slots: DaySlots, alignment: Alignment) -> SidePricing:
    """Label each half-hour with its overall period, and work out each period's own price."""
    own = _own_slots(slots, alignment)

    def label(slot: Slot) -> Slot:
        if alignment is Alignment.SUPPLIER:
            bucket = slot.supplier
        elif alignment is Alignment.NETWORK:
            bucket = slot.network
        else:
            # The period whose own price is nearest, preferring Ecotricity's on a tie.
            bucket = min(
                own,
                key=lambda name: (
                    abs(own[name].price - slot.price),
                    name != slot.supplier,
                    name != slot.network,
                    name,
                ),
            )
        return replace(slot, bucket=bucket)

    labelled = DaySlots(
        weekday=tuple(label(slot) for slot in slots.weekday),
        weekend=tuple(label(slot) for slot in slots.weekend),
    )
    used = {slot.bucket for slot in labelled.all}
    return SidePricing(
        slots=labelled,
        alignment=alignment,
        buckets={bucket: own[bucket].price for bucket in sorted(used)},
        components={bucket: own[bucket].components for bucket in sorted(used)},
    )


def _controlled(
    plan: Plan, day: date
) -> tuple[dict[str, float], dict[str, tuple[PriceComponent, ...]]]:
    """Return controlled-load prices per bucket, which no chart shows.

    Controlled load is switched by the network, not the clock. An anytime controlled rate applies
    alongside every other controlled bucket, and levies apply to every controlled kWh.
    """
    rates = [
        rate
        for rate in plan.rates
        if rate.applies_on(day) and rate.unit is RateUnit.PER_KWH and rate.is_controlled
    ]
    levies = tuple(
        _component(rate)
        for rate in plan.rates
        if rate.applies_on(day) and rate.unit is RateUnit.PER_KWH and rate.is_levy
    )
    buckets: dict[str, tuple[PriceComponent, ...]] = {}
    for rate in rates:
        if rate.is_levy:
            continue
        bucket = resolve_bucket(rate)
        buckets[bucket] = (*buckets.get(bucket, ()), _component(rate))
    anytime = CONTROLLED_PREFIX + ANYTIME
    if anytime in buckets and len(buckets) > 1:
        always = buckets.pop(anytime)
        buckets = {bucket: (*parts, *always) for bucket, parts in buckets.items()}
    components = {bucket: (*parts, *levies) for bucket, parts in buckets.items()}
    return (
        {
            bucket: round(sum(part.price for part in parts), 4)
            for bucket, parts in components.items()
        },
        components,
    )


def price_day(
    plan: Plan,
    day: date,
    import_slots: DaySlots | None,
    export_slots: DaySlots | None,
    alignments: tuple[Alignment, Alignment] = (Alignment.SUPPLIER, Alignment.SUPPLIER),
) -> DayPricing:
    """Return a day's prices from its half-hours (see `day_slots`) and the plan's daily charges."""
    daily = tuple(
        DailyCharge(rate.description, rate.price, is_network=rate.is_network)
        for rate in plan.rates
        if rate.applies_on(day) and rate.unit is RateUnit.PER_DAY
    )
    controlled, controlled_components = _controlled(plan, day)
    return DayPricing(
        import_side=price_side(import_slots, alignments[0]) if import_slots else None,
        export_side=price_side(export_slots, alignments[1]) if export_slots else None,
        controlled_buckets=controlled,
        controlled_components=controlled_components,
        daily_charges=daily,
        has_export=not side_rates(plan, day, export=True).is_empty,
    )


def season_for(plan: Plan, day: date) -> Season | None:
    """Return the plan season that applies on a day.

    Seasons can overlap: a plan can publish "Standard Rates" for the whole year and a winter
    season inside it. Like Ecotricity's site, any other season covering the day wins.
    """
    covering = [season for season in plan.seasons if season.contains(day)]
    return next(
        (season for season in covering if season.name.strip().lower() != STANDARD_SEASON),
        covering[0] if covering else None,
    )


def next_season_change(plan: Plan, day: date) -> date | None:
    """Return the first day a different season applies.

    Within a season that's at the latest the day after it ends, even if Ecotricity hasn't
    published the next season yet, because that's when prices or windows can change. Outside any
    season it's the start of the next published one.
    """
    current = season_for(plan, day)
    boundaries = sorted(
        {season.start for season in plan.seasons}
        | {season.end + timedelta(days=1) for season in plan.seasons}
    )
    return next(
        (
            boundary
            for boundary in boundaries
            if boundary > day and season_for(plan, boundary) != current
        ),
        None,
    )


def _change_key(slot: Slot, key: str = "bucket") -> tuple[str, float]:
    return (getattr(slot, key), _price_key(slot.price) if key == "bucket" else 0.0)


def next_change(
    slots: DaySlots, now: datetime, tz: tzinfo, until: datetime | None = None
) -> tuple[datetime, Slot] | None:
    """Return when the period or price next changes, and the half-hour from then.

    Steps through half-hour boundaries for up to a week. Stops at `until` (the next season
    change), after which these slots may no longer apply. Steps are counted in UTC, so the hour
    skipped when daylight saving starts is skipped and the repeated one is visited twice.
    """
    now = now.astimezone(UTC)
    current = _change_key(slots.at(now.astimezone(tz)))
    boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(
        minutes=SLOT_MINUTES if now.minute < SLOT_MINUTES else 2 * SLOT_MINUTES
    )
    for step in range(SLOTS_PER_DAY * 7):
        instant = boundary + timedelta(minutes=SLOT_MINUTES * step)
        if until is not None and instant >= until:
            return None
        slot = slots.at(instant.astimezone(tz))
        if _change_key(slot) != current:
            return instant, slot
    return None


def day_windows(slots: Sequence[Slot], key: str = "bucket") -> list[tuple[str, str, Slot]]:
    """Group a day's half-hours into (start, end, slot) windows of one period and price.

    For example ("07:00", "11:00", slot). The last window ends at "24:00". With `key` set to
    "supplier" or "network", windows follow that party's periods instead, whatever the price.
    """
    windows: list[tuple[str, str, Slot]] = []
    start = 0
    for index in range(1, SLOTS_PER_DAY + 1):
        if index == SLOTS_PER_DAY or _change_key(slots[index], key) != _change_key(
            slots[start], key
        ):
            windows.append((_slot_time(start), _slot_time(index), slots[start]))
            start = index
    return windows


def _slot_time(slot: int) -> str:
    return f"{slot // 2:02d}:{SLOT_MINUTES * (slot % 2):02d}"

"""Turn a plan's published rates into prices per charge bucket and per half-hour.

This mirrors how the Ecotricity onboarding site combines rates, so the numbers match what
customers see there:

- Each rate goes into a bucket by its charge code (CHARGE_24, CHARGE_HIGH, CHARGE_LOW, CHARGE_3),
  prefixed with EG_ for export. Rates without a code are bucketed by their description.
- Levies (for example the EA levy) are added to every import per-kWh bucket.
- Retail and network rates in the same bucket are summed.
- When a plan has an anytime retail energy rate but time-of-use network rates, the anytime rate is
  folded into each time-of-use bucket.

Everything here is in cents excluding GST. This module has no Home Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from .api.models import RateUnit

if TYPE_CHECKING:
    from datetime import date, datetime, tzinfo

    from .api.models import Plan, Rate, Season

ANYTIME = "CHARGE_24"
TOU_BUCKETS = frozenset({"CHARGE_HIGH", "CHARGE_LOW", "CHARGE_3"})
EXPORT_PREFIX = "EG_"
CONTROLLED_PREFIX = "CN_"
PREFIXES = (EXPORT_PREFIX, CONTROLLED_PREFIX)
SLOTS_PER_DAY = 48
HALF_HOUR = 30
WEEKDAY_COUNT = 5


class TouPeriod(StrEnum):
    """Friendly names for Ecotricity's four charge codes.

    These are the only codes Ecotricity's own site recognises: CHARGE_24 (anytime), CHARGE_HIGH
    (peak), CHARGE_LOW (off-peak) and CHARGE_3 (shoulder). Anytime is a period when a network has
    no time-of-use rates for that direction; shoulder is used by networks with a third tier. A
    bucket with any other code is still priced, but has no period.
    """

    ANYTIME = "anytime"
    PEAK = "peak"
    OFF_PEAK = "off_peak"
    SHOULDER = "shoulder"


_PERIODS = {
    "CHARGE_24": TouPeriod.ANYTIME,
    "CHARGE_HIGH": TouPeriod.PEAK,
    "CHARGE_LOW": TouPeriod.OFF_PEAK,
    "CHARGE_3": TouPeriod.SHOULDER,
}


def period_for_bucket(bucket: str) -> TouPeriod | None:
    """Return the friendly period for a bucket code, ignoring any export or controlled prefix."""
    return _PERIODS.get(_unprefixed(bucket))


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


@dataclass(frozen=True, slots=True)
class DailyCharge:
    """A fixed charge per day."""

    description: str
    price: float
    is_network: bool


@dataclass(frozen=True, slots=True)
class DayPricing:
    """Prices that apply on one day, in cents excluding GST."""

    import_buckets: dict[str, float]
    export_buckets: dict[str, float]
    """Export prices are negative: they are credits."""
    controlled_buckets: dict[str, float]
    """Prices for controlled-load registers (such as hot water). Empty for most plans."""
    components: dict[str, tuple[PriceComponent, ...]]
    """The parts that make up each bucket's price (import and export)."""
    daily_charges: tuple[DailyCharge, ...]
    levy: float

    @property
    def daily_total(self) -> float:
        """Return the total of all fixed daily charges."""
        return sum(charge.price for charge in self.daily_charges)

    @property
    def import_is_flat(self) -> bool:
        """Return True when one import price applies all day."""
        return len(self.import_buckets) <= 1

    @property
    def export_is_flat(self) -> bool:
        """Return True when one export price applies all day."""
        return len(self.export_buckets) <= 1


@dataclass(frozen=True, slots=True)
class SlotMap:
    """The charge bucket for each half-hour, for weekdays and weekends."""

    weekday: tuple[str, ...]
    weekend: tuple[str, ...]

    def bucket_at(self, local: datetime) -> str:
        """Return the bucket for a local time."""
        slots = self.weekday if local.weekday() < WEEKDAY_COUNT else self.weekend
        return slots[local.hour * 2 + local.minute // 30]

    @classmethod
    def flat(cls, bucket: str) -> SlotMap:
        """Return a map with the same bucket all day, every day."""
        slots = (bucket,) * SLOTS_PER_DAY
        return cls(weekday=slots, weekend=slots)


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
    """One part of a bucket's price, in cents excluding GST."""

    kind: ComponentKind
    period: TouPeriod | None
    """The period of the rate this part came from, which can differ from the bucket's period.
    An anytime supplier rate folded into a peak bucket keeps the period ANYTIME."""
    description: str
    price: float


@dataclass(frozen=True, slots=True)
class _Bucket:
    components: tuple[PriceComponent, ...] = ()

    def of(self, kind: ComponentKind) -> tuple[PriceComponent, ...]:
        return tuple(part for part in self.components if part.kind is kind)

    @property
    def total(self) -> float:
        return sum(part.price for part in self.components)


def resolve_bucket(rate: Rate) -> str:
    """Return the bucket code for a rate, following the onboarding site's rules."""
    prefix = _prefix_for(rate)
    code = (rate.charge or "").strip().upper()
    if code:
        return prefix + code
    description = rate.description.strip().lower()
    bucket: str | None = None
    if rate.unit is RateUnit.PER_KWH:
        if "off peak" in description:
            bucket = "CHARGE_LOW"
        elif "on peak" in description or description in ("peak usage", "peak"):
            bucket = "CHARGE_HIGH"
        elif "shoulder" in description:
            bucket = "CHARGE_3"
        elif "anytime" in description:
            bucket = ANYTIME
    if bucket is None and prefix == CONTROLLED_PREFIX:
        # Controlled load is switched by the network's ripple control, not by time of day, so a
        # controlled rate with no period of its own applies whenever the load is on.
        bucket = ANYTIME
    return prefix + (bucket or description or "UNKNOWN")


def _add(total: float | None, value: float) -> float:
    return value if total is None else total + value


def price_day(plan: Plan, day: date) -> DayPricing:
    """Return the per-bucket prices, their parts, and the daily charges that apply on a day."""
    rates = [rate for rate in plan.rates if rate.applies_on(day)]
    levies = tuple(
        PriceComponent(ComponentKind.LEVY, TouPeriod.ANYTIME, rate.description, rate.price)
        for rate in rates
        if rate.is_levy and rate.unit is RateUnit.PER_KWH
    )

    usage: dict[str, _Bucket] = {}
    daily: list[DailyCharge] = []
    for rate in rates:
        if rate.is_levy and rate.unit is RateUnit.PER_KWH:
            continue
        if rate.unit is RateUnit.PER_DAY:
            daily.append(DailyCharge(rate.description, rate.price, is_network=rate.is_network))
            continue
        if rate.unit is not RateUnit.PER_KWH:
            continue
        key = resolve_bucket(rate)
        part = PriceComponent(
            kind=ComponentKind.NETWORK if rate.is_network else ComponentKind.SUPPLIER,
            period=period_for_bucket(key),
            description=rate.description,
            price=rate.price,
        )
        usage[key] = _Bucket((*usage.get(key, _Bucket()).components, part))

    for prefix in ("", *PREFIXES):
        _merge_anytime_into_tou(usage, prefix)
        _fill_missing_network(usage, prefix)

    # Levies apply to every kWh imported, so they're part of every import bucket's price.
    for key, bucket in list(usage.items()):
        if not key.startswith(EXPORT_PREFIX):
            usage[key] = _Bucket((*bucket.components, *levies))

    return DayPricing(
        import_buckets={
            key: round(bucket.total, 4)
            for key, bucket in usage.items()
            if not key.startswith(PREFIXES)
        },
        export_buckets={
            key: round(bucket.total, 4)
            for key, bucket in usage.items()
            if key.startswith(EXPORT_PREFIX)
        },
        controlled_buckets={
            key: round(bucket.total, 4)
            for key, bucket in usage.items()
            if key.startswith(CONTROLLED_PREFIX)
        },
        components={key: bucket.components for key, bucket in usage.items()},
        daily_charges=tuple(daily),
        levy=round(sum(levy.price for levy in levies), 4),
    )


def _tou_keys(usage: dict[str, _Bucket], prefix: str) -> list[str]:
    """Return the time-of-use bucket keys for one group (import, export or controlled)."""
    return [
        key
        for key in usage
        if key.startswith(prefix)
        and key.removeprefix(prefix) in TOU_BUCKETS
        and (prefix or not key.startswith(PREFIXES))
    ]


def _merge_anytime_into_tou(usage: dict[str, _Bucket], prefix: str) -> None:
    """Fold an anytime supplier rate into network-defined time-of-use buckets."""
    anytime_key = prefix + ANYTIME
    anytime = usage.get(anytime_key)
    tou = _tou_keys(usage, prefix)
    tou.extend(
        key
        for key, bucket in usage.items()
        if key not in tou
        and key.startswith(prefix)
        and key != anytime_key
        and (prefix or not key.startswith(PREFIXES))
        and bucket.of(ComponentKind.NETWORK)
    )
    if anytime is None or not anytime.of(ComponentKind.SUPPLIER) or not tou:
        return
    if any(usage[key].of(ComponentKind.SUPPLIER) for key in tou):
        return
    for key in tou:
        usage[key] = _Bucket((*anytime.of(ComponentKind.SUPPLIER), *usage[key].components))
    if anytime.of(ComponentKind.NETWORK):
        usage[anytime_key] = _Bucket(anytime.of(ComponentKind.NETWORK))
    else:
        del usage[anytime_key]


def _fill_missing_network(usage: dict[str, _Bucket], prefix: str) -> None:
    """Charge the off-peak network rate where a period has no network rate of its own.

    Some networks only publish a peak network rate for their peak season, and charge peak hours
    at the off-peak rate for the rest of the year; the onboarding site's charts price it this way.
    On networks that publish a rate for every period this changes nothing. If a network ever
    behaves differently, the chart bar heights won't match these prices and the chart is rejected
    (see api/timeframes.py), so a wrong assumption shows up as an unavailable sensor and a repair
    issue rather than a wrong price.
    """
    off_peak = usage.get(prefix + "CHARGE_LOW")
    if off_peak is None or not off_peak.of(ComponentKind.NETWORK):
        return
    for key in _tou_keys(usage, prefix):
        bucket = usage[key]
        if bucket.of(ComponentKind.SUPPLIER) and not bucket.of(ComponentKind.NETWORK):
            usage[key] = _Bucket((*bucket.components, *off_peak.of(ComponentKind.NETWORK)))


def season_for(plan: Plan, day: date) -> Season | None:
    """Return the plan season that contains the day."""
    return next((season for season in plan.seasons if season.contains(day)), None)


def next_season_change(plan: Plan, day: date) -> date | None:
    """Return the first day of the next season.

    Within a season that's the day after it ends, even if Ecotricity hasn't published the next
    season yet, because that's when prices or time-of-use windows can change. Outside any season
    it's the start of the next published one.
    """
    if (season := season_for(plan, day)) is not None:
        return season.end + timedelta(days=1)
    upcoming = [season.start for season in plan.seasons if season.start > day]
    return min(upcoming, default=None)


def next_bucket_change(
    slots: SlotMap, now: datetime, tz: tzinfo, until: datetime | None = None
) -> tuple[datetime, str] | None:
    """Return when the bucket next changes, and the bucket it changes to.

    Steps through half-hour boundaries for up to a week. Stops at `until` (the next season
    change), after which these slots may no longer apply. Steps are counted in UTC, so the hour
    skipped when daylight saving starts is skipped and the repeated one is visited twice.
    """
    now = now.astimezone(UTC)
    current = slots.bucket_at(now.astimezone(tz))
    boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(
        minutes=30 if now.minute < HALF_HOUR else 60
    )
    for step in range(SLOTS_PER_DAY * 7):
        instant = boundary + timedelta(minutes=30 * step)
        if until is not None and instant >= until:
            return None
        bucket = slots.bucket_at(instant.astimezone(tz))
        if bucket != current:
            return instant, bucket
    return None


def day_windows(slots: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """Group a day's half-hour slots into (start, end, bucket) windows.

    For example ("07:00", "11:00", "CHARGE_HIGH"). The last window ends at "24:00".
    """
    windows: list[tuple[str, str, str]] = []
    start = 0
    for slot in range(1, SLOTS_PER_DAY + 1):
        if slot == SLOTS_PER_DAY or slots[slot] != slots[start]:
            windows.append((_slot_time(start), _slot_time(slot), slots[start]))
            start = slot
    return windows


def _slot_time(slot: int) -> str:
    return f"{slot // 2:02d}:{30 * (slot % 2):02d}"

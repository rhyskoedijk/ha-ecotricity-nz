"""Test the API clients against the real Ecotricity services.

This calls the live onboarding site and, if credentials are set, the customer portal. It is not
part of the test suite and never runs in CI. Settings come from environment variables or a
git-ignored .env file in the repository root:

    ECOTRICITY_ICP=...           required
    ECOTRICITY_PLAN=...          optional, plan name as on the website (default: the site's default)
    ECOTRICITY_ACCOUNT=...       optional, portal customer account number
    ECOTRICITY_PASSWORD=...      optional, portal password

Usage: uv run python scripts/api_client_smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ecotricity_nz.api import EcotricityError, OnboardingClient, PortalClient
from custom_components.ecotricity_nz.api.timeframes import read_chart
from custom_components.ecotricity_nz.tariff import (
    chart_seasons,
    day_windows,
    plan_slots,
    price_day,
    season_charts,
    side_alignment,
)

if TYPE_CHECKING:
    from custom_components.ecotricity_nz.api.models import Plan
    from custom_components.ecotricity_nz.api.timeframes import ChartReading

NZ = ZoneInfo("Pacific/Auckland")
BERLIN = ZoneInfo("Europe/Berlin")
USER_AGENT = "ecotricity_nz smoke test"


def _load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _mask(text: str) -> str:
    return text[:4] + "*" * (len(text) - 6) + text[-2:] if len(text) > 6 else "***"  # noqa: PLR2004 - shortest value worth partly masking


async def _async_read_charts(
    onboarding: OnboardingClient, plan: Plan, today: date
) -> dict[str, ChartReading]:
    """Read every season's charts, as the integration does: periods are worked out across them."""
    charts: dict[str, ChartReading] = {}
    for season in chart_seasons(plan, today, upcoming=True):
        for path in season_charts(plan, season):
            try:
                charts[path] = read_chart(await onboarding.async_get_chart(path))
            except EcotricityError as err:
                print(f"Chart {path}: {err}")
    return charts


def _print_pricing(plan: Plan, today: date, charts: dict[str, ChartReading]) -> None:
    """Print today's periods, windows and daily charge, as the integration works them out."""
    slots = plan_slots(charts, plan, today)
    for error in (*slots.errors, *slots.later_errors):
        print(f"Charts: {error}")
    alignments = (
        side_alignment(plan, today, slots.seasons(export=False), export=False),
        side_alignment(plan, today, slots.seasons(export=True), export=True),
    )
    pricing = price_day(plan, today, slots.today[False], slots.today[True], alignments)
    print(f"Plan {plan.name} ({plan.description}), today (cents excl GST):")
    for label, side, alignment in (
        ("import", pricing.import_side, alignments[0]),
        ("export", pricing.export_side, alignments[1]),
    ):
        if side is None:
            continue
        print(f"  {label} periods ({alignment.value}): {side.buckets}")
        for day_type, day in (("weekday", side.slots.weekday), ("weekend", side.slots.weekend)):
            windows = ", ".join(
                f"{start}-{end} {slot.bucket} {slot.price:.2f}"
                for start, end, slot in day_windows(day)
            )
            print(f"  {label} {day_type}: {windows}")
    print(f"  daily {pricing.daily_total:.2f}")


async def main() -> int:
    """Run the smoke test."""
    _load_env()
    icp_number = os.environ.get("ECOTRICITY_ICP")
    if not icp_number:
        print("Set ECOTRICITY_ICP (and optionally ECOTRICITY_ACCOUNT, ECOTRICITY_PASSWORD).")
        return 2

    async with aiohttp.ClientSession() as session:
        onboarding = OnboardingClient(session, user_agent=USER_AGENT)
        icp = await onboarding.async_get_icp(icp_number)
        plans = (await onboarding.async_get_plans(icp)).with_titles(
            await onboarding.async_get_plan_titles()
        )
        print(
            f"ICP {_mask(icp.icp)}: {icp.network_name} ({icp.network_code}), {icp.price_category}"
        )
        print("Plans:", ", ".join(plan.name for plan in plans.all_plans))

        plan = plans.default
        if (wanted := os.environ.get("ECOTRICITY_PLAN")) and (match := plans.find_by_name(wanted)):
            plan = match
        account = os.environ.get("ECOTRICITY_ACCOUNT")
        password = os.environ.get("ECOTRICITY_PASSWORD")
        portal = None
        if account and password:
            async with aiohttp.ClientSession() as portal_session:
                portal = PortalClient(
                    portal_session,
                    account,
                    password,
                    user_agent=USER_AGENT,
                    local_tz=NZ,
                    server_tz=BERLIN,
                )
                indicator = await portal.async_get_indicator()
                print(f"Portal data {indicator.data_start} to {indicator.data_end}")
                print(f"Last meter data update {indicator.last_import}")
                if indicator.data_end:
                    data = await portal.async_get_half_hourly(
                        indicator.data_end - timedelta(days=1), indicator.data_end
                    )
                    print(
                        f"Last day: {len(data.imported_kwh)} readings, "
                        f"{sum(r.value for r in data.imported_kwh):.2f} kWh imported, "
                        f"{sum(r.value for r in data.exported_kwh):.2f} kWh exported"
                    )

        today = datetime.now(NZ).date()
        _print_pricing(plan, today, await _async_read_charts(onboarding, plan, today))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

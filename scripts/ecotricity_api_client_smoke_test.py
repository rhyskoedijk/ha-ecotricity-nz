"""
Test the API clients against the real Ecotricity services.

This calls the live onboarding site and, if credentials are set, the customer portal.
It is not part of the test suite and never runs in CI. Settings come from environment variables
or a git-ignored .env file in the repository root:

    ECOTRICITY_ICP=...           required
    ECOTRICITY_PLAN=...          optional, plan name as on the website (default: the site's default)
    ECOTRICITY_ACCOUNT=...       optional, portal customer account number
    ECOTRICITY_PASSWORD=...      optional, portal password

Usage: uv run python scripts/ecotricity_api_client_smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ecotricity_nz.api import OnboardingClient, PortalClient
from custom_components.ecotricity_nz.api.timeframes import (
    chart_path,
    match_tiers,
    read_chart,
)
from custom_components.ecotricity_nz.tariff import price_day, season_for

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
        pricing = price_day(plan, today)
        print(f"Plan {plan.name} ({plan.description}), prices today (cents excl GST):")
        print("  import", pricing.import_buckets)
        print("  export", pricing.export_buckets)
        print(f"  daily {pricing.daily_total:.2f}")

        season = season_for(plan, today)
        for label, template in (
            ("weekday", plan.timeframe_chart_weekday),
            ("weekend", plan.timeframe_chart_weekend),
        ):
            if not template:
                continue
            path = chart_path(template, season.start, season.end) if season else template
            reading = read_chart(await onboarding.async_get_chart(path))
            slots = match_tiers(reading.import_tiers, pricing.import_buckets)
            changes = [
                f"{slot // 2:02d}:{30 * (slot % 2):02d} {bucket}"
                for slot, bucket in enumerate(slots)
                if slot == 0 or slots[slot - 1] != bucket
            ]
            print(f"  {label} import windows: {', '.join(changes)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

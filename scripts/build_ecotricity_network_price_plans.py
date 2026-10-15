"""Build a network's test fixtures from Ecotricity's onboarding site, without a real ICP.

The plan list service doesn't need an ICP, only a network and price category, so this never sends
one. It saves one price category per network: the most common residential category in
tests/fixtures/network_price_categories.json, unless --category picks another. Before saving, it
fetches the plan lists of the network's next most common categories (--compare) and reports
whether they differ in shape (plans, seasons, charge codes, descriptions, units or validity dates)
or only in prices. A category that differs in shape deserves its own fixture; run the script
again with --category for it.

It then downloads the weekday and weekend timeframe charts of every plan, for every season,
keeping one chart per distinct image (later seasons often reuse an earlier season's chart).

The onboarding site blocks an IP for about 24 hours after a burst of a few hundred requests, so
every request waits --delay seconds, and the script stops at the first error without writing
anything. A network takes roughly 40 to 80 requests.

Usage: uv run python scripts/build_ecotricity_network_price_plans.py NETWORK
       [--category CODE] [--compare N] [--delay SECONDS]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ecotricity_nz.api import OnboardingClient
from custom_components.ecotricity_nz.api.exceptions import EcotricityError
from custom_components.ecotricity_nz.api.onboarding import PLAN_PATH

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
NETWORKS = FIXTURES / "networks"
CATEGORIES = FIXTURES / "network_price_categories.json"
USER_AGENT = "ecotricity_nz test fixture builder"
# The register codes of a typical solar ICP. The plan list doesn't change with them as far as
# has been seen, but the web app always sends some.
METER_TYPE = "UN,EG"


class Paced:
    """Wait between requests, so a whole network doesn't arrive as one burst."""

    def __init__(self, delay: float) -> None:
        """Initialise with the delay in seconds."""
        self._delay = delay
        self._first = True
        self.count = 0

    async def wait(self, what: str) -> None:
        """Wait before the next request (not before the first), and log what it's for."""
        if not self._first:
            await asyncio.sleep(self._delay)
        self._first = False
        self.count += 1
        print(f"  [{self.count}] {what}")


def _plans(raw: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - raw JSON
    content = raw["content"][0]
    plans = [content["default"], *(content["plan_list"] or []), content["alternative"]]
    return [plan for plan in plans if plan]


def _shape(raw: Any) -> dict[str, Any]:  # noqa: ANN401 - raw JSON
    """Return what the integration parses from a plan list, leaving out prices and IDs."""
    shape: dict[str, Any] = {}
    for plan in _plans(raw):
        rates = sorted(
            (
                "network" if "priceplan_id" in rate else "retail",
                rate.get("charge") or "",
                rate.get("charge_description") or "",
                rate.get("register_type") or "",
                rate.get("unit") or "",
                rate.get("valid_start") or "",
                rate.get("valid_end") or "",
            )
            for rate in [*plan["retail_rates"], *plan["network_rates"]]
        )
        seasons = sorted((season["start"], season["end"]) for season in plan["seasons"])
        shape[plan["price_plan"]] = (plan["price_plan_description"], seasons, rates)
    return shape


def _prices(raw: Any) -> dict[tuple[str, ...], float]:  # noqa: ANN401 - raw JSON
    return {
        (plan["price_plan"], rate.get("charge") or "", rate.get("charge_description") or ""): rate[
            "price"
        ]
        for plan in _plans(raw)
        for rate in [*plan["retail_rates"], *plan["network_rates"]]
    }


async def _plan_list(client: OnboardingClient, pace: Paced, network: str, category: str) -> Any:  # noqa: ANN401 - raw JSON
    await pace.wait(f"plan list {network}/{category}")
    params = {
        "networkCode": network,
        "capacityCode": category,
        "meterType": METER_TYPE,
        "customerType": "Residential",
    }
    # The client's public method needs ICP details; the service itself doesn't.
    raw = await client._async_gated_get(PLAN_PATH, "Plan list", params)  # noqa: SLF001
    plans = _plans(raw)
    if not plans:
        sys.exit(f"{network}/{category}: Ecotricity returned no plans")
    if {plan["network"] for plan in plans} != {network} or {
        plan["price_category"] for plan in plans
    } != {category}:
        sys.exit(f"{network}/{category}: the plan list is for another network or category")
    return raw


def _compare(category: str, raw: Any, other: str, other_raw: Any) -> None:  # noqa: ANN401 - raw JSON
    shape, other_shape = _shape(raw), _shape(other_raw)
    if shape != other_shape:
        print(f"  {other}: DIFFERS IN SHAPE from {category}, consider a fixture for it too")
        for code in sorted(shape.keys() ^ other_shape.keys()):
            print(f"    plan {code} is only in one of them")
        for code in sorted(shape.keys() & other_shape.keys()):
            if shape[code] != other_shape[code]:
                print(f"    plan {code} has different seasons or rates")
        return
    prices, other_prices = _prices(raw), _prices(other_raw)
    changed = sorted(key for key in prices if prices[key] != other_prices.get(key))
    if changed:
        print(
            f"  {other}: same shape as {category}, {len(changed)} prices differ (no fixture needed)"
        )
        for key in changed:
            print(f"    {' / '.join(key)}: {prices[key]} vs {other_prices.get(key)}")
    else:
        print(f"  {other}: identical to {category} apart from IDs (no fixture needed)")


async def _charts(
    client: OnboardingClient,
    pace: Paced,
    raw: Any,  # noqa: ANN401 - raw JSON
) -> dict[str, bytes]:
    charts: dict[str, bytes] = {}
    for plan in _plans(raw):
        for key in ("timeframes_chart_wd", "timeframes_chart_we"):
            seen: set[str] = set()
            for season in sorted(plan["seasons"], key=lambda season: season["start"]):
                path = (
                    plan[key]
                    .replace("SSSSSSSS", season["start"].replace("-", ""))
                    .replace("EEEEEEEE", season["end"].replace("-", ""))
                    .replace(".png", "-NOGST.png")
                )
                await pace.wait(path.rsplit("/", 1)[1])
                image = await client.async_get_chart(path)
                digest = hashlib.sha256(image).hexdigest()
                if digest not in seen:
                    seen.add(digest)
                    charts[path.rsplit("/", 1)[1]] = image
    return charts


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("network", help="distributor code, such as VECT")
    parser.add_argument("--category", help="price category to save (default: the most common)")
    parser.add_argument(
        "--compare",
        type=int,
        default=1,
        help="how many of the next most common categories to compare with it (default: 1)",
    )
    parser.add_argument(
        "--delay", type=float, default=5, help="seconds between requests (default: 5)"
    )
    return parser.parse_args()


async def main() -> int:
    """Build the fixture folder for one network and price category."""
    # Print each line as it happens, even when the output goes to a file or pipe, so a long
    # paced run shows its progress and its end.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    args = _arguments()
    network = args.network.upper()
    networks = json.loads(CATEGORIES.read_text(encoding="utf-8"))["networks"]
    if network not in networks:
        sys.exit(f"{network} isn't in {CATEGORIES.name}; refresh it first")
    ranked = list(networks[network]["price_categories"])
    category = args.category or ranked[0]
    folder = NETWORKS / network / category
    if folder.exists():
        sys.exit(f"{folder} already exists; delete it first to rebuild it")
    others = [code for code in ranked if code != category][: max(args.compare, 0)]

    pace = Paced(args.delay)
    async with aiohttp.ClientSession() as session:
        client = OnboardingClient(session, user_agent=USER_AGENT)
        try:
            raw = await _plan_list(client, pace, network, category)
            print(f"{network}/{category}: {', '.join(p['price_plan'] for p in _plans(raw))}")
            for other in others:
                _compare(category, raw, other, await _plan_list(client, pace, network, other))
            charts = await _charts(client, pace, raw)
        except EcotricityError as err:
            print(f"Stopped after {pace.count} requests, nothing was saved: {err}")
            return 1

    (folder / "charts").mkdir(parents=True)
    (folder / "plan_list.json").write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for name, image in charts.items():
        (folder / "charts" / name).write_bytes(image)
    print(f"Saved {folder.relative_to(Path.cwd())}: {len(charts)} charts, {pace.count} requests")
    print(f"Check it with: uv run pytest tests/test_network_fixtures.py -k {network}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Refresh tests/fixtures/network_price_categories.json from Electricity Authority registry data.

The Electricity Authority publishes "Distribution price category codes by NSP" daily, built from
the registry. This script downloads the newest file (or reads one given on the command line),
totals the residential ICPs for each price category on each distributor's own network supply
points, and rewrites the fixture. Network names already in the fixture are kept; a new network
is named from the dataset's reporting region, so check it by hand.

It then reports what changed, and which networks and price categories have no fixtures yet under
tests/fixtures/networks. It only calls the Electricity Authority's site, never Ecotricity's.

Usage: uv run python scripts/build_ea_network_price_categories.py [path/to/file.csv]
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DATASET_PAGE = (
    "https://www.emi.ea.govt.nz/Retail/Datasets/MarketStructure/DistributionPriceCategoryCodes"
)
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUTPUT = FIXTURES / "network_price_categories.json"
NETWORKS = FIXTURES / "networks"
# A distributor's own NSPs end in its four-letter code and GN, for example CPK0111CKHKGN.
# Other NSPs, such as embedded networks, are left out.
DISTRIBUTOR_NSP = re.compile(r".{7}([A-Z]{4})GN")
REGION_NAME = re.compile(r"\(([^)]+)\)\s*$")
USER_AGENT = "ecotricity_nz test fixture builder"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - fixed https URL
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
        return bytes(response.read())


def _download() -> tuple[str, str]:
    page = _get(DATASET_PAGE).decode("utf-8", "replace")
    links = sorted(set(re.findall(r'href="([^"]*DistributionPriceCategoryCodesByNSP\.csv)"', page)))
    if not links:
        sys.exit(f"No dataset file found on {DATASET_PAGE}")
    link = links[-1]  # Names start with the date, so the last is the newest.
    url = link if link.startswith("http") else "https://www.emi.ea.govt.nz" + link
    return url.rsplit("/", 1)[1], _get(url).decode("utf-8-sig")


def _read(text: str) -> tuple[dict[str, Counter[str]], dict[str, str]]:
    categories: dict[str, Counter[str]] = defaultdict(Counter)
    regions: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        match = DISTRIBUTOR_NSP.fullmatch(row["NSP"].strip())
        if not match:
            continue
        code = match.group(1)
        if region := REGION_NAME.search(row["Network reporting region"]):
            regions.setdefault(code, region.group(1).strip())
        residential = int(row["ICPs (Residential)"] or 0)
        if residential:
            categories[code][row["Distributor price category code"].strip()] += residential
    return categories, regions


def _covered() -> dict[str, set[str]]:
    """Return the price categories with a plan_list.json fixture, for each network's folder."""
    return {
        folder.name: {path.parent.name for path in folder.glob("*/plan_list.json")}
        for folder in sorted(path for path in NETWORKS.iterdir() if path.is_dir())
    }


def _report_changes(old: dict[str, Any], networks: dict[str, Any]) -> None:
    """Print the networks and price categories added or gone since the last refresh."""
    for code in sorted(networks.keys() - old.keys()):
        print(f"New network: {code} ({networks[code]['name']}), check its name")
    for code in sorted(old.keys() - networks.keys()):
        print(f"Network no longer in the registry data: {code}")
    for code in sorted(networks.keys() & old.keys()):
        before = set(old[code].get("price_categories", {}))
        after = set(networks[code]["price_categories"])
        if added := sorted(after - before):
            print(f"{code}: new price categories {', '.join(added)}")
        if removed := sorted(before - after):
            print(f"{code}: price categories gone {', '.join(removed)}")


def _report_coverage(networks: dict[str, Any]) -> None:
    """Print which networks and price categories have fixtures."""
    covered = _covered()
    print("\nFixture coverage (price categories with a {category}/plan_list.json fixture):")
    for code, network in networks.items():
        have = covered.get(code)
        if have is None:
            print(f"  {code}: no fixtures")
            continue
        listed = network["price_categories"]
        missing = [category for category in listed if category not in have]
        stale = sorted(have - listed.keys())
        line = f"  {code}: covers {', '.join(sorted(have & listed.keys())) or 'none'}"
        line += f"; missing {len(missing)} of {len(listed)}"
        if stale:
            line += f"; fixture categories not in the registry data: {', '.join(stale)}"
        print(line)
    for code in sorted(covered.keys() - networks.keys()):
        print(f"  {code}: fixture folder for a network not in the registry data")


def main() -> int:
    """Rewrite the fixture and report the changes and the gaps in coverage."""
    if len(sys.argv) > 1:
        source = Path(sys.argv[1])
        name, text = source.name, source.read_text(encoding="utf-8-sig")
    else:
        name, text = _download()
    categories, regions = _read(text)

    old = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    old_networks = old.get("networks", {})
    networks = {
        code: {
            "name": old_networks.get(code, {}).get("name") or regions.get(code, code),
            "price_categories": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
        }
        for code, counts in sorted(categories.items())
    }
    OUTPUT.write_text(
        json.dumps(
            {"source": DATASET_PAGE, "file": name, "networks": networks},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT.relative_to(Path.cwd())} from {name}: {len(networks)} networks")

    _report_changes(old_networks, networks)
    _report_coverage(networks)
    return 0


if __name__ == "__main__":
    sys.exit(main())

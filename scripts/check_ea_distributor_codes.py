"""Compare saved distributor codes with the latest Electricity Authority dataset."""

from __future__ import annotations

import csv
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

DATASET_PAGE = (
    "https://www.ea.govt.nz/data-and-insights/datasets/retail/market-structure/"
    "icp-and-metering-details/"
)
DATASET_BASE = (
    "https://emidatasets.blob.core.windows.net/publicdata/Datasets/Retail/"
    "MarketStructure/ICPandMeteringDetails/"
)
DATASET_FILENAME = re.compile(
    r"(?P<date>\d{8})_MeteringConfigurationsInclDistributionPriceCatCode\.csv$"
)
NETWORK_CODE = re.compile(r"[A-Z]{4}GN$")
# This code appears on only two ICPs in the EA export and is not a lines company in the ENA list.
EXCLUDED_CODES = {"TRPG"}
NETWORK_CODES_FILE = Path(__file__).parents[1] / "tests/fixtures/network_codes.json"


class _DatasetLinks(HTMLParser):
    """Collect links to dated EA metering-configuration exports."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href and DATASET_FILENAME.search(href):
            self.links.append(href)


def latest_dataset_url(page: str) -> str:
    """Return the newest metering-configuration CSV linked from the EA page."""
    links = _DatasetLinks()
    links.feed(page)
    dated_links = [
        (match.group("date"), link)
        for link in links.links
        if (match := DATASET_FILENAME.search(link)) is not None
    ]
    if not dated_links:
        raise ValueError("The EA dataset page did not link a dated metering-configuration CSV")
    return max(dated_links)[1]


def network_codes_from_csv(content: str) -> set[str]:
    """Extract distribution network codes from the EA network service provider identifiers."""
    codes: set[str] = set()
    for row in csv.DictReader(content.splitlines()):
        nsp = row["NSP"].strip()
        if NETWORK_CODE.search(nsp):
            codes.add(nsp[-6:-2])
    return codes - EXCLUDED_CODES


def _fetch(url: str) -> str:
    if url != DATASET_PAGE and not url.startswith(DATASET_BASE):
        raise ValueError(f"Refusing to fetch an unexpected EA dataset URL: {url}")
    request = Request(  # noqa: S310 - URL is restricted to the EA page and its dataset container
        url, headers={"User-Agent": "ha-ecotricity-nz network-code check"}
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - trusted EA URLs only
        return response.read().decode("utf-8-sig")


def main() -> int:
    """Fail if the saved mapping no longer covers the codes in the latest EA dataset."""
    page = _fetch(DATASET_PAGE)
    dataset_url = latest_dataset_url(page)
    actual_codes = network_codes_from_csv(_fetch(dataset_url))
    saved_mapping = json.loads(NETWORK_CODES_FILE.read_text(encoding="utf-8"))
    saved_codes = set(saved_mapping)

    missing = sorted(actual_codes - saved_codes)
    retired = sorted(saved_codes - actual_codes)
    if missing or retired:
        print("The network code mapping is out of date.", file=sys.stderr)
        if missing:
            print(f"New codes to add: {', '.join(missing)}", file=sys.stderr)
        if retired:
            print(f"Codes to review or remove: {', '.join(retired)}", file=sys.stderr)
        return 1

    print(f"Network code mapping matches the latest EA dataset ({len(saved_codes)} codes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

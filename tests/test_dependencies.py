"""Development dependencies stay in step with the Home Assistant version tests run on."""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

import homeassistant
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION


def test_frontend_matches_the_home_assistant_tests_run_on() -> None:
    # The integration depends on `frontend`, which the test harness doesn't install, so the dev
    # dependency must be the version that Home Assistant release ships.
    constraints = Path(homeassistant.__file__).parent / "package_constraints.txt"
    pinned = [
        line.strip()
        for line in constraints.read_text(encoding="utf-8").splitlines()
        if line.startswith("home-assistant-frontend==")
    ]

    assert pinned == [f"home-assistant-frontend=={metadata.version('home-assistant-frontend')}"]


def test_hacs_minimum_is_the_release_tests_run_on() -> None:
    # The Marketplace offers the integration to every Home Assistant version from this one, so it
    # must be the monthly release the `pytest-homeassistant-custom-component` pin tests against.
    hacs = json.loads((Path(__file__).parents[1] / "hacs.json").read_text(encoding="utf-8"))
    year, month, _ = hacs["homeassistant"].split(".")

    assert (int(year), int(month)) == (MAJOR_VERSION, MINOR_VERSION)

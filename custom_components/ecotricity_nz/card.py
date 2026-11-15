"""Serve the bill card, so dashboards can use it without installing anything else.

The card is built from frontend/ into this package's frontend folder (see docs/development.md),
served from the integration's own URL, and loaded on every dashboard. Its URL carries a hash of
its contents, so a new version gets a new URL when Home Assistant restarts. Browsers check it's
still current each time instead of caching it for weeks, so a rebuild while developing shows on
the next reload too.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http.server import StaticPathConfig

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

CARD_URL = f"/{DOMAIN}/ecotricity-bill-card.js"
CARD_PATH = Path(__file__).parent / "frontend" / "ecotricity-bill-card.js"
# Enough of the hash to tell versions apart.
_VERSION_LENGTH = 12


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the bill card and load it on every dashboard, if it's been built."""
    path = CARD_PATH
    version = await hass.async_add_executor_job(_version, path)
    if version is None:
        LOGGER.warning("The bill card isn't available: %s hasn't been built", path.name)
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(path), cache_headers=False)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")


def _version(path: Path) -> str | None:
    """Return a hash of the card's contents, or None if it hasn't been built."""
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(content).hexdigest()[:_VERSION_LENGTH]

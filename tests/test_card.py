"""Tests for serving the bill card."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.setup import async_setup_component

from custom_components.ecotricity_nz import card as card_module
from custom_components.ecotricity_nz.const import DOMAIN

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator


async def test_card_is_served_and_loaded_on_every_dashboard(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, tmp_path: Path
) -> None:
    bundle = tmp_path / "ecotricity-bill-card.js"
    bundle.write_text("customElements.define('ecotricity-bill-card', class {});")
    with patch.object(card_module, "CARD_PATH", bundle):
        assert await async_setup_component(hass, DOMAIN, {})

    urls = [url for url in hass.data[DATA_EXTRA_MODULE_URL].urls if "ecotricity" in url]
    assert len(urls) == 1
    url, version = urls[0].split("?v=")
    assert url == card_module.CARD_URL
    assert version

    client = await hass_client()
    response = await client.get(urls[0])
    assert response.status == 200
    assert "ecotricity-bill-card" in await response.text()


def test_new_card_gets_a_new_url(tmp_path: Path) -> None:
    # Browsers cache the card, so a new build must change its URL.
    bundle = tmp_path / "ecotricity-bill-card.js"
    bundle.write_text("first")
    first = card_module._version(bundle)
    bundle.write_text("second")
    assert first != card_module._version(bundle)


async def test_card_that_hasnt_been_built_is_skipped(
    hass: HomeAssistant, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with patch.object(card_module, "CARD_PATH", tmp_path / "missing.js"):
        assert await async_setup_component(hass, DOMAIN, {})

    assert not [url for url in hass.data[DATA_EXTRA_MODULE_URL].urls if "ecotricity" in url]
    assert "The bill card isn't available" in caplog.text

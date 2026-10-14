"""Tests for extracting distributor codes from the EA metering dataset."""

from __future__ import annotations

import pytest

from scripts.check_ea_distributor_codes import latest_dataset_url, network_codes_from_csv


def test_latest_dataset_url_selects_newest_export() -> None:
    page = """
        <a href="https://data.example/20260831_MeteringConfigurationsInclDistributionPriceCatCode.csv">
            August
        </a>
        <a href="https://data.example/20261001_MeteringConfigurationsInclDistributionPriceCatCode.csv">
            October
        </a>
    """

    assert (
        latest_dataset_url(page)
        == "https://data.example/20261001_MeteringConfigurationsInclDistributionPriceCatCode.csv"
    )


def test_latest_dataset_url_requires_a_dated_export() -> None:
    with pytest.raises(ValueError, match="did not link"):
        latest_dataset_url("<html></html>")


def test_network_codes_from_csv_ignores_embedded_and_outlier_identifiers() -> None:
    content = """NSP,Network reporting region,ICP count
CPK0111CKHKGN,Wellington,200
TWC2201TRPGGN,Manawatu,2
APC0011KIPTEN,Auckland,10
"""

    assert network_codes_from_csv(content) == {"CKHK"}

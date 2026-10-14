"""Tests for the response parsers."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.ecotricity_nz.api import (
    EcotricityResponseError,
    IcpNotFoundError,
    RateUnit,
)
from custom_components.ecotricity_nz.api.parsers import (
    localise_wall_time,
    parse_icp,
    parse_indicator,
    parse_plan_list,
    parse_plan_titles,
    parse_values,
    portal_wall_time,
)
from tests.conftest import TEST_ICP, load_json_fixture

NZ = ZoneInfo("Pacific/Auckland")


def test_parse_icp() -> None:
    icp = parse_icp(load_json_fixture("icp.json"))

    assert icp.icp == TEST_ICP
    assert icp.network_code == "CKHK"
    assert icp.network_name == "Wellington Electricity Lines"
    assert icp.price_category == "RTOU24"
    assert icp.retailer_code == "ECOT"
    assert icp.register_codes == ("UN", "EG", "7304", "7304")
    assert icp.meter_serial_numbers == ("900000001",)
    assert icp.has_export
    assert icp.address == "1 Example Road, Testville, Sampletown 9999"


@pytest.mark.parametrize(
    "payload",
    [
        {"returnCode": {"ID": 1, "name": "NOT_FOUND"}, "content": None},
        {"returnCode": {"ID": 0}, "content": {}},
        "not json object",
    ],
)
def test_parse_icp_not_found(payload: object) -> None:
    with pytest.raises((IcpNotFoundError, EcotricityResponseError)):
        parse_icp(payload)


def test_parse_icp_single_item_lists() -> None:
    payload = load_json_fixture("icp.json")
    content = payload["content"]
    component = content["metering"]["allMeteringComponents"]["MeteringComponent"][0]
    component["allMeteringChannels"]["MeteringChannel"] = component["allMeteringChannels"][
        "MeteringChannel"
    ][0]
    content["metering"]["allMeteringComponents"]["MeteringComponent"] = component

    assert parse_icp(payload).register_codes == ("UN",)


def test_parse_plan_list() -> None:
    plans = parse_plan_list(load_json_fixture("plan_list.json"))

    assert plans.default.description == "Eco Off Peak"
    assert plans.alternative is not None
    assert plans.alternative.description == "Low Off Peak"
    names = [plan.description for plan in plans.all_plans]
    assert names[0] == "Eco Off Peak"
    assert "Low User" in names
    assert len(names) == len(set(names))

    levy = next(rate for rate in plans.default.retail_rates if rate.is_levy)
    assert levy.unit is RateUnit.PER_KWH
    assert levy.price == pytest.approx(0.16)

    export = next(rate for rate in plans.default.retail_rates if rate.is_export)
    assert export.price < 0

    daily = [rate for rate in plans.default.rates if rate.unit is RateUnit.PER_DAY]
    assert daily

    seasonal = next(rate for rate in plans.default.network_rates if rate.valid_end)
    assert seasonal.applies_on(seasonal.valid_end)
    assert not seasonal.applies_on(seasonal.valid_end + timedelta(days=1))

    assert plans.default.seasons[0].contains(date(2026, 5, 1))
    assert plans.default.timeframe_chart_weekday is not None


def test_find_plans() -> None:
    plans = parse_plan_list(load_json_fixture("plan_list.json"))

    assert plans.find_by_name("  low   USER ") is not None
    assert plans.find_by_name("Nonexistent") is None
    assert plans.find(plans.default.id) is plans.default
    assert plans.find("missing") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"returnCode": {"ID": 5, "name": "ERROR"}},
        {"returnCode": {"ID": 0}, "content": [{}]},
        {"returnCode": {"ID": 0}, "content": []},
    ],
)
def test_parse_plan_list_errors(payload: object) -> None:
    with pytest.raises(EcotricityResponseError):
        parse_plan_list(payload)


def test_portal_wall_time_drops_offset() -> None:
    assert portal_wall_time("2026-10-08T07:30:00.000+02:00") == datetime(2026, 10, 8, 7, 30)


def test_localise_wall_time_dst() -> None:
    # The repeated hour when daylight saving ends takes the later fold once time has passed it.
    first = localise_wall_time(datetime(2026, 4, 5, 2, 30), NZ)
    assert first == datetime(2026, 4, 4, 13, 30, tzinfo=UTC)
    second = localise_wall_time(datetime(2026, 4, 5, 2, 30), NZ, first)
    assert second == datetime(2026, 4, 4, 14, 30, tzinfo=UTC)
    # Wall-clock times skipped when daylight saving starts don't exist.
    assert localise_wall_time(datetime(2026, 9, 27, 2, 30), NZ) is None


def test_parse_values_across_dst_end() -> None:
    data = parse_values(load_json_fixture("portal_values_dst_end.json"), NZ)

    assert len(data.imported_kwh) == 50
    assert len(data.exported_kwh) == 50
    assert len(data.carbon_savings_kg) == 50
    starts = [reading.start for reading in data.imported_kwh]
    assert starts == sorted(starts)
    assert all(b - a == timedelta(minutes=30) for a, b in pairwise(starts))
    assert starts[0] == datetime(2026, 4, 4, 11, 0, tzinfo=UTC)
    assert data.imported_kwh[-1].end == datetime(2026, 4, 5, 12, 0, tzinfo=UTC)
    assert data.imported_kwh[0].value == pytest.approx(0.5)
    assert not data.is_empty


def test_parse_values_skips_bad_items() -> None:
    payload = {
        "indicatorValueResponses": [
            {
                "featureValueResponses": [
                    {
                        "featureID": "POWER_ACTIVE_PURCHASE_CONSUMPTION",
                        "values": [
                            {"value": None, "measuringPeriod": {"a": "b"}},
                            {"value": 1.0, "measuringPeriod": {}},
                            {
                                "value": 1.0,
                                "measuringPeriod": {
                                    "2026-09-27T02:00:00.000+02:00": "2026-09-27T02:30:00.000+02:00"
                                },
                            },
                            {
                                "value": 2.0,
                                "measuringPeriod": {
                                    "2026-09-27T03:00:00.000+02:00": "2026-09-27T03:30:00.000+02:00"
                                },
                            },
                        ],
                    }
                ]
            }
        ]
    }
    data = parse_values(payload, NZ)
    assert [reading.value for reading in data.imported_kwh] == [2.0]
    assert parse_values({}, NZ).is_empty
    with pytest.raises(EcotricityResponseError):
        parse_values([], NZ)


def _import_payload(*periods: tuple[str, str]) -> dict[str, object]:
    """Return a valueRequest response with one import reading per (from, to) wall-clock pair."""
    return {
        "indicatorValueResponses": [
            {
                "featureValueResponses": [
                    {
                        "featureID": "POWER_ACTIVE_PURCHASE_CONSUMPTION",
                        "values": [
                            {"value": 1.0, "measuringPeriod": {start: end}}
                            for start, end in periods
                        ],
                    }
                ]
            }
        ]
    }


@pytest.mark.parametrize(
    "gap_end",
    [
        # Wall-clock arithmetic gives a time that never happened.
        "2026-09-27T02:00:00.000+02:00",
        # The true local end.
        "2026-09-27T03:00:00.000+02:00",
    ],
)
def test_parse_values_across_dst_start(gap_end: str) -> None:
    # Clocks go from 02:00 to 03:00, so the 01:30 reading ends in the skipped hour. It's kept,
    # or the 01:00 hour would be half-filled and hold back the statistics import.
    payload = _import_payload(
        ("2026-09-27T01:00:00.000+02:00", "2026-09-27T01:30:00.000+02:00"),
        ("2026-09-27T01:30:00.000+02:00", gap_end),
        ("2026-09-27T03:00:00.000+02:00", "2026-09-27T03:30:00.000+02:00"),
    )
    readings = parse_values(payload, NZ).imported_kwh

    assert [(r.start, r.end) for r in readings] == [
        (datetime(2026, 9, 26, 13, 0, tzinfo=UTC), datetime(2026, 9, 26, 13, 30, tzinfo=UTC)),
        (datetime(2026, 9, 26, 13, 30, tzinfo=UTC), datetime(2026, 9, 26, 14, 0, tzinfo=UTC)),
        (datetime(2026, 9, 26, 14, 0, tzinfo=UTC), datetime(2026, 9, 26, 14, 30, tzinfo=UTC)),
    ]


def test_parse_values_end_before_start_takes_half_an_hour() -> None:
    # The second 02:30 can't end at either 02:00, so it's given the usual reading length.
    payload = _import_payload(
        ("2026-04-05T02:30:00.000+02:00", "2026-04-05T02:00:00.000+02:00"),
        ("2026-04-05T02:30:00.000+02:00", "2026-04-05T02:00:00.000+02:00"),
    )
    readings = parse_values(payload, NZ).imported_kwh

    assert [(r.start, r.end) for r in readings] == [
        (datetime(2026, 4, 4, 13, 30, tzinfo=UTC), datetime(2026, 4, 4, 14, 0, tzinfo=UTC)),
        (datetime(2026, 4, 4, 14, 30, tzinfo=UTC), datetime(2026, 4, 4, 15, 0, tzinfo=UTC)),
    ]


def test_parse_indicator_range_ending_in_skipped_hour() -> None:
    payload = {
        "currentIndicator": {
            "indicatorID": "ind",
            "features": {
                "POWER_ACTIVE_PURCHASE_CONSUMPTION": [
                    {
                        "measuredValueRange": {
                            "2026-09-01T00:00:00.000+02:00": "2026-09-27T02:00:00.000+02:00"
                        }
                    }
                ]
            },
        }
    }
    indicator = parse_indicator(payload, NZ)

    assert indicator.data_start == datetime(2026, 9, 1, 0, 0, tzinfo=NZ)
    assert indicator.data_end == datetime(2026, 9, 27, 3, 0, tzinfo=NZ)


def test_parse_indicator() -> None:
    indicator = parse_indicator(load_json_fixture("portal_init_data.json"), NZ)

    assert indicator.indicator_id == "00000000-0000-0000-0000-000000000001"
    assert indicator.display_name == TEST_ICP
    assert indicator.data_start == datetime(2026, 3, 1, 0, 0, tzinfo=NZ)
    assert indicator.data_end == datetime(2026, 4, 9, 0, 0, tzinfo=NZ)
    assert indicator.last_import == datetime(2026, 4, 10, 3, 6, 24, 0, tzinfo=NZ)
    assert indicator.features == {
        "POWER_ACTIVE_PURCHASE_CONSUMPTION",
        "POWER_ACTIVE_SUPPLY_CONSUMPTION",
        "EMISSION|POWER_ACTIVE_PURCHASE_CONSUMPTION|Carbon Savings",
    }


@pytest.mark.parametrize(
    "payload", [{}, {"currentIndicator": {}}, {"currentIndicator": "x"}, ["x"]]
)
def test_parse_indicator_errors(payload: object) -> None:
    with pytest.raises(EcotricityResponseError):
        parse_indicator(payload, NZ)


def test_plan_titles() -> None:
    titles = parse_plan_titles(load_json_fixture("plan_mappings.json"))

    assert titles["lowbattery user"] == "Low ecoSOLAR"
    assert "default" not in titles

    plans = parse_plan_list(load_json_fixture("plan_list.json")).with_titles(titles)
    battery = plans.find_by_name("Low ecoSOLAR")
    assert battery is plans.find_by_name("LowBATTERY User")
    assert battery is not None
    assert battery.name == "Low ecoSOLAR"
    assert battery.description == "LowBATTERY User"
    assert plans.default.name == "ecoSAVER"
    assert plans.alternative is not None
    assert plans.alternative.name == "Low ecoSAVER"


def test_plan_without_title_uses_description() -> None:
    plans = parse_plan_list(load_json_fixture("plan_list.json")).with_titles({})

    assert plans.default.name == "Eco Off Peak"


def test_plan_titles_skip_bad_entries() -> None:
    payload = {"mappings": ["junk", {"name": "", "title": "x"}, {"name": "a", "title": ""}]}
    assert parse_plan_titles(payload) == {}


@pytest.mark.parametrize("payload", [{}, {"mappings": "x"}, []])
def test_plan_titles_rejects_bad_payloads(payload: object) -> None:
    with pytest.raises(EcotricityResponseError):
        parse_plan_titles(payload)

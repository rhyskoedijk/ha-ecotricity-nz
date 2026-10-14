# Development

This page explains how to build and test the integration locally, for contributors only.

## Prerequisites

- Python 3.14 or later. `.python-version` pins the minor version, so uv uses 3.14 locally and in CI even when a newer Python is installed.
- [uv](https://docs.astral.sh/uv/) package and project manager.

## Setup

```bash
scripts/setup
```

This installs Home Assistant, the test harness and the tools into `.venv`, and installs the pre-commit hooks.

## Checks

```bash
uv run pytest                                  # all tests
uv run pytest --cov --cov-report=term-missing  # code coverage
uv run ruff format . && uv run ruff check .    # formatting and linting
uv run mypy custom_components                  # type checking (strict)
uv run pre-commit run --all-files              # git hook checks
```

CI runs the same commands and fails below 95% code coverage. The pre-commit hooks run in CI too, so a commit made without them installed is still checked.

Tests never touch the network. The API client tests mock HTTP responses, and the integration tests mock the clients using fixtures in `tests/fixtures`. Each network has its own folder under `tests/fixtures/networks`, named by its distributor code (e.g. `CKHK`, `ORON`), with its `plan_list.json`, its timeframe charts in `charts/`, and an `icp.json` where it has a test ICP. Most tests run against `CKHK`, the test ICP's network (`DEFAULT_NETWORK` in `tests/conftest.py`). The chart images are real Ecotricity charts; they contain only public pricing.

## Running Home Assistant locally

```bash
scripts/develop
```

This creates a `config/` directory (git-ignored), links the integration into `config/custom_components`, writes a minimal `configuration.yaml`, and starts Home Assistant. The first start takes a minute or two while Home Assistant installs its own runtime packages. Open http://localhost:8123, create a user, then go to Settings > Devices & services > Add integration > Ecotricity NZ.

Code changes need a restart: stop it with <kbd>Ctrl</kbd>+<kbd>C</kbd> and run `scripts/develop` again.

The first portal import backfills the whole history in the background, which can take a few minutes on an account with years of data. Watch for it in the log, then check Developer tools > Statistics for `ecotricity_nz:` entries.

## Trying a development build on an existing Home Assistant

Any one of these works:

- Copy `custom_components/ecotricity_nz` into the `custom_components` folder of your Home Assistant config directory, then restart.
- Download `ha_ecotricity_nz.zip` from a GitHub release and unzip it into `custom_components/ecotricity_nz`.
- In HACS, add this repository as a custom repository, then pick a branch or pre-release from the download dialog.

## Live smoke test

`scripts/api_client_smoke_test.py` runs the real API clients against the live sites and prints a summary: ICP details, plans, today's prices, the time-of-use windows read from the charts, and with a login, the last day of readings. It masks the ICP but prints prices and readings.

Create a `.env` file in the repository root (it's git-ignored):

```bash
ECOTRICITY_ICP=your ICP number
ECOTRICITY_PLAN=your plan, as named on the Ecotricity website
ECOTRICITY_ACCOUNT=your customer account number
ECOTRICITY_PASSWORD=your portal password
```

Everything except the ICP is optional. Without a plan, the site's default plan is used. Then:

```bash
uv run python scripts/api_client_smoke_test.py
```

Run it after changing anything in `custom_components/ecotricity_nz/api/`, and whenever the integration starts failing, to see whether Ecotricity has changed something.

## Refreshing fixtures

When Ecotricity's responses change, capture new ones with your browser's developer tools (Network tab, then Save all as HAR) into a `.captures/` folder in the repository root, which is git-ignored. Delete the captures once the fixtures are rebuilt.

Build fixtures from the captures without personal data:

- the ICP becomes `0000123456AB1C2`
- keep public network codes and names real so tests identify the network being covered; do not mask them in plan codes or chart paths
- replace personal identifiers and other customer-specific details with obviously fake values
- the address, region, meter serial, account number, contract number, registry audit numbers and portal IDs get obviously fake values

Timeframe chart images contain only prices and times. Keep the real network code in their filenames; replace any customer-specific values with obviously fake ones.

### Checking network codes

`tests/fixtures/network_codes.json` maps each distributor's four-letter code to its network name. Codes change rarely, so nothing checks the file automatically. To check it by hand:

1. Open the Electricity Authority's [ICP and metering details](https://www.ea.govt.nz/data-and-insights/datasets/retail/market-structure/icp-and-metering-details/) dataset page and download the newest `YYYYMMDD_MeteringConfigurationsInclDistributionPriceCatCode.csv`.
2. Take the distinct values of the `NSP` column that end in four capital letters followed by `GN`, for example `CPK0111CKHKGN`. The four letters before `GN` are the distributor code (`CKHK`). Ignore other NSPs, such as embedded networks.
3. Add any codes missing from `network_codes.json`, and review any that no longer appear.

### Adding a network

1. Create `tests/fixtures/networks/{code}/`, where `{code}` is the network's distributor code from `tests/fixtures/network_codes.json`.
2. Save the plan list response (`RESTPLAN/plan/list_unbundle`) for an ICP on that network as `plan_list.json`. Keep the rates and descriptions exactly as published, give the plans fake IDs, and leave the ICP out.
3. Save the plans' charts into `charts/`, named exactly as Ecotricity publishes them: the path from the plan's `timeframes_chart_wd` or `timeframes_chart_we`, with the season's start and end dates in place of `SSSSSSSS` and `EEEEEEEE`, and `-NOGST` before `.png`. These are the charts the integration reads.

`tests/test_network_fixtures.py` picks up the new folder by itself. It checks that the folder holds only that network's plans, and matches every chart against the prices worked out from that network's plans for the season and day type in the chart's name. A chart that doesn't match points to a pricing rule this network uses that the integration doesn't handle yet.

Before committing, search the whole repository (not just `tests/fixtures`) for your real ICP, street, suburb, account number, contract number and meter serial. Nothing should turn up. Public network codes and names are not personal data and should remain unchanged.

## Brand images

`custom_components/ecotricity_nz/brand/` holds the icon and logos Home Assistant shows for the integration. They follow the [home-assistant/brands](https://github.com/home-assistant/brands) image rules: transparent PNGs trimmed to their content, with `icon.png` at 256×256, `logo.png` 128 px tall, and `@2x` versions at double size. `dark_logo.png` is a white version for the dark theme; the icon works on both themes as it is.

They're rendered from the vector wordmark in the header of ecotricity.co.nz. The icon recreates Ecotricity's own favicon (the white wordmark on a `#333D47` circle) at full resolution.

## Releasing

The git tag is the only place the version is set. `manifest.json` and `pyproject.toml` stay at `0.0.0` in the repository, so an install from a branch shows `0.0.0` rather than a version it isn't.

1. In `CHANGELOG.md`, rename "Unreleased" to the new version and start a new "Unreleased" section.
2. Create a GitHub release with a tag like `v0.2.0`.
3. The CD workflow (`.github/workflows/cd.yml`) sets that version in `manifest.json`, zips `custom_components/ecotricity_nz` and attaches `ha_ecotricity_nz.zip`, which HACS installs.

Nothing is committed, pushed or released without the maintainer's approval.

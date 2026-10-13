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

Tests never touch the network. The API client tests mock HTTP responses, and the integration tests mock the clients using synthetic fixtures in `tests/fixtures`. The chart images there are real Ecotricity charts; they contain only public pricing.

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

`scripts/api_smoke_test.py` runs the real clients against the live sites and prints a summary: ICP details, plans, today's prices, the time-of-use windows read from the charts, and with a login, the last day of readings. It masks the ICP but prints prices and readings.

Create a `.env` file in the repository root (it's git-ignored):

```bash
ECOTRICITY_ICP=your ICP number
ECOTRICITY_PLAN=your plan, as named on the Ecotricity website
ECOTRICITY_ACCOUNT=your customer account number
ECOTRICITY_PASSWORD=your portal password
```

Everything except the ICP is optional. Without a plan, the site's default plan is used. Then:

```bash
uv run python scripts/api_smoke_test.py
```

Run it after changing anything in `custom_components/ecotricity_nz/api/`, and whenever the integration starts failing, to see whether Ecotricity has changed something.

## Refreshing fixtures

When Ecotricity's responses change, capture new ones with your browser's developer tools (Network tab, then Save all as HAR) into a `.captures/` folder in the repository root, which is git-ignored. Delete the captures once the fixtures are rebuilt.

Build fixtures from the captures with fake data throughout:

- the ICP becomes `0000123456AB1C2`
- the network becomes the fictional "Example Lines Network" (`EXLN`), with price categories `RTOU24` and `RLTOU24`, including inside plan codes and chart paths
- the address, region, meter serial, account number, contract number, registry audit numbers and portal IDs get obviously fake values

Timeframe chart images contain only prices and times, but their filenames contain the network and price category codes, so rename them to the fake codes.

Before committing, search the whole repository (not just `tests/fixtures`) for your real ICP, street, suburb, network code and name, price category, account number, contract number and meter serial. Nothing should turn up.

## Brand images

`custom_components/ecotricity_nz/brand/` holds the icon and logos Home Assistant shows for the integration. They follow the [home-assistant/brands](https://github.com/home-assistant/brands) image rules: transparent PNGs trimmed to their content, with `icon.png` at 256×256, `logo.png` 128 px tall, and `@2x` versions at double size. `dark_logo.png` is a white version for the dark theme; the icon works on both themes as it is.

They're rendered from the vector wordmark in the header of ecotricity.co.nz. The icon recreates Ecotricity's own favicon (the white wordmark on a `#333D47` circle) at full resolution.

## Releasing

The git tag is the only place the version is set. `manifest.json` and `pyproject.toml` stay at `0.0.0` in the repository, so an install from a branch shows `0.0.0` rather than a version it isn't.

1. In `CHANGELOG.md`, rename "Unreleased" to the new version and start a new "Unreleased" section.
2. Create a GitHub release with a tag like `v0.2.0`.
3. The CD workflow (`.github/workflows/cd.yml`) sets that version in `manifest.json`, zips `custom_components/ecotricity_nz` and attaches `ha_ecotricity_nz.zip`, which HACS installs.

Nothing is committed, pushed or released without the maintainer's approval.

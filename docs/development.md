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

Tests never touch the network. The API client tests mock HTTP responses, and the integration tests mock the clients using fixtures in `tests/fixtures`. Each network has its own folder under `tests/fixtures/networks`, named by its distributor code (e.g. `CKHK`, `ORON`), with an `icp.json` where it has a test ICP, and a folder for its price category (e.g. `VECT/ARHLC/`) holding that category's `plan_list.json` and its timeframe charts in `charts/`. Usually a network has one price category folder; see [Choosing price categories](#choosing-price-categories). Load a plan list with `load_plan_list(network, category)`, and a chart with `chart_file(name)`, which finds the folder from the network and category at the start of the chart's name (categories can contain hyphens, such as `T-1RL`, so it matches the name against the network's folders rather than splitting it). Most tests run against `CKHK` and `RLUTOU`, the test ICP's network and price category (`DEFAULT_NETWORK` and `DEFAULT_PRICE_CATEGORY` in `tests/conftest.py`).

The chart images are real Ecotricity charts; they contain only public pricing. Only charts that test something no other chart does are kept in git (see [Adding a scenario's chart](#keeping-a-networks-charts)), so most plans have a chart or two, not one per season.

### Writing tests

Write every test around the scenario it covers, not the network, plan or bug report where the behaviour was first seen. Pricing quirks turn up on more than one network, and fixtures get refreshed and added, so a test tied to one network's data either breaks or stops testing anything.

1. Name the test after the scenario. `test_price_combines_supplier_and_network_rates_from_different_periods` says what's being checked; a name like `test_issue_9` or `test_vector_transmission_charge` doesn't, and wouldn't fit when the same thing shows up elsewhere. Comments explain the scenario too, not where it came from.
2. Don't write helpers for a particular network, plan or issue. Shared helpers in `tests/conftest.py` describe pricing situations, never sources.
3. Don't hard-code plan IDs, plan codes or chart file names. They change whenever Ecotricity republishes its plans. When a test needs a plan from the default fixture, ask for the kind of pricing it needs: `TIME_OF_USE_PLAN` (peak and off-peak retail import prices), `ANYTIME_PLAN` (one retail import price) or `SOLAR_PLAN` (one import price with peak and off-peak export credits), each a plan's internal description. `find_plan` returns the plan, `plan_id` its ID for a config entry's options, and `chart_name` the file name of its chart for the season containing a day, built from the plan's own chart template.
4. When a test needs data that only some networks have, describe it with a scenario function in `tests/conftest.py` and find it with `find_plan_where(scenario)` for a plan, or `find_chart_where(scenario)` for a chart, each returning the first match in any network's fixtures. `has_extra_network_buckets` (one anytime energy rate, with the network charging for more periods than peak and off-peak) and `has_mixed_periods` (half-hours where Ecotricity's period and the network's differ) are examples. `chart_reading` and `explained_chart` give a chart's legend and its half-hours explained by its plan, and `chart_plan` its plan and season. Use `stored_charts(plan)` to list a plan's charts, since only some are kept. If no fixture has the scenario yet, add the network that does (see [Adding a network](#adding-a-network)) rather than building the data by hand.
5. Work out expected values from the plan's own rates (`side_rates`, `import_rates` and `extra_network_codes` help), or from data the test controls, such as rates and legend windows it builds or descriptions it renames. Don't copy one network's legend values into a scenario test, or it only passes for that network. Values printed in a chart's legend belong only in tests that check the default fixture's plans against Ecotricity's site, such as `test_prices_match_website`.
6. Refer to the test ICP's network and price category through `DEFAULT_NETWORK` and `DEFAULT_PRICE_CATEGORY`, and take other details of the test ICP (its network name, say) from its `icp.json`, so each value is written down once.
7. A bug fix gets a test of the scenario that caused it, written the same way. The bug report's number belongs in the commit message and changelog, not the test.

`tests/test_network_fixtures.py` covers every kept chart automatically, so a new network needs no new test unless it brings a new scenario.

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

Plan lists fetched without an ICP hold nothing personal, so keep them as published, including plan IDs. A plan ID is Ecotricity's ID for a published plan and is the same for everyone; it only becomes personal when stored next to an ICP, as a user's chosen plan, which fixtures never do. Tests find plans by description, so a refresh doesn't change them.

Timeframe chart images contain only prices and times. Keep the real network code in their filenames; replace any customer-specific values with obviously fake ones.

### Networks and price categories

`tests/fixtures/network_price_categories.json` lists every distributor network and the residential price categories in use on it. Fixture coverage is measured against it. Each network is keyed by its four-letter distributor code, and has its `name` and a `price_categories` map from each category code to the number of residential ICPs on it (ready and active), largest first. Categories with no residential ICPs are left out, because the integration only asks Ecotricity for residential plans. `source` and `file` record the dataset the counts came from.

The data comes from the Electricity Authority's [Distribution price category codes](https://www.emi.ea.govt.nz/Retail/Datasets/MarketStructure/DistributionPriceCategoryCodes) dataset, which is regenerated daily from the registry. Only a distributor's own network supply points count: NSPs ending in its code and `GN`, such as `CPK0111CKHKGN` for `CKHK`. Embedded networks are left out.

To refresh it, for example every few months or before adding fixtures:

```bash
uv run python scripts/build_ea_network_price_categories.py
```

The script downloads the newest file, rewrites the fixture, and prints:

- networks that are new (check the name it gives them, which comes from the dataset's reporting region) or no longer in the data
- price categories added to or gone from each network
- fixture coverage: each network with no fixture folder, the price category folders each network has and how many of its categories are missing, and any fixture category the registry no longer lists

To use a file you've already downloaded, pass its path as the only argument. Network names already in the fixture are kept, so correct a name by editing the file. The script only calls the Electricity Authority's site, never Ecotricity's.

### Choosing price categories

A network's price categories mostly share the same Ecotricity data. Comparing the top categories of Wellington Electricity (`CKHK`), Orion (`ORON`) and Vector (`VECT`) in October 2026, every category returned the same six plans, seasons, per-kWh rates and charts (byte for byte). Only two things changed: the plan the site suggests as the default (a low-user plan on low-user categories, a standard plan otherwise), and on Orion the daily network charge (`URES` charges 90c a day on low-user plans where `RES` and `RSC` charge 75.72c). The low-user and standard-user split is in the plans themselves (`_LOW` and `_STD` plan codes), not in the price category.

So each network gets one price category folder, which covers everything the integration parses:

- For a new network, use its most common residential category in `network_price_categories.json`.
- A network that already has a folder in `tests/fixtures/networks` keeps its category, such as `CKHK/RLUTOU` (the test ICP's, which most tests use).
- Add a second category only when it differs in shape from the first: different plans, seasons, charge codes, rate descriptions, units or validity dates. A difference in prices alone, such as Orion's daily charge, isn't worth a fixture, because it doesn't exercise anything new.
- Download the charts of every plan in the category, for every season. Charts differ between plans and seasons, and different plans exercise different pricing rules. Only the charts showing a new scenario are kept in git (see below).

### Adding a network

Ecotricity's onboarding site blocks an IP for about 24 hours after a burst of a few hundred requests (October 2026), so the script paces every request.

```bash
uv run python scripts/build_ea_network_price_categories.py        # see which networks have no fixtures
uv run python scripts/build_ecotricity_network_price_plans.py WAIK  # one network, its most common category
```

`scripts/build_ecotricity_network_price_plans.py` never sends an ICP. It:

1. fetches the plan list for the network's most common category (or `--category CODE`), and the plan list for the next most common category (`--compare N` for more, `--compare 0` for none), and reports whether that one differs in shape or only in prices; if it differs in shape, run the script again with `--category` for it
2. downloads the weekday and weekend charts of every plan for every season
3. saves the plan list as published, plan IDs included, to `tests/fixtures/networks/{code}/{category}/plan_list.json`, and the charts to `charts/`

It waits `--delay` seconds (10 by default) before every request, which is about 30 to 80 requests and 5 to 15 minutes per network. It stops at the first connection error, such as a refused connection when the site has blocked you, without saving anything; stop for the day if that happens. It won't overwrite an existing folder, so delete one to rebuild it.

To build a folder by hand instead: save the plan list response (`RESTPLAN/plan/list_unbundle`) with `networkCode` and `capacityCode` set and no `icpNumber` (without a price category the site returns no plans), keeping it exactly as published, plan IDs included. Save the charts named exactly as Ecotricity publishes them: the path from the plan's `timeframes_chart_wd` or `timeframes_chart_we`, with the season's start and end dates in place of `SSSSSSSS` and `EEEEEEEE`, and `-NOGST` before `.png`. These are the charts the integration reads.

### Keeping a network's charts

The build script saves every chart of a network, but only charts that test something no other chart does are kept in git. In October 2026 every network's charts were read and described by the pricing scenarios they show (how many periods, whether Ecotricity's and the network's periods line up, hours with no network rate, a cut-off legend price, overlapping or unpublished seasons and so on), and one chart was kept for each scenario: 34 of 576 charts, from 11 networks. The test ICP's network (the one with `icp.json`) was kept whole, because the integration tests run against it.

After building a network, or when a bug turns up a scenario the kept charts don't cover:

1. Run `uv run pytest tests/test_network_fixtures.py` to check every new chart reads and its prices are explained.
2. Keep the charts that show a scenario no kept chart does, and delete the rest. Delete the network's folder if none are kept.
3. Add a scenario function to `tests/conftest.py` and a test for each new scenario (see [Writing tests](#writing-tests)).

### Chart legend glyphs

The chart reader matches each character of a chart's legend against the templates in `custom_components/ecotricity_nz/api/glyphs.py`, cut from the fixture charts. Small changes to how Ecotricity draws the text still read. If it changes its font, every chart is rejected with "a character that can't be read". To make new templates, cut one example of each character from a new chart's legend: a row per pixel from the top of the text line (the top of the digits), a hex digit per pixel for its darkness (255 minus the greyscale value, divided by 16), trimmed to the ink. Then check the fixture charts with `tests/test_network_fixtures.py`, and that `test_glyph_templates_are_far_apart` still passes.

`tests/test_network_fixtures.py` picks up kept folders by itself. It checks that each folder holds only that network's and price category's plans, and that every chart's legend reads and each price is explained by the plan's rates for the season in the chart's name. It also checks that every import and export period on the network's plans has a friendly name under the `current_import_period` and `current_export_period` states, and the supplier and network period sensors' states, in `strings.json`. If one doesn't, add it there, in `translations/en.json` and in `icons.json`.

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

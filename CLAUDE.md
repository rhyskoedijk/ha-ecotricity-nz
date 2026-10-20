# Ecotricity NZ for Home Assistant

A HACS custom integration (`custom_components/ecotricity_nz`) that reads Ecotricity's public onboarding site for plan pricing and time-of-use windows, and the customer portal for half-hourly usage. Neither site has a published API; `docs/api.md` records what's known about them. Targets Home Assistant 2026.10+ on Python 3.14.

## Writing rules

These apply to everything you write: chat replies, docs, code comments, docstrings, commit messages and UI strings.

- Use New Zealand English spelling.
- Casual phrasing is fine; slang and idioms are not.
- No emojis.
- No em dashes, and no double hyphens as punctuation. Use a full stop, comma or semicolon, or rephrase. Double hyphens are fine in code and command-line flags.
- No stock AI phrasing: no "No X. No Y. Just Z." cadence, no rule-of-three lists that aren't needed, no "It's not just A, it's B", and no hedges such as "It's worth noting that", "It is important to consider", "This ensures that" or "Please note that".
- Vary sentence and paragraph length naturally rather than writing uniform blocks.
- In Markdown files, don't hard-wrap. Each paragraph or list item is one line.

## Hard rules

- Never commit, push, tag or publish a release without the maintainer's explicit permission for that specific action. Approval for one commit doesn't carry over to the next.
- Never call the live Ecotricity services from your own session without asking first. Work from fixtures; `scripts/api_client_smoke_test.py` is for the maintainer to run. The onboarding site blocks an IP for about 24 hours after a few hundred quick requests, so build fixtures with `scripts/build_ecotricity_network_price_plans.py`, which paces its requests and stops at the first refused connection. It saves to `tests/fixtures/networks`.
- Never call `RESTUSER/user/change` or any other onboarding endpoint that writes data. It creates a sign-up lead at Ecotricity.
- Never commit personal or identifying data: credentials, real ICP numbers, addresses, account or contract numbers, meter serials, or a user's association with a network or price category. Public network codes may be kept in tests and fixtures. ICPs and other personal identifiers must be fake. Browser captures go in `.captures/` (git-ignored) and are deleted once fixtures are built.
- Never log or expose secrets. Passwords, tokens and session cookies must not appear in logs, exceptions, diagnostics or test output.
- Before falling back to image or pixel parsing for any data, exhaust the structured sources (API responses, page data, public datasets) and show the evidence.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), in the style of the existing history (`git log`).

- Subject: `type: summary` in lower case, imperative and without a full stop, for example `fix: don't skip past gaps in portal readings`. Use `fix`, `feat`, `refactor`, `docs`, `test`, `build`, `ci` or `chore`.
- Body: skip it when the subject says it all. Otherwise, a short paragraph or two wrapped at about 76 characters: first why the change was needed (what was wrong, and what it caused), then what changed. Name the sensors, functions or files involved.
- Keep it brief. Don't list every file touched, restate the diff, or mention tests and docs updated as part of the definition of done.
- Never add `Co-Authored-By` lines or any other mention of AI tools.

## Definition of done

A change isn't finished until all of these are true:

1. `uv run ruff format .`, `uv run ruff check .` and `uv run mypy custom_components` pass.
2. `uv run pytest --cov` passes with at least 95% coverage. New behaviour has tests, changed behaviour has updated tests, and fixed bugs have a test that would have caught them.
3. Every document the change affects is updated in the same change, even if it's only a sentence:
   - `README.md` for anything a user sees: entities, options, setup steps, behaviour, limitations, troubleshooting.
   - `docs/architecture.md` for design, data flow or diagrams.
   - `docs/security.md` for anything that changes how credentials are stored, sent, logged or redacted, or what's sent outside Home Assistant.
   - `docs/api.md` for anything learnt about Ecotricity's services.
   - `docs/development.md` for tooling, scripts, setup or release steps.
   - `CLAUDE.md` for new conventions, commands, gotchas or architecture.
   - `CHANGELOG.md` under "Unreleased", for anything a user would notice.
4. UI text changes go in `strings.json`, and `translations/en.json` is an exact copy of it. New translation keys also get an entry in `icons.json`.
5. `quality_scale.yaml` still matches what the code actually does.

## Commands

```bash
scripts/setup                        # uv sync and pre-commit hooks
uv run pytest                        # tests; network access is blocked
uv run pytest --cov                  # with coverage (CI requires 95%)
uv run ruff format . && uv run ruff check .
uv run mypy custom_components        # strict mode
uv run pre-commit run --all-files
scripts/develop                      # local Home Assistant at http://localhost:8123 (needs g++)
uv run python scripts/api_client_smoke_test.py  # live check against the real services (needs .env)
uv run python scripts/build_ea_network_price_categories.py  # refresh networks and price categories from EA registry data
uv run python scripts/build_ecotricity_network_price_plans.py VECT  # save a network's plans and charts to tests/fixtures, paced (asks first: it calls Ecotricity)
```

Manage dependencies with `uv add` and `uv lock`; never edit `uv.lock` by hand. The Home Assistant version used for development is set by the `pytest-homeassistant-custom-component` pin in `pyproject.toml`. When bumping it, also update the minimum in `hacs.json`, and read the release notes for API changes.

## Architecture

See `docs/architecture.md` for diagrams and `docs/api.md` for the services.

- `api/` is a self-contained client library with no Home Assistant imports, so it can move to PyPI later. Keep it that way. `OnboardingClient` fetches ICP details, plans and timeframe charts (behind an attestation token, see `attestation.py`). `PortalClient` fetches half-hourly readings. `timeframes.py` reads time-of-use windows and their exact prices from the legend under each plan chart image, because they're published nowhere else. It matches each character against the templates in `glyphs.py` (nearest within `_MAX_GLYPH_DISTANCE`, so small rendering changes still read), and a cut-off price keeps its digits with an estimate from its bar (`LegendEntry.estimate`). No OCR library: none runs on Home Assistant OS's musl ARM builds.
- `tariff.py` explains every legend price as one of Ecotricity's (supplier) rates plus one of the network's, plus anytime rates and import levies, compared the way the legend rounds (`shown_price`), giving 48 `Slot`s per day type with the exact price and both rates' buckets. Ecotricity's and the network's periods don't always line up, so the two can be different periods; never fold rates into buckets. A price nothing explains rejects the chart; a side whose charts can't be read gets each period's price from its rates alone (`rate_periods`, in `DayPricing.rate_buckets`), or none where only the chart can tell. `plan_alignment` decides per side, across every season with readable charts, whether Ecotricity's periods, the network's, or both (`Alignment.SPLIT`) decide the price; `price_side` labels each half-hour's overall period (on a split side, the period whose own price is nearest). It also has no Home Assistant imports. `TouPeriod` fixes the periods of `CHARGE_24`, `CHARGE_HIGH` and `CHARGE_LOW` (anytime, peak, off-peak) for every plan. Every other bucket is named from the plan's own rate descriptions by `bucket_names` ("Shoulder Usage" gives "Shoulder" and the period `shoulder`), falling back to its code ("Charge 4") in `bucket_name`.
- `coordinator.py`: `PricingCoordinator` keeps fetching apart from working out the day. It checks once a day (`PRICING_CHECK_INTERVAL`, counted from when the entry loads, so installs don't all fetch at midnight), and only fetches when `_fetch_reason` gives a reason (weekly, the 1st, a season's last day, an unpublished season, unreadable windows, a different plan, or the refresh action). It downloads the charts of every published season that hasn't ended (later seasons only until read once), because the plan's periods and split sensors are worked out across seasons (`PlanPeriods`). Each day's prices and windows come from the saved `PricingSnapshot` with no requests (`_price_day`), so pricing must keep working offline: never add a network call to the daily path. Just after NZ midnight `_async_new_day` updates the entities from the snapshot without a coordinator refresh, which would move the daily check to midnight. A failed fetch falls back to the snapshot and backs off from `PRICING_RETRY_INTERVAL` to `PRICING_RETRY_MAX_INTERVAL` by setting `update_interval`; only with no snapshot does it raise `UpdateFailed`, with the same delay as `retry_after`. `ConsumptionCoordinator` imports daily and backfills all history in the background on its first run; when an import adds nothing it rechecks `initData` every `CONSUMPTION_RECHECK_INTERVAL` until the portal's data range moves. Its failures also raise `UpdateFailed` with `retry_after`.
- `store.py` saves the pricing snapshot (ICP details, plans, plan names, chart readings) per entry and converts the models to and from JSON by hand. If a model gains a field, add it to both directions here, and bump `STORAGE_VERSION` if old files can no longer be read: older versions are discarded (`_SnapshotStore`), and like an unreadable file, pricing is fetched again. Personal details no entity uses (the ICP's address and region) are saved blank; keep it that way for any new personal field, and only save one if an entity shows it.
- `services.py` registers the `ecotricity_nz.refresh` action in `async_setup`. It calls `async_fetch_now` on both coordinators of an entry (the only loaded one if no `config_entry_id` is given), which fetches whatever the schedule, and raises `HomeAssistantError` (`refresh_failed`) if either fetch fails.
- `image.py` shows Ecotricity's time-of-use chart for today's day type and season, as an image URL.
- `sensor.py` adds a price sensor for each period the plan has (`PlanPeriods.buckets`), and supplier and network period sensors for a split side, and the period sensors' `options` list only the plan's periods, so all stay limited to what the ICP's plan has. Sensors the plan no longer needs are removed from the entity registry (price sensors matched by the `{icp}_price_` unique ID prefix, split period sensors by `_SPLIT_PERIOD_KEYS`), so don't give any other sensor a key starting `price_`. Nothing is removed while `tou_error` is set, since the plan's periods aren't fully known until the charts can be read. Never hard-code a name for any other code: names must come from the plan's data. Price sensor names follow the plan: `BucketPriceSensor` drops Home Assistant's cached `name` when its placeholder changes. Schedule helpers in `tariff.py` (`next_change`, `day_windows`, `next_season_change`) back the period change sensors, today's windows and next pricing season change; overall windows and changes follow the period and the price. Import and export each have their own period, windows and next change sensors. Price sensors break each price into `supplier_rate`, `network_rate` and levy attributes (such as `ea_levy`) using each `Slot`'s or period's components; daily charge uses `supplier_rate`, `metering_rate` and `network_rate`, with any unrecognised item under a key from its description (`_daily_charge_key`).
- Entity categories: an entity is primary (no `entity_category`) if a user would put it on a dashboard or check it day to day for prices or usage: prices, periods, the daily charge, usage totals, the chart. It's `EntityCategory.DIAGNOSTIC` if it describes the connection, the user's settings, or how fresh the data is: ICP, network, pricing category, meter, plan, pricing season, line loss factors and dates like the last sync. Diagnostic entities still work in automations and custom dashboards; they're only left out of auto-generated dashboards and Assist. Decide this for every new entity, and mark rarely wanted ones `entity_registry_enabled_default=False` as well.
- Naming: "price" is a per-kWh sensor and "rate" is a part of a price. Attribute keys are snake_case, never repeat the sensor's own name (a plan's code is `code`, not `plan_code`), and never include a period, so they don't change through the day. Sensor keys match their translated names.
- `statistics.py` writes external statistics. The last imported hour is the backfill checkpoint, so nothing before it is ever written again. Imports stop at the first recent gap (within `CONSUMPTION_SETTLE_PERIOD` of the portal's latest data) instead of skipping past it.

## Gotchas

- Nothing about pricing is fixed. Network codes, price categories, plan codes, charge codes, seasons and time-of-use windows vary by network and change over time. Never hard-code or assume values from the fixtures.
- The user chooses their plan during setup (stored as the plan `id` in the options). Neither site reliably says which plan an ICP is on, so don't add automatic plan detection without new, verified evidence.
- Seasons can overlap ("Standard Rates" all year with "Seasonal Rates" inside it). `season_for` prefers any season covering the day that isn't "Standard Rates", as Ecotricity's site does; always use it rather than the first matching season.
- Bills charge Ecotricity's energy rates on usage times the ICP's loss factor, but chart prices leave losses out. The integration shows the factors as sensors and doesn't apply them to prices.
- One network rate on a side doesn't mean one price all day: it can apply all day (an anytime plan's off-peak rate) or only in its period (an export peak rate), with no network rate in the other hours. `SideRates.flat` is only for sides with no network time-of-use rates; `likely_flat` assumes the rate applies all day, and `day_slots` uses it only when there's no chart.
- A plan can publish no seasons when its rates are the same all year. The parser then gives it a season for each date range of its rates (`_rates_season`), because charts and day pricing need a season. Next year's rates can be published before this year ends, so never merge the ranges into one season: its chart URL wouldn't exist. Don't assume `seasons` in the raw JSON is non-empty.
- Price category codes can contain hyphens (`T-1RL`, `GS20-UA`), so never split a chart file name on `-` to find its network and category; `chart_file` and `chart_plan` in `tests/conftest.py` match it against the fixture folders.
- Show plans with `Plan.name` (the customer-facing title, falling back to the internal description); match against the API with `Plan.description`.
- Rates are grouped by bucket prefix: none for general import, `EG_` for export, `CN_` for controlled load (registry register code `CN`). Keep the groups apart; controlled-load sensors are only created when the plan has controlled rates. Controlled-load handling is unverified against real data (see `docs/api.md`).
- Prices stay in cents excluding GST everywhere; only `sensor.py` converts to NZD. GST is added to import prices and daily charges only, never to export credits (residential customers who aren't GST-registered are paid the published rate).
- Portal timestamps are NZ wall-clock times labelled with a Europe/Berlin offset. Parse them only with `portal_wall_time` and `localise_wall_time` (ranges go through `_localise_range`, which copes with end times that don't exist). Portal requests must start and end at NZ midnight, or readings in the hour repeated when daylight saving ends can land on the wrong occurrence.
- Do time arithmetic on UTC datetimes. Adding a `timedelta` to a datetime with a `ZoneInfo` attached does wall-clock arithmetic, which goes wrong across daylight saving changes.
- Entity unique IDs (`{icp}_{key}`) and statistic IDs (`ecotricity_nz:{icp}_energy_imported`, `_energy_exported` and `_carbon_savings`, ICP in lower case) are permanent. Changing them breaks users' history, so don't, unless you add a migration.
- If the config entry's data or options change shape, bump `VERSION` in `config_flow.py` and add `async_migrate_entry`.
- Config flow schemas use `probatio` (Home Assistant's voluptuous-compatible replacement), not `voluptuous`.
- Never block the event loop. CPU-bound work such as chart reading goes through `hass.async_add_executor_job`, and time zones load with `dt_util.async_get_time_zone`.
- Use HA's shared aiohttp session for the onboarding site. The portal needs its own (`async_create_clientsession`) for its cookie jar. One created during entry setup is released by HA when the entry unloads; one created in a config flow isn't, so the flow creates it with `auto_cleanup=False` and calls `detach()` when the check is done.
- Log with `LOGGER` and `%` formatting, not f-strings.
- The version in `manifest.json` and `pyproject.toml` stays at `0.0.0`. The CD workflow sets the real one from the git tag, so never bump it by hand.
- Don't add `requirements` to `manifest.json` without a good reason. Pillow already ships with Home Assistant.
- Brand images live in `brand/` and must keep the home-assistant/brands sizes; see `docs/development.md` before changing them.

## Testing

- Tests never touch the network. API client tests mock HTTP with `aioclient_mock`; integration tests patch the clients (see `tests/conftest.py`).
- Fixtures use a fake ICP such as `0000123456AB1C2`; public network codes may remain real. Do not include real ICPs, addresses, meter serials, accounts or other personal identifiers. Price categories (public in the Electricity Authority's registry data) and plan IDs (Ecotricity's IDs for its published plans, the same for everyone) stay as published, as long as no fixture pairs them with an ICP. Timeframe chart images can be real, as they contain only prices and times.
- Fixtures for each network live in `tests/fixtures/networks/{code}/` (`icp.json` where there's a test ICP), with a price category folder, `{code}/{category}/`, holding `plan_list.json` and `charts/`. Only charts that test a scenario no other chart does are kept (34 charts from 11 networks, chosen in October 2026). `scripts/build_ecotricity_network_price_plans.py` saves every chart of a network into `tests/fixtures`; the maintainer then says which to keep. Keep the charts that show a scenario no kept chart does, add a scenario function and test for each, and delete the rest (and the network's folder, if none are kept) before committing. One category per network is enough: categories have so far differed only in prices and the default plan, so add a second only if it differs in shape (rules in `docs/development.md`). Load them with `load_network_fixture`, `load_plan_list` and `chart_file` from `tests/conftest.py`. `DEFAULT_NETWORK` and `DEFAULT_PRICE_CATEGORY` are the test ICP's.
- Write tests around the scenario they cover, never a network, plan or issue number (rules in `docs/development.md`, "Writing tests"):
  - Name tests and helpers after the scenario (`test_price_combines_supplier_and_network_rates_from_different_periods`), not where it was found (`test_issue_9`, `test_vector_transmission`). No helpers for a particular network, plan or issue.
  - Never hard-code plan IDs, plan codes or chart file names; they change whenever Ecotricity republishes. Ask for a default-fixture plan by role (`TIME_OF_USE_PLAN`, `ANYTIME_PLAN`, `SOLAR_PLAN`) with `find_plan`, `plan_id` and `chart_name`.
  - For data only some networks have, write a scenario function in `tests/conftest.py` (like `has_extra_network_buckets` or `has_mixed_periods`) and use `find_plan_where` or `find_chart_where`. Use `stored_charts` for a plan's charts, and `chart_reading` and `explained_chart` for a chart's legend and explained half-hours. If no fixture has the scenario, add that network's fixtures rather than inventing data.
  - Work out expected values from the plan's own rates or from data the test controls. Legend values only belong in tests of the default fixture's plans against the site.
  - Use `DEFAULT_NETWORK`, `DEFAULT_PRICE_CATEGORY` and the test ICP's `icp.json` for its details, never literals.
  - A bug fix's test describes the scenario; the issue number goes in the commit and changelog.
- `tests/test_network_fixtures.py` reads every chart and explains its prices with its own plan's rates. Run it after changing the chart reader or pricing. `tests/fixtures/network_price_categories.json` lists every network and its residential price categories from the Electricity Authority's registry data, so fixture coverage can be checked against it; refresh it with `scripts/build_ea_network_price_categories.py`, not by hand.
- Freeze time (`freezer`, or `@pytest.mark.freeze_time` when a fixture issues auth tokens) for anything that depends on the date, season or time of day.
- `recorder_mock` is set up for every test by the autouse fixture in `tests/conftest.py`; don't request `hass` before it.

## References

Home Assistant:

- [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) and its [rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules). The target is Gold.
- [Config flows](https://developers.home-assistant.io/docs/config_entries_config_flow_handler), [reauthentication](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/reauthentication-flow) and [reconfiguration](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/reconfiguration-flow)
- [Fetching data](https://developers.home-assistant.io/docs/integration_fetching_data) and [`runtime_data`](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data)
- [Entity naming](https://developers.home-assistant.io/docs/core/entity#entity-naming), [registry properties](https://developers.home-assistant.io/docs/core/entity#registry-properties) and [icon translations](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/icon-translations)
- [Sensor statistics](https://developers.home-assistant.io/docs/core/entity/sensor#long-term-statistics)
- [Diagnostics](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics) and [repairs](https://developers.home-assistant.io/docs/core/platform/repairs)
- [API libraries](https://developers.home-assistant.io/docs/api_lib_index) and [injecting the websession](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/inject-websession)
- [Testing](https://developers.home-assistant.io/docs/development_testing)
- [HACS publishing](https://www.hacs.xyz/docs/publish/start/) and [integration requirements](https://www.hacs.xyz/docs/publish/integration/)

Python and tooling:

- [PEP 8](https://peps.python.org/pep-0008/) via [Ruff](https://docs.astral.sh/ruff/) with all rules on (exceptions in `pyproject.toml`); add a `noqa` only with a reason in the comment.
- [PEP 484](https://peps.python.org/pep-0484/) types everywhere, checked by [mypy strict](https://mypy.readthedocs.io/en/stable/existing_code.html#introduce-stricter-options). Models are frozen dataclasses, and `Any` stays inside the parsers.
- [asyncio development](https://docs.python.org/3/library/asyncio-dev.html)
- [pytest good practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html)
- [GitHub Actions security hardening](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions): pin actions to a full commit SHA, use least-privilege `permissions`, and set `persist-credentials: false`.
- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for commit messages (see "Commit messages" above).

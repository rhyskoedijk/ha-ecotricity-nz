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
- Never call the live Ecotricity services from your own session without asking first. Work from fixtures; `scripts/smoke_test.py` is for the maintainer to run.
- Never call `RESTUSER/user/change` or any other onboarding endpoint that writes data. It creates a sign-up lead at Ecotricity.
- Never commit personal or identifying data: credentials, real ICP numbers, addresses, account or contract numbers, meter serials, a real person's network or price category, or HAR files. Browser captures go in `.captures/` (git-ignored) and are deleted once fixtures are built.
- Never log or expose secrets. Passwords, tokens and session cookies must not appear in logs, exceptions, diagnostics or test output.
- Before falling back to image or pixel parsing for any data, exhaust the structured sources (API responses, page data, public datasets) and show the evidence.

## Definition of done

A change isn't finished until all of these are true:

1. `uv run ruff format .`, `uv run ruff check .` and `uv run mypy custom_components` pass.
2. `uv run pytest --cov` passes with at least 95% coverage. New behaviour has tests, changed behaviour has updated tests, and fixed bugs have a test that would have caught them.
3. Every document the change affects is updated in the same change, even if it's only a sentence:
   - `README.md` for anything a user sees: entities, options, setup steps, behaviour, limitations, troubleshooting.
   - `docs/architecture.md` for design, data flow or diagrams.
   - `docs/api.md` for anything learnt about Ecotricity's services.
   - `docs/development.md` for tooling, scripts, setup or release steps.
   - `CLAUDE.md` for new conventions, commands, gotchas or architecture.
   - `CHANGELOG.md` under "Unreleased", for anything a user would notice.
   - `SECURITY.md` for anything that changes how credentials are stored, sent, logged or redacted, or what's sent outside Home Assistant.
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
uv run python scripts/smoke_test.py  # live check against the real services (needs .env)
```

Manage dependencies with `uv add` and `uv lock`; never edit `uv.lock` by hand. The Home Assistant version used for development is set by the `pytest-homeassistant-custom-component` pin in `pyproject.toml`. When bumping it, also update the minimum in `hacs.json`, and read the release notes for API changes.

## Architecture

See `docs/architecture.md` for diagrams and `docs/api.md` for the services.

- `api/` is a self-contained client library with no Home Assistant imports, so it can move to PyPI later. Keep it that way. `OnboardingClient` fetches ICP details, plans and timeframe charts (behind an attestation token, see `attestation.py`). `PortalClient` fetches half-hourly readings. `timeframes.py` reads time-of-use windows from plan chart images, because they're published nowhere else.
- `tariff.py` turns plan rates into a price per charge bucket the same way the onboarding site does. It also has no Home Assistant imports.
- `coordinator.py`: `PricingCoordinator` refreshes daily and just after NZ midnight, re-fetching plans, seasons and rates, and re-downloading charts at least daily. `ConsumptionCoordinator` refreshes daily and imports hourly external statistics, backfilling all history in the background on its first run. Both raise `UpdateFailed` with `retry_after` (`RETRY_AFTER_FAILURE`) so a failure is retried within the hour rather than the next day.
- `image.py` shows Ecotricity's time-of-use chart for today's day type and season, as an image URL.
- `sensor.py` adds a price sensor for each new bucket that appears. Schedule helpers in `tariff.py` (`next_bucket_change`, `day_windows`, `next_season_change`) back the period change sensors, today's windows and next season change. Import and export each have their own period, windows and next change sensors. Price sensors break each price into `supplier_rate`, `network_rate` and levy attributes (such as `ea_levy`) using `DayPricing.components`; daily charge uses `supplier_rate`, `metering_rate` and `network_rate`, with any unrecognised item under a key from its description (`_daily_charge_key`).
- Entity categories: an entity is primary (no `entity_category`) if a user would put it on a dashboard or check it day to day for prices or usage: prices, periods, the daily charge, usage totals, the chart. It's `EntityCategory.DIAGNOSTIC` if it describes the connection, the user's settings, or how fresh the data is: ICP, network, price category, meter, plan, season and dates like the last sync. Diagnostic entities still work in automations and custom dashboards; they're only left out of auto-generated dashboards and Assist. Decide this for every new entity, and mark rarely wanted ones `entity_registry_enabled_default=False` as well.
- Naming: "price" is a per-kWh sensor and "rate" is a part of a price. Attribute keys are snake_case, never repeat the sensor's own name (a plan's code is `code`, not `plan_code`), and never include a period, so they don't change through the day. Sensor keys match their translated names.
- `statistics.py` writes external statistics. The last imported hour is the backfill checkpoint, so nothing before it is ever written again. Imports stop at the first recent gap (within `CONSUMPTION_SETTLE_PERIOD` of the portal's latest data) instead of skipping past it.

## Gotchas

- Nothing about pricing is fixed. Network codes, price categories, plan codes, charge codes, seasons and time-of-use windows vary by network and change over time. Never hard-code or assume values from the fixtures.
- The user chooses their plan during setup (stored as the plan `id` in the options). Neither site reliably says which plan an ICP is on, so don't add automatic plan detection without new, verified evidence.
- Show plans with `Plan.name` (the customer-facing title, falling back to the internal description); match against the API with `Plan.description`.
- Rates are grouped by bucket prefix: none for general import, `EG_` for export, `CN_` for controlled load (registry register code `CN`). Keep the groups apart; controlled-load sensors are only created when the plan has controlled rates. Controlled-load handling is unverified against real data (see `docs/api.md`).
- Prices stay in cents excluding GST everywhere; only `sensor.py` converts to NZD. GST is added to import prices and daily charges only, never to export credits (residential customers who aren't GST-registered are paid the published rate).
- Portal timestamps are NZ wall-clock times labelled with a Europe/Berlin offset. Parse them only with `portal_wall_time` and `localise_wall_time`.
- Entity unique IDs (`{icp}_{key}`) and statistic IDs (`ecotricity_nz:{icp}_energy_import`, `_energy_export` and `_carbon_savings`, ICP in lower case) are permanent. Changing them breaks users' history, so don't, unless you add a migration.
- If the config entry's data or options change shape, bump `VERSION` in `config_flow.py` and add `async_migrate_entry`.
- Config flow schemas use `probatio` (Home Assistant's voluptuous-compatible replacement), not `voluptuous`.
- Never block the event loop. CPU-bound work such as chart reading goes through `hass.async_add_executor_job`, and time zones load with `dt_util.async_get_time_zone`.
- Use HA's shared aiohttp session for the onboarding site. The portal needs its own (`async_create_clientsession`) for its cookie jar.
- Log with `LOGGER` and `%` formatting, not f-strings.
- Don't add `requirements` to `manifest.json` without a good reason. Pillow already ships with Home Assistant.
- Brand images live in `brand/` and must keep the home-assistant/brands sizes; see `docs/development.md` before changing them.

## Testing

- Tests never touch the network. API client tests mock HTTP with `aioclient_mock`; integration tests patch the clients (see `tests/conftest.py`).
- Fixtures use fake data only: ICP `0000123456AB1C2` on the fictional "Example Lines Network" (`EXLN`), price categories `RTOU24` and `RLTOU24`. Timeframe chart images can be real, as they contain only prices and times, but their filenames must use the fake codes.
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
- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for commit messages.

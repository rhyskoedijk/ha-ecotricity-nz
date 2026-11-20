# Branch review

This page holds the repository's own instructions for the `branch-review` Claude Code skill. The skill handles the process (scope, lanes, severity, evidence, decisions and the report) and assumes nothing about the project; this page gives everything specific to this integration, from its tools and commands to its rules, documents and checks. Each section below is read by name, so keep the headings as they are. The skill's scripts read the "Settings" block; the skill's `settings.md` explains each key. `<merge_base>`, `<run folder>` and `<skill>` are filled in by the skill.

## Settings

```toml
base = "main"
rule_sources = ["CLAUDE.md", "docs/development.md", "docs/security.md"]
runs = ".review/"
probes = ["tests/test_review_probe_*.py", "frontend/test/review-probe.test.ts"]

[skip]
code = ["uv.lock", "frontend/package-lock.json", "tests/fixtures/", "custom_components/ecotricity_nz/frontend/", "custom_components/ecotricity_nz/brand/", "docs/screenshots/"]
writing = ["uv.lock", "frontend/package-lock.json", "tests/fixtures/", "custom_components/ecotricity_nz/frontend/", "custom_components/ecotricity_nz/brand/", "docs/screenshots/"]
docs = ["uv.lock", "frontend/package-lock.json", "tests/fixtures/", "custom_components/ecotricity_nz/frontend/", "custom_components/ecotricity_nz/brand/", "docs/screenshots/"]
simplicity = ["uv.lock", "frontend/package-lock.json", "tests/fixtures/", "custom_components/ecotricity_nz/frontend/", "custom_components/ecotricity_nz/brand/", "docs/screenshots/"]
performance = ["uv.lock", "frontend/package-lock.json", "tests/fixtures/", "custom_components/ecotricity_nz/frontend/", "custom_components/ecotricity_nz/brand/", "docs/screenshots/"]
conventions = ["uv.lock", "frontend/package-lock.json", "custom_components/ecotricity_nz/frontend/", "custom_components/ecotricity_nz/brand/", "docs/screenshots/"]
obsolete = ["uv.lock", "frontend/package-lock.json", "custom_components/ecotricity_nz/frontend/", "custom_components/ecotricity_nz/brand/", "docs/screenshots/"]

[coverage]
report = "<run folder>/coverage.json"
format = "json"
measured = ["custom_components/ecotricity_nz/**.py"]

[conventions]
sources = [
  "CLAUDE.md#Hard rules", "CLAUDE.md#Definition of done", "CLAUDE.md#Commands",
  "CLAUDE.md#Architecture", "CLAUDE.md#Gotchas", "CLAUDE.md#Testing",
  "docs/development.md#Writing tests", "docs/development.md#Choosing price categories",
  "docs/development.md#Keeping a network's charts",
]

[writing]
sources = ["CLAUDE.md#Commit messages"]

[prose_scan]
commit_subject = '^(fix|feat|refactor|docs|test|build|ci|chore)(\([^)]+\))?!?: [a-z0-9`].*[^.]$'
commit_width = 80

[prose_scan.map]
"*.md" = "markdown"
"*.py" = "python"
"*.ts" = "slash"
"*.js" = "slash"
"*.sh" = "hash"
"*.yaml" = "hash"
"*.yml" = "hash"
"*.toml" = "hash"
"*.json" = "values"

[prose_scan.commit_forbid]
"ai mention" = '^co-authored-by:(?! dependabot\[bot\] )|generated with \[?claude|noreply@anthropic\.com'

[obsolete]
scope = "repository"
tests = ["tests/", "frontend/test/"]
docs = ["*.md"]
ignore_names = '__\w+__|async_(setup|setup_entry|unload_entry|remove_entry|migrate_entry|get_config_entry_diagnostics|get_options_flow|step_\w+)|_async_migrate_func|CONFIG_SCHEMA|EcotricityConfigFlow|main'

[obsolete.define]
"custom_components/**.py" = '^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)|^([A-Z][A-Z0-9_]*)\s*(?::[^=\n]*)?='
"scripts/*.py" = '^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)|^([A-Z][A-Z0-9_]*)\s*(?::[^=\n]*)?='
"frontend/src/*.ts" = '^export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|let|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)'
```

The rule sources are `CLAUDE.md`, all of it; `docs/development.md`, where only the sections under `conventions.sources` hold rules ("Writing tests", where each numbered item is a rule, and the two fixture sections, for fixture changes); and `docs/security.md`, for what may be stored, sent and logged.

Every lane but security skips the lock files, the built card (git-ignored) and the images; skip globs match from the repository root, so the brand images are named by their full folder. The code, writing, docs, simplicity and performance lanes also skip `tests/fixtures/` (data, not code or prose; the performance lane's timing scripts still load it). The conventions lane reads `tests/fixtures/` for `CLAUDE.md` "Testing" (fake identifiers, which charts are kept), the obsolete lane reads it for OBS-R02, and the security lane reads everything.

`obsolete.scope` is `repository`, so the obsolete lane audits the whole repository on every run, not only what the branch changed. `obsolete.ignore_names` holds Python's special methods, Home Assistant hooks the framework calls by name, and scripts' entry points. Add any new hook there rather than dismissing it on every run.

## Never

- Never call Ecotricity's onboarding site or customer portal, from a probe, a script or the session. Work only from `tests/fixtures`, `docs/api.md` and the code. Never run `scripts/api_client_smoke_test.py` or `scripts/build_ecotricity_network_price_plans.py`.
- Never call `RESTUSER/user/change` or any other onboarding endpoint that writes data.
- Never quote anything from `.captures/` or `.env` in a finding, lane file or report.
- Never commit, push, tag or publish, fixes included.

## Baseline

Run in this order, from the repository root. Commands that install (`uv sync`, `npm ci`) or build (`npm run build`) only write to git-ignored folders.

```bash
uv lock --check
uv sync --locked
uv run ruff format --check .
uv run ruff check --output-format concise .
uv run mypy
uv run pytest --cov --cov-report=term-missing --cov-report=json:<run folder>/coverage.json -q
npm ci --prefix frontend
npm run typecheck --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
git diff --check <merge_base>
rc=0; for hook in check-json check-yaml check-toml check-added-large-files detect-private-key translations-match zizmor shellcheck; do uv run pre-commit run "$hook" --all-files || { echo "FAILED: $hook"; rc=1; }; done; exit $rc
rc=0; for sha in $(git rev-list --no-merges <merge_base>..HEAD); do git log -1 --format=%B "$sha" > "<run folder>/commit-msg"; uv run pre-commit run --hook-stage commit-msg --commit-msg-filename "<run folder>/commit-msg" || { echo "FAILED: commit $sha"; rc=1; }; done; exit $rc
```

Don't run `pre-commit run --all-files` as a whole: its `ruff-check`, `trailing-whitespace` and `end-of-file-fixer` hooks rewrite files. `git diff --check` covers trailing whitespace and missing final newlines instead.

## Checkout setup

A `--committed` run's checkout of HEAD needs its own environment and built card. Install them fresh rather than linking the working tree's git-ignored folders: `tests/test_conventions.py` runs `git check-ignore` on `.captures/` and the built card's folder, which fails ("beyond a symbolic link") when they're links. No test needs `.env`, `.captures/` or `config/`. Don't run `uv run pre-commit install` here: a checkout shares the repository's git hooks.

```bash
uv sync --locked
npm ci --prefix frontend
npm run build --prefix frontend
```

## Coverage

The `pytest --cov` line writes the report the settings name. Only the integration's Python is measured: the bill card and `scripts/` have no coverage report.

## Bug fix commits

A commit whose subject starts `fix:` or `fix(` (Conventional Commits, as `CLAUDE.md` "Commit messages" says).

## Probe tests

- Python probes go in `tests/test_review_probe_<area>.py`, where `<area>` is the check's prefix in lower case (`tests/test_review_probe_price.py`). Run one with `uv run pytest tests/test_review_probe_<area>.py -q --no-cov`.
- Frontend probes go in `frontend/test/review-probe.test.ts`. Run with `npm test --prefix frontend -- review-probe`.
- Build every probe the way `docs/development.md` "Writing tests" says: the fixtures and helpers in `tests/conftest.py` (`find_plan`, `find_plan_where`, `find_chart_where`, `stored_charts`, `chart_reading`, `explained_chart`, `chart_plan`, `load_plan_list`, `load_network_fixture`, `DEFAULT_NETWORK`, `DEFAULT_PRICE_CATEGORY`), plans by role (`TIME_OF_USE_PLAN`, `ANYTIME_PLAN`, `SOLAR_PLAN`), and expected values worked out from the plan's own rates. For a scenario only some networks have, run the probe on every fixture network that has it, via a scenario function.
- Freeze time (`freezer`, or `@pytest.mark.freeze_time` when a fixture issues auth tokens) for anything that depends on the date, season or time of day. Don't request `hass` before `recorder_mock`; the autouse fixture sets it up.
- Integration probes patch the clients as `tests/conftest.py` does. Tests can't reach the network.

## Likely scenarios

A scenario is likely if one of these shows it happens:

- A fixture under `tests/fixtures` has it (a network, price category, plan, season layout, chart or portal response).
- `docs/api.md` says Ecotricity's services do it, or lists it under "Unknowns" as something seen.
- `tests/fixtures/network_price_categories.json` has the network or price category.
- A calendar fact: New Zealand daylight saving ends on the first Sunday of April (03:00 goes back to 02:00, so 2027-04-04 and 2028-04-02) and starts on the last Sunday of September (02:00 goes forward to 03:00, so 2027-09-26); midnight; month and year end; 1 April, when pricing years usually start; a season's first and last day; leap day 2028-02-29; weekday to weekend changes.
- Normal Home Assistant use: a restart or reload; an options change (another plan, the GST options); reauthentication; a dropped connection to either site; the onboarding site refusing connections for a day; the portal rejecting a login; Home Assistant OS on slow ARM hardware; the Energy dashboard and the bill card with a date picker.
- Normal customer variety: an ICP with or without an export register, with or without a portal login, on an anytime, time-of-use or solar plan, and on a plan whose network price category differs from the ICP's.

Two config entries for the same ICP, a hand-edited `.storage` file, and a clock set wrongly are not likely.

## Severity calibration

- **Critical**: long-term statistics written wrong in a way the checkpoint makes permanent (wrong `sum`, rows rewritten or dropped, an hour costed twice); an entity unique ID or statistic ID changed without a migration; a real ICP, credential, address, account or contract number or meter serial in the repository or a commit; a password, token or session cookie in a log, exception, diagnostics or test output; a call to a write endpoint, or a live call from tests or the daily pricing path; setup failing for a typical ICP.
- **High**: a wrong price, period, window, next change, season, daily charge, loss factor use, GST, sign, cost statistic or bill card figure in a normal workflow; price sensors unavailable when the charts can be read; a repair issue raised when nothing is wrong, or not raised when the user must act; a fetch or import awaited in `async_setup_entry` when there's a saved snapshot; the event loop blocked by chart reading, cost pricing or file I/O.
- **Medium**: any document wrong about the code; a user-visible change missing from the README or the changelog's "Unreleased" section; implementation detail added to `README.md`, `CHANGELOG.md` or `docs/development.md`; a writing rule broken; a convention in `CLAUDE.md` or `docs/development.md` broken; a likely scenario without a test; a baseline command failing; a leftover from an earlier implementation (a function, branch, option, fixture or doc passage nothing needs); an over-engineered solution with a simpler one that behaves the same; a speed-up past the thresholds in "Performance lane", such as pricing a month of history in half the time on a Raspberry Pi.
- **Low** (never reported): a name or comment you'd word differently, a refactoring idea that isn't simpler by the simplicity lane's list, a single unused import or variable, a speed-up under the thresholds, an internal detail not in the README.

## Documentation map

| Document | Audience | What belongs there |
| --- | --- | --- |
| `README.md` | user | Entities and attributes, options, setup steps, actions, statistics, dashboards and automations, behaviour, limitations, troubleshooting. In user terms: entity, attribute, option and action names, never internal functions, classes or constants. |
| `CHANGELOG.md` | user | Under "Unreleased", in its sections (breaking changes, new features, improvements, bug fixes), anything a user would notice, written as what changed for them. No internal names. |
| `docs/security.md` | user | How credentials are stored, sent, logged and redacted, and what's sent outside Home Assistant. Hosts and data, not code. |
| `docs/development.md` | contributor | Prerequisites, setup, commands, scripts, test practice, fixtures, the bill card's build, releases. Commands, file paths and test helpers are fine; internal design isn't. |
| `docs/architecture.md` | technical | Design, data flow, components and diagrams. |
| `docs/api.md` | technical | Everything known about Ecotricity's services: endpoints, responses, charge codes, seasons, charts, unknowns. |
| `CLAUDE.md` | technical | Conventions, commands, gotchas and architecture detail for agents and maintainers. |
| `docs/review.md` | technical | This page. |
| `custom_components/ecotricity_nz/strings.json` | user | UI text for the config flow, options, entities, issues, exceptions and actions. `translations/en.json` is an exact copy. |
| `frontend/src/` | user | The bill card's UI text lives in the card. |

## Declared facts

- `custom_components/ecotricity_nz/quality_scale.yaml`: every rule's status matches what the code does.
- `custom_components/ecotricity_nz/manifest.json`: `dependencies`, `iot_class`, `integration_type` and `config_flow` match the code; no `requirements`; version `0.0.0`.
- `hacs.json`: the `homeassistant` minimum matches the `pytest-homeassistant-custom-component` pin's Home Assistant.
- `custom_components/ecotricity_nz/services.yaml` and `icons.json`: every action and translation key the code uses has an entry, and every entry is used.
- `tests/fixtures/permanent_ids.json`: lists every entity and statistic key the code makes.

## Docs lane

- **DOC-R01 README entities.** Every sensor, image and statistic the branch adds, renames, removes or changes is in the README's tables with its name, what it shows, its unit, whether it's diagnostic or disabled by default, and its attributes, all as the code has them.
- **DOC-R02 README options and setup.** Every config flow, options and reconfigure field the branch adds or changes is described in "Setup", with its default.
- **DOC-R03 Changelog shape.** Changelog lines the branch adds are under "Unreleased", in the right section; a renamed or removed entity, attribute, option, statistic or action is under "Breaking changes"; a fix says what the user saw before and what they see now. Don't ask for issue numbers no commit gives.
- **DOC-R04 Definition of done docs.** For each kind of change in `CLAUDE.md` "Definition of done" item 3, the document it names was updated.
- **DOC-R05 Bill card docs.** Card options the branch adds or changes in `frontend/src/editor.ts` (`DEFAULTS`, the schema) are in the README's "Bill card" section with their defaults.
- **DOC-R06 Statistic and attribute names.** README examples (YAML cards, templates, statistic IDs) use keys the code actually makes (`costs.py`, `tariff.py` `line_key`, `component_key`, `daily_charge_key`, `sensor.py`).

## Writing lane

All of the skill's default writing rules apply. `CLAUDE.md` "Writing rules" repeats them, and the settings add `CLAUDE.md` "Commit messages". They cover every kind of prose, including UI text in `strings.json` and the bill card.

- Home Assistant's own UI terms and API names, quoted or used as names, keep Home Assistant's spelling.
- Pull requests are squash-merged, so a branch commit's message doesn't reach `main`, but the rules still apply to it, as its subject and body are what the squash message is written from.

## Conventions lane

Enforced by tooling, so Pass when the baseline passes: everything `tests/test_conventions.py`, `tests/test_permanent_ids.py` and `tests/test_dependencies.py` check (listed in `docs/development.md` "Checks"); Ruff with all rules; mypy strict; `tsc` strict; the `translations-match`, `uv-lock`, `zizmor`, `shellcheck`, `commit-subject` and `commit-no-ai-mention` hooks.

External guidelines, checked for these parts only:

- [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules), Gold and below: for each entity, config flow step, issue, exception and action the branch adds or changes, the rules on entity naming (`has_entity_name`, `translation_key`), unique IDs, entity categories and disabled-by-default, icon translations, exception translations, repair issues, reauthentication and reconfiguration, diagnostics, `runtime_data`, and config flow test coverage.
- [Fetching data](https://developers.home-assistant.io/docs/integration_fetching_data): coordinator use, `UpdateFailed`, `retry_after`, `ConfigEntryNotReady` and `ConfigEntryAuthFailed`.
- [asyncio development](https://docs.python.org/3/library/asyncio-dev.html): no blocking call (file I/O, `time.sleep`, Pillow, a long loop) in a coroutine or callback; CPU-bound work through `hass.async_add_executor_job`.
- [Sensor statistics](https://developers.home-assistant.io/docs/core/entity/sensor#long-term-statistics): external statistic metadata (`source`, `statistic_id`, `unit_class`, `has_sum`, `mean_type`).
- Lit: reactive properties for rendered state, no `innerHTML` or `unsafeHTML`, and the element registration rule in `CLAUDE.md`.

Checks beyond the rules:

- **CONV-R01 Entity category.** Each entity the branch adds has the category `CLAUDE.md` "Entity categories" gives it, and a rarely wanted one is disabled by default.
- **CONV-R02 New stored fields.** A model field the branch adds is converted both ways in `store.py`, and `STORAGE_VERSION` is bumped if old files can't be read.
- **CONV-R03 New UI text.** Every UI string the branch adds is in `strings.json` (the bill card's excepted), and every new translation key has an `icons.json` entry where `CLAUDE.md` asks for one.

## Security lane

- Allowed hosts are the ones `docs/security.md` lists. `tests/test_conventions.py` checks URLs in the code; check that requests the branch adds send only what `docs/security.md` says leaves Home Assistant.
- Fake identifiers: ICPs like `0000123456AB1C2`. Network codes, price categories and plan IDs are public and fine, except that no fixture may pair a plan ID with an ICP as its chosen plan.
- `diagnostics.py` `TO_REDACT` must cover every new field that holds a credential, token, cookie, ICP-linked personal detail or account number.
- Websocket commands (`websocket_api.py`) must check the config entry exists and belongs to this integration, and validate every argument with a schema.
- Personal details no entity uses (the ICP's address and region) must not be parsed into models or saved (`CLAUDE.md`, `store.py`).

## Obsolete lane

Must stay, so never a finding:

- `async_migrate_entry` and every config entry migration step for a `VERSION` an install can still have, and `_SnapshotStore`'s handling of older storage versions.
- Anything that keeps an entity unique ID or statistic ID in `tests/fixtures/permanent_ids.json` working.
- The exports of `api/__init__.py`, which are the client library's public API.
- `scripts/` that `CLAUDE.md` or `docs/development.md` tells the maintainer to run, and the helpers and scenario functions in `tests/conftest.py` that a test uses.

Checks:

- **OBS-R01 Older Home Assistant support.** Code, conditions and comments that handle a Home Assistant version below the `homeassistant` minimum in `hacs.json` are obsolete.
- **OBS-R02 Fixtures.** A fixture under `tests/fixtures/networks` that no test or scenario function reads, or a chart kept for a scenario another kept chart already shows (`CLAUDE.md` "Testing"), is obsolete.
- **OBS-R03 Retired statistics and sensors.** Code that makes, migrates or removes a statistic or sensor key no longer in `permanent_ids.json`'s current set, past the release that retired it, is obsolete unless it cleans up users' registries.

## Simplicity lane

These are deliberate, with their reasons in `CLAUDE.md`, so never candidates: reading charts by glyph templates instead of an OCR library; keeping supplier and network rates apart instead of folding them into buckets; `api/`, `tariff.py` and `costs.py` having no Home Assistant imports (so code there may repeat what a Home Assistant helper does); `_price_day` reusing its last result; `FetchBackoff` saved apart from the snapshot; sparse cost statistics with the row kept before each run of non-zero hours; one checkpoint per cost statistic; `index.ts` waiting for `<home-assistant>` before importing the card; and models converted to and from JSON by hand in `store.py`.

- **SIMP-R01 Shared helpers.** Before proposing a new helper for time, dates or seasons, check `tariff.py` (`season_for`, `first_day`, `next_change`, `day_windows`), `api/parsers.py` (`portal_wall_time`, `localise_wall_time`) and `tests/conftest.py` for one that already does it.

## Performance lane

Assume Home Assistant OS on a Raspberry Pi 4: scale times measured on a development machine by 5.

Paths that must stay responsive: Home Assistant's event loop (any coroutine, `@callback` or listener), and startup.

Scratch scripts run with `uv run python <run folder>/perf_<name>.py`, from the repository root. Load test data straight from `tests/fixtures`, the way `load_plan_list` and `chart_file` in `tests/conftest.py` find it, and call the pure modules (`api/`, `tariff.py`, `costs.py`), which need no Home Assistant instance.

Paths and how often they run:

- Startup and `async_setup_entry`: every restart and reload. It must make no requests when there's a snapshot.
- The half-hour timer (`async_start_half_hour_updates`), updating every half-hourly sensor: 48 times a day.
- `_async_new_day` at NZ midnight, updating every entity from the snapshot: daily.
- The daily pricing check and, when due, the fetch and chart reading: daily, more often while charts are missing.
- The consumption import: daily, plus a backfill of all history on the first run, pricing about a month of half-hours per chunk with `hourly_costs` in the executor.
- The bill card: on each dashboard load and date range change, through the `ecotricity_nz/bill` and `ecotricity_nz/bill_days` websocket commands (and `ecotricity_nz/icps` for its editor) and recorder statistics queries.
- External statistics: one row an hour per dense series, so each new series adds about 8,760 rows a year.

Typical sizes: the default fixture's plans and charts; a year of half-hourly readings (17,520 per series) for backfill; every sensor `sensor.py` creates for an ICP with export on a time-of-use plan.

Thresholds, after scaling: at least 100 ms on the event loop at once; at least 1 second added to startup, setup, a refresh action or a bill card load; a first backfill at least 25% faster; or at least 10% fewer statistics rows or stored bytes with the same data.

- **PERF-R01 No requests on the daily path.** A change that adds a request to `_price_day`, `cost_pricer`'s function or the half-hour and midnight updates is a finding under PERF-03, and High, as the pricing must keep working offline.

## Code lane

Each check runs for a file in scope when the file matches its trigger. Checks are listed by area; run areas in this order, and checks in ID order.

### PRICE: prices and their parts

Trigger: `tariff.py`, `sensor.py`, `api/models.py`, `api/parsers.py`.

- **PRICE-01** Each slot's price is its supplier rate plus network rate plus anytime rates plus levies, in cents excluding GST, for every kept chart (`explained_chart`), and `shown_price` rounds the way the legend does.
- **PRICE-02** GST: added to import prices and daily charges under `include_gst`, to export prices under `include_gst_export`, in the state and every breakdown attribute; never both or neither.
- **PRICE-03** Signs: an export credit is negative in the state and in `supplier_rate`, `network_rate` and levy attributes.
- **PRICE-04** Units: cents until `sensor.py` or `costs.py`, converted exactly once.
- **PRICE-05** Network rates come from the ICP's registered price category (`_pricing_plan`, `billed_plan`), and charts are explained with the plan's own (`chart_network_rates`, `rebill`).
- **PRICE-06** Rate groups: no `EG_` rate in import, no `CN_` rate in general import, and controlled-load rates get no costs.
- **PRICE-07** Loss factors are never applied to price sensors.

### TOU: periods, windows and changes

Trigger: `tariff.py`, `sensor.py`, `image.py`, `coordinator.py` (half-hour timer, `_async_new_day`).

- **TOU-01** Each day type's 48 slots cover the day with no gap or overlap, and `day_windows` joins them into the legend's windows.
- **TOU-02** `next_change` across midnight, on the last and first half-hour, across a day type change, and across both daylight saving changes; it returns a time that exists.
- **TOU-03** Split sides (`Alignment.SPLIT`): `price_side` labels each half-hour with the period whose own price is nearest; import and export are worked out separately.
- **TOU-04** A side with one network rate is only priced flat (`likely_flat`) when there's no chart, and a chart that shows the price varying is still rejected.
- **TOU-05** At NZ midnight every entity updates from the snapshot, with no coordinator refresh and no entity timer of its own.

### SEASON: seasons

Trigger: `tariff.py`, `coordinator.py`, `api/parsers.py`.

- **SEASON-01** `season_for` prefers the inner season over "Standard Rates" on every day it covers, including the first and last.
- **SEASON-02** An upcoming season's rates are its first day's (`first_day`), and an outer season resumes only after the inner one ends.
- **SEASON-03** A plan with no seasons gets one per date range of its rates (`_rates_season`), never merged.
- **SEASON-04** Season boundaries are compared as NZ dates, at 00:30 and 23:30 NZ time.
- **SEASON-05** Which seasons' charts are fetched: unended later seasons until explained once, ended seasons until read once, and ended seasons never feed `plan_slots`, gaps or `_chart_reason`.

### CHART: chart reading

Trigger: `api/timeframes.py`, `api/glyphs.py`, `coordinator.py` (chart fetches), `image.py`.

- **CHART-01** `tests/test_network_fixtures.py` passes (from the baseline).
- **CHART-02** A truncated, empty, non-PNG or unexpected image raises the documented error, and the coordinator keeps the saved reading.
- **CHART-03** A cut-off price keeps its digits with its bar estimate, and the explained price is the exact one.
- **CHART-04** Chart file names are never split on `-`.
- **CHART-05** An unexplained chart sets `tou_error` on the Plan sensor and makes only its own current price sensors unavailable.

### COORD: fetching, scheduling and recovery

Trigger: `coordinator.py`, `__init__.py`, `services.py`.

- **COORD-01** `_fetch_reason` fetches only for its listed reasons, and the daily path makes no requests (`_price_day`).
- **COORD-02** With a snapshot for the chosen plan, setup makes no requests and defers fetches and the import to `async_fetch_deferred` after startup.
- **COORD-03** A failed fetch falls back to the snapshot and backs off; `UpdateFailed` with `retry_after` only without a snapshot or for another plan; `FetchBackoff` is saved, honoured by `_wait_to_retry`, and cleared as `CLAUDE.md` says.
- **COORD-04** Missing charts: retried daily up to `PRICING_MISSING_CHART_RETRIES`, counted in the snapshot, restarted only for a new gap keyed by side and season.
- **COORD-05** The refresh action fetches both coordinators and raises `refresh_failed` if either fails.

### STAT: usage and cost statistics

Trigger: `costs.py`, `statistics.py`, `coordinator.py` (consumption and costs), `store.py` (`CostStore`).

- **STAT-01** `sum` is continuous: importing in two chunks, with a restart between, gives the same rows as one; nothing before the checkpoint is rewritten.
- **STAT-02** Imports stop at the first recent gap within `CONSUMPTION_SETTLE_PERIOD`; `_scanned_until` doesn't lose a new series' first readings.
- **STAT-03** Daylight saving end imports both 02:00 hours separately; daylight saving start makes no hour for the skipped one.
- **STAT-04** Costs: every `cost_keys` key has a value in every priced hour; parts exclude GST; `total_electricity_cost` is usage, levies, daily charges and GST; `total_electricity_compensation` is export and its GST with the sign flipped; loss factors multiply only Ecotricity's rates, consumption on import and generation on export.
- **STAT-05** Daily charges spread over 23, 24 and 25 hour days add up to one day's charge.
- **STAT-06** Sparse series: no row for an empty hour unless the next hour adds something or it's the import's last hour; `total_electricity_cost` stays dense.
- **STAT-07** Resume: from the statistic furthest behind; a new statistic starts at `async_earliest_priced_day` or after its side's latest costed hour; a plan change doesn't cost the old plan's hours again (`CostStore`, `CostStart`).
- **STAT-08** Cost pricing runs in the executor and touches no Home Assistant state, and only days a published season covers are priced.

### CARD: bill card and its API

Trigger: `frontend/src/**`, `websocket_api.py`, `card.py`.

- **CARD-01** Ranges start at NZ midnight (`billingPeriodStart`) and are widened to whole hours (`wholeHours`) before asking the recorder.
- **CARD-02** Daily rates divide by `bill_days`' charged days, not the range's length.
- **CARD-03** Line labels come from `lineLabel` and the websocket's kind, part and period, never the plan's descriptions.
- **CARD-04** The total adds GST on both sides; credits show negative.
- **CARD-05** No element is defined at the bundle entry's top level, and the card follows an `energy-date-selection` card's `collection_key`.
- **CARD-06** The websocket commands make no requests and return statistic IDs that exist for the saved plan.

### API: Ecotricity client

Trigger: `api/**`.

- **API-01** Every endpoint, header, parameter and body the branch adds or changes is in `docs/api.md`, and is read-only.
- **API-02** Parsers handle every shape the fixtures have (a list or an object, a number as a string, missing keys), and parse every fixture.
- **API-03** Portal timestamps go only through `portal_wall_time` and `localise_wall_time`, and requests start and end at NZ midnight.
- **API-04** Every request has a timeout, and every aiohttp, JSON and `returnCode` error maps to an exception in `api/exceptions.py`; a session expiry re-logs in once, never in a loop.

### FLOW: config flow and options

Trigger: `config_flow.py`, `strings.json`.

- **FLOW-01** A changed data or options shape bumps `VERSION` and adds `async_migrate_entry`.
- **FLOW-02** Schemas use `probatio`; the flow's portal session is created with `auto_cleanup=False` and detached.
- **FLOW-03** Every error a step can show has a string, and reauthentication and reconfiguration keep the entry's unique ID.

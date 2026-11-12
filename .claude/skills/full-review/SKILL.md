---
name: full-review
description: Full review of every file in the repository. Covers dead code, documentation accuracy, missing test scenarios, logic bugs in price, time-of-use, season and chart handling, usage and billing statistics, Ecotricity API accuracy, best practice for each technology, and security. Every finding is verified and backed by evidence. Run only when the maintainer asks for a full review.
disable-model-invocation: true
---

# Full review

This skill reviews the whole repository against a fixed checklist and writes one report. It's built so that two runs on the same tree report the same findings. To make that happen, every step below is mandatory and runs in the order given. Don't skip, reorder, sample or summarise any step, and don't stop after some number of findings. A run that hasn't finished every check hasn't finished.

## 1. Ground rules

1. **Read-only.** Don't change any tracked file. The only files you create are probe tests (`tests/test_review_probe_*.py`, `frontend/test/review-probe.test.ts`) and scratch output in your scratchpad directory. Delete the probe tests at the end and put their code in the report.
2. **Repository rules apply.** Follow every rule in `CLAUDE.md`. In particular: never call the live Ecotricity services (onboarding or portal), never call any endpoint that writes data, never commit or push, and never print a secret, credential, real ICP or other personal identifier. Package registries (PyPI, npm) and the documentation sites linked below are fine to reach.
3. **One context, no subagents.** Do the whole review yourself, in this conversation. Spreading it over subagents changes what each part sees and makes runs differ.
4. **Fixed order.** Work through files in the order `git ls-files --cached --others --exclude-standard | sort` prints them, and through checks in ID order. Never prioritise by what looks interesting.
5. **No cap.** Report every finding. If there are 60, report 60. Never write "and others", "for example" or "some of".
6. **Nothing unverified.** A suspicion isn't a finding. Verify it as section 3 says, or put it under "Checked and dismissed" with the reason.
7. **Nothing invented.** If a check finds nothing, its verdict is Pass. If a whole section finds nothing, say "No issues found" for that section. An empty review is a valid result.
8. **Write the report the way `CLAUDE.md` says to write:** New Zealand English, no emojis, no em dashes, no double hyphens as punctuation, no stock AI phrasing.

## 2. Severity

Assign severity only by these rules. If more than one fits, use the highest.

- **High:** a user sees a wrong price, period, window, season, cost or usage figure; statistics are written wrong or lost; a secret or personal identifier is exposed; a crash or unhandled exception on a scenario that's likely to happen (see section 3).
- **Medium:** documentation contradicts the code; a likely scenario has no test; a best-practice rule from a linked guideline is broken in a way that affects users or maintainers (blocking the event loop, an entity that can't be renamed, a missing translation); a dependency with a known vulnerability that this repo pins.
- **Low:** dead code, stale comments, naming, small style deviations from a linked guideline, missing tests for unlikely scenarios.

## 3. Evidence standard

Every finding needs at least one of these, recorded in the report exactly as given:

- **(A) Failing probe test.** A test that fails on the current code and would pass once the issue is fixed. Required for every finding in PRICE, TOU, SEASON, CHART, STAT and API that claims wrong behaviour. Put the full test code and the failure output in the report.
- **(B) Command output.** The exact command and the lines of output that show the issue.
- **(C) Quoted contradiction.** Both sides quoted with `file:line`: the code, and the doc, comment, docstring, `CLAUDE.md` rule or spec it contradicts. Required for every DOC finding.
- **(D) Guideline breach.** The guideline's link, the clause or rule quoted (or named exactly, such as a Ruff rule code or a quality scale rule ID), and the code quoted with `file:line`. Required for PY, HA, TS, CI and SEC findings that rest on a guideline.

A scenario is **likely to happen** only if at least one of these shows it happens: a fixture under `tests/fixtures` has it; `docs/api.md` says Ecotricity's services do it; the Electricity Authority data in `tests/fixtures/network_price_categories.json` has it; it's a calendar fact (New Zealand daylight saving starts on the last Sunday of September and ends on the first Sunday of April, so 2026-09-27, 2027-04-04 and 2027-09-26; year rollover; leap day 2028-02-29); or it follows from normal Home Assistant use (restarts, reloads, a dropped connection, a plan change in the options, a second config entry). Say which one applies in the finding.

If you can't produce evidence of the required kind, it isn't a finding. Put it under "Checked and dismissed", with what you tried and why it didn't hold up. Listing dismissed candidates is required. It's how a later run can tell that a candidate was looked at.

## 4. Phase 0: baseline

First save `git status --porcelain` to your scratchpad (section 7 compares against it). Then run these from the repository root, in this order, and save each output to your scratchpad. A failure is evidence (B) for a finding under the check it belongs to, and the rest of the review still runs. If a command can't run (no network, tool missing), record "could not run" and why in the baseline table, and carry on.

```bash
uv sync
uv run ruff format --check .
uv run ruff check --output-format concise .
uv run mypy custom_components
uv run pytest --cov --cov-report=term-missing -q
npm ci --prefix frontend
npm run typecheck --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
npx --prefix frontend tsc --noEmit --noUnusedLocals --noUnusedParameters -p frontend
for hook in check-json check-yaml check-toml check-added-large-files detect-private-key frontend-typecheck translations-match; do uv run pre-commit run "$hook" --all-files; done
git grep --untracked -nI -E '[[:space:]]+$' -- . ':!uv.lock' ':!frontend/package-lock.json'
git ls-files --cached --others --exclude-standard -z | xargs -0 -I{} sh -c 'test -s "{}" && test "$(tail -c1 "{}" | wc -l)" -eq 0 && file -b --mime "{}" | grep -q text && echo "no final newline: {}"'
uv run python .claude/skills/full-review/scripts/symbols.py
uv run python .claude/skills/full-review/scripts/doc_refs.py
uv run python .claude/skills/full-review/scripts/api_shapes.py
uv run python .claude/skills/full-review/scripts/pii_scan.py
uv export --frozen --no-emit-project --format requirements-txt -o "$SCRATCH/requirements.txt"
uvx pip-audit --no-deps --disable-pip --progress-spinner off -r "$SCRATCH/requirements.txt"
npm audit --prefix frontend
uvx zizmor --offline .github/workflows
```

`$SCRATCH` is your scratchpad directory. The four skill scripts only read files and print sorted results, so their output is the same on every run of the same tree. Their docstrings say what each one checks.

Only the pre-commit hooks that never change files are run. The trailing-whitespace and end-of-file hooks, and `ruff check --fix`, rewrite files, so the two `git` lines stand in for the first two and the Ruff lines above cover the last. Never run `pre-commit run --all-files`, and never use `git checkout`, `git restore` or `git stash` during a review: the working tree can hold the maintainer's uncommitted work. Any output from the two `git` lines is evidence for DEAD-09.

## 5. Phase 1: read everything

1. List the files: `git ls-files --cached --others --exclude-standard | sort`. Leave out `.claude/skills/full-review/` itself.
2. Read every text file in full, in that order. That includes `custom_components/`, `frontend/src`, `frontend/test`, `tests/` (except the fixture data below), `scripts/`, `docs/`, `.github/`, and the root files (`README.md`, `CLAUDE.md`, `CHANGELOG.md`, `pyproject.toml`, `hacs.json`, `.pre-commit-config.yaml`, `.gitignore`, `frontend/package.json`, `frontend/tsconfig.json`). Use offsets for files over 2000 lines so nothing is cut off.
3. Fixture data: don't read `tests/fixtures/**/*.json` or `uv.lock`/`package-lock.json` in full. The scripts cover them. Open a fixture file only to verify a specific candidate.
4. Binary files (`*.png`, `*.webp`): don't open them, except a chart image to verify a CHART candidate, a bill image under `.captures/bill-examples/` for STAT-14, or a brand image's dimensions for HA-16.
5. Keep a coverage table: every file, and the check IDs that looked at it. Every file must appear in at least one check. If a file fits no check, put it under "Unreviewed files" with the reason. That list should normally be empty.

## 6. Phase 2: checklists

Work through every check below in order. For each one, record a verdict: **Pass**, **Finding(s)** (with finding IDs) or **Not applicable** (with a one-line reason). The "Verify" step is the minimum; do all of it.

### DEAD: obsolete and dead code

- **DEAD-01 Unreferenced symbols.** Every entry under "UNREFERENCED" in `symbols.py`. Verify: grep the name across the repo, including string uses (`getattr`, translation keys, `services.yaml`, `manifest.json`) and Home Assistant hooks that call it implicitly. A finding only if nothing calls it.
- **DEAD-02 Test-only symbols.** Every entry under "REFERENCED ONLY BY TESTS". A finding if production code never calls it and it isn't a public API of `api/` (the package `api/__init__.py` exports). Quote the test that uses it.
- **DEAD-03 Lines coverage never reaches.** Every line and branch in the `pytest --cov` "Missing" column for `custom_components/`. Classify each one: unreachable (a finding here, proved by showing why no input reaches it), reachable but untested (goes to TEST-01 instead), or defensive handling of an external failure (Pass if a comment or the context says so).
- **DEAD-04 Unused TypeScript.** Every error from the `tsc --noUnusedLocals --noUnusedParameters` run, and every `export` in `frontend/src` that no other file imports (grep each exported name in `frontend/src` and `frontend/test`).
- **DEAD-05 Unused constants and config.** Every name in `const.py`, every option and data key in `config_flow.py`, and every key in `manifest.json`, `hacs.json` and `services.yaml`, checked for a use.
- **DEAD-06 Unused UI strings.** The "TRANSLATION KEYS" section of `doc_refs.py`. Each key reported as not used in code, verified by grep, including keys built from parts (such as `f"{side}_price"`).
- **DEAD-07 Unused fixtures.** For every file under `tests/fixtures`, find a test or `tests/conftest.py` helper that loads it, directly or by directory walk (`test_network_fixtures.py` reads every chart). A finding for each file nothing loads.
- **DEAD-08 Stale references.** Comments, docstrings, scripts and workflows that name a function, file, option, sensor or command that no longer exists. Use the "MISSING NAMES" and "MISSING PATHS" lists from `doc_refs.py` for docs, and grep the names in comments of the code you read in Phase 1.
- **DEAD-09 Leftovers.** `TODO`, `FIXME`, `XXX`, commented-out code, `print` calls outside `scripts/`, `breakpoint()`, skipped or `xfail` tests with no reason, trailing whitespace and missing final newlines (from the baseline `git` lines).
- **DEAD-10 Obsolete compatibility code.** Code kept only for a Home Assistant or Python version older than the minimum (`hacs.json` `homeassistant`, `pyproject.toml` `requires-python`), or for a `STORAGE_VERSION` or config entry `VERSION` that's no longer migrated from.

### DOC: documentation matches the implementation

- **DOC-01 Docs' code references.** Every entry in "MISSING PATHS" and "MISSING NAMES" from `doc_refs.py`. Classify each: external name (Home Assistant, a library, an HTTP path on Ecotricity's site, a placeholder or example such as `test_issue_9`): Pass. Removed or renamed in this repo: finding, with the current name.
- **DOC-02 README entities.** The "README ENTITIES" section of `doc_refs.py`, and then each row of every table in README "Sensor Entities" and "Statistic Entities" against `sensor.py`, `image.py` and `statistics.py`: name, description, when it's created, attributes (compare the attribute keys to `extra_state_attributes`), unit, whether it's diagnostic and whether it's disabled by default.
- **DOC-03 README behaviour.** Each statement in README "Setup", "Actions", "Updates and working offline", "Using it with the Energy dashboard", "Bill card", "Known limitations", "Troubleshooting" and "Removing it", checked against the code that implements it. Every number in the README (intervals, retries, days, percentages) must match the constant in `const.py` or the code.
- **DOC-04 Architecture.** Each paragraph and diagram in `docs/architecture.md` against the code it describes. Every function, class, constant and data flow arrow named there must exist and behave as described.
- **DOC-05 API reference.** `docs/api.md` against `api/`: covered in detail by API-01 to API-10. Here, check only the parts the API checks don't: the "How prices are made up", "Line losses", "Charge codes", "GST" and "Controlled load" sections against `tariff.py` and `costs.py`.
- **DOC-06 Security doc.** Every claim in `docs/security.md` against the code (what's stored, where, what's redacted, what's sent where). Contradictions are findings here; actual exposures go under SEC.
- **DOC-07 Development doc.** Every command, script, path and step in `docs/development.md` exists and works as written (run commands that are safe and local; for the others, check the script's arguments).
- **DOC-08 CLAUDE.md.** Every bullet under "Architecture", "Gotchas" and "Testing" against the code. Every named function, constant, attribute and behaviour must exist and match. Every command under "Commands" must exist.
- **DOC-09 CHANGELOG.** Every entry under "Unreleased" describes behaviour that exists in the code. Every change a user would notice in `git diff main...HEAD` and in uncommitted work has an entry.
- **DOC-10 Docstrings and comments.** For every function in `custom_components/` that has a docstring or a comment about its behaviour, the description matches what the code does (arguments, return value, edge cases mentioned). Read the code; don't trust the docstring.
- **DOC-11 Quality scale.** Every rule in `quality_scale.yaml`. `done` must be true of the code (find the code), `exempt` must have a `comment` that is still true, and `todo` must still be not done. Compare against each rule's page under https://developers.home-assistant.io/docs/core/integration-quality-scale/rules.
- **DOC-12 UI text.** `strings.json` descriptions (config, options, services, issues, exceptions) against the behaviour, and `translations/en.json` identical to `strings.json` (the `doc_refs.py` line).

### TEST: scenarios that should have tests

For each row, find the test or tests that cover it (give the test's `file::name`). If none does, it's a finding: Medium if the scenario is likely (section 3), Low if not. To check whether a test really covers the scenario, read its body. A test with the right name but the wrong data doesn't count. Then do TEST-01.

- **TEST-01 Coverage gaps.** Every reachable-but-untested line from DEAD-03, grouped by function. Each function with untested lines is one finding, with what scenario would reach the lines.
- **TEST-02** A day when daylight saving starts (46 half-hours) and ends (50 half-hours): pricing slots, windows, `next_change`, daily charge spread (`costs.py`), statistics import and portal readings.
- **TEST-03** NZ midnight crossing (`_async_new_day`), year rollover (31 December to 1 January), and a leap day.
- **TEST-04** Overlapping seasons, with `season_for` choosing the inner one, and the outer season applying again once the inner one ends (`first_day`).
- **TEST-05** A season ending today, a season starting tomorrow, and an upcoming season not yet published.
- **TEST-06** A plan with no published seasons (`_rates_season`), and next year's rates published before this year ends.
- **TEST-07** Split alignment (`Alignment.SPLIT`): supplier and network period sensors, `price_side` choice of the nearest period.
- **TEST-08** Export: an ICP with an `EG` register, one without (`_pricing_plan`, `without_export`), and export prices keeping their sign.
- **TEST-09** Controlled load (`CN_` buckets).
- **TEST-10** Chart reading: a cut-off legend price (`LegendEntry.estimate`), an unreadable chart, a missing chart, a price nothing explains, a side with no windows, `_shows_varying_price` both ways.
- **TEST-11** Fetch scheduling: each reason `_fetch_reason` returns, `PRICING_MISSING_CHART_RETRIES` counted across a restart (`missing_chart_fetches`), a new gap resetting the count, retry backoff to `PRICING_RETRY_MAX_INTERVAL`, `UpdateFailed` with `retry_after` when there's no snapshot.
- **TEST-12** A plan change in the options: sensors removed and added, statistics continuing.
- **TEST-13** Daily pricing offline: `_price_day` and `day_costs` making no requests (a test that fails if a client method is called).
- **TEST-14** Statistics: backfill from empty, resume from the checkpoint, a gap within `CONSUMPTION_SETTLE_PERIOD` stopping the import, a gap older than that being skipped, `_scanned_until`, `CONSUMPTION_RECHECK_INTERVAL`.
- **TEST-15** Cost statistics: every `cost_keys` key present in each hour, totals adding up, GST, the loss factor on Ecotricity import rates only, daily charges over 23, 24 and 25 hour days, `_async_cost_resume` resuming from the furthest-behind statistic, a new statistic starting at `earliest_priced_day`, days with no published season left unpriced.
- **TEST-16** Portal timestamps: the Berlin offset (`portal_wall_time`, `localise_wall_time`), the repeated hour at daylight saving end, an end time that doesn't exist (`_localise_range`).
- **TEST-17** `store.py`: a round trip for every field of every saved model (list the fields from `api/models.py` and `tariff.py` and check each is in a round-trip test), an old `STORAGE_VERSION` discarded, an unreadable file.
- **TEST-18** Config flow: user step, plan step, reauth, reconfigure, options, each error branch, and `async_migrate_entry` if `VERSION` is above 1.
- **TEST-19** Services: `refresh` with and without `config_entry_id`, with an unloaded entry, and a failed fetch raising `refresh_failed`.
- **TEST-20** Diagnostics redact every secret and personal field (compare the redaction list to every field in the config entry and snapshot).
- **TEST-21** Bill card: `wholeHours`, `charged_days` and `ecotricity_nz/bill_days`, `lineLabel` for every line kind, editor `validateConfig` and `DEFAULTS`, the `index.ts` wait for `<home-assistant>`, following an `energy-date-selection` card.
- **TEST-22** Websocket commands: each command with a valid entry, an unknown ICP, and no loaded entry.
- **TEST-23** Sensors added later (generation capacity, export, controlled load) without a reload, and never removed; nothing removed while `tou_error` is set or `PlanPeriods.complete` is false.
- **TEST-24** Every scenario function in `tests/conftest.py` (`has_*`) is used by at least one test, and every network fixture folder is used by a scenario.

### PRICE: price handling

For each probe, write a test in `tests/test_review_probe_pricing.py` that uses the repo's fixtures and helpers (`find_plan`, `find_plan_where`, `find_chart_where`, `stored_charts`, `chart_reading`, `explained_chart`, `load_plan_list`, `DEFAULT_NETWORK`, `DEFAULT_PRICE_CATEGORY`). Work out the expected value from the plan's own rates, never from the code under test. If the probe passes, the verdict is Pass. If it fails, confirm the failure is in the code and not the probe (re-derive the expected value by hand and show the arithmetic), then report it with evidence (A). Run each probe against every fixture network that has the scenario (use the scenario functions), not just the default one.

- **PRICE-01** Every slot's price equals its supplier rate plus network rate plus anytime rates plus levies, in cents excluding GST, for every kept chart (compare `explained_chart` against the plan's rates).
- **PRICE-02** `shown_price` rounding matches the legend for every legend entry in every kept chart (`chart_reading`), including prices ending in 5 at the rounding digit.
- **PRICE-03** GST: added to import prices and daily charges at 15% when `include_gst` is on, never to export prices; check the sensors' states and every breakdown attribute.
- **PRICE-04** Sign: every export credit is negative in the sensor state and in `supplier_rate`, `network_rate` and levy attributes.
- **PRICE-05** Unit conversion: cents to NZD happens only in `sensor.py` and `costs.py`, exactly once (grep every `/ 100` and `* 0.01` and trace each value's unit).
- **PRICE-06** Daily charge: the sum of `supplier_rate`, `metering_rate`, `network_rate` and every unrecognised item equals the state; capacity charges are left out as the README says.
- **PRICE-07** `bucket_names` and `bucket_name`: every rate description in every fixture plan gives a distinct name and period key, and two buckets never end up with the same period key.
- **PRICE-08** `resolve_bucket` and the prefixes: no `EG_` rate lands in import, no `CN_` rate lands in general import, for every fixture plan.
- **PRICE-09** Loss factors: not applied to any price sensor; applied only to Ecotricity's import rates in `costs.py`.
- **PRICE-10** A plan whose rates change mid-season (two rates with the same bucket and different `valid_start`): the price on each side of the change date uses the right rate.

### TOU: time-of-use handling

Probe tests go in `tests/test_review_probe_tou.py`, with time frozen by `freezer`.

- **TOU-01** For every kept chart, the 48 slots of each day type cover the whole day with no gap or overlap, and `day_windows` joins them back into the legend's windows.
- **TOU-02** Windows that wrap past midnight (such as 21:00 to 07:00): `next_change` just before midnight gives the right next change, which is on the next day if that day's first window differs.
- **TOU-03** `next_change` on the last half-hour of the day, the first half-hour, and on a day type change (Friday to Saturday, Sunday to Monday).
- **TOU-04** Daylight saving start (2026-09-27) and end (2027-04-04): the current period and price at 01:30, 02:30 and 03:30 local time match the chart's windows by wall-clock time, and `next_change` returns a time that exists.
- **TOU-05** `plan_alignment` and `price_side` on every split fixture chart: the overall period is the one whose own price is nearest, as `CLAUDE.md` says, and ties are handled deterministically.
- **TOU-06** Import and export periods are worked out independently: an export window that differs from import shows in the export sensors only.
- **TOU-07** `SideRates.flat` and `likely_flat`: a side with one network rate that applies only in its period (export peak) isn't priced flat when a chart exists.
- **TOU-08** Public holidays: check whether `docs/api.md`, a chart or Ecotricity's plan data says holidays are priced differently. If there's evidence they are, a probe showing a holiday priced as a weekday is a finding. If there's no evidence, the verdict is Pass with the note "no evidence that holidays differ". Don't report it as a finding on assumption.

### SEASON: season handling

Probe tests go in `tests/test_review_probe_season.py`.

- **SEASON-01** `season_for` on every fixture plan, for the first and last day of each season and the day either side: it returns the inner season when seasons overlap, and never "Standard Rates" when another season covers the day.
- **SEASON-02** `next_season_change` on the day before, the day of and the day after each change, including an outer season resuming after an inner one ends.
- **SEASON-03** `first_day` for an upcoming season gives the rates of the first day it applies.
- **SEASON-04** `chart_seasons`, `ended_seasons` and `season_charts`: on each season boundary, the charts fetched are the current season's, each unended later season's until explained, and each ended season's until read once, as `CLAUDE.md` says.
- **SEASON-05** `_rates_season`: a plan with no seasons gives one season per date range of its rates, never merged; a rate with an open end date.
- **SEASON-06** A day no published season covers: `day_costs` returns None and no cost statistic is written for it, and the price sensors' behaviour matches the README.
- **SEASON-07** Season dates and time zones: season start and end are compared as NZ dates, not UTC dates (probe at 00:30 and 23:30 NZ time on a boundary day).
- **SEASON-08** `_ended_periods`: a winter-only period's sensor isn't removed when winter ends.

### CHART: chart handling

Probe tests go in `tests/test_review_probe_chart.py`. The chart reader and its tests use real chart images, which contain no personal data.

- **CHART-01** `tests/test_network_fixtures.py` passes for every chart (from the baseline). Any failure is a finding.
- **CHART-02** `read_chart` on a truncated PNG, an empty byte string, a non-PNG, a very large image and a palette or greyscale image: it raises `ChartReadError` (or another documented error) and never anything unhandled. Check what the coordinator does with each error.
- **CHART-03** `_match_glyph` against `_MAX_GLYPH_DISTANCE`: for every glyph in every kept chart, the nearest template's distance and the second nearest's. A finding if any glyph's best match is within 25% of the limit, or the second nearest is within 25% of the best (a small rendering change would misread it). Report the numbers.
- **CHART-04** `_estimate_clipped` and `LegendEntry.estimate`: on every kept chart with a cut-off price, the estimate is within the tolerance the code assumes, and the explained price is the exact one.
- **CHART-05** `chart_path`, `season_chart_path`, `needs_season` and `chart_file`: price category codes with hyphens (`T-1RL`, `GS20-UA`), and no code splits a file name on `-`.
- **CHART-06** Chart reading runs in the executor (`hass.async_add_executor_job`) everywhere it's called. Grep every `read_chart` call.
- **CHART-07** The image entity (`image.py`) shows today's day type and season's chart, switches at midnight and on a season change, and handles a missing chart.
- **CHART-08** A chart fetched but unexplained: `tou_error` is set on the Plan sensor, the current price sensors are unavailable, and a repair issue is raised or not raised as `CLAUDE.md` says.

### STAT: usage and billing long-term statistics

Probe tests go in `tests/test_review_probe_statistics.py`, built from `tests/fixtures/portal_*.json` and the default plan. Work out expected values by hand from the readings and the plan's rates.

- **STAT-01** Statistic IDs and metadata: for every series (`energy_imported`, `energy_exported`, `carbon_savings`, every cost key and every line key), `statistic_id`, `source`, `name`, `unit_of_measurement`, unit class, `has_sum` and `mean_type` match the README and Home Assistant's external statistics contract (https://developers.home-assistant.io/docs/core/entity/sensor#long-term-statistics). IDs use the lower-case ICP.
- **STAT-02** `sum` is continuous: importing in two chunks gives the same `sum` and `state` per hour as importing in one; a restart between chunks doesn't double-count or drop the hour at the `async_last_point` checkpoint.
- **STAT-03** The checkpoint: nothing before the last imported hour is ever rewritten; the import stops at the first gap within `CONSUMPTION_SETTLE_PERIOD` of the portal's latest data; `_scanned_until` for a series with no statistic yet.
- **STAT-04** Daylight saving end with `portal_values_dst_end.json`: both 02:00 hours are imported as separate UTC hours with the right readings, and the day's total equals the sum of its readings. Daylight saving start: no hour is created for the skipped hour.
- **STAT-05** `hourly_totals` adds exactly two half-hour readings per hour, and an hour with one missing reading is treated as the README says ("wait up to a week, then import what the portal has").
- **STAT-06** Cost totals: for every priced hour, the parts add to `import_cost_total`, `daily_charge_total` and `export_total`; `total_electricity_cost` equals import parts plus daily charges plus GST; `total_electricity_net_billed` equals `total_electricity_cost` plus `export_total`; `total_electricity_compensation` equals `export_total` with its sign flipped and is the only positive credit.
- **STAT-07** Every key from `cost_keys` has a value in every priced hour, zero where it doesn't apply, including a seasonal part outside its season.
- **STAT-08** Lines: the `_energy` series of all import lines for an hour add to `energy_imported` for that hour (and export likewise), and every line's NZD equals its energy times its rate. Keys come only from `line_key`.
- **STAT-09** GST is on import parts and daily charges only, at 15%, and the consumption loss factor multiplies Ecotricity's import rates only.
- **STAT-10** Daily charges spread over a 23, 24 and 25 hour day add up to exactly one day's charge (to the cent after rounding, and check the rounding doesn't drift over a month).
- **STAT-11** `_async_cost_resume` resumes from the statistic furthest behind; a statistic with no hours starts at `earliest_priced_day`; days no published season covers are left unpriced and don't block later days.
- **STAT-12** A plan change mid-history: past hours aren't repriced (the checkpoint) and new hours use the new plan, and the README describes this.
- **STAT-13** Websocket and card: `ecotricity_nz/bill`, `bill_lines` and `bill_days` return the statistic IDs that exist for the plan; the card widens to whole hours (`wholeHours`) and uses `charged_days`, not the range length, for daily rates; line labels come from `lineLabel`.
- **STAT-14** Real bills. If `.captures/bill-examples/` exists, open each bill image. For each one where the plan, rates and period can be matched to fixture data, compare the bill's line items (names, rates, units, GST treatment, rounding, daily charge count) with how `costs.py` and the bill card would present the same period. Report only the line names, rates and differences, never names, addresses, ICPs or account numbers. If the folder doesn't exist or no bill can be matched, the verdict is Not applicable with the reason.
- **STAT-15** The Energy dashboard: `total_electricity_cost` and `total_electricity_compensation` fit how the dashboard uses a cost statistic and a compensation statistic (https://www.home-assistant.io/docs/energy/), and the README setup steps for them work.

### API: Ecotricity API accuracy

No live calls. Work only from `api/`, `docs/api.md`, `tests/fixtures`, `tests/api/`, `scripts/` and, if present, local files under `.captures/` (never quote values from those).

- **API-01** Endpoints: every URL, method, header, query parameter and request body in `onboarding.py`, `portal.py` and `attestation.py` is described in `docs/api.md`, and every endpoint `docs/api.md` says the client uses is called.
- **API-02** Response keys: every key under "KEYS IN RESPONSES, NEVER READ" and "KEYS READ, IN NO RESPONSE" from `api_shapes.py`. Classify each: ignored on purpose (personal data, or not needed: Pass), read but missing from every fixture (a finding if the parser depends on it, because nothing tests that path; Pass if it's a header or request field), or a key whose absence breaks parsing.
- **API-03** Shapes: the "SHAPES" section. For each key path the parsers read, every type seen in the fixtures is handled (for example a value that's sometimes a list and sometimes an object, which `_as_list` exists for; a number that's sometimes a string). A probe test for each type that isn't handled.
- **API-04** Parsers against every fixture: `parse_icp` on every `icp.json`, `parse_plan_list` on every `plan_list.json`, `parse_plan_titles` on `plan_mappings.json`, `parse_indicator` and `parse_values` on the portal fixtures. Each must parse without error and give values that match the fixture (spot-check every field of one plan and one ICP per network by hand).
- **API-05** Rates: `_rate_unit` handles every unit string in every fixture; charge codes and bucket prefixes match `docs/api.md` "Charge codes"; every rate in every fixture plan maps to a bucket.
- **API-06** Seasons: every season date format in the fixtures parses; empty `seasons` gives `_rates_season`; overlapping seasons appear as `docs/api.md` says.
- **API-07** Attestation: the token flow in `attestation.py` (challenge, `solve_challenge`, expiry and refresh) matches `docs/api.md` "Attestation token", and an expired or rejected token is retried at most as documented.
- **API-08** Portal session: login, cookies in the entry's own session, `_SessionExpiredError` causing one re-login and retry, never a loop, and `AuthError` leading to reauth.
- **API-09** Errors and timeouts: every request has a timeout; every aiohttp and JSON error maps to an exception in `api/exceptions.py`; `returnCode` errors become `EcotricityResponseError`; nothing raises a bare `KeyError`, `TypeError` or `ValueError` to Home Assistant.
- **API-10** Tooling and unknowns: `scripts/api_client_smoke_test.py` and `scripts/build_ecotricity_network_price_plans.py` call the same client methods with the same arguments the integration uses, and the builder still paces requests and stops at the first refused connection. Each item in `docs/api.md` "Unknowns" is still unknown: if the code now relies on an answer, the doc is out of date (a DOC finding under DOC-05) or the code assumes something unverified (an API finding, quote the code).

### PY: Python best practice

Guidelines: PEP 8 (https://peps.python.org/pep-0008/), PEP 257 (https://peps.python.org/pep-0257/), PEP 484 (https://peps.python.org/pep-0484/), typing best practices (https://typing.python.org/en/latest/reference/best_practices.html), mypy strict (https://mypy.readthedocs.io/en/stable/existing_code.html), asyncio development (https://docs.python.org/3/library/asyncio-dev.html), Ruff rules (https://docs.astral.sh/ruff/rules/), pytest good practices (https://docs.pytest.org/en/stable/explanation/goodpractices.html), aiohttp client usage (https://docs.aiohttp.org/en/stable/client_quickstart.html, https://docs.aiohttp.org/en/stable/client_advanced.html).

- **PY-01** Ruff format, Ruff check and mypy pass (baseline). Each failure is a finding.
- **PY-02** Every `noqa` and `type: ignore` has a reason in the comment, as `CLAUDE.md` requires, and the reason is still true (the suppressed rule would actually fire).
- **PY-03** Typing: `Any` appears only in the parsers; no `cast` that hides a real type error; public functions in `api/` are fully typed; models are frozen dataclasses (`@dataclass(frozen=True)` or `slots=True, frozen=True`).
- **PY-04** asyncio: no blocking call in a coroutine or callback (file I/O, `time.sleep`, `requests`, Pillow, `zoneinfo.ZoneInfo(...)` construction, CPU-heavy loops); every task created is kept or tracked (`hass.async_create_task`, `entry.async_create_background_task`); no un-awaited coroutine; cancellation isn't swallowed (`except Exception` around an `await` that could be cancelled is fine; `except BaseException` or bare `except` isn't).
- **PY-05** Time: arithmetic on UTC datetimes only, as `CLAUDE.md` requires. Grep every `timedelta` addition and subtraction and every `.replace(` on a datetime, and check the operand is UTC or a date.
- **PY-06** Logging: `LOGGER` with `%` formatting, no f-strings, the right level (no `error` for an expected retry), no secret in any argument (also SEC-01).
- **PY-07** Exceptions: no bare `except`, no `except Exception` that hides a bug where a narrower type is known, `raise ... from err` when wrapping.
- **PY-08** Tests follow pytest good practice and `CLAUDE.md` "Testing": named by scenario, no network, time frozen where it matters, no hard-coded plan IDs, codes or chart names, expected values from rates.

### HA: Home Assistant best practice

Guidelines: quality scale (https://developers.home-assistant.io/docs/core/integration-quality-scale/) and its rules (https://developers.home-assistant.io/docs/core/integration-quality-scale/rules), config flows (https://developers.home-assistant.io/docs/config_entries_config_flow_handler), fetching data (https://developers.home-assistant.io/docs/integration_fetching_data), entities (https://developers.home-assistant.io/docs/core/entity), sensors and long-term statistics (https://developers.home-assistant.io/docs/core/entity/sensor), blocking operations (https://developers.home-assistant.io/docs/asyncio_blocking_operations), repairs (https://developers.home-assistant.io/docs/core/platform/repairs), API libraries (https://developers.home-assistant.io/docs/api_lib_index), the manifest (https://developers.home-assistant.io/docs/creating_integration_manifest), translations (https://developers.home-assistant.io/docs/internationalization/core), websocket commands (https://developers.home-assistant.io/docs/frontend/extending/websocket-api), custom cards (https://developers.home-assistant.io/docs/frontend/custom-ui/custom-card), testing (https://developers.home-assistant.io/docs/development_testing), HACS (https://www.hacs.xyz/docs/publish/start/, https://www.hacs.xyz/docs/publish/integration/).

- **HA-01** Every Bronze, Silver and Gold rule on the rules page, checked against the code (not just `quality_scale.yaml`, which DOC-11 checks). Read each rule's page and quote the requirement for any finding.
- **HA-02** Coordinators: `DataUpdateCoordinator` used as the fetching data guide says; `UpdateFailed` and `ConfigEntryAuthFailed` raised in the right places; `retry_after` usage; `always_update` set deliberately; first refresh in setup.
- **HA-03** `runtime_data` holds the coordinators and clients, with a typed `ConfigEntry` alias; nothing stored in `hass.data` per entry.
- **HA-04** Entities: `has_entity_name`, `translation_key`, unique IDs as `CLAUDE.md` says, device info, `entity_category` as the "Entity categories" rule in `CLAUDE.md` says (check every entity), `entity_registry_enabled_default`, `available`, state class and device class valid for the unit.
- **HA-05** Sensor rules: a sensor with `state_class` has a numeric state; `device_class` matches the unit (`monetary` with a currency, `energy` with kWh, `timestamp` with an aware datetime); `options` for enum sensors lists every state the sensor can take.
- **HA-06** Translations and icons: every entity, exception, issue and service has translations and icons (from `doc_refs.py`), placeholders match between `strings.json` and the code that fills them.
- **HA-07** Config flow: unique ID set and `_abort_if_unique_id_configured`, reauth and reconfigure steps update the entry correctly, errors map to `strings.json` keys, `probatio` used rather than `voluptuous`.
- **HA-08** Setup and unload: `async_setup_entry` raises `ConfigEntryNotReady` or `ConfigEntryAuthFailed` correctly, everything registered (listeners, timers, websocket commands, frontend resources, services) is cleaned up on unload or registered once in `async_setup`.
- **HA-09** Sessions: the onboarding client uses `async_get_clientsession`; the portal uses `async_create_clientsession`; the config flow's session uses `auto_cleanup=False` and `detach()`, as `CLAUDE.md` says.
- **HA-10** Statistics: external statistics use `async_add_external_statistics` with the domain as source, metadata as STAT-01 says, and recorder calls run in the recorder's executor (`get_instance(hass).async_add_executor_job`).
- **HA-11** Websocket commands: decorated with `websocket_command` and a schema, `async_response` where they await, and permission handling (see SEC-07).
- **HA-12** Frontend registration: `card.py` serves the bundle with a cache-busting version and registers it once; the card's `getConfigElement`, `getStubConfig` and `setConfig` follow the custom card docs.
- **HA-13** Repairs: each issue has a translation, is deleted when the problem is fixed, and isn't raised for a condition that fixes itself (as `CLAUDE.md` says for missing charts).
- **HA-14** Diagnostics: `async_redact_data` covers every secret and personal field (also SEC-04).
- **HA-15** Manifest and HACS: `manifest.json` keys (`domain`, `name`, `codeowners`, `config_flow`, `documentation`, `integration_type`, `iot_class`, `issue_tracker`, `loggers`, `quality_scale`, `requirements`, `version`, `dependencies` including `recorder`, `http`, `frontend` and `websocket_api` if used) match what the code uses; `hacs.json` minimum Home Assistant matches the `pytest-homeassistant-custom-component` pin.
- **HA-16** Brand images keep the home-assistant/brands sizes `docs/development.md` lists (check with `file` or Pillow).

### TS: TypeScript, Lit and frontend tooling best practice

Guidelines: TSConfig reference (https://www.typescriptlang.org/tsconfig/), TypeScript handbook (https://www.typescriptlang.org/docs/handbook/intro.html), Lit lifecycle (https://lit.dev/docs/components/lifecycle/), Lit reactive properties (https://lit.dev/docs/components/properties/), Lit directives including `unsafeHTML` (https://lit.dev/docs/templates/directives/), Vitest (https://vitest.dev/guide/), esbuild (https://esbuild.github.io/api/), Home Assistant custom cards (https://developers.home-assistant.io/docs/frontend/custom-ui/custom-card).

- **TS-01** Type check, tests and build pass (baseline). Each failure is a finding.
- **TS-02** No `any`, non-null `!` assertions or `as` casts that hide a possible `undefined` (with `noUncheckedIndexedAccess` on); each is either justified by the code around it or a finding.
- **TS-03** Lit: reactive properties declared with `@property` or `@state` (or `static properties`); derived state computed in `willUpdate`, not in `updated` (which triggers a second render) or in `render`; no side effects (fetches, subscriptions) in `render`.
- **TS-04** Subscriptions and listeners started in `connectedCallback` or on `hass` set are stopped in `disconnectedCallback`; no fetch loop when `hass` changes on every state update (check the fetch is guarded by `FETCH_OPTIONS` and the date range).
- **TS-05** Element registration: every `customElements.define` is guarded against double registration, and none runs at the entry's top level, as `CLAUDE.md` says.
- **TS-06** Editor: every option in the card's config is in the editor's schema, `DEFAULTS` and `validateConfig`, and every option that changes what's fetched is in `FETCH_OPTIONS`.
- **TS-07** Tests: no shared state between tests, timers and dates faked where they matter, `happy-dom` used consistently.
- **TS-08** Build: esbuild target matches `tsconfig.json` `target`; the bundle is git-ignored and built by `scripts/setup`, `scripts/develop` and CD, as `CLAUDE.md` says.

### CI: workflows, Dependabot and pre-commit

Guidelines: GitHub Actions security hardening (https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions), zizmor audits (https://docs.zizmor.sh/), OWASP CI/CD Security Cheat Sheet (https://cheatsheetseries.owasp.org/cheatsheets/CI_CD_Security_Cheat_Sheet.html), Dependabot (https://docs.github.com/en/code-security/dependabot), OpenSSF best practices (https://best.openssf.org/).

- **CI-01** Every `uses:` is pinned to a full commit SHA with a version comment.
- **CI-02** Every workflow and job has least-privilege `permissions` (top-level `permissions: {}` or read-only, with write granted only to the job that needs it).
- **CI-03** Every `actions/checkout` sets `persist-credentials: false` unless a later step needs to push, with a comment saying so.
- **CI-04** No `${{ github.event.* }}`, `${{ github.head_ref }}` or other attacker-controlled expression inside `run:`; no `pull_request_target` or `workflow_run` with a checkout of untrusted code.
- **CI-05** Each zizmor finding (from the baseline), verified by reading the workflow line it names. Suppressed findings count only if the suppression has a reason.
- **CI-06** CI runs every check in the "Definition of done" in `CLAUDE.md` (Ruff, mypy, pytest with 95% coverage, frontend type check, tests and build, pre-commit, hassfest, HACS validation).
- **CI-07** Dependabot covers every ecosystem the repo uses (pip or uv, npm, GitHub Actions, pre-commit if supported) with a sensible schedule.
- **CI-08** CD: the version is set from the tag (not by hand, as `CLAUDE.md` says), the frontend is built before packaging, and the release asset contains exactly the integration folder and the built card.

### SEC: security review

Guidelines: OWASP ASVS 5.0 (https://owasp.org/www-project-application-security-verification-standard/), OWASP Top 10 (https://owasp.org/Top10/), the OWASP Logging (https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html), Secrets Management (https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html), Session Management (https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), Error Handling (https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html), Input Validation (https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html), Transport Layer Security (https://cheatsheetseries.owasp.org/cheatsheets/Transport_Layer_Security_Cheat_Sheet.html), DOM-based XSS Prevention (https://cheatsheetseries.owasp.org/cheatsheets/DOM_based_XSS_Prevention_Cheat_Sheet.html), NPM Security (https://cheatsheetseries.owasp.org/cheatsheets/NPM_Security_Cheat_Sheet.html) and Vulnerable Dependency Management (https://cheatsheetseries.owasp.org/cheatsheets/Vulnerable_Dependency_Management_Cheat_Sheet.html) cheat sheets, CWE-532 sensitive information in logs (https://cwe.mitre.org/data/definitions/532.html), CWE-209 sensitive information in error messages (https://cwe.mitre.org/data/definitions/209.html), CWE-409 decompression bombs (https://cwe.mitre.org/data/definitions/409.html), CWE Top 25 (https://cwe.mitre.org/top25/), Python security considerations (https://docs.python.org/3/library/security_warnings.html), Pillow's `Image.MAX_IMAGE_PIXELS` decompression bomb protection (https://pillow.readthedocs.io/en/stable/reference/Image.html), Home Assistant diagnostics redaction (https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics), pip-audit (https://github.com/pypa/pip-audit), npm audit (https://docs.npmjs.com/cli/commands/npm-audit).

- **SEC-01 Logs.** Every `LOGGER.*` call and every `_LOGGER`/`logging` call in `custom_components/`, `scripts/` and tests: list each variable argument and trace what it can hold. A finding if any can hold a password, token, cookie, session ID, attestation value, full URL with a token, a raw response body (which can contain the address and meter serials), or the ICP at a level above `debug` without the README saying so (CWE-532).
- **SEC-02 Exceptions.** Every `raise` with a message built from data: the message never holds a secret or a raw response (CWE-209). `HomeAssistantError` translation placeholders never hold one either.
- **SEC-03 Storage.** What the config entry and `store.py` save: the password only where Home Assistant config entries hold credentials (matching `docs/security.md`), no tokens or cookies in the snapshot, and no personal field saved that no entity uses (as `CLAUDE.md` says).
- **SEC-04 Diagnostics.** The redaction set in `diagnostics.py` covers every secret and personal field in the entry data, options and snapshot (list every field and mark each redacted or not). A probe test with a populated entry that asserts no secret or identifier appears in the output.
- **SEC-05 Transport.** Every request is HTTPS, nothing passes `ssl=False` or `verify_ssl=False`, every request has a timeout, redirects to another host aren't followed with credentials.
- **SEC-06 Untrusted input.** Response parsing: JSON from Ecotricity is validated before use (API-09); chart images are size-checked before decoding or rely on Pillow's `MAX_IMAGE_PIXELS` and don't disable it (CWE-409); no `eval`, `pickle`, `yaml.load` without a safe loader, or `subprocess` with input from a response.
- **SEC-07 Websocket and frontend.** Each websocket command: does it need `require_admin`, and does it only return data for entries the integration owns? The data it returns is safe for any logged-in user to see (statistics IDs and plan data are; ICPs and personal fields would need justification). The card never uses `unsafeHTML`, `innerHTML` or `unsafeCSS` with data from the API (Lit escapes template text by default; check every exception to that).
- **SEC-08 Personal data in the repo.** Every candidate from `pii_scan.py`. Open each and decide whether it's real or fake. A real ICP, address, meter serial, account number, email or phone number is High. Also check `git log -p` for personal data in history only if the scan finds a real value in the current tree (report the commit, never the value).
- **SEC-09 Dependencies.** Every pip-audit and npm audit result. Classify each: a dependency this repo pins and ships (finding), a development or test dependency (finding, lower severity), or one Home Assistant ships and the integration doesn't pin, such as Pillow (Pass, note the Home Assistant version that fixes it if known). `manifest.json` `requirements` adds nothing unpinned.
- **SEC-10 Secrets in files.** `.env` and `.captures/` are git-ignored (check `.gitignore` and `git check-ignore`), `detect-private-key` runs in pre-commit, and no tracked file holds a credential (from `pii_scan.py`).
- **SEC-11 Outside Home Assistant.** Every host the code contacts (grep every URL and hostname in `custom_components/` and `frontend/src`), compared with `docs/security.md`. Anything sent to a host the doc doesn't list is a finding.

## 7. Clean up

1. Run `git status --porcelain` at the start of Phase 0 and save it. At the end, delete every probe test you created, then run it again: the output must be identical. If it isn't, say which files differ in the report and don't try to restore them yourself.
2. Don't commit anything.

## 8. Report

Write the report in this exact shape and order. Use the same headings every run, even when a section is empty.

```markdown
# Full review: <branch> at <short commit SHA>, <date>

## Summary
<counts: findings by severity and by section; checks passed, with findings, not applicable>

## Baseline
| Command | Result | Notes |

## Findings
### DEAD
#### DEAD-03-1 <one-line title>
- Severity: High | Medium | Low
- Location: `file:line` (every location, if several)
- Issue: <what's wrong, in plain words>
- Likely because: <which section 3 source shows it happens> (PRICE, TOU, SEASON, CHART, STAT, API and TEST only)
- Verified by: <evidence type (A) to (D), with the exact command, test, quotes or guideline clause>
- Recommendation: <the specific change, naming functions and files>
<repeat for every finding; "No issues found." if the section has none>
### DOC
### TEST
### PRICE
### TOU
### SEASON
### CHART
### STAT
### API
### PY
### HA
### TS
### CI
### SEC

## Check results
| Check | Verdict | Findings or reason |
<one row for every check ID in section 6, in order>

## Checked and dismissed
| Check | Candidate | Why it isn't an issue |

## File coverage
| File | Checks |
<every file from Phase 1>

### Unreviewed files
<normally "None.">

## Probe tests
<each probe file's full code, with which findings it supports>
```

Finding IDs are `<check ID>-<n>`, numbered from 1 within each check in order of file path then line number. Sort findings within a section by check ID, then file path, then line.

After writing the report, give the maintainer a short summary in chat (counts by severity, and the High findings by title), and offer to save the full report as a file or publish it.

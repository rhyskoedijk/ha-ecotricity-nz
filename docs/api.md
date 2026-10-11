# Ecotricity services reference

What we know about the two Ecotricity websites this integration reads, worked out from the public web app's JavaScript and from browser captures in October 2026. Neither site has a published API, so any of this can change without notice. Examples use a made-up ICP (`0000123456AB1C2`) and placeholder IDs.

## Onboarding site (public)

Base URL `https://onboarding.ecotricity.co.nz`. This is the Angular sign-up app. It reads its service URLs from `/assets/config/config.json`:

| Service | Path |
| --- | --- |
| ICP details | `/cgi-bin/RESTICP/energy/{icp}` |
| Plans | `/cgi-bin/RESTPLAN/plan/` |
| Monthly estimate | `/cgi-bin/RESTMONTHLYESTIMATE/monthlyestimate/` |
| Sign-up session | `/cgi-bin/RESTUSER/user/` |

The integration only reads ICP details, plans and timeframe charts.

The monthly estimate service returns `"Please Call"` for residential ICPs, so it's unused. **Never call `RESTUSER/user/change`.** It saves the visitor's address against a sign-up session and creates a lead at Ecotricity.

### Attestation token

The ICP and plan services reject requests without an `X-Onboarding-Token` header (HTTP 403). The web app gets a token by proving it can lay out HTML:

1. `GET /cgi-bin/RESTATTEST/challenge` returns `{"cid", "expiry", "sig", "params": {"container_width", "items": [...]}}`.
2. The app renders a hidden flex container `container_width` pixels wide with `flex-wrap: wrap`, containing one box per item, each `items[i]` pixels wide with no margin, padding or border. It finds the index of the first box that wraps onto a second row (or the number of boxes if none wrap).
3. The answer is the lowercase hex SHA-256 of `"{cid}|{index}"`.
4. `POST /cgi-bin/RESTATTEST/token` with `{"cid", "expiry", "sig", "params", "answer"}` returns `{"token", "expires"}`, where `expires` is a Unix timestamp.

The app reuses a token until 90 seconds before `expires` or until it has made 45 calls with it. If a gated call gets a 403, it mints a fresh token and retries once with `X-Onboarding-Attestation-Retry: 1`. If the token POST itself gets a 403, it starts again with a new challenge, once.

Since the boxes have fixed widths and no spacing, the layout is a running sum, and `custom_components/ecotricity_nz/api/attestation.py` reproduces it without a browser. This was a deliberate choice, accepting the terms-of-use and breakage risk, because the onboarding site is the only source of Ecotricity's prices. The app also posts failures to `/cgi-bin/RESTATTEST/report`; the integration doesn't.

### ICP details

`GET /cgi-bin/RESTICP/energy/{icp}` returns registry data under `content`: `network` (network code and name), `pricing` (price category codes and loss factors), `metering` (meter components and channels), `retailer` and `address`.

One quirk: lists with a single item are collapsed into a bare object, for example `MeteringChannel` may be an object or a list. The parser accepts either.

### Plans

`GET /cgi-bin/RESTPLAN/plan/list_unbundle/` takes these query parameters, all built from the ICP details:

| Parameter | Source |
| --- | --- |
| `networkCode` | `network.networkIdentifier`, the distributor's four-letter code, for example `EXLN` |
| `capacityCode` | the active `PriceCategoryCode.code`, the distributor's price category, for example `RTOU24` |
| `meterType` | every channel's `vRegisterContentCode`, comma-joined in order, for example `UN,EG` |
| `customerType` | `Residential` |
| `icpNumber` | the ICP |

The example codes are made up. Real network codes, price categories, register codes and plan codes vary from network to network across the country, so the integration never hard-codes them; it passes on whatever the ICP details contain.

The response's `content[0]` holds `default` (the plan the site suggests), `alternative` and `plan_list`. Each plan has:

- `retail_rates` and `network_rates`, each a list of `{charge, charge_description, register_type, unit, price, valid_start, valid_end, season}`. Prices are cents excluding GST. Units are `cents/kWh` or `cents/d`. Export rates have `register_type` `EG` and negative prices. Network rates carry a `priceplan_id` key, which is how the web app tells them apart.
- `seasons`, a list of `{season, start, end}`. These populate the site's season dropdown ("Seasonal Rates 04/2026 - 09/2026").
- `timeframes_chart_wd` and `timeframes_chart_we`, paths to the weekday and weekend charts described below.

The `default` plan is not necessarily the customer's plan, and neither site reliably says which plan a customer is on, so the user chooses it during setup. The choice is stored as the plan's `id`.

### Plan names

`price_plan_description` is an internal name ("LowBATTERY User"). The web app shows customers a different name ("Low ecoSOLAR"), looked up in `GET /assets/planmappings.json`. That file is static and needs no token. Its `mappings` list pairs a lower-case internal `name` with the customer-facing `title`, plus card text and colours. A `default` entry ("ecoSPECIAL") is used for plans not in the list; the integration ignores it and falls back to the internal name instead.

### Seasons change

Seasons last a few months and start on the 1st of a month. The list in a plan response covers the current pricing year, but Ecotricity can add a season, change its dates, or change rates whenever it likes, and the next year's seasons may only appear shortly before 1 April. So nothing about seasons is cached for long:

- Plans, seasons and rates are fetched again every day, and again just after each NZ midnight, so a season that starts on the 1st applies from 12:00am that day.
- Charts are downloaded again at least once a day, in case one is re-issued for the same season dates.
- If no published season covers today, the plan's prices still apply (rates carry their own validity dates), but the time-of-use hours can't be read. The current price sensors go unavailable until the season is published, and a repair issue is raised after three refreshes in a row without one (about a day and a half).
- A new season can bring a new bucket (a shoulder rate, say) or a new daily charge. Its price sensor is created at the next refresh.

### How the site combines rates

`tariff.py` copies the web app's `mapPricePlanToOldFormat` and `mergeAnytimeRetailIntoTouNetwork`, plus one rule only visible in the charts:

- Each rate goes into a bucket by its `charge` code (`CHARGE_24` anytime, `CHARGE_HIGH` peak, `CHARGE_LOW` off peak, `CHARGE_3` shoulder), prefixed `EG_` for export. Rates without a code are bucketed by description ("off peak", "on peak", "shoulder", "anytime").
- Only rates whose `valid_start`/`valid_end` cover the day apply.
- Any per-kWh rate with "levy" in its description (the EA levy, 0.16c or 0.2c depending on plan) is added to every import bucket.
- Retail and network rates in the same bucket are summed.
- If a plan has an anytime retail energy rate and time-of-use network rates, the anytime rate is added to each time-of-use bucket. This applies to export as well as import.
- If a time-of-use bucket has retail energy but no network rate that day, the off-peak network rate applies. Some networks only publish a peak network rate for their peak season and charge peak hours at the off-peak rate for the rest of the year. On networks that publish a rate for every period, this rule changes nothing.

These rules reproduce the legend prices on every chart captured so far: three plans across three seasons, all on one network. Other networks may combine rates in ways not seen yet. If they do, the chart check below catches it, because the bar heights won't match the computed prices, and the integration reports the problem rather than showing a wrong price.

Ecotricity's site recognises four charge codes: `CHARGE_24` (anytime), `CHARGE_HIGH` (peak), `CHARGE_LOW` (off-peak) and `CHARGE_3` (shoulder). The captured network uses the first three. Anytime only shows as a period when a network has no time-of-use rates for that direction (otherwise it's folded into them), and shoulder is used by networks with a third tier, such as Orion's 5 to 7am and 11am to 5pm weekday shoulder. A code outside these four would still be kept as its own bucket, priced and matched to the charts the same way, but it has no period.

### Controlled load

ICPs with a controlled-load register (such as ripple-controlled hot water) have the registry register content code `CN` among their channels, alongside `UN` (uncontrolled), `EG` (export) and others. Every register code is passed to the plan lookup in `meterType`, so the plan's rates cover each register.

Ecotricity's site keeps rates for different registers apart (it groups rows by charge code, register type and unit). The integration does the same for controlled load: a rate whose `register_type` is `CN`, or whose description says "controlled" but not "uncontrolled", goes into its own `CN_` buckets. Those are priced with the same rules as import (including the EA levy), but kept out of import prices and chart matching. A controlled rate with no charge code and no period in its description is treated as anytime, because the network switches controlled load on and off rather than the clock.

This hasn't been checked against a real controlled-load ICP yet; the captured ICP has only `UN` and `EG` registers.

### GST

Plan prices in the API exclude GST. The web app has a GST toggle that multiplies every row by 1.15, export rates included, but that's a display choice, not what customers are paid.

For residential customers who aren't GST-registered, NZ export buy-back rates are paid as published, with GST added only for GST-registered customers ([Nova Energy](https://www.novaenergy.co.nz/electricity/solar-buy-back) states this outright; [Solar Scout](https://solarscout.co.nz/solar-news/solar-buy-back-rates-nz-2026/) puts it as "if you are not GST-registered the number you see is the number you get"). Ecotricity doesn't state its GST basis, but its [solar page](https://ecotricity.co.nz/solar) quotes ecoSOLAR at 21c peak and 16c off-peak export alongside an 18c low-user import rate, and 18c is the GST-exclusive figure the plan data gives, so the export figures are on the same basis.

So the integration adds GST to import prices and daily charges (when the option is on) and never to export credits.

### Timeframe charts (time-of-use hours)

The hours each bucket applies are not in any API response or page data. The web app only shows them as server-rendered PNGs, and NZ has no public dataset of network time-of-use windows (the Electricity Authority's EIEP12 price files have rates but no times). The windows differ between networks and seasons, and change most years on 1 April, which is why they're read from each plan's own charts rather than kept in a table.

Chart URLs come from the plan's `timeframes_chart_wd` or `timeframes_chart_we`, for example `/timeframeimages_ecotricity/EXLN-RTOU24-EXLN_OFPK_LOW_260401_T0-RESIDENTIAL-SSSSSSSS-EEEEEEEE-WD.png` (the codes in it are made up). Replace `SSSSSSSS` and `EEEEEEEE` with the season's start and end dates (`YYYYMMDD`), and add `-NOGST` before `.png` for GST-exclusive prices. Charts are static files and need no token.

Each chart draws 48 half-hour bars, blue import above the axis and orange export below, with each bar's height proportional to its price and its shade marking its tier. `custom_components/ecotricity_nz/api/timeframes.py` finds the bars, groups them by colour, and measures each group's height. The legend shows the full palette: 7 shades of blue for import and 6 of orange for export, light to dark. The closest two are only 23.5 apart in RGB distance, so bars are grouped into a tier only when their colours are within 12, which keeps every shade separate. That matters for plans with three tiers (peak, shoulder and off-peak), which can use neighbouring shades. The heights are then matched against the plan's bucket prices for that season: one pixels-per-cent scale has to fit every group to within 0.6c, or the chart is rejected. A rejected chart leaves the current price sensors unavailable and, after three failures in a row, raises a repair issue.

## Customer portal (private)

Base URL `https://portal.ecotricity.co.nz/powercommerce/eco/fo/portal/`. It's a PowerCommerce portal behind Cloudflare.

### Login

1. `GET` the base URL to get a `JSESSIONID` cookie (path `/powercommerce/eco/fo`).
2. `POST loginProcess` with form fields `login` (the customer account number) and `password`.
3. Success redirects (302) to `.../portal/home`. Failure redirects to `.../portal/start`. The redirect is to plain `http://`, so the client checks the `Location` header rather than following it.

An expired session answers JSON requests with a redirect or with the HTML start page. The client logs in again once, then gives up with an authentication error.

The analysis endpoints only work after a page containing the analysis widget has been loaded in the session; the web app always loads `/home` or `consumptionDetails` first. Calling `analysis/initData` straight after logging in returned HTTP 503 in testing. So after each login the client loads `consumptionDetails` once before its first analysis call. This is inferred from the browser's behaviour and the 503; it hasn't been confirmed by Ecotricity.

### JSON endpoints

All are called with `X-Requested-With: XMLHttpRequest`.

- `POST analysis/initData` with `{"fetchCurrentIndicator": true, "fetchLastImport": true, ...}` returns `currentIndicator` with `indicatorID`, `displayName` (the ICP), `lastImportDate`, and, for each feature, `measuredValueRange`, the range of data held.
- `POST analysis/valueRequest` with `dateType: "INDIVIDUAL"` accepts any range. Each feature request names a `featureID`, a `granularityType` (`HALF_HOUR`, `DAY`, `MONTH`, `YEAR`) and `aggregationType: "SUM"`. Values come back as `{"value", "measuringPeriod": {"from": "to"}}`.

| Feature ID | Meaning | Unit |
| --- | --- | --- |
| `POWER_ACTIVE_PURCHASE_CONSUMPTION` | Imported energy | kWh |
| `POWER_ACTIVE_SUPPLY_CONSUMPTION` | Exported energy | kWh |
| `EMISSION\|POWER_ACTIVE_PURCHASE_CONSUMPTION\|Carbon Savings` | Carbon savings | kg |

`analysis/csvDownload` takes no parameters and exports whatever the last `valueRequest` asked for. Its export column was a day out of step with the others in the capture, so the integration doesn't use it.

### Timestamps

Every portal timestamp carries a `+02:00` or `+01:00` offset, which is Europe/Berlin (the server's zone), but the wall-clock part is New Zealand time. Solar export appears between 08:30 and 17:00 on those timestamps; as true Berlin times that would be the middle of the night in NZ.

So the offset is discarded and the wall-clock time is read as `Pacific/Auckland`. Request ranges are sent the way the web app sends them: NZ wall-clock time with the Berlin offset for that date attached.

When NZ daylight saving ends, the wall-clock hour from 2am to 3am happens twice. The parser assigns the second occurrence to the later fold, so readings keep moving forward. A capture of a real daylight-saving-end day hasn't been checked yet.

## Unknowns

- The longest range one `valueRequest` accepts at half-hourly resolution. The integration asks for seven days at a time.
- How long a portal session lasts.
- What the portal returns for the hour repeated when daylight saving ends.
- Whether weekend charts ever differ from weekday charts on networks that charge peak rates at weekends. The integration always fetches both.

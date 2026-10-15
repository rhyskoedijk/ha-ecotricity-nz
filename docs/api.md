# Ecotricity services reference

This page explains what is known about the two Ecotricity websites this integration reads, worked out from the public web app's JavaScript and from browser captures. Neither site has a published API, so any of this can change without notice. Examples use a made-up ICP (`0000123456AB1C2`) and placeholder IDs.

## Onboarding site (public)

Base URL `https://onboarding.ecotricity.co.nz`. This is the Angular sign-up app. It reads its service URLs from `/assets/config/config.json`:

| Service          | Path                                            |
| ---------------- | ----------------------------------------------- |
| ICP details      | `/cgi-bin/RESTICP/energy/{icp}`                 |
| Plans            | `/cgi-bin/RESTPLAN/plan/`                       |
| Monthly estimate | `/cgi-bin/RESTMONTHLYESTIMATE/monthlyestimate/` |
| Sign-up session  | `/cgi-bin/RESTUSER/user/`                       |

The integration only reads ICP details, plans and timeframe charts.

The monthly estimate service returns `"Please Call"` for residential ICPs, so it's unused.

**Never call `RESTUSER/user/change`.** It saves the visitor's address against a sign-up session and creates a lead at Ecotricity.

### Attestation token

The ICP and plan services reject requests without an `X-Onboarding-Token` header (HTTP 403). The web app gets a token by proving it can lay out HTML:

1. `GET /cgi-bin/RESTATTEST/challenge` returns `{"cid", "expiry", "sig", "params": {"container_width", "items": [...]}}`.
2. The app renders a hidden flex container `container_width` pixels wide with `flex-wrap: wrap`, containing one box per item, each `items[i]` pixels wide with no margin, padding or border. It finds the index of the first box that wraps onto a second row (or the number of boxes if none wrap).
3. The answer is the lowercase hex SHA-256 of `"{cid}|{index}"`.
4. `POST /cgi-bin/RESTATTEST/token` with `{"cid", "expiry", "sig", "params", "answer"}` returns `{"token", "expires"}`, where `expires` is a Unix timestamp.

The app reuses a token until 90 seconds before `expires` or until it has made 45 calls with it. If a gated call gets a 403, it mints a fresh token and retries once with `X-Onboarding-Attestation-Retry: 1`. If the token POST itself gets a 403, it starts again with a new challenge, once. The integration treats only a 403 as the token scheme refusing it; a 404 or 5xx from `/challenge` or `/token`, or a reply that isn't JSON (such as a maintenance page served with a 200), is handled as an outage, so it doesn't raise the attestation repair issue.

Since the boxes have fixed widths and no spacing, the layout is a running sum, and `custom_components/ecotricity_nz/api/attestation.py` reproduces it without a browser. This was a deliberate choice, accepting the terms-of-use and breakage risk, because the onboarding site is the only source of Ecotricity's prices. The app also posts failures to `/cgi-bin/RESTATTEST/report`; the integration doesn't.

### ICP details

`GET /cgi-bin/RESTICP/energy/{icp}` returns registry data under `content`: `network` (network code and name), `pricing` (price category codes and loss factors), `metering` (meter components and channels), `retailer` and `address`.

One quirk: lists with a single item are collapsed into a bare object, for example `MeteringChannel` may be an object or a list. The parser accepts either.

### Plans

`GET /cgi-bin/RESTPLAN/plan/list_unbundle/` takes these query parameters, all built from the ICP details:

| Parameter      | Source                                                                                      |
| -------------- | ------------------------------------------------------------------------------------------- |
| `networkCode`  | `network.networkIdentifier`, the distributor's four-letter code, for example `CKHK`         |
| `capacityCode` | the active `PriceCategoryCode.code`, the distributor's price category, for example `RTOU24` |
| `meterType`    | every channel's `vRegisterContentCode`, comma-joined in order, for example `UN,EG`          |
| `customerType` | `Residential`                                                                               |
| `icpNumber`    | the ICP                                                                                     |

The service doesn't check the ICP: requests with no `icpNumber`, an empty one or a made-up one return the same plans for a network and price category. Without a `capacityCode` it returns an empty `plan_list` and no default plan. Every network's price categories are public, in the Electricity Authority's registry data (see [development.md](development.md#networks-and-price-categories)). Within a network, the price category has so far changed only the suggested default plan and, on Orion, the daily network charge; the plans, seasons, per-kWh rates and charts were the same (see [development.md](development.md#choosing-price-categories)).

The site rate-limits by IP. It was observed when about 200 plan list and chart requests in a few minutes got the IP refused for about 24 hours (connections refused, not an HTTP error). The integration makes a handful of requests a week per install, far below that.

`CKHK` is the real code for Wellington Electricity. Other example price categories, register codes and plan codes are illustrative; these values vary from network to network, so the integration never hard-codes them and passes on whatever the ICP details contain.

The response's `content[0]` holds `default` (the plan the site suggests), `alternative` and `plan_list`. Each plan has:

- `retail_rates` and `network_rates`, each a list of `{charge, charge_description, register_type, unit, price, valid_start, valid_end, season}`. Prices are cents excluding GST. Units are `cents/kWh` or `cents/d`. Export rates have `register_type` `EG` and negative prices. Network rates carry a `priceplan_id` key, which is how the web app tells them apart.
- `seasons`, a list of `{season, start, end}`. These populate the site's season dropdown ("Seasonal Rates 04/2026 - 09/2026").
- `timeframes_chart_wd` and `timeframes_chart_we`, paths to the weekday and weekend charts described below.

The `default` plan is not necessarily the customer's plan, and neither site reliably says which plan a customer is on, so the user chooses it during setup. The choice is stored as the plan's `id`.

### Plan names

`price_plan_description` is an internal name ("LowBATTERY User"). The web app shows customers a different name ("Low ecoSOLAR"), looked up in `GET /assets/planmappings.json`. That file is static and needs no token. Its `mappings` list pairs a lower-case internal `name` with the customer-facing `title`, plus card text and colours. A `default` entry ("ecoSPECIAL") is used for plans not in the list; the integration ignores it and falls back to the internal name instead.

### Seasons change

Seasons last a few months and start on the 1st of a month. The list in a plan response covers the current pricing year, but Ecotricity can add a season, change its dates, or change rates whenever it likes. Season dates and lengths also differ between networks. So the integration saves what it fetches and works out each day from that, but fetches again often enough to notice changes (the schedule is in [architecture.md](architecture.md#pricing)):

- Plans, seasons, rates and charts are fetched weekly, on the 1st of each month, and on the last day of each season. Rates and seasons carry their own dates, so a season that starts on the 1st applies from 12:00am that day without a fetch.
- Once the next season appears in the plan list, the next fetch downloads its charts too, so the switch doesn't depend on a connection at midnight.
- Charts are downloaded again at every fetch, in case one is re-issued for the same season dates.
- If no published season covers today or tomorrow, pricing is fetched daily until one does. The plan's prices still apply meanwhile (rates carry their own validity dates), but the time-of-use hours can't be read for a day with no season. The current price sensors go unavailable until the season is published, and a repair issue is raised after three fetches in a row without one (about three days).
- A new season can bring a new bucket (a shoulder rate, say) or a new daily charge. Its price sensor is created when the season starts, or straight away if it's already started.

### How the site combines rates

`tariff.py` copies the web app's `mapPricePlanToOldFormat` and `mergeAnytimeRetailIntoTouNetwork`, plus one rule only visible in the charts:

- Each rate goes into a bucket by its `charge` code (`CHARGE_24` anytime, `CHARGE_HIGH` peak, `CHARGE_LOW` off peak, `CHARGE_3` shoulder), prefixed `EG_` for export. Rates without a code are bucketed by description ("off peak", "on peak", "shoulder", "anytime"); any other description keeps its own bucket.
- Only rates whose `valid_start`/`valid_end` cover the day apply.
- Any per-kWh rate with "levy" in its description (the EA levy, 0.16c or 0.2c depending on plan) is added to every import bucket.
- Retail and network rates in the same bucket are summed.
- If a plan has an anytime retail energy rate and time-of-use network rates, the anytime rate is added to each time-of-use bucket. This applies to export as well as import.
- If a time-of-use bucket has retail energy but no network rate that day, the off-peak network rate applies. Some networks only publish a peak network rate for their peak season and charge peak hours at the off-peak rate for the rest of the year. On networks that publish a rate for every period, this rule changes nothing.
- An anytime network rate with no anytime retail rate beside it is added to every other bucket in its group (import, export or controlled), after the rule above, so it isn't counted as a period's own network rate. Vector publishes a "Transmission Charge" this way, a `CHARGE_24` network rate next to its peak and off-peak network rates, and its charts include it in every period's price.

### Charge codes

Ecotricity's web app recognises multiple charge codes: `CHARGE_24` (anytime), `CHARGE_HIGH` (peak), `CHARGE_LOW` (off-peak) and `CHARGE_3` (shoulder) are common. Plans can also use further numbered codes that the network defines. This is how each code is used in the test fixtures so far, based on network fixtures tested to date:

| Code          | Period in the integration | Use in the fixtures                                                                                                                                                                                                                                                                                                                |
| ------------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CHARGE_24`   | `anytime` (fixed)         | The EA levy on every plan, anytime retail energy rates, flat export credits, and anytime network charges such as Vector's transmission charge. On plans with time-of-use network rates, the anytime retail rate is folded into each of them, so `CHARGE_24` only stands as a bucket of its own on plans without them.              |
| `CHARGE_HIGH` | `peak` (fixed)            | Peak network rates and peak export credits on both networks. One network publishes its peak import network rate only for its winter season; both pay a peak export network credit only in part of the year. Peak retail rates appear on one network's time-of-use plans; the other network's plan has only an anytime retail rate. |
| `CHARGE_LOW`  | `off_peak` (fixed)        | Off-peak network rates and off-peak export credits on both networks, all year. Off-peak retail rates appear on the same plans as peak retail rates.                                                                                                                                                                                |
| `CHARGE_3`    | named from the plan       | Commonly used as "Shoulder Usage" network rate on weekdays from 5am to 7am and 11am to 5pm.                                                                                                                                                                                                                                        |
| `CHARGE_4`    | named from the plan       | Commonly used as "Super Off Peak Usage" network rate of 0c from 3am to 5am every day, weekdays and weekends. With the anytime retail rate folded in, it's the cheapest import price of the day.                                                                                                                                    |
| `CHARGE_5`    | named from the plan       | Commonly used as "Weekend Usage" network rate covering the whole weekend except 3am to 5am.                                                                                                                                                                                                                                        |

So far `CHARGE_3`, `CHARGE_4` and `CHARGE_5` only appear as import network rates. No retail rate or export credit uses them, and no plan's export has more than peak and off-peak.

Numbered codes like these aren't defined by Ecotricity, and another network could use the same number for something else. So only `CHARGE_24`, `CHARGE_HIGH` and `CHARGE_LOW` have fixed names. Every other bucket, `CHARGE_3` included, is named from the plan's own rate descriptions (retail first, then network) with "usage" and "export" dropped, and falls back to its code ("Charge 4") when no description names it. The customer portal lists the same descriptions, but leaves out rates priced at 0c, such as the `CHARGE_4` network rate above. When a plan has an anytime retail rate and network rates in other buckets, the retail rate is folded into every network-defined bucket, named or not. The chart still determines when those buckets apply.

### Controlled load

ICPs with a controlled-load register (such as ripple-controlled hot water) have the registry register content code `CN` among their channels, alongside `UN` (uncontrolled), `EG` (export) and others. Every register code is passed to the plan lookup in `meterType`, so the plan's rates cover each register.

Ecotricity's site keeps rates for different registers apart (it groups rows by charge code, register type and unit). The integration does the same for controlled load: a rate whose `register_type` is `CN`, or whose description says "controlled" but not "uncontrolled", goes into its own `CN_` buckets. Those are priced with the same rules as import (including the EA levy), but kept out of import prices and chart matching. A controlled rate with no charge code and no period in its description is treated as anytime, because the network switches controlled load on and off rather than the clock.

This hasn't been checked against a real controlled-load ICP yet; the captured ICP has only `UN` and `EG` registers.

### GST

Plan prices in the API exclude GST. The web app has a GST toggle that multiplies every row by 1.15, export rates included, but that's a display choice, not what customers are paid.

For residential customers who aren't GST-registered, NZ export buy-back rates are paid as published, with GST added only for GST-registered customers. Ecotricity doesn't state its GST basis, but its [solar page](https://ecotricity.co.nz/solar) quotes ecoSOLAR at 21c peak and 16c off-peak export alongside an 18c low-user import rate, and 18c is the GST-exclusive figure the plan data gives, so the export figures are on the same basis.

So the integration adds GST to import prices and daily charges (when the option is on) and never to export credits.

### Timeframe charts (time-of-use hours)

The hours each bucket applies are not in any API response or page data. The web app only shows them as server-rendered PNGs, and NZ has no public dataset of network time-of-use windows (the Electricity Authority's EIEP12 price files have rates but no times). The windows differ between networks and seasons, and change most years on 1 April, which is why they're read from each plan's own charts rather than kept in a table.

Chart URLs come from the plan's `timeframes_chart_wd` or `timeframes_chart_we`, for example `/timeframeimages_ecotricity/CKHK-RTOU24-CKHK_OFPK_LOW_260401_T0-RESIDENTIAL-SSSSSSSS-EEEEEEEE-WD.png`. `CKHK` is the real network code; the other codes are illustrative. Replace `SSSSSSSS` and `EEEEEEEE` with the season's start and end dates (`YYYYMMDD`), and add `-NOGST` before `.png` for GST-exclusive prices. Charts are static files and need no token.

Each chart draws 48 half-hour bars, blue import above the axis and orange export below, with each bar's height proportional to its price and its shade usually marking its tier. `custom_components/ecotricity_nz/api/timeframes.py` finds the bars, groups them by colour and height, and measures each group's height. The legend shows the full palette: 7 shades of blue for import and 6 of orange for export, light to dark. The closest two are only 23.5 apart in RGB distance, so bars are grouped into a tier only when their colours are within 12, which keeps every shade separate. That matters for plans with three tiers (peak, shoulder and off-peak), which can use neighbouring shades. A shade doesn't always mean one price, though: some network charts draw two close prices in the same shade only a few pixels apart (issue #9). Bars of one price measure the same height to the pixel on every captured chart, so bars of one shade more than 2px apart are separate tiers. The heights are then matched against the plan's bucket prices for that season: one pixels-per-cent scale has to fit every group to within 0.6c and 3 pixels, or the chart is rejected. The pixel limit matters when a price is close to zero, such as a 0c network rate plus the levy: anchoring the tallest bar on it gives a scale of hundreds of pixels per cent, where every bar is within 0.6c of that price, so the whole chart would otherwise read as that one price. On every captured chart the matched groups are within 1.4px of their prices, at 5 to 10 pixels per cent. The scale differs from chart to chart, but import and export bars share one axis (on every captured chart, both sides give the same pixels per cent to within 0.1). Because the two sides share one axis, a scale has to fit the other side's bars and prices too. That catches prices that are all wrong by the same amount (a charge left out, say), which one side alone can fit at a different scale. It also settles charts with few tiers, which can fit more than one scale on their own (a single tier fits any price). A side that can't be read at any scale is left out of this check rather than rejecting the other side. If more than one answer is left, the chart is rejected rather than guessed. A rejected chart leaves the current price sensors unavailable and, after three failures in a row, raises a repair issue.

## Customer portal (private)

Base URL `https://portal.ecotricity.co.nz/powercommerce/eco/fo/portal/`. It's a PowerCommerce portal behind Cloudflare.

### Login

1. `GET` the base URL to get a `JSESSIONID` cookie (path `/powercommerce/eco/fo`).
2. `POST loginProcess` with form fields `login` (the customer account number) and `password`.
3. Success redirects (302) to `.../portal/home`. Failure redirects to `.../portal/start`. The redirect is to plain `http://`, so the client checks the `Location` header rather than following it. Only a redirect to `/start` is treated as rejected credentials; any other response (an error status, a 200, or a redirect elsewhere) is treated as the portal being unavailable, so an outage doesn't trigger reauthentication.

An expired session answers JSON requests with a redirect or with the HTML start page. The client logs in again once. If the same answer comes back straight after a successful login, the credentials are clearly fine, so it's treated as the portal being unavailable rather than an authentication error, and doesn't trigger reauthentication.

The analysis endpoints only work after a page containing the analysis widget has been loaded in the session; the web app always loads `/home` or `consumptionDetails` first. Calling `analysis/initData` straight after logging in returned HTTP 503 in testing. So after each login the client loads `consumptionDetails` once before its first analysis call. This is inferred from the browser's behaviour and the 503; it hasn't been confirmed by Ecotricity.

### JSON endpoints

All are called with `X-Requested-With: XMLHttpRequest`.

- `POST analysis/initData` with `{"fetchCurrentIndicator": true, "fetchLastImport": true, ...}` returns `currentIndicator` with `indicatorID`, `displayName` (the ICP), `lastImportDate`, and, for each feature, `measuredValueRange`, the range of data held.
- `POST analysis/valueRequest` with `dateType: "INDIVIDUAL"` accepts any range. Each feature request names a `featureID`, a `granularityType` (`HALF_HOUR`, `DAY`, `MONTH`, `YEAR`) and `aggregationType: "SUM"`. Values come back as `{"value", "measuringPeriod": {"from": "to"}}`.

| Feature ID                                                    | Meaning         | Unit |
| ------------------------------------------------------------- | --------------- | ---- |
| `POWER_ACTIVE_PURCHASE_CONSUMPTION`                           | Imported energy | kWh  |
| `POWER_ACTIVE_SUPPLY_CONSUMPTION`                             | Exported energy | kWh  |
| `EMISSION\|POWER_ACTIVE_PURCHASE_CONSUMPTION\|Carbon Savings` | Carbon savings  | kg   |

`analysis/csvDownload` takes no parameters and exports whatever the last `valueRequest` asked for. Its export column was a day out of step with the others in the capture, so the integration doesn't use it.

Half-hourly requests for every length from 31 days to five years (1,826 days), all ending on the last full day, are accepted. Every reading came back half an hour long, so the portal doesn't switch to a coarser granularity for long ranges, and the held days matched a request covering only those days, so it doesn't silently shorten them either.

### Timestamps

Every portal timestamp carries a `+02:00` or `+01:00` offset, which is Europe/Berlin (the server's zone), but the wall-clock part is New Zealand time. Solar export appears between 08:30 and 17:00 on those timestamps; as true Berlin times that would be the middle of the night in NZ.

So the offset is discarded and the wall-clock time is read as `Pacific/Auckland`. Request ranges are sent the way the web app sends them: NZ wall-clock time with the Berlin offset for that date attached. The integration only asks for ranges that start and end at NZ midnight.

When NZ daylight saving ends, the wall-clock hour from 2am to 3am happens twice. The parser assigns the second occurrence to the later fold, so readings keep moving forward. That only works when a response starts before the repeated hour, which is one reason requests start at midnight. The fixture for that day shows the portal works out end times with wall-clock arithmetic: the first 02:30 reading ends at "02:00".

When daylight saving starts, 2am to 3am is skipped. The same arithmetic would label the 01:30 reading as ending at 02:00, a time that never happened, so an end time that doesn't exist (or isn't after the start) is replaced by the start plus the reading's wall-clock length, or half an hour. A reading whose start doesn't exist is dropped. Neither daylight saving change has been checked against a real capture yet.

## Unknowns

- How long a portal session lasts.
- What the portal returns for the hour repeated when daylight saving ends, and for the hour skipped when it starts.

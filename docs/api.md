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
| `capacityCode` | the active `PriceCategoryCode.code`, the distributor's price category, for example `RLUTOU` |
| `meterType`    | every channel's `vRegisterContentCode`, comma-joined in order, for example `UN,EG`          |
| `customerType` | `Residential`                                                                               |
| `icpNumber`    | the ICP                                                                                     |

The service doesn't check the ICP: requests with no `icpNumber`, an empty one or a made-up one return the same plans for a network and price category. Without a `capacityCode` it returns an empty `plan_list` and no default plan. Every network's price categories are public, in the Electricity Authority's registry data (see [development.md](development.md#networks-and-price-categories)). Within a network, the price category has so far changed only the suggested default plan and, on Orion, the daily network charge; the plans, seasons, per-kWh rates and charts were the same (see [development.md](development.md#choosing-price-categories)).

The site rate-limits by IP. It was observed when about 200 plan list and chart requests in a few minutes got the IP refused for about 24 hours (connections refused, not an HTTP error). The integration makes a handful of requests a week per install, far below that.

`CKHK` is the real code for Wellington Electricity. Other example price categories, register codes and plan codes are illustrative; these values vary from network to network, so the integration never hard-codes them and passes on whatever the ICP details contain.

The response's `content[0]` holds `default` (the plan the site suggests), `alternative` and `plan_list`. Each plan has:

- `retail_rates` and `network_rates`, each a list of `{charge, charge_description, register_type, unit, price, valid_start, valid_end, season}`. Prices are cents excluding GST. Units are `cents/kWh` or `cents/d`. Export rates have `register_type` `EG` and negative prices. Network rates carry a `priceplan_id` key, which is how the web app tells them apart.
- `seasons`, a list of `{season, start, end}`. These populate the site's season dropdown ("Seasonal Rates 04/2026 - 09/2026"). Plans whose rates are the same all year can have an empty list (every rate is "All-Year Rates" from 1 April to 31 March the following year). The web app then shows every rate and builds the chart URL from a pricing year written into its own code. The integration gives such a plan one season spanning its dated rates instead, named after their season (`_rates_season` in `parsers.py`), which gives the same dates without depending on the web app's code.
- `timeframes_chart_wd` and `timeframes_chart_we`, paths to the weekday and weekend charts described below.

The `default` plan is not necessarily the customer's plan, and neither site reliably says which plan a customer is on, so the user chooses it during setup. The choice is stored as the plan's `id`.

### Plan names

`price_plan_description` is an internal name ("LowBATTERY User"). The web app shows customers a different name ("Low ecoSOLAR"), looked up in `GET /assets/planmappings.json`. That file is static and needs no token. Its `mappings` list pairs a lower-case internal `name` with the customer-facing `title`, plus card text and colours. A `default` entry ("ecoSPECIAL") is used for plans not in the list; the integration ignores it and falls back to the internal name instead.

### Seasons change

Seasons last a few months and start on the 1st of a month. The list in a plan response covers the current pricing year, but Ecotricity can add a season, change its dates, or change rates whenever it likes. Season dates and lengths also differ between networks. So the integration saves what it fetches and works out each day from that, but fetches again often enough to notice changes (the schedule is in [architecture.md](architecture.md#pricing)):

- Plans, seasons, rates and charts are fetched weekly, on the 1st of each month, and on the last day of each season. Rates and seasons carry their own dates, so a season that starts on the 1st applies from 12:00am that day without a fetch.
- Every published season's charts are downloaded, not only today's, because which periods a plan has (and whether Ecotricity's and the network's line up, see below) can change from season to season. The current and next season's charts are downloaded again at every fetch, in case one is re-issued for the same season dates; later seasons' charts only until they've been read once. A chart that can't be downloaded yet (a season not drawn yet, say) is tried again at the next fetch.
- If no published season covers today or tomorrow, pricing is fetched daily until one does. The time-of-use hours and prices can't be read for a day with no season, so the current price sensors go unavailable until the season is published, and a repair issue is raised after three fetches in a row without one (about three days).
- A new season can bring a new period (a shoulder rate, say) or a new daily charge. Its price sensor is created once the season's charts have been read.
- Seasons can overlap. Scanpower publishes "Standard Rates" for the whole year and "Seasonal Rates" for June to September. The web app (`main.js`) prices a day with any season covering it whose name isn't "Standard Rates", then "Standard Rates", and the integration does the same (`season_for`).

### How prices are made up

The charts' legends (see [Timeframe charts](#timeframe-charts-time-of-use-hours)) print the exact price of every window. Across all 576 charts of every network, each legend price is made up the same way, which `tariff.py` follows:

- Each rate goes into a bucket by its `charge` code (`CHARGE_24` anytime, `CHARGE_HIGH` peak, `CHARGE_LOW` off peak, `CHARGE_3` and up named by the plan), prefixed `EG_` for export and `CN_` for controlled load. Every published rate has a code.
- Only rates whose `valid_start`/`valid_end` cover the day apply.
- A half-hour's price is one of Ecotricity's (retail) rates plus one of the network's rates, plus every anytime (`CHARGE_24`) rate on that side, plus, for import, any per-kWh rate with "levy" in its description (the EA levy, 0.16c or 0.2c depending on plan). An anytime retail rate with time-of-use network rates (the battery plans' import) and an anytime network rate next to time-of-use rates (Vector's "Transmission Charge") both work this way.
- Ecotricity's periods and the network's don't always line up. On Vector, Ecotricity's weekday peak is 7am to 11am and 5pm to 9pm all year, but the network's winter peak runs to 10pm, every day; so 9pm to 10pm is Ecotricity's off-peak rate plus the network's peak rate, a price no single period has. 96 of the 576 charts show prices like this, on 23 networks: the import side of the off-peak plans on both of Vector's networks (`VECT` and `UNET`), Unison and Centralines, and the export side of the battery plans on most networks. Ecotricity's own peak hours also differ between networks (Unison has a weekend peak), and a network's hours can differ between import and export (Network Tasman's winter import peak is 7am to 11pm, its export peak 7am to 9:30am and 5:30pm to 7:30pm).
- Some networks only publish rates for some periods, such as Scanpower's peak export rate, and charge no network rate in the other hours. The rates alone can't tell that apart from a network whose one rate applies all day (an anytime plan's off-peak network rate), so a side with one network rate is read from its chart, and only taken to be that rate all day when there's no chart.
- Where two combinations give the same price (two network periods at the same rate, which is common for export at 0c), the retail period is always the same, so the integration takes the network period that matches it.
- A legend price is the GST-inclusive price rounded to the cent, converted back and rounded again: 30.5532c is shown as 30.56c, not 30.55c (`shown_price`). Every price is compared that way, so a chart whose prices the rates don't explain is rejected rather than guessed.

Bills confirm the combinations. On published Ecotricity bills, the kWh charged at each of Ecotricity's periods add up to the same total as the kWh at each of the network's periods, but split differently. On a Northpower battery-plan bill, 123.42 kWh of export was credited at Ecotricity's peak rate but only 44.68 kWh at the network's peak rate, so most of those half-hours were Ecotricity's peak and the network's shoulder, credited at both rates.

### Line losses

The same bills charge Ecotricity's energy rates on usage plus line losses ("21.37 kWh, 1.02 kWh losses @ 0.29"), and the network's rates and the levy on usage alone. Export had no losses. The loss percentage matches the ICP's loss factors, which the ICP details return under `pricing.myLossFactorCode` (`lossFactorConsumption`, such as 1.0450, and `lossFactorGeneration`, 1.0000 on the captured ICP). The charts' prices leave losses out, so the integration shows the factors as sensors for people to apply themselves.

### Charge codes

Ecotricity's web app recognises multiple charge codes: `CHARGE_24` (anytime), `CHARGE_HIGH` (peak), `CHARGE_LOW` (off-peak) and `CHARGE_3` (shoulder) are common. Plans can also use further numbered codes that the network defines. This is how each code is used in the test fixtures so far, based on network fixtures tested to date:

| Code          | Period in the integration | Use in the fixtures                                                                                                                                                                                                                                                                                                                |
| ------------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CHARGE_24`   | `anytime` (fixed)         | The EA levy on every plan, anytime retail energy rates, flat export credits, and anytime network charges such as Vector's transmission charge. An anytime rate is part of every half-hour's price on its side, so `anytime` is only a period on a side where neither Ecotricity nor the network has time-of-use rates.               |
| `CHARGE_HIGH` | `peak` (fixed)            | Peak network rates and peak export credits on both networks. One network publishes its peak import network rate only for its winter season; both pay a peak export network credit only in part of the year. Peak retail rates appear on one network's time-of-use plans; the other network's plan has only an anytime retail rate. |
| `CHARGE_LOW`  | `off_peak` (fixed)        | Off-peak network rates and off-peak export credits on both networks, all year. Off-peak retail rates appear on the same plans as peak retail rates.                                                                                                                                                                                |
| `CHARGE_3`    | named from the plan       | Commonly used as "Shoulder Usage" network rate on weekdays from 5am to 7am and 11am to 5pm.                                                                                                                                                                                                                                        |
| `CHARGE_4`    | named from the plan       | Commonly used as "Super Off Peak Usage" network rate of 0c from 3am to 5am every day, weekdays and weekends. With the anytime retail rate added, it's the cheapest import price of the day.                                                                                                                                    |
| `CHARGE_5`    | named from the plan       | Commonly used as "Weekend Usage" network rate covering the whole weekend except 3am to 5am.                                                                                                                                                                                                                                        |

Numbered codes appear as retail rates too (Unison's off-peak plans have a retail shoulder) and on export (Northpower's export has three network periods).

Numbered codes like these aren't defined by Ecotricity, and another network could use the same number for something else. So only `CHARGE_24`, `CHARGE_HIGH` and `CHARGE_LOW` have fixed names. Every other bucket, `CHARGE_3` included, is named from the plan's own rate descriptions (retail first, then network) with "usage" and "export" dropped, and falls back to its code ("Charge 4") when no description names it. The customer portal lists the same descriptions, but leaves out rates priced at 0c, such as the `CHARGE_4` network rate above.

### Controlled load

ICPs with a controlled-load register (such as ripple-controlled hot water) have the registry register content code `CN` among their channels, alongside `UN` (uncontrolled), `EG` (export) and others. Every register code is passed to the plan lookup in `meterType`, so the plan's rates cover each register.

Ecotricity's site keeps rates for different registers apart (it groups rows by charge code, register type and unit). The integration does the same for controlled load: a rate whose `register_type` is `CN`, or whose description says "controlled" but not "uncontrolled", goes into its own `CN_` buckets. No chart shows them, so each is priced from its own rates plus any anytime controlled rate and the EA levy, and kept out of import prices. A controlled rate with no charge code and no period in its description is treated as anytime, because the network switches controlled load on and off rather than the clock.

This hasn't been checked against a real controlled-load ICP yet; the captured ICP has only `UN` and `EG` registers.

### GST

Plan prices in the API exclude GST. The web app has a GST toggle that multiplies every row by 1.15, export rates included, but that's a display choice, not what customers are paid.

For residential customers who aren't GST-registered, NZ export buy-back rates are paid as published, with GST added only for GST-registered customers. Ecotricity doesn't state its GST basis, but its [solar page](https://ecotricity.co.nz/solar) quotes ecoSOLAR at 21c peak and 16c off-peak export alongside an 18c low-user import rate, and 18c is the GST-exclusive figure the plan data gives, so the export figures are on the same basis.

So the integration adds GST to import prices and daily charges (when the option is on) and never to export credits.

### Timeframe charts (time-of-use hours)

The hours each bucket applies are not in any API response or page data. The web app only shows them as server-rendered PNGs, and NZ has no public dataset of network time-of-use windows (the Electricity Authority's EIEP12 price files have rates but no times). The windows differ between networks and seasons, and change most years on 1 April, which is why they're read from each plan's own charts rather than kept in a table.

Chart URLs come from the plan's `timeframes_chart_wd` or `timeframes_chart_we`, for example `/timeframeimages_ecotricity/CKHK-RLUTOU-CKHK_OFPK_LOW_260401_T0-RESIDENTIAL-SSSSSSSS-EEEEEEEE-WD.png`. The network, price category and plan codes change over time, so treat these as an example only. Replace `SSSSSSSS` and `EEEEEEEE` with the season's start and end dates (`YYYYMMDD`), and add `-NOGST` before `.png` for GST-exclusive prices. Charts are static files and need no token.

Each chart draws 48 half-hour bars, blue import above the axis and orange export below, with a legend under them listing every window and its price, such as "7am - 11am : -21.00c" (cents excluding GST, see the rounding above). `custom_components/ecotricity_nz/api/timeframes.py` reads the legend rather than the bars:

- The legend is set in a monospace font and drawn the same way on every chart, so each character is matched to the nearest of 19 templates (`api/glyphs.py`, cut from the fixture charts). On all 576 charts every character is within 0.11 of its template and at least 0.39 from any other, so the reader accepts a character within 0.2. That tolerates small changes to how the text is drawn (15% lighter text still reads), but never confuses one character for another. An unknown character rejects the chart.
- The column headings ("Import rate" and "Export rate") give the width of a character and where each column starts. Some characters ("m", "r", "3") are drawn as more than one run of ink, so ink is grouped by character cell rather than taken one run per character.
- Each column's windows must cover the day once, in half-hours, or the chart is rejected.
- Battery plans' export lines can be longer than the image is wide, so their last price is cut off (18 prices on 14 of the 576 charts, such as "-2" for -28.34c). The digits that fit are kept, and the price is estimated from its bar's height, scaled by the windows whose prices are printed. The estimates are within 0.25c on every captured chart. `tariff.py` takes the plan's price that matches the digits, or, when more than one does, the one clearly nearest the estimate.

Optical character recognition libraries were considered and ruled out: Tesseract needs native libraries with no Home Assistant OS build for ARM, and the others (RapidOCR, EasyOCR, PaddleOCR) need onnxruntime, PyTorch or PaddlePaddle, which have no musl Linux wheels at all. They also guess, where templates either match or don't.

A chart that can't be read, or whose prices the plan's rates don't explain, leaves the current price sensors unavailable and, after three failures in a row, raises a repair issue.

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

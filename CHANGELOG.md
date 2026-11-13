# Changelog

## Unreleased

### Breaking changes

- **Home Assistant 2026.10.4 or later is now required**, up from 2026.10.0, as that's the release the integration is tested against.
- **The Plan sensor's `code` attribute is now `internal_code`**, to match `internal_name` beside it: both are Ecotricity's internal identifiers for the plan, not what its site shows. Update any templates or automations that read it.

### New features

- **Cost statistics** show what your usage cost each hour, in dollars, split into each part of your bill: Ecotricity's energy rate (including line losses, as your bill charges it), the network's rate, the EA levy, each daily charge, Ecotricity's and the network's export rates, and the GST on import and on export, as your bill charges GST on its total after export credits. Stack them in a chart to recreate your bill for any day or month; the README has an example. Totals add up the import costs, the daily charges and the export amounts, and the total electricity cost (import costs, daily charges and GST) and total electricity compensation are there for the Energy dashboard. The total electricity net billed is what your bill comes to, with export credits taken off. Export keeps its sign, so a credit is negative, except in the total electricity compensation, which the Energy dashboard expects to be positive for money received. Costs are worked out from your plan's saved pricing, so they're backfilled to the start of the current pricing year (usually 1 April), not as far back as your usage goes. They need your portal login.

- **Bill card**: a dashboard card that lays out your costs like your Ecotricity bill, with network, energy and other charges, each rate's kWh (and line losses), rate and amount, and the total excluding GST, the GST and the total including it, as your bill does, whatever the **Include GST** options. Add an energy date picker with the same `collection_key` and it shows whatever day, week, month or range you pick. With more than one ICP set up, choose one with the card's `icp` option. It has a visual editor, and options for the billing period start day (when there's no date picker), a compact layout, the heading colour and lines with no charges. It comes with the integration, so there's nothing else to install; the README has an example.
- **Bill line statistics**: each rate on your plan also gets statistics of what it charged and the kWh it was charged on, like the lines on your bill, so peak and off-peak usage can be told apart in charts.

### Improvements

- The time-of-use charts of the pricing year's seasons that have ended are now downloaded once each, to work out the cost of past usage.
- The plan, sign-in-again and reconfigure steps now explain each field, as setup already did.

### Bug fixes

- Export prices left GST out, but Ecotricity's bills charge GST on the total after export credits are taken off, so each credit is worth 15% more than its published rate. Export prices now include GST, and a new **Include GST in export prices** option (on by default) turns it off; the existing option is now **Include GST in import prices**.
- Network prices and costs could be wrong when your plan is for a different network price category than your ICP is registered on, such as a low-user plan on a standard-user ICP. Ecotricity lists each plan with its own category's network rates, but the network charges your ICP's registered category until it's moved. The integration now prices with your ICP's category, a repair notice explains the difference, and the **Plan** sensor's `expected_network_price_category` and `actual_network_price_category` attributes show both categories, with `network_price_category_change_required` true while they differ. Your saved pricing is fetched again once after updating.
- A portal login the portal briefly rejected asked you to sign in again, even though your password was right. A rejected login is now tried once more after 10 seconds before Home Assistant asks.
- A usage series whose first readings arrived with a half-hour missing (such as export on a new solar install) skipped those readings for good once the portal filled the gap. They're now read again and imported.
- A chart image with marks the chart reader didn't expect could stop the whole pricing update, making every pricing sensor unavailable. It's now treated as an unreadable chart, so its saved reading is kept and everything else carries on.

## 0.4.0 (2026-10-21)

Time-of-use pricing only worked on a handful of networks; on most, it was either unavailable or inaccurate. This release reads the exact prices printed under Ecotricity's time-of-use charts instead of working them out from the height of the bars, and has been checked against every chart Ecotricity publishes on every network as of October 2026.

Some sensors have changed in ways that can affect automations, templates and dashboards, so please read the breaking changes before updating. These changes lay the groundwork for recreating Ecotricity's bills more accurately, which will continue in the next release.

### Breaking changes

- **Export prices are now negative when they are a credit.** Every price now keeps its sign exactly as Ecotricity publishes it, so a credit is negative and a cost is positive. This applies to every sensor and attribute that shows an export price. If you use these sensors in automations, templates or the Energy dashboard, you'll need to flip the sign to keep the old behaviour.
- **Line loss factors have moved to their own sensors.** The **Network** sensor no longer has the `loss_factor_consumption` and `loss_factor_generation` attributes. They're now the **Network import loss factor** and **Network export loss factor** sensors.
- **A period's price sensor has no value on days its period doesn't apply.** Sensors such as **Peak import price** used to show another period's price on days when their own period didn't apply. They now show unknown, and the `charged_as` attribute has been removed.
- **Next period change now also changes when the price does.** **Next import period change** and **Next export period change** now also change when the price changes within a period, and today's `windows` attribute starts a new window wherever the price changes, so one period can appear in more than one window in a day. This only happens where Ecotricity's and your network's periods don't line up exactly, which is the case on some networks but not all.
- **The `tou_error` attribute has moved to the Plan sensor.** It was on **Current import price** and **Current export price**, but those sensors are unavailable, and so show no attributes, when their own chart can't be read, so it only ever explained the other side's problem. **Plan** stays available, so its `tou_error` attribute now says why the time-of-use hours can't be read, and is empty when they can.
- **Registered generation capacity is no longer created when none is recorded.** Some networks record a capacity of zero even for connections with solar or other generation, so the value couldn't be relied on. If your ICP has no generation capacity above zero on record, the sensor isn't created. An existing one shows as no longer provided and can be deleted from the device page.

### New features

- **Network import loss factor** and **Network export loss factor** sensors show your connection's line loss factors. Ecotricity bills its energy rates on your usage times this factor, which the prices leave out; the README's [Known limitations](README.md#known-limitations) show how to include it.
- Where Ecotricity's and your network's pricing periods don't line up, **Current supplier import period** and **Current network import period** sensors (and the export ones) show each side's own period. **Current import period** then shows the overall period: the one whose own price is nearest what you're paying. Plans whose periods line up exactly keep one period sensor per direction.

### Improvements

- Time-of-use prices are now read from the price list printed under Ecotricity's charts, instead of from the height of the bars, so every half-hour's price is exact.
- The charts of every published season are now downloaded, so sensors for your plan's periods don't come and go with the seasons.
- If a chart can't be downloaded, or shows prices that don't match your plan's rates, for import or export where your network has only one rate, such as on an anytime plan, prices carry on with that rate all day and the chart is tried again daily up to three times, then at the usual times. A chart that goes missing later gets three tries of its own, but one that goes from missing to not matching your rates doesn't.
- Pricing saved by earlier versions can't be read by this one, so it's fetched again once after updating.
- The **Time-of-use chart** image is now called **Current time-of-use chart**, as it switches between the weekday and weekend charts and changes with the season. Its entity ID stays the same, so existing automations are unaffected.

### Bug fixes

- Fix the current price sensors being unavailable, with a repair issue, on most networks because their charts couldn't be read. This covered hours where Ecotricity's and the network's peak times don't line up (such as 9pm to 10pm in winter on Vector, Unison's weekends, and battery plans' export on most networks), prices too close together to tell apart by height, pale or patterned bars, and all-day prices such as Powerco's weekends. Hours where a network charges no rate at all, such as outside Scanpower's export peak, are now priced too.
- Fix peak and off-peak prices on Vector being about 3c short. Its transmission charge is published as an anytime network rate, and is now added to every period's price.
- Fix time-of-use hours never being read on networks whose plans have the same rates all year, such as Unison, Alpine, Horizon, Waipa, Network Waitaki, Centralines and Buller. Ecotricity publishes no pricing seasons for these plans, so their charts were never downloaded or checked. The plan's rate dates now stand in for the season, as Ecotricity's website does, and each pricing year keeps its own charts when next year's rates are published early.
- Fix the wrong season being used on plans with overlapping seasons, such as Scanpower's "Standard Rates" for the whole year and "Seasonal Rates" from June to September. The seasonal one now applies in its months, as on Ecotricity's website.
- Fix the options failing to save when Ecotricity no longer lists your chosen plan, unless you picked another plan. The plan now defaults to the one in use.
- Fix **Current export price**, **Current export period**, **Next export period change** and **Network export loss factor** being created for ICPs that don't export. They're now only created when the ICP has an export (EG) register. Existing ones on an ICP without one show as no longer provided and can be deleted from the device page. Export period price sensors, such as **Peak export price**, and **Current supplier export period** and **Current network export period** are removed too, and an export chart that can't be read no longer raises the repair notice.
- Fix a period's price sensor being removed when the only season with that period ended, such as **Peak import price** on networks with a winter-only peak. It came back disabled the next winter, without your settings. Periods from seasons that have ended now count as the plan's own while Ecotricity still lists them.
- Fix the half-hourly sensors, such as **Current import price** and **Next import period change**, showing the previous day's pricing for a few seconds after midnight.
- Fix the "time-of-use hours can't be read" repair notice naming the plan by Ecotricity's internal name instead of the one shown everywhere else.
- Fix the **Current export period** sensor showing a raw value on networks whose plans have an "Export Shoulder Peak" rate, such as Northpower and Top Energy. That period is now shown as "Shoulder peak".

## 0.3.0 (2026-10-15)

Pricing only worked properly for networks shaped like Wellington Electricity's. This release fixes pricing on Orion and other similar networks that have extra network-defined charge buckets such as "weekend" and "super off-peak" (#9).

- **Breaking change:** charge code `CHARGE_3`, which was always the **Shoulder** (`shoulder`) price, now follows your plan's own description; if your plan describes it as something other than "Shoulder Usage", the sensor's name, its period value and the `period`, `charged_as` and `windows` attributes will now change to match. **Automations or templates that check for `shoulder` may need updating**.
- Fix pricing for plans that combine an anytime retail rate with additional network-defined charge buckets, such as Orion plans. The anytime retail rate is now included in every one of those buckets, not only peak, off-peak and shoulder.
- Fix time-of-use charts that draw two close prices in the same colour, such as "off-peak" and "super off-peak". They were read as one price, so the cheaper half-hours were charged at the other price.
- A rate with no charge code that is described as "super off-peak" now keeps its own price instead of being merged into off-peak, and hyphenated descriptions such as "Off-Peak" are recognised.
- Anytime, peak and off-peak keep their fixed names. Every other price is now named from your plan's own rate descriptions, as listed in the Ecotricity portal, for example **Super off peak import price** and the period `super_off_peak`. The names follow your plan if Ecotricity renames a rate.
- The **Current import period** and **Current export period** sensors only list the periods your plan uses as options, so "shoulder" is no longer an option on plans without a shoulder price.
- Price sensors for prices your plan no longer has, such as after switching to a different plan, are now removed instead of being left behind as no longer provided.

## 0.2.0 (2026-10-13)

- Pricing is saved and keeps working through restarts and internet outages, including into the next pricing season (once Ecotricity has published it). Ecotricity's pricing data is now fetched weekly, on the 1st of each month and at the end of each season, instead of twice a day. If it can't be reached, retries slow down from hourly to every 8 hours until it eventually succeeds again.
- New **Last pricing update** sensor shows when pricing data was last fetched.
- The **Last data sync** sensor has been renamed to **Last meter data update**, which better describes its purpose: when Ecotricity's portal last received readings from your meter. It's a new entity, so the old one shows as no longer provided and can be safely deleted from the device page after the update.
- When the daily usage import finds nothing new, the portal is checked again every 3 hours until the data for the day becomes available.
- The first import of your usage history is now faster, as it asks the portal for 31 days at a time instead of only seven.

## 0.1.2 (2026-10-13)

- HACS now lists the integration as New Zealand only, and the repository passes all HACS validation checks.

## 0.1.1 (2026-10-13)

- New **Refresh data** action (`ecotricity_nz.refresh`) fetches the latest pricing and usage immediately.

## 0.1.0 (2026-10-13)

Initial release.

A custom integration for [Ecotricity NZ](https://ecotricity.co.nz) customers. It brings your plan's prices, time-of-use periods and half-hourly usage into Home Assistant.

- **Current prices**: the import and export price right now, following your plan's peak, off-peak and shoulder windows. Each price is broken down into supplier, network and levy rates via sensor attributes.
- **Time-of-use periods**: the current period, today's windows, and when the period next changes. Useful for automations and utility meter helpers.
- **Every price on your plan**: a sensor for each period's price, plus the fixed daily charge. The fixed daily charge is broken down into supplier, network, and metering hire rates via sensor attributes.
- **Pricing seasons**: the current season and when the next one starts, as prices and windows can change between them.
- **Time-of-use chart**: Ecotricity's own chart for today, as an image.
- **Usage history**: hourly import, export and carbon savings statistics, backfilled as far back as the Ecotricity portal data goes. This functionality requires your customer portal login.
- **Controlled load**: prices for controlled-load supply such as ripple-controlled hot water, if your plan has them.
- **ICP, smart meter, and regional network info**: sensors for your ICP number, smart meter serial number (with type and certification expiry attributes), and regional network (with import and export loss factor multiplier attributes).

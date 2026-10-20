# Changelog

## Unreleased

## v0.4.0 (2026-10-21)

Time-of-use pricing only worked on a handful of networks; on most, it was either unavailable or inaccurate. This release reads the exact prices printed under Ecotricity's time-of-use charts instead of working them out from the height of the bars, and has been checked against every chart Ecotricity publishes on every network as of October 2026.

Some sensors have changed in ways that can affect automations, templates and dashboards, so please read the breaking changes before updating. These changes lay the groundwork for recreating Ecotricity's bills more accurately, which will continue in the next release.

### Breaking changes

- **Export prices are now negative when they are a credit.** Every price now keeps its sign exactly as Ecotricity publishes it, so a credit is negative and a cost is positive. This applies to every sensor and attribute that shows an export price. If you use these sensors in automations, templates or the Energy dashboard, you'll need to flip the sign to keep the old behaviour; the README's [Using it with the Energy dashboard](README.md#using-it-with-the-energy-dashboard) section shows how.
- **Line loss factors have moved to their own sensors.** The **Network** sensor no longer has the `loss_factor_consumption` and `loss_factor_generation` attributes. They're now the **Network import loss factor** and **Network export loss factor** sensors.
- **A period's price sensor has no value on days its period doesn't apply.** Sensors such as **Peak import price** used to show another period's price on days when their own period didn't apply. They now show unknown, and the `charged_as` attribute has been removed.
- **Next period change now also changes when the price does.** **Next import period change** and **Next export period change** now also change when the price changes within a period, and today's `windows` attribute starts a new window wherever the price changes, so one period can appear in more than one window in a day. This only happens where Ecotricity's and your network's periods don't line up exactly, which is the case on some networks but not all.
- **Registered generation capacity is no longer created when none is recorded.** Some networks record a capacity of zero even for connections with solar or other generation, so the value couldn't be relied on. If your ICP has no generation capacity above zero on record, the sensor isn't created, and an existing one is removed.

### New features

- **Network import loss factor** and **Network export loss factor** sensors show your connection's line loss factors. Ecotricity bills its energy rates on your usage times this factor, which the prices leave out; the README's [Known limitations](README.md#known-limitations) show how to include it.
- Where Ecotricity's and your network's pricing periods don't line up, **Current supplier import period** and **Current network import period** sensors (and the export ones) show each side's own period. **Current import period** then shows the overall period: the one whose own price is nearest what you're paying. Plans whose periods line up exactly keep one period sensor per direction.

### Improvements

- Time-of-use prices are now read from the price list printed under Ecotricity's charts, instead of from the height of the bars, so every half-hour's price is exact.
- The charts of every published season are now downloaded, so sensors for your plan's periods don't come and go with the seasons.
- If a chart can't be downloaded for import or export where your network has only one rate, such as on an anytime plan, prices carry on with that rate all day and the chart is tried again daily.
- The **Time-of-use chart** image is now called **Current time-of-use chart**, as it switches between the weekday and weekend charts and changes with the season. Its entity ID stays the same, so existing automations are unaffected.

### Bug fixes

- Fix the current price sensors being unavailable, with a repair issue, on most networks because their charts couldn't be read. This covered hours where Ecotricity's and the network's peak times don't line up (such as 9pm to 10pm in winter on Vector, Unison's weekends, and battery plans' export on most networks), prices too close together to tell apart by height, pale or patterned bars, and all-day prices such as Powerco's weekends. Hours where a network charges no rate at all, such as outside Scanpower's export peak, are now priced too.
- Fix peak and off-peak prices on Vector being about 3c short. Its transmission charge is published as an anytime network rate, and is now added to every period's price.
- Fix time-of-use hours never being read on networks whose plans have the same rates all year, such as Unison, Alpine, Horizon, Waipa, Network Waitaki, Centralines and Buller. Ecotricity publishes no pricing seasons for these plans, so their charts were never downloaded or checked. The plan's rate dates now stand in for the season, as Ecotricity's website does, and each pricing year keeps its own charts when next year's rates are published early.
- Fix the wrong season being used on plans with overlapping seasons, such as Scanpower's "Standard Rates" for the whole year and "Seasonal Rates" from June to September. The seasonal one now applies in its months, as on Ecotricity's website.
- Fix the **Current export period** sensor showing a raw value on networks whose plans have an "Export Shoulder Peak" rate, such as Northpower and Top Energy. That period is now shown as "Shoulder peak".

## v0.3.0 (2026-10-15)

Pricing only worked properly for networks shaped like Wellington Electricity's. This release fixes pricing on Orion and other similar networks that have extra network-defined charge buckets such as "weekend" and "super off-peak" (#9).

- **Breaking change:** charge code `CHARGE_3`, which was always the **Shoulder** (`shoulder`) price, now follows your plan's own description; if your plan describes it as something other than "Shoulder Usage", the sensor's name, its period value and the `period`, `charged_as` and `windows` attributes will now change to match. **Automations or templates that check for `shoulder` may need updating**.
- Fix pricing for plans that combine an anytime retail rate with additional network-defined charge buckets, such as Orion plans. The anytime retail rate is now included in every one of those buckets, not only peak, off-peak and shoulder.
- Fix time-of-use charts that draw two close prices in the same colour, such as "off-peak" and "super off-peak". They were read as one price, so the cheaper half-hours were charged at the other price.
- A rate with no charge code that is described as "super off-peak" now keeps its own price instead of being merged into off-peak, and hyphenated descriptions such as "Off-Peak" are recognised.
- Anytime, peak and off-peak keep their fixed names. Every other price is now named from your plan's own rate descriptions, as listed in the Ecotricity portal, for example **Super off peak import price** and the period `super_off_peak`. The names follow your plan if Ecotricity renames a rate.
- The **Current import period** and **Current export period** sensors only list the periods your plan uses as options, so "shoulder" is no longer an option on plans without a shoulder price.
- Price sensors for prices your plan no longer has, such as after switching to a different plan, are now removed instead of being left behind as no longer provided.

## v0.2.0 (2026-10-13)

- Pricing is saved and keeps working through restarts and internet outages, including into the next pricing season (once Ecotricity has published it). Ecotricity's pricing data is now fetched weekly, on the 1st of each month and at the end of each season, instead of twice a day. If it can't be reached, retries slow down from hourly to every 8 hours until it eventually succeeds again.
- New **Last pricing update** sensor shows when pricing data was last fetched.
- The **Last data sync** sensor has been renamed to **Last meter data update**, which better describes its purpose: when Ecotricity's portal last received readings from your meter. It's a new entity, so the old one shows as no longer provided and can be safely deleted from the device page after the update.
- When the daily usage import finds nothing new, the portal is checked again every 3 hours until the data for the day becomes available.
- The first import of your usage history is now faster, as it asks the portal for 31 days at a time instead of only seven.

## v0.1.2 (2026-10-13)

- HACS now lists the integration as New Zealand only, and the repository passes all HACS validation checks.

## v0.1.1 (2026-10-13)

- New **Refresh data** action (`ecotricity_nz.refresh`) fetches the latest pricing and usage immediately.

## v0.1.0 (2026-10-13)

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

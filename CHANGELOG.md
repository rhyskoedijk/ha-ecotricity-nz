# Changelog

## Unreleased

- Fix prices on networks that publish a charge for all hours as a network rate, such as Vector's transmission charge. It's now added to every import price (peak and off-peak), as Ecotricity's website does, instead of appearing as an anytime price of its own.
- Fix time-of-use charts being read as a single price when a plan has a price close to zero, such as a network rate of 0c plus the levy. Every half-hour could be shown in that one period. The chart is now rejected instead, leaving the current price sensors unavailable and raising a repair issue if it keeps happening, rather than showing the wrong periods.
- Fix time-of-use charts that show one price all day being rejected when the plan's import and export prices are in about the same ratio, such as Powerco's weekend charts. The chart's axis now settles which prices it shows, so the current price sensors stay available on those days.
- Time-of-use charts are now checked against both their import and export bars, which share one scale. Prices that are all wrong by the same amount are now caught, and the chart is rejected instead of being read with those prices.

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

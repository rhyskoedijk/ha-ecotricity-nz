# Changelog

## Unreleased

- Fix pricing for plans that combine an anytime retail rate with additional network-defined charge buckets, such as Orion plans. Those buckets were missing the retail rate, so their time-of-use charts were reported as fitting more than one set of the plan's prices (#9).
- Keep rates with unfamiliar descriptions in their own price buckets.
- Fix time-of-use charts that draw two close prices in the same shade. The cheaper half-hours were charged at the other price.
- Prices other than anytime, peak and off-peak are now named from your plan's own rate descriptions, as listed in the Ecotricity portal, for example **Super off peak import price** and the period `super_off_peak`. The names follow your plan if Ecotricity renames a rate. A price with no description is named after its charge code, such as **Charge 4 import price**, instead of showing its period as unknown.
- **Possible breaking change:** the third price on a plan (charge code `CHARGE_3`) was always called **Shoulder** with the period `shoulder`. It now takes its name from your plan too, so if your plan describes it as something other than "Shoulder Usage", the sensor's name, its period value and the `period`, `charged_as` and `windows` attributes change to match (for example `uncontrolled_shoulder`). Automations or templates that check for `shoulder` won't match any more and should be updated. Entity IDs and history aren't affected.
- The **Current import period** and **Current export period** sensors only list the periods your plan uses as options.
- Price sensors for prices your plan no longer has, such as after choosing a different plan, are now removed instead of being left behind as no longer provided.

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

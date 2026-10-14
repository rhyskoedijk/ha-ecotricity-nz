# Changelog

## Unreleased

- Fix pricing for plans that combine an anytime retail rate with additional network-defined charge buckets.

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

# Changelog

## Unreleased

First version.

- Plan pricing from the Ecotricity onboarding site, including the current import and export price and time-of-use period
- Plan chosen during setup (and changeable in the options), shown by the name the Ecotricity website uses
- Half-hourly import and export from the customer portal as hourly statistics for the Energy dashboard, with a full history backfill on first run
- Price sensors with `supplier_rate`, `network_rate` and `ea_levy` attributes, shown to 4 decimal places
- Export credits shown as published, without GST
- Current and next period change sensors for import and export, a next pricing season change sensor, and today's time-of-use windows as an attribute
- A time-of-use chart image entity
- Carbon savings as an hourly statistic, alongside import and export
- Controlled-load (for example hot water) pricing, shown only when the plan has a controlled-load rate
- Registry details: registered generation capacity, price category, meter type and certification expiry, and loss factors
- Rate sensors for periods a season doesn't price separately show the price actually charged instead of unknown
- Reauthentication, reconfiguration, diagnostics and repair issues
- Integration icon and logo, shown in Home Assistant's integration list and setup screens
- Daylight saving changes are handled in the consumption statistics and the next period change times

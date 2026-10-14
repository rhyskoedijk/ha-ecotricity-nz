# Ecotricity NZ for Home Assistant

A custom integration for [Ecotricity NZ](https://ecotricity.co.nz) customers. It brings your plan's prices, time-of-use periods and half-hourly usage into Home Assistant.

This is a community integration and isn't affiliated with Ecotricity NZ. The Ecotricity name and logo belong to Ecotricity NZ and are used only to identify the service.

![Service](/docs/screenshots/screenshot.service.png)

## Features

- **Current prices**: the import and export price right now, following your plan's peak, off-peak and shoulder windows. Each price is broken down into supplier, network and levy rates via sensor attributes.
- **Time-of-use periods**: the current period, today's windows, and when the period next changes. Useful for automations and utility meter helpers.
- **Every price on your plan**: a sensor for each period's price, plus the fixed daily charge. The fixed daily charge is broken down into supplier, network, and metering hire rates via sensor attributes.
- **Pricing seasons**: the current season and when the next one starts, as prices and windows can change between them.
- **Time-of-use chart**: Ecotricity's own chart for today, as an image.
- **Usage history**: hourly import, export and carbon savings statistics, backfilled as far back as the Ecotricity portal data goes. This functionality requires your customer portal login.
- **Controlled load**: prices for controlled-load supply such as ripple-controlled hot water, if your plan has them.
- **ICP, smart meter, and regional network info**: sensors for your ICP number, smart meter serial number (with type and certification expiry attributes), and regional network (with import and export loss factor multiplier attributes).

## Installation

Home Assistant **2026.10** or later is required.

### Automatic via HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rhyskoedijk&repository=ha-ecotricity-nz&category=Integration)

Click **Download** in HACS, then restart Home Assistant.

### Manual via HACS

1. In HACS, open the menu, choose **Custom repositories**, and add `https://github.com/rhyskoedijk/ha-ecotricity-nz` as an **Integration**.
2. Search HACS for **Ecotricity NZ** and download it.
3. Restart Home Assistant.
4. Go to **Settings > Devices & services > Add integration** and choose **Ecotricity NZ**.

## Setup

| Field                              | Required | Notes                                                                                                                                                                                       |
| ---------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ICP number                         | Yes      | The 15-character number on your bill, for example `0000123456AB1C2`. You can also [look it up on the Electricity Authority website](https://www.ea.govt.nz/your-power/your-meter/address/). |
| Ecotricity customer account number | No       | The account number you log in to the [Ecotricity portal](https://portal.ecotricity.co.nz) with.                                                                                             |
| Ecotricity password                | No       | Your [Ecotricity portal](https://portal.ecotricity.co.nz) password. See [docs/security.md](docs/security.md) for how your login is kept safe.                                               |

With only the ICP number you get the pricing sensors. Add your portal login to get the usage sensors and statistics too.

Next, choose your plan from the plans Ecotricity offers for your ICP. Ecotricity's data doesn't reliably say which plan you're on, so you always pick it yourself.

Once it's set up, these can be changed under <kbd>Options</kbd> (the cog on the integration):

- **Include GST in prices**: on by default. Adds GST to import prices and daily charges; export prices never include it.
- **Plan**: the Ecotricity plan your ICP is on. Change it here if you switch plans.

To change or remove your portal login later, use <kbd>Reconfigure</kbd> on the integration. If the portal starts rejecting the saved login, Home Assistant asks you to sign in again.

## Sensor Entities

All entities belong to a device named **ICP &lt;icp&gt;**, so entity IDs look like `sensor.icp_0000123456ab1c2_current_import_price`. Prices are in NZD/kWh and are broken down in their attributes into `supplier_rate`, `network_rate` and any levies such as `ea_levy`. Only the parts your plan has are shown.

### Pricing

| Entity                                      | Description                                                                                 | Attributes                                               |
| ------------------------------------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Current import price                        | Import price for the current half-hour.                                                     | `period`, `supplier_rate`, `network_rate`, `ea_levy`     |
| Current export price                        | Export credit for the current half-hour. Only created if you export.                        | `period`, `supplier_rate`, `network_rate`                |
| Current controlled price                    | Controlled-load price. Only created if your plan has one.                                   | `supplier_rate`, `network_rate`                          |
| Daily charge                                | Total fixed charge per day, in NZD/d.                                                       | `supplier_rate`, `network_rate`, `metering_rate`         |
| _Period_ import, export or controlled price | One sensor per period on your plan, for example **Peak import price**. Disabled by default. | `charged_as`, `supplier_rate`, `network_rate`, `ea_levy` |

If a season doesn't price a period separately and one price applies all day (e.g. it's "off-peak" all day), that period's sensor shows the price you're actually charged, and `charged_as` says which period it is. Anytime, peak and off-peak have the same names on every plan. Every other price takes its name from your plan's own rate descriptions, the same ones the Ecotricity portal lists. For example, the "Shoulder Usage" rate provides a **Shoulder import price** sensor and the period `shoulder`; the "Super Off Peak Usage" rate provides **Super off peak import price** and `super_off_peak`. These names follow your plan, so if Ecotricity renames a rate, the sensor's name changes with it (its entity ID and history stay the same). Price sensors are only created for prices your plan has, and are removed if your plan stops having them (e.g. you switch plans). Likewise, the period sensors only list the periods your plan uses as options, so for example, a peak and off-peak only plan won't offer shoulder.

### Time of use

| Entity                     | Description                                                                                                            | Attributes                                                    |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Current import period      | `anytime`, `peak`, `off_peak`, or a period named from your plan, such as `shoulder`, `super_off_peak`, `weekend`, etc. | `windows`, list of today's windows such as `07:00-11:00 peak` |
| Current export period      | As above, for export. Import and export can have different windows.                                                    | `windows`                                                     |
| Next import period change  | When the import period next changes.                                                                                   | `period`, `price`                                             |
| Next export period change  | When the export period next changes.                                                                                   | `period`, `price`                                             |
| Next pricing season change | When the pricing season next starts.                                                                                   |                                                               |
| Time-of-use chart          | Image of Ecotricity's chart for today's day type and season.                                                           |                                                               |

### Usage

These sensors are only enabled if you have supplied your Ecotricity customer account details during integration configuration.

| Entity                    | Description                                                                                                                                                                                         | Attributes             |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- | --- |
| Latest day of usage       | The latest complete day that contains meter consumption data in the Ecotricity portal. This generally lags a few days behind todays date, due to the delay in receiving data from your smart meter. |                        |
| Latest day imported       | kWh imported on the latest day of data.                                                                                                                                                             | `date`, `statistic_id` | f   |
| Latest day exported       | kWh exported on the latest day of data.                                                                                                                                                             | `date`, `statistic_id` |
| Latest day carbon savings | kg of carbon saved on the latest day of data.                                                                                                                                                       | `date`, `statistic_id` |

### Diagnostic

These describe your electricity connection and integration settings. They're listed under **Diagnostic** on the device page.

| Entity                         | Description                                                           | Attributes                                                  |
| ------------------------------ | --------------------------------------------------------------------- | ----------------------------------------------------------- |
| ICP                            | Your ICP number.                                                      |                                                             |
| Network                        | Your network lines company.                                           | `code`, `loss_factor_consumption`, `loss_factor_generation` |
| Pricing category               | Your network pricing category, such as "Residential Time of Use".     | `code`                                                      |
| Meter                          | Your meter serial number.                                             | `type`, `smart`, `certification_expiry`                     |
| Plan                           | Your current Ecotricity plan.                                         | `internal_name`, `code`                                     |
| Pricing season                 | The current pricing season and its dates.                             | `start`, `end`, `published`                                 |
| Registered generation capacity | The solar or other generation your network has approved, in kW.       |                                                             |
| Last meter data update         | When Ecotricity's portal last received new readings from your meter.  |                                                             |
| Last pricing update            | When this integration last fetched pricing from Ecotricity's website. |                                                             |

## Statistic Entities

Consumption history is imported as hourly long-term statistics, going back as far as the Ecotricity portal has data for. The first import runs in the background and can take a few minutes. Statistic IDs use your ICP **in lower case**.

| Statistic ID                          | Name                                  | Unit |
| ------------------------------------- | ------------------------------------- | ---- |
| `ecotricity_nz:<icp>_energy_imported` | Ecotricity &lt;ICP&gt; imported       | kWh  |
| `ecotricity_nz:<icp>_energy_exported` | Ecotricity &lt;ICP&gt; exported       | kWh  |
| `ecotricity_nz:<icp>_carbon_savings`  | Ecotricity &lt;ICP&gt; carbon savings | kg   |

Each ID is also shown in the `statistic_id` attribute of the matching **Latest day** sensor, ready to copy, and under **Developer tools > Statistics**.

Each hour adds up two of the portal's half-hourly readings. The minimum time period supported by Home Assistant is one hour, so half-hourly readings must be merged to the nearest full hour. If a reading is missing, the hours from that point on wait for it for up to a week, then are imported with whatever the portal has.

## Actions

| Action                  | Description                                                                                                                                    | Fields                                                                                  |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `ecotricity_nz.refresh` | Fetches the latest pricing and usage now, instead of waiting for the next scheduled update. Usage only goes as far as the portal has data for. | `config_entry_id` (optional): the ICP to refresh. Can be left out if you only have one. |

## Updates and working offline

Pricing data comes from Ecotricity's public website, which requires internet access. The integration saves this data locally when fetched, and works out each day's prices and periods from the saved copy, so they keep working through restarts and internet outages. Once Ecotricity publishes the next season, the next fetch saves its pricing and time-of-use charts too, so the new season starts on time even if you're offline at midnight. The integration checks once a day whether anything needs fetching. It fetches weekly, on the 1st of each month, on the last day of each season, and daily while next season's pricing isn't published yet. The **Last pricing update** sensor shows when it last fetched.

Usage data is imported from the Ecotricity customer portal once a day. If an import finds nothing new, the integration checks the portal again every 3 hours until the day's data appears.

## Using it with the Energy dashboard

1. Go to **Settings > Dashboards > Energy**.
2. Under **Grid consumption**, add **Ecotricity &lt;ICP&gt; imported** as your energy import sensor.
3. If you export, add **Ecotricity &lt;ICP&gt; exported** as your energy export sensor.

Usage will appears a day or two after it happens, depending on when Ecotricity receives and processes your meter data.

![Energy Dashboard](/docs/screenshots/screenshot.energy.dashboard.png)

## Using it in charts

You can emulate the Ecotricity portal consumption chart using a energy date selector and statistics graph cards:

![Example Chart](/docs/screenshots/screenshot.example.chart.png)

```yaml
type: grid
cards:
  - type: energy-date-selection
    collection_key: energy_ecotricity
    disable_compare: true
    grid_options:
      columns: full
  - type: statistics-graph
    chart_type: bar
    stat_types:
      - change
    energy_date_selection: true
    collection_key: energy_ecotricity
    entities:
      - entity: ecotricity_nz:0000123456ab1c2_energy_imported
        name: Electricity consumed
        color: "#7cb5ec"
      - entity: ecotricity_nz:0000123456ab1c2_energy_exported
        name: Solar/Export
        color: "#f2d21b"
  - type: statistic
    name: Electricity consumed
    entity: ecotricity_nz:0000123456ab1c2_energy_imported
    icon: mdi:transmission-tower-import
    stat_type: change
    energy_date_selection: true
    collection_key: energy_ecotricity
    period:
      calendar:
        period: day
  - type: statistic
    name: Solar/Export
    entity: ecotricity_nz:0000123456ab1c2_energy_exported
    icon: mdi:solar-power-variant
    stat_type: change
    energy_date_selection: true
    period:
      calendar:
        period: day
    collection_key: energy_ecotricity
```

Carbon savings are in kilograms, so they're easiest to read in a card of their own.

## Using it in automations

Run the dishwasher once the import off-peak window starts:

```yaml
automation:
  - alias: Switch dishwasher on when import price is off-peak
    triggers:
      - trigger: state
        entity_id: sensor.icp_0000123456ab1c2_current_import_period
        to: off_peak
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.dishwasher
```

Switch a solar inverter to prioritise exporting when export is paid at the peak rate, then back to prioritising battery storage when off-peak starts:

```yaml
automation:
  - alias: Prioritise solar exporting over battery when export price is peak
    triggers:
      - trigger: state
        entity_id: sensor.icp_0000123456ab1c2_current_export_period
        to: peak
        id: peak
      - trigger: state
        entity_id: sensor.icp_0000123456ab1c2_current_export_period
        to: off_peak
        id: off_peak
    actions:
      - action: select.select_option
        target:
          entity_id: select.inverter_work_mode
        data:
          option: "{{ 'Export first' if trigger.id == 'peak' else 'Battery first' }}"
```

## Known limitations

- Only residential plans are supported.
- Ecotricity publishes peak and off-peak hours only as chart images, not as data. The integration reads the bars on those charts and checks them against the plan's prices. If the bars could match more than one set of prices, the chart is treated as unreadable rather than guessed. When a chart can't be read, the current price and period sensors become unavailable and a repair notice appears. Import and export are checked separately, so if only the export chart is unreadable, the import sensors keep working.
- Controlled-load support hasn't been tested against a real controlled-load ICP. It's based on Ecotricity's site code and the electricity registry's codes. If your controlled-load prices look wrong, please open an issue.
- Ecotricity's onboarding website guards its data with a browser check, which this integration reproduces in code. If Ecotricity changes that check, pricing updates stop until the integration is updated, and a repair notice tells you so. The saved pricing keeps working, so the current season's prices and windows carry on, and so does the next season if Ecotricity had already published it. A season published after that won't be picked up.

## Troubleshooting

- **No usage data**: make sure you can log in at https://portal.ecotricity.co.nz, then check the **Last meter data update** sensor. New accounts may have no data for a few days.
- **Wrong prices**: check the **Plan** sensor shows the plan you're on, and change it in the options if not. If Ecotricity stops listing your plan for your ICP, a repair notice asks you to choose again.
- For anything else, turn on debug logging for the integration, reproduce the problem, and open an issue with the log and the downloaded diagnostics (they hide your ICP, login and address).

## Removing it

Go to **Settings > Devices & services**, open **Ecotricity NZ**, and delete the entry. Imported statistics stay in your history until you remove them under **Developer tools > Statistics**. Then remove the integration in HACS.

## Security and privacy

See [docs/security.md](docs/security.md) for how your portal login is stored and used, and what data is sent outside Home Assistant.

## Development

See [docs/development.md](docs/development.md) for building and testing locally, [docs/architecture.md](docs/architecture.md) for how it works, and [docs/api.md](docs/api.md) for what's known about Ecotricity's services.

## Licence

[MIT](LICENSE)

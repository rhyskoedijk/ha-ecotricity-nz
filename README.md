# Ecotricity NZ for Home Assistant

A custom integration that brings your [Ecotricity](https://ecotricity.co.nz) electricity plan and usage into Home Assistant: the prices on your plan, the price right now under time-of-use pricing, and your half-hourly import and export from the customer portal, ready for the Energy dashboard.

It isn't made by or affiliated with Ecotricity, and it reads their websites rather than an official API, so a change on their side can break it until it's updated. The Ecotricity name and logo belong to their owner and are used only to identify the service.

## What you get

With just your ICP number:

- **Plan**, by the name the Ecotricity website uses ("Low ecoSOLAR", say), with the internal name (`internal_name`) and plan `code` as attributes
- **Network**, with its code and the network's loss factors as attributes, and **Price category**, your network price category (such as "Residential Time of Use"), with its `code` as an attribute
- **Season**, the current pricing season, with its dates and every published season as attributes, and **Next season change**, the date the next season starts
- **Current import price** and **current export price** in NZD/kWh, which change at the start of each peak or off-peak window. Use them as the price entities in the Energy dashboard. Their attributes break the price into its parts: `supplier_rate`, `network_rate` and (for import) `ea_levy`, plus the current `period`. Only the parts your plan has are shown.
- **Current import period** and **current export period**: anytime, peak, off-peak or shoulder, each with today's windows (such as `07:00-11:00 peak`) as a `windows` attribute. Import and export can have different windows; on Low ecoSOLAR, for example, import is one price all day but export has peak hours.
- **Next import period change** and **next export period change**: when the period next changes, with the `period` and `price` from then as attributes. Handy for automations. Unknown when the period doesn't change again before the season ends.
- **Time-of-use chart**: an image of Ecotricity's chart for today (weekday or weekend) and the current season. It follows the GST option, but Ecotricity's own GST-inclusive chart also adds GST to export prices, unlike the price sensors. If Ecotricity hasn't published a season covering today yet, there's no chart to show until it does.
- **Daily charge**: the total fixed charge per day, broken down into `supplier_rate` (Ecotricity's own fee), `metering_rate` (the metering fee) and `network_rate` (the lines company's charge) as attributes. Any other item on your plan appears under its own name.
- A price sensor for each period on the plan, such as **Peak import price** and **Off-peak export price**, with the same breakdown attributes. These are disabled by default; enable the ones you want. If a season doesn't price a period separately (no peak rate outside winter, for example) and one price applies all day, that period's sensor shows the price you're actually charged, with a `charged_as` attribute saying which period it is. Parts that are zero, such as a 0c network export rate, aren't listed.
- **Current controlled price**, if your plan has a controlled-load rate (for example ripple-controlled hot water), with the same breakdown attributes, plus per-period controlled price sensors (disabled by default). Controlled load is switched by your network rather than by the clock, so it has no time windows. Without a controlled-load rate, none of these sensors are created.
- Prices are shown to 4 decimal places, so a value like $0.1580 isn't rounded up to look like $0.16.
- Diagnostic details from the electricity registry: **Registered generation capacity** (the solar or other generation the network has approved, in kW), and the meter's `type`, whether it's `smart`, and its `certification_expiry` as attributes of the **Meter** sensor, which shows the meter's serial number.

With your customer portal login as well:

- Hourly import, export and carbon savings statistics going back as far as the portal has data, for the Energy dashboard and statistics graphs
- **Last day imported**, **last day exported** and **last day carbon savings**, for the latest complete day. Each has a `statistic_id` attribute naming the statistic that holds its full history.
- **Last meter reading** and **last data sync** times

Details about your connection and settings (ICP, network, price category, meter, plan, season, next season change, registered generation capacity, last meter reading and last data sync) are listed under **Diagnostic** on the device page. They work in automations and dashboards like any other sensor.

Import prices and daily charges include 15% GST unless you turn that off in the options. Export credits are always shown as published, without GST, because that's what customers who aren't GST-registered are paid.

## Installation

1. In HACS, open the menu, choose **Custom repositories**, and add `https://github.com/rhyskoedijk/ha-ecotricity-nz` as an **Integration**.
2. Search HACS for **Ecotricity NZ** and download it.
3. Restart Home Assistant.
4. Go to **Settings > Devices & services > Add integration** and choose **Ecotricity NZ**.

Home Assistant 2026.10 or later is required.

## Setup

| Field | Required | Notes |
| --- | --- | --- |
| ICP number | Yes | The 15-character number on your bill, for example `0000123456AB1C2` |
| Customer account number | No | What you log in to the [customer portal](https://portal.ecotricity.co.nz) with |
| Portal password | No | Your customer portal password |

Leave the login blank to skip usage data. Next you'll choose your plan from the plans Ecotricity lists for your ICP, by the same names the Ecotricity website uses ("Low ecoSOLAR", for example). Ecotricity's sites don't reliably say which plan you're on, so it's always your choice.

Options (the cog on the integration):

- **Include GST in prices**: on by default.
- **Plan**: the plan your ICP is on. Change it here if you switch plans. It's only offered while the integration is running, because the list of plans comes from Ecotricity.

To change or remove the portal login later, use **Reconfigure** on the integration. If the portal starts rejecting the saved login, Home Assistant will ask you to sign in again.

## Using it with the Energy dashboard

1. Go to **Settings > Dashboards > Energy**.
2. Under **Grid consumption**, add **Ecotricity &lt;ICP&gt; import**. Choose **Use an entity with current price** and pick **Current import price**.
3. If you export, add **Ecotricity &lt;ICP&gt; export** under **Return to grid**, priced with **Current export price**.

Usage appears a day or two after it happens, because that's when Ecotricity's portal receives it from the meter.

## Charting usage history

The usage history is stored as long-term statistics, not as a sensor's history. Their IDs use your ICP in lower case:

- `ecotricity_nz:<icp>_energy_import` (kWh)
- `ecotricity_nz:<icp>_energy_export` (kWh)
- `ecotricity_nz:<icp>_carbon_savings` (kg)

Each one is also shown as the `statistic_id` attribute of the matching **Last day imported**, **Last day exported** or **Last day carbon savings** sensor, ready to copy. **Developer tools > Statistics** lists them too. A statistics graph card shows hourly bars, like the portal's chart:

```yaml
type: statistics-graph
title: Ecotricity usage
entities:
  - ecotricity_nz:0000123456ab1c2_energy_import
  - ecotricity_nz:0000123456ab1c2_energy_export
chart_type: bar
period: hour   # or day, week, month
stat_types:
  - change
days_to_show: 7
```

Carbon savings are in kilograms, so they're easiest to read in a card of their own. Statistics are hourly, so each bar adds up two of the portal's half-hourly readings. If the portal is missing a reading, the hours from that point on wait until it appears, for up to a week; after that the hour is imported with what the portal has.

## Example automation

Run the dishwasher once the off-peak window starts:

```yaml
automation:
  - alias: Dishwasher on off peak
    triggers:
      - trigger: state
        entity_id: sensor.icp_0000123456ab1c2_current_import_period
        to: off_peak
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.dishwasher
```

## How it updates

- Plan, season and pricing details refresh once a day and again just after midnight. Ecotricity's seasons change every few months and can be updated at any time, so a new season starts on its first day and any price change shows up within a day. The current price and period update every half hour on their own.
- Portal usage is fetched once a day. The first time, the whole history is imported in the background, which can take a few minutes. If either site can't be reached, the integration tries again after 30 minutes instead of waiting until the next day.

## Known limitations

- Ecotricity publishes the peak and off-peak hours only as chart images. The integration reads the bars on those charts and checks them against the plan's prices. If a chart can't be read, the current price and period sensors become unavailable and a repair notice appears. Import and export are checked separately, so if only the export bars don't match, the import sensors keep working.
- Ecotricity's charts only cover weekdays and weekends, so public holidays are priced like any other day of the week. If your network charges public holidays at off-peak rates, the current price on those days may show the weekday rate.
- Only residential plans are supported.
- Controlled-load support is based on Ecotricity's site code and the electricity registry's codes, not yet on a real controlled-load ICP. If your controlled-load prices look wrong, please open an issue with your diagnostics.
- Usage statistics are total imported energy. The portal doesn't separate controlled-load usage.
- The onboarding website guards its data with a browser check, which the integration answers in code. If Ecotricity changes that check, pricing stops updating until the integration is updated. A repair notice will say so.

## Troubleshooting

- **Current price unavailable**: check **Settings > Repairs** for a notice about time-of-use hours, and the `tou_error` attribute on the sensor.
- **No usage data**: make sure the portal login works at https://portal.ecotricity.co.nz, then check the **Last data sync** sensor. New accounts may have no data for a few days.
- **Wrong prices**: check the **Plan** sensor shows the plan you're on, and change it in the options if not. If Ecotricity stops listing your plan for your ICP, a repair notice asks you to choose again.
- For anything else, turn on debug logging for the integration, reproduce the problem, and open an issue with the log and the downloaded diagnostics (they hide your ICP, login and address).

## Removing it

Go to **Settings > Devices & services**, open **Ecotricity NZ**, and delete the entry. Imported statistics stay in your history until you remove them under **Developer tools > Statistics**. Then remove the integration in HACS.

## Security and privacy

See [SECURITY.md](SECURITY.md) for how your portal login is stored and used, what's sent to Ecotricity, and how to check it yourself.

## Development

See [docs/development.md](docs/development.md) for building and testing locally, [docs/architecture.md](docs/architecture.md) for how it works, and [docs/api.md](docs/api.md) for what's known about Ecotricity's services.

## Licence

[MIT](LICENSE)

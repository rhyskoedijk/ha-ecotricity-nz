# Security

This page explains how the integration handles your Ecotricity customer portal login, and what it sends outside Home Assistant.

## Your Ecotricity portal account number and password

The Ecotricity portal login is optional. Without it, the integration never interacts with the portal.

**Where they're stored.** When you enter your account number and password during setup, Home Assistant saves them in the integration's config entry, in `.storage/core.config_entries` inside your Home Assistant configuration folder. This is how every Home Assistant integration stores credentials. Home Assistant doesn't encrypt that file, so it relies on the protection of the machine Home Assistant runs on and of its backups.

**Where they're sent.** Only to Ecotricity's customer portal, `https://portal.ecotricity.co.nz`, as the login form in the request that signs in (`custom_components/ecotricity_nz/api/portal.py`, `_async_login`). It's the same request your browser makes when you log in yourself, and it always uses HTTPS.

**What happens after login.** The portal gives the integration a session cookie, which is kept in memory only, in a connection used for nothing but the portal. The password isn't sent again until that session expires and the integration has to log in again.

## What's sent outside Home Assistant

The integration talks to two Ecotricity websites and nothing else. It has no analytics, telemetry or third-party services.

| Destination                                                    | When                             | What's sent                                                                                                                                                                                                                                               |
| -------------------------------------------------------------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `onboarding.ecotricity.co.nz` (public sign-up site)            | At setup, then about weekly      | Your ICP number, plus the network code, price category and meter register codes that this same site returns for your ICP. Requests for public plan, plan name and chart files. A browser-check answer the site requires (see [api.md](api.md)). |
| `portal.ecotricity.co.nz` (customer portal), only with a login | At setup, then at least daily    | Your account number and password when logging in, then the session cookie. Date ranges for the usage data being imported.                                                                                                                                 |

With both, the integration identifies itself with a User-Agent of `HomeAssistant/<version> ecotricity_nz/<version>`, so Ecotricity can see the requests that come from this integration and which Home Assistant version is running. During setup, reauthentication and reconfiguration, the User-Agent is `HomeAssistant ecotricity_nz config flow` instead.

## What stays visible inside Home Assistant

- Entity IDs include your ICP number (for example `sensor.icp_0000123456ab1c2_current_import_price`), and so does the device name. Keep that in mind when sharing screenshots, dashboards, automations or logs.
- Sensors and their attributes include your network, price category, plan and meter details, including the meter's serial number, but not your address.
- The pricing fetched from Ecotricity is saved in `.storage/ecotricity_nz.<entry_id>.pricing`, so it keeps working offline. It includes your ICP number and meter serial numbers, because sensors show them, but not your address or region, which no sensor uses. It holds no credentials. It's deleted when you delete the entry, and it's included in Home Assistant backups like the rest of `.storage`.

## Reporting a vulnerability

Please don't report security problems in a public issue. Use **Report a vulnerability** on the repository's Security tab to send a private report, and include what's affected and how to reproduce it.

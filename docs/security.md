# Security

This page explains how the integration handles your Ecotricity customer portal login, what it sends outside Home Assistant, and how you can check those claims yourself. Everything here can be confirmed in the source code, and the relevant files are named so you can.

The portal login is optional. Without it, the integration never sees your account number or password, and only Ecotricity's public sign-up site is contacted.

## Your account number and password

**Where they're stored.** When you enter them during setup, Home Assistant saves them in the integration's config entry, in `.storage/core.config_entries` inside your Home Assistant configuration folder. This is how every Home Assistant integration stores credentials. Home Assistant doesn't encrypt that file, so the protection is the protection of the machine Home Assistant runs on and of its backups.

**Where they're sent.** Only to Ecotricity's customer portal, `https://portal.ecotricity.co.nz`, as the login form in the request that signs in (`custom_components/ecotricity_nz/api/portal.py`, `_async_login`). It's the same request your browser makes when you log in yourself, and it always uses HTTPS. After a successful login the portal redirects to a plain `http://` address, but the integration doesn't follow that redirect, so nothing after login goes over an unencrypted connection either.

**What happens after login.** The portal gives the integration a session cookie, which is kept in memory only, in a connection used for nothing but the portal. The password isn't sent again until that session expires and the integration has to log in again.

**Where they never appear.**

- In logs, at any level, including debug logging. The integration never logs them, and a test (`tests/test_init.py`, `test_credentials_never_logged`) checks this with debug logging on and the portal failing.
- In error messages, repair notices or sensor attributes.
- In diagnostics downloads. The account number, password, ICP number, address and region, meter serial numbers and portal identifiers are replaced with `**REDACTED**` (`custom_components/ecotricity_nz/diagnostics.py`).
- In setup and reconfigure forms. The password field is masked, and it's never filled in for you.

**Removing them.** Use **Reconfigure** on the integration and clear both fields, or delete the integration. Either way the credentials are removed from the config entry. They'll stay in any backups made before that.

## What's sent outside Home Assistant

The integration talks to two Ecotricity websites and nothing else. It has no analytics, telemetry or third-party services. The GitHub links in `manifest.json` are documentation links that Home Assistant shows you; nothing connects to them.

| Destination | When | What's sent |
| --- | --- | --- |
| `onboarding.ecotricity.co.nz` (public sign-up site) | At setup, then about twice a day | Your ICP number, plus the network code, price category and meter register codes that this same site returns for your ICP. Requests for public plan, plan name and chart files. A browser-check answer the site requires (see [docs/api.md](docs/api.md)). |
| `portal.ecotricity.co.nz` (customer portal), only with a login | At setup, then once a day | Your account number and password when logging in, then the session cookie. Date ranges for the usage data being imported. |

With both, the integration identifies itself with a User-Agent of `HomeAssistant/<version> ecotricity_nz/<version>`, so Ecotricity can see the requests come from this integration and which Home Assistant version is running. The time-of-use chart image is fetched by Home Assistant itself, from the same public onboarding site.

Both sites sit behind Cloudflare, Ecotricity's hosting provider, so requests pass through Cloudflare on their way to Ecotricity, as they do from a browser.

Nothing goes back from Ecotricity's sites to anyone else through this integration. Your usage data is stored in Home Assistant's own database and stays there.

## What stays visible inside Home Assistant

- Entity IDs include your ICP number (for example `sensor.icp_0000123456ab1c2_current_import_price`), and so does the device name. Keep that in mind when sharing screenshots, dashboards, automations or logs.
- Sensors and their attributes include your network, price category, plan and meter details, including the meter's serial number (the **Meter** sensor), but not your address. Diagnostics downloads still redact the serial number.

## Checking it yourself

- **Read the code.** The login is in `custom_components/ecotricity_nz/api/portal.py`. Searching the integration for `password` shows every place the password is touched: the setup flow, setup of the portal client, the diagnostics redaction list, and the login request. The only addresses the code contacts are the two above; search the integration folder for `https://` (the portal address also appears as the device's link to the portal, which only opens if you click it).
- **Watch the network.** If you use a DNS filter or firewall that logs requests (Pi-hole, AdGuard Home or your router), the integration's only destinations will be `onboarding.ecotricity.co.nz` and `portal.ecotricity.co.nz`.
- **Check the diagnostics.** On the integration's page choose **Download diagnostics**, and look for your account number, password and ICP number. They should all show as `**REDACTED**`.
- **Check the logs.** Turn on debug logging for the integration, let it run for a while, and search the log for your password. It won't be there.
- **Check what you installed.** HACS installs the zip attached to a GitHub release, built by the CD workflow (`.github/workflows/cd.yml`) from the tagged source. You can compare the installed `custom_components/ecotricity_nz` folder with that tag; the only difference is the version number the workflow writes into `manifest.json`.

## Keeping your login safe

- Use a password for the Ecotricity portal that you don't use anywhere else.
- Protect the machine Home Assistant runs on, since its configuration folder holds the credentials.
- Keep your backups encryption key safe. Home Assistant encrypts backups by default, and backups include the config entry.
- If you share diagnostics or logs, check them first. Diagnostics hide your credentials and ICP number, but logs from other parts of Home Assistant can show entity IDs, which include the ICP number.

## Reporting a vulnerability

Please don't report security problems in a public issue. Use **Report a vulnerability** on the repository's Security tab to send a private report, and include what's affected and how to reproduce it. Fixes go into the latest release only.

![Eneco Belgium for Home Assistant](images/header.png)

# Eneco Belgium meter readings for Home Assistant

Sends your electricity meter readings to the Eneco Belgium customer portal once
a day, taking the values from sensors Home Assistant already has.

Eneco lets you enter meter readings by hand at
*My Eneco > Consommation > Encoder un relevé*. If your smart meter is already in
Home Assistant, doing that by hand every day is pointless work. This integration
reads the index of each register from the sensors you choose and posts them to
the portal at a time you pick.

Requires Home Assistant **2026.8** or newer.

## What it does

At the configured local time, once a day, it:

1. reads the current value of one sensor per meter register,
2. converts it to whole kWh,
3. posts the whole set to Eneco, dated today.

Which registers exist is decided by Eneco, not by this integration: it asks the
portal which ones your meter accepts and only offers those. A dual tariff meter
with solar panels typically has four (import day, import night, export day,
export night); a single tariff meter without injection has one.

A submission is all or nothing. If any of the chosen sensors is unavailable or
does not report a usable number, nothing is sent and the reason is recorded,
because a partial set would record an incoherent moment across the registers.

## Installation

### HACS

Add this repository as a custom repository of type *Integration*, install it,
and restart Home Assistant.

### Manually

Copy `custom_components/eneco_be` into your Home Assistant `config` directory and
restart.

## Setup

*Settings > Devices & services > Add integration > Eneco Belgium*.

1. **Credentials.** The email and password of your My Eneco account.
2. **Account, address, meter.** Only asked when you have more than one; the
   integration skips straight past any step with a single answer.
3. **Meter readings.** One sensor per register, and the time of day to submit.

The sensor for a register must be a *total* index in an energy unit, the same
number you would read off the meter, not a power or a daily delta. On a Belgian
digital meter these are the sensors your P1 or DSMR integration exposes as
"total consumption tariff 1", "total production tariff 2", and so on. Units
other than kWh are converted automatically.

## Entities

Every entry creates one device, named after the meter's EAN, with:

| Entity | Purpose |
| --- | --- |
| `time.*_submit_at` | The time of day readings are sent; change it here and the schedule moves immediately |
| `sensor.*_next_submission` | When the next submission is due, shown as a countdown |
| `button.*_submit_readings` | Submit the readings now |
| `sensor.*_last_submission` | When the last submission succeeded |
| `sensor.*_status` | `pending`, `success` or `failed`, with the reason as an attribute |

Press the button from an automation if you want a different schedule than the
built-in daily one.

Use *Download diagnostics* on the device to see exactly what was last sent.

What the last attempt did is remembered across restarts, so the sensors carry on
reporting the real last submission rather than resetting, and a day that was
already submitted is not sent again after a restart.

## Changing the configuration

To move the **submission time**, set the `time.*_submit_at` entity on the device
page. The schedule is rearmed as soon as you change it, and
`sensor.*_next_submission` updates to match.

To change **which sensor feeds which register**, open the integration page,
click the three dots on the entry and choose *Configure*. The form comes up
prefilled with your current mapping.

*Reconfigure*, in the same menu, walks the whole flow again including the
credentials, which is what you want after a meter change. If Eneco stops
accepting the stored password, Home Assistant raises a reauthentication
prompt by itself.

## Limitations

- **Electricity only.** Gas readings use the same API but are not offered.
- **No two-factor authentication.** The login is a plain Okta username and
  password exchange. An account that requires MFA cannot be used headlessly, and
  setup will say so rather than fail obscurely.
- **Whole units.** Eneco stores the index as a 32 bit integer, so values are
  rounded.
- **One reading per day.** Eneco rejects a second reading for a date that
  already has one, and rejects any value below the previous reading. Both show up
  on the status sensor.
- Your credentials are stored in the Home Assistant configuration, like every
  other cloud integration.
- **A restart across the submission time skips that day.** The schedule is a
  wall clock trigger, so if Home Assistant is down at the configured moment
  nothing is sent until the next day, and there is no catch up. Meter readings
  are cumulative, so the following day's reading still accounts for the gap.
- **A submission time between 02:00 and 03:00 is affected by daylight saving.**
  On the spring forward night that hour does not exist and the day is skipped;
  on the autumn night it happens twice, and the second run is a no op because
  the date has already been submitted. Any other time of day is unaffected.

## How it works

The portal has no public API. `docs/API.md` records how the endpoints, payloads
and login sequence were established, from the source maps the site ships in
production. In short: an Okta password grant produces a one-shot session token,
NextAuth trades it for a session cookie, and the session yields the bearer token
the REST API wants.

Nothing here is scraped at runtime: every call is a JSON request against the same
endpoints the website itself uses.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install homeassistant mypy pillow pytest-homeassistant-custom-component
ruff format . && ruff check .
.venv/bin/mypy custom_components/eneco_be tests tools
python3 tools/check-translations.py
.venv/bin/pytest
```

`ruff` and `mypy` are configured with the same rule set and error codes Home
Assistant core uses, so anything that would fail a core review fails here too.

`tools/license-header.py` adds the licence header to any new source file, and
`tools/check-translations.py` keeps `strings.json`, the translations,
`icons.json` and the code in step.

The brand images in `custom_components/eneco_be/brand` are what HACS validates
and what Home Assistant serves for the integration.

## Licence

BSD 2-Clause. See `LICENSE`.

<p align="center">
  <img src="images/header.png" alt="Eneco Belgium meter readings for Home Assistant" width="720"/>
</p>

<p align="center">
  <a href="https://github.com/renaudallard/homeassistant_eneco/releases/latest">
    <img src="https://img.shields.io/github/v/release/renaudallard/homeassistant_eneco?label=version&style=flat-square&sort=semver" alt="Latest release"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_eneco/releases">
    <img src="https://img.shields.io/github/downloads/renaudallard/homeassistant_eneco/total?style=flat-square&label=downloads" alt="GitHub release downloads"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_eneco/actions/workflows/checks.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_eneco/checks.yml?style=flat-square&label=tests" alt="Tests"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_eneco/actions/workflows/hassfest.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_eneco/hassfest.yml?style=flat-square&label=hassfest" alt="hassfest"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_eneco/actions/workflows/hacs.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_eneco/hacs.yml?style=flat-square&label=hacs" alt="HACS validation"/>
  </a>
  <a href="https://www.home-assistant.io/">
    <img src="https://img.shields.io/badge/Home%20Assistant-2026.8%2B-41BDF5?logo=home-assistant&logoColor=white&style=flat-square" alt="Home Assistant"/>
  </a>
  <a href="https://hacs.xyz">
    <img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square" alt="HACS custom repository"/>
  </a>
  <a href="./LICENSE">
    <img src="https://img.shields.io/github/license/renaudallard/homeassistant_eneco?style=flat-square" alt="License"/>
  </a>
  <a href="https://www.paypal.me/RenaudAllard">
    <img src="https://img.shields.io/badge/PayPal-Donate-blue.svg?logo=paypal&style=flat-square" alt="PayPal"/>
  </a>
</p>

---

Sends your electricity and gas meter readings to the Eneco Belgium customer
portal once a day, taking the values from sensors Home Assistant already has.

Eneco lets you enter meter readings by hand at
*My Eneco > Consommation > Encoder un relevé*. If your smart meter is already in
Home Assistant, doing that by hand every day is pointless work. This integration
reads the index of each register from the sensors you choose and posts them to
the portal at a time you pick.

Requires Home Assistant **2026.8** or newer.

## What it does

At the configured local time, once a day, it:

1. reads the current value of the sensor you chose for each register,
2. rounds it to whole kWh for electricity, or whole m³ for gas,
3. posts the set to Eneco, dated today.

Which registers exist is decided by Eneco, not by this integration: it asks the
portal which ones your meter accepts and only offers those. A dual tariff
electricity meter with solar panels typically has four (consumption day,
consumption night, injection day, injection night); a gas meter has one.

**Every register is optional.** Leave one without a sensor and it is simply not
part of the submission, which is what you want when a meter is still read by
hand, or when only some of its registers reach Home Assistant.

**One entry per login, one meter under it per connection.** The entry holds
your My Eneco credentials and signs in once; each electricity or gas meter it
submits for sits under it, with its own device, sources and time. You can have
either kind without the other, and add the second one later without signing in
again.

Among the registers you *did* map, a submission is all or nothing. If one of
those sensors is unavailable or does not report a usable number, nothing is sent
and the reason is recorded, because a partial set would record an incoherent
moment across the registers and the date cannot be submitted again to correct
it.

## Installation

### HACS

Add this repository as a custom repository of type *Integration*, install it,
and restart Home Assistant. Releases ship as a single archive, so downloading
one is a single request rather than a file at a time.

### Manually

Copy `custom_components/eneco_be` from a checkout into the `custom_components`
directory of your Home Assistant `config`, so that
`config/custom_components/eneco_be/manifest.json` exists, and restart. The
`eneco_be.zip` on a release holds those same files at its root, so unpack that
one into a `custom_components/eneco_be` you make yourself.

## Setup

*Settings > Devices & services > Add integration > Eneco Belgium*.

1. **Credentials.** The email and password of your My Eneco account.
2. **Account, address, meter.** Only asked when you have more than one; the
   integration skips straight past any step with a single answer.
3. **Meter readings.** A sensor per register, each optional, and the time of day
   to submit. The time offered is drawn at random from between 22:30 and 23:50,
   so that the households running this do not all reach Eneco in the same
   second. Change it to whatever suits you; it is only a suggestion, and only a
   meter that has not chosen one is given it.

That sets up the login with its first meter. To add the other one, a gas meter
next to an electricity meter for instance, open the integration page and choose
*Add meter* on the entry. It goes through the same steps from the account on,
using the login the entry is already signed in with, and only offers meters
that are not set up yet: an address with one meter of each kind goes straight
to the one that is missing. Adding the integration a second time with the same
login is refused and points you there instead.

The sensor for a register must be a *total* index, the same number you would
read off the meter, not a rate or a daily delta. The picker lists only sensors
carrying the right device class, energy for an electricity meter and gas for a
gas meter, so a template sensor without one has to be given it before it shows
up. For electricity that is an energy sensor in kWh; on a Belgian digital meter
these are the ones your P1 or DSMR integration exposes as "total consumption
tariff 1", "total production tariff 2", and so on. For gas it is a sensor with
the gas device class, read in m³, not one published as a volume. Other units of
the same kind are converted automatically.

## Entities

Every meter creates one device, named after its EAN, with:

| Entity | Purpose |
| --- | --- |
| `time.*_submit_at` | The time of day readings are sent; change it here and the schedule moves immediately |
| `sensor.*_next_submission` | When the next submission is due, shown as a countdown |
| `button.*_submit_readings` | Submit the readings now |
| `sensor.*_last_submission` | When the last submission succeeded |
| `sensor.*_status` | How the last attempt went, with the reason as an attribute |
| `sensor.*_last_yearly_bill` | The last full year billed, line by line, with [Read bills](#reading-bills) on |
| `sensor.*_tariff_card` | The tariff card the contract is billed on, with [Read bills](#reading-bills) on |

`sensor.*_status` reads `pending` before the first attempt, then `success`,
`already_submitted` when the date was already done, `nothing_to_submit` when no
register has a sensor, or `failed` with the reason in its `last_error`
attribute. Three failures in a row also raise a repair card under
*Settings > System > Repairs*, naming the meter by the last four digits of its
EAN. It clears on the next attempt that does not fail, and it survives a
restart. Disabling the entry takes the cards of its meters down and enabling it
again brings them back; removing a meter, or the entry, takes them away for
good.

Press the button from an automation if you want a different schedule than the
built-in daily one. A press that cannot submit fails the service call with the
reason, so a script notices; a press on a day that is already done, or with no
register mapped, is a no op rather than an error.

Use *Download diagnostics* on the entry to see, for every meter, what was last
sent, whether the last run was skipped or found nothing mapped, and how many
attempts have failed in a row, and with *Read bills* on, which card and bill
were read and why a newer one was not. Your email address, password, account number, EAN and address are left
out of it, and the account number is kept out of the log as well. The entry is
titled with a masked form of your email address, such as `s•••@e•••.com`,
because Home Assistant writes entry titles into its own log lines; rename it if
you prefer something else.

What the last attempt did is remembered across restarts, so the sensors carry on
reporting the real last submission rather than resetting, and a day that was
already submitted is not sent again after a restart.

A wall clock trigger cannot fire while Home Assistant is down, so if the
configured time has already gone by when it starts and today has not been
submitted yet, the readings are sent rather than the day being lost. That
happens once Home Assistant has finished starting, not while it is still
loading, so the sensors it reads have had a chance to publish a value. It also
means a freshly configured meter submits as soon as you finish setting it up, if
you chose a time earlier in the day.

## Changing the configuration

To move the **submission time**, set the `time.*_submit_at` entity on the device
page. The schedule is rearmed as soon as you change it, and
`sensor.*_next_submission` updates to match.

To change **which sensor feeds which register**, open the integration page,
click the three dots on the meter and choose *Change sources*. The registers are
asked of Eneco again, so a replaced meter shows the ones it has now, and the
form comes up prefilled with your current mapping and time. Neither this nor
moving the time signs in again.

*Reconfigure*, on the entry, changes the credentials, which is what you want if
the email address you sign in to Eneco with has changed. The meters stay as
they are, so the new login has to reach the account of every one of them. If
Eneco stops accepting the stored password, Home Assistant raises a
reauthentication prompt by itself; that one asks for the password alone,
because it is there to re-establish the login the entry already belongs to.

## Reading bills

Off by default. To turn it on, click *Configure* on the entry and tick *Read
bills*. Once a day the integration then lists your invoices and reads the
last full year billed for each meter. The year is the one Eneco billed, never
a count of days, as the meter is read whenever Eneco gets to it: the newest
yearly or closing bill, however many days it covers, together with the
intermediary bills of that year (one comes with a product change, for
instance). An intermediary bill newer than the last yearly one waits for the
yearly bill that closes its year. On monthly billing the year is the last
twelve monthly bills. Each bill has to start the day after the one before it
ends, and no line is ever cut or spread over days, so every line shown is one
a bill prints. Each is the same PDF *My Eneco > Factures* offers for
download, and their billing lines become two more sensors per meter:

| Entity | Purpose |
| --- | --- |
| `sensor.*_last_yearly_bill` | What the year cost for the meter, VAT included, in EUR |
| `sensor.*_tariff_card` | The version of the card the contract is billed on, such as `012210` |

The total is each line with its own VAT rate, added up, so it can be a cent off
what a bill prints. It is the cost of the year, not what was left to pay on the
bill: the advances paid during the year are not taken off. A sensor created
while it was still called *Last bill* keeps its entity ID, `sensor.*_last_bill`.

`sensor.*_last_yearly_bill` has `period_start` and `period_end`, the first and
last day billed, `issued`, the day the newest of the bills was issued, `bills`,
the `invoice_type`, `issued`, `period_start` and `period_end` of each bill
taken, oldest first, and a `lines` attribute listing every billed line of them:

```yaml
- section: energy        # energy, network or taxes, as the bill groups them
  label: Conso. simple
  code: Energiecomponent ElectricityAplusTH   # printed on electricity bills only
  start: "2026-01-01"
  end: "2026-01-31"
  quantity: 195.8
  unit: kWh              # kWh or year, or the bill's own word otherwise
  unit_price: 0.1589     # EUR per unit, before VAT
  vat: 0.06
  total: 31.11           # EUR, before VAT
```

`sensor.*_tariff_card` has the month of that card in `card_month` (`2022-10`
for `012210`), the `product` and `product_type` Eneco reports, and the card's
`urls` by language. On a variable contract that card can be years older than
the one Eneco publishes this month, and it is the one the bill is priced on.

- **Read once.** A bill is downloaded once; the daily run only lists the
  invoices again. What was read is kept across restarts, so a day the portal
  is down still shows the last year, with the reason in `last_error`.
- **All or nothing.** Only a bill printing the meter's own EAN is taken, and
  only whole: every block of lines has to add up to the subtotal printed under
  it. A bill that does not, or that covers more than one meter, is refused
  rather than showing figures that may be wrong, and so is a run of bills with
  a gap or an overlap between two of them. The sensor then shows what it can
  without them, the year before or a yearly bill without the intermediary one
  refused, and `last_error` names what was refused or left out; the dates and
  `bills` say what you are looking at. With no yearly bill yet, or fewer than
  twelve monthly ones in a row, the sensor shows nothing and `last_error` says
  why. A refused bill is not fetched again, one that would not download is
  tried again the next day.
- **Private.** The PDF is fetched from eneco.be with the login's own session,
  read inside Home Assistant and sent nowhere else. The `lines` attribute is
  not written to the history.
- **Seen in French so far.** Only the shape of a line is relied on, not its
  wording, but no bill in Dutch or German has been read yet. If one is
  refused, please open an issue with its `last_error`.

Turning the option off removes the two sensors and what was read.

## Upgrading from 0.3 or earlier

Until 0.3.2 every meter was an entry of its own, signing in on its own. The
first start of 0.4.0 folds the entries of each login into one, with a meter
under it for each, and signs that login in once. Entity ids, history, the
submission state and any repair card carry over; nothing needs setting up
again. An entry you had disabled stays disabled and on its own, so a meter you
switched off does not start submitting. The change cannot be undone by going
back to an older release, so take a backup first if you might want to.

## Limitations

- **No two-factor authentication.** The login is a plain Okta username and
  password exchange. An account that requires MFA cannot be used headlessly. The
  setup form says so under the password field, but the attempt itself comes back
  as rejected credentials, with the Okta status only in the debug log.
- **Whole units.** Eneco stores the index as a 32 bit integer, so values are
  rounded: whole kWh for electricity, whole m³ for gas. A sensor reporting a
  negative index, or one past what the portal can hold, is treated as unusable
  rather than sent.
- **One reading per day.** Eneco rejects a second reading for a date that
  already has one, and refuses a value below the one it already holds. Its own
  code for that case is named `NewReadingsShouldBeHigher`, so a register that has
  not advanced a whole unit since the last submission may be refused as well.
  Both show up on the status sensor.
- **A tariff period this integration does not know is skipped.** The portal
  has no versioning, so if Eneco ever adds one it is left out of the form and
  out of the submission, and the registers alongside it carry on working. The
  Home Assistant log names it. The register list is settled when the meter is
  set up and stored with it, so one that was skipped, or one Eneco adds later,
  reaches the form only after *Change sources*.
- Your credentials are stored in the Home Assistant configuration, like every
  other cloud integration.
- **A submission time between 02:00 and 03:00 is affected by daylight saving.**
  On the spring forward night that hour does not exist, so the trigger does not
  fire; the reading is caught up the next time Home Assistant starts, and
  otherwise that day is skipped. On the autumn night the hour happens twice, and
  the second run is a no op because the date has already been submitted. Any
  other time of day is unaffected.

## How it works

The portal has no public API. `docs/API.md` records how the endpoints, payloads
and login sequence were established, from the source maps the site ships in
production. In short: an Okta password grant produces a one-shot session token,
NextAuth trades it for a session cookie, and the session yields the bearer token
the REST API wants.

Once signed in, submitting is JSON throughout: every call goes to the same REST
endpoints the website uses. The sign in itself is neither, and the token lives
only in memory, so the first submission after a restart posts a form to NextAuth
and follows Okta's redirect chain before any of that. Every meter of a login
shares that one sign in, and only one runs at a time. Setting up a meter has
one more exception: no endpoint exposes the list of accounts you may act on,
which reaches the browser only inside the `__NEXT_DATA__` payload the dashboard
renders, so that list is read out of the page.

Each submission first asks the portal for the meter's registers, because the
website sends the meter number they report along with the readings. Asking every
time rather than storing it means a replaced meter is picked up by itself.

With *Read bills* on, the invoice list and the contract's product come from the
same REST API, and the bill itself from the website route that serves it to a
signed in browser. The text of the PDF is read with pypdf.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff format --check custom_components tests tools
.venv/bin/ruff check custom_components tests tools
.venv/bin/mypy custom_components/eneco_be tests tools
.venv/bin/python tools/check-translations.py
.venv/bin/pytest tests -q
```

That is exactly what CI runs, alongside hassfest and the HACS validator. Build
the virtualenv with Python 3.14: the pinned Home Assistant does not install on
anything older.

`ruff` and `mypy` use the same rule set and error codes Home Assistant core
uses, so anything that would fail a core review fails here too.

The formatter targets Python 3.14 and rewrites a multiple exception `except`
clause to the PEP 758 form, which nothing older can parse. Every clause that
affects carries a `# fmt: skip`, and a test parses everything the integration
ships against the previous version of the language to keep it that way.

`tools/license-header.py` adds the licence header to any new source file, and
`tools/check-translations.py` keeps `strings.json`, the translations,
`icons.json` and the code in step.

`tools/check-portal.py` signs in to a real account and reads back the discovery
chain, then the registers and recent readings of every meter it would set up,
gas as well as electricity, without submitting anything. The portal
has no public API and no versioning, so when something stops working this is
what tells you which endpoint moved:

```sh
ENECO_EMAIL=you@example.com .venv/bin/python tools/check-portal.py
```

The password is asked for at the prompt unless `ENECO_PASSWORD` is set as well,
so the check needs a terminal to run in.

The brand images in `custom_components/eneco_be/brand` are what HACS validates
and what Home Assistant serves for the integration.

## Languages

The interface is translated into English, French, Dutch and German. Home
Assistant picks the one matching your profile.

## Licence

BSD 2-Clause. See [LICENSE](./LICENSE).

If this saved you the daily trip to the Eneco website, the PayPal badge at the
top is there.

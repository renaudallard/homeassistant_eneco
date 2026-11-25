# Eneco Belgium portal API

Reverse engineered from the production bundle of `https://eneco.be` on 2026-08-21,
then confirmed against a live account the same day: the sign in sequence, the
discovery chain, and a real submission that was accepted and read back.

The site ships source maps (`/_next/static/chunks/<chunk>.js.map`) that still contain
`sourcesContent`, so the notes below come from the original TypeScript rather than from
guesswork. Anything not backed by that source is marked *unverified*.

## Hosts

| Purpose | Host |
| --- | --- |
| Web app / auth broker | `https://eneco.be` (`https://www.eneco.be` 302s to it, preserving the query string) |
| Okta | `https://login.eneco.be` |
| REST API | `https://api-prd.be-digitalcore.enecogroup.com` |

The web app is Next.js with `trailingSlash` enabled: `/api/auth/session` 308s to
`/api/auth/session/`. Always request the trailing slash.

## Authentication

The site uses NextAuth with an Okta OIDC provider. `pages/login.tsx` does nothing but
call `signIn('okta', { callbackUrl })`, and `pages/[[...route]].tsx` hands
`useSession().data.accessToken` to the API client provider, which sets
`Authorization: Bearer <accessToken>` on every REST call.

OAuth parameters, read from a live `/api/auth/signin/okta/` response:

| Field | Value |
| --- | --- |
| Authorization server | `https://login.eneco.be/oauth2/aus1bzuoqpLj3XGl5417` |
| `client_id` | `0oalhj5pyeAf2hECZ417` |
| `redirect_uri` | `https://www.eneco.be/api/auth/callback/okta` |
| `scope` | `openid email profile` |
| `response_type` | `code` |
| PKCE | S256, generated and stored server side |

State, nonce and the PKCE verifier are held in `__Secure-next-auth.*` cookies, so a client
only needs a cookie jar. It never has to implement PKCE itself.

### Sequence

1. `POST https://login.eneco.be/api/v1/authn`
   with `{"username": ..., "password": ..., "options": {...}}`.
   Returns `{"status": "SUCCESS", "sessionToken": "..."}`.
   Any other `status` (`MFA_REQUIRED`, `PASSWORD_EXPIRED`, ...) needs a browser.
2. `GET https://eneco.be/api/auth/csrf/` to obtain a CSRF token and its cookie.
3. `POST https://eneco.be/api/auth/signin/okta/`
   (form encoded: `csrfToken`, `callbackUrl`, `json=true`).
   Returns `{"url": "<authorize URL>"}` and sets the state/nonce/PKCE cookies.
4. `GET https://login.eneco.be/login/sessionCookieRedirect?token=<sessionToken>&redirectUrl=<authorize URL>`,
   following redirects. Okta exchanges the one-shot session token for a session cookie,
   `/authorize` issues the code, and the redirect chain ends at
   `/api/auth/callback/okta`, where NextAuth writes the session cookie.
5. `GET https://eneco.be/api/auth/session/` returns the session JSON, including
   `accessToken`.

Requesting `/authorize` without a session renders the Okta hosted sign in widget
(HTTP 200 with a `stateToken`), so step 4 is what turns the flow headless.

The session cookie is chunked (`__Secure-next-auth.session-token.0`, `.1`, ...); a cookie
jar handles that transparently.

### Account claims

Account numbers are not exposed by any REST endpoint. They travel in the NextAuth JWT and
reach the page through `getServerSideProps`, which serialises them into `__NEXT_DATA__`.
Fetching `https://eneco.be/fr/my-eneco/dashboard/` with the session cookie and reading
`props.pageProps.accounts` yields:

```json
[{ "crmAccountNumber": "...", "alias": "...", "role": "owner|editor|reader" }]
```

The equivalent API shape is `CrmAccountClaimDto` (`crmAccountNumber`, `alias`,
`accessType` in `Read|Write|Owner`).

## REST API

Base path: `/eneco-be/xapi/site/api/v1`. Every response body is wrapped once:

```json
{ "data": { ...payload... } }
```

Errors are wrapped twice:

```json
{ "errors": { "errors": { "Validation": ["MeterReading.AlreadyExists"] } } }
```

`Validation` is not the only kind: the website's itsme link reads its codes from
`Unexpected` the same way. The integration reports the codes of any other kind, and
the keys of a body that is wrapped only once, by name alone. It leaves out free text,
which may quote what was sent, and any key without a letter in it.

### `GET /accounts/{accountNumber}/delivery-addresses`

`ListContractAddressesResponse`:

```json
{ "addresses": [ { "id": "<64 hex>", "address": "...", "hasActiveContracts": true,
                   "details": { "street": "", "houseNumber": "", "bus": null,
                                "postalCode": "", "municipality": "" } } ] }
```

`id` is what the portal puts in the `?address=` query parameter.

### `GET /accounts/{accountNumber}/delivery-addresses/{addressIdentifier}/service-delivery-points`

`GetServiceDeliveryPointOverviewForAddressResponse`:

```json
{ "serviceDeliveryPoints": [ { "contractId": "", "ean": "", "type": "Electricity|Gas|Both|Unknown",
                               "meterType": "Analogue|Digital|DigitalNightExclusive|Unknown",
                               "meterRegisterTypes": ["Single|Day|Night|ExclusiveNight"],
                               "meterRegime": { "current": "NotApplicable|Smr1|Smr3" } } ] }
```

### `GET /accounts/{accountNumber}/meter-readings/registers?ean=<ean>`

`AvailableMeterRegistersResponse`, the authoritative list of registers a reading may be
submitted for:

```json
{ "ean": "", "meterNumber": "", "energyType": "Electricity",
  "meterType": "Digital", "meterRegime": "Smr3", "hasInjection": true,
  "registers": [ { "meterTimeFrameType": "TH|HI|LO|EX",
                   "direction": "OffTake|Injection" } ] }
```

### `GET /accounts/{accountNumber}/meter-readings?eans=<ean>`

Array of `MeterReadingResponse`:

```json
{ "ean": "", "date": "yyyy-MM-dd", "registeredDate": "yyyy-MM-dd",
  "meterReadingType": "Official|Informative|CustomerAdded|RLP",
  "utilityType": "Electricity|Gas",
  "indexValues": [ { "value": 0, "meterTimeFrameType": "HI", "direction": "OffTake",
                     "isValidated": true, "meterNumber": "", "reason": "..." } ] }
```

### `POST /accounts/{accountNumber}/meter-readings`

The body is a JSON **array**, one entry per register, of `AddMeterReadingRequest`:

```json
[ { "accountNumber": "...", "meterNumber": "...", "readingValue": 12345, "ean": "...",
    "meterTimeFrameType": "HI", "registeredDate": "2026-08-21",
    "utilityType": "Electricity", "direction": "OffTake" } ]
```

`meterNumber` is the one the registers endpoint above reports, and the website sends
`''` when it has none. These notes were first taken without it, and the live
submission of 2026-08-21 that confirmed them did not send it. The website has sent it
since the redeploy of 28 October 2026, when the site moved to Turbopack, and from the
next evening on a submission without it came back as HTTP 400 with no validation
code. Whether the backend checks the value or only that the field is there is
*unverified*.

`readingValue` is an `int32`; the form widget is configured with
`maximumFractionDigits={0}`, so only whole units are accepted. Success is HTTP 200
with an empty body.

A submitted reading comes back from the history endpoint as `meterReadingType`
`CustomerAdded`, with both `date` and `registeredDate` set to the date that was
sent. Eneco does not record a reading of its own every day, so
`MeterReading.AlreadyExists` only fires when a reading already exists for that
exact date, not as a matter of course.

### `GET /accounts/{accountNumber}/delivery-addresses/{addressIdentifier}/invoices`

Read from the website's `InvoiceOverview.tsx` and confirmed against a live
account on 2026-11-24. Query: `InvoiceFromDate` and `InvoiceToDate`
(`yyyy-MM-dd`, a two year window was accepted) and `ShowActiveContractsOnly`.
Without the dates every invoice since the contract began came back, which is
how the integration asks: a year of bills can reach back further than two
years.

```json
{ "invoices": [ { "invoiceId": "", "invoiceType": "YearlyBill",
                  "invoiceEnergyType": "Electricity|Gas|Both|Unknown",
                  "invoiceStatus": "Paid", "invoiceDate": "2026-04-26T00:00:00+00:00",
                  "invoicingPeriodStartDate": "", "invoicingPeriodEndDate": "",
                  "dueAmount": -234.09, "paidAmount": 0, "openAmount": 0,
                  "paymentMethod": "", "dueDate": "", "paymentDate": "",
                  "billingAccountNumber": "", "billInserts": [],
                  "documents": [ { "documentType": "Invoice", "url": "" } ] } ] }
```

`invoiceType` is one of `AdvanceInvoice`, `MonthlyBill`, `YearlyBill`,
`ClosingBill`, `IntermediaryInvoice`, `CorrectionInvoice`, `Invoice` and
`CollectionLetter`. `invoicingPeriodEndDate` is the day after the last day billed.
The website offers for download the first document whose type is not
`DynamicBill` or `SimplifiedBill`. No amount other than the totals above is in
the JSON: what was billed, at what price, is only in the PDF.

### Bill documents

A document `url` points at the website, not the API:
`https://eneco.be/api/my-eneco/download/<id>?accountNumber=...&fileId=...`. It
answers 308 to add the trailing slash, then serves the PDF to the NextAuth
session cookie; the bearer token is not needed. The bill is in the customer's
language. Its billing lines are printed one per row, in blocks of energy,
network and taxes, each followed by its subtotal, the three repeated for each
part of a split period:

```text
Conso. simple 01/01/2026 - 31/01/2026 195,80 kWh 15,89 €c 6% € 31,11
Tarif prosumer plafonnement 01/01/2026 - 31/01/2026 6% € -36,92
Total coût de l'énergie Eneco (hors TVA) € 348,51
```

Electricity bills also carry the type of each line in hidden text after it,
in Dutch whatever the bill's language (`ItemTypeSys: Distributie variabel (per kWh)`).
Eight settlement bills from 2023 to 2026, electricity and gas, all read this
way and added up to the totals they print.

### `GET /accounts/{accountNumber}/contracts/{contractNumber}/products`

`contractNumber` is the `contractId` of a service delivery point. Read from
`ProductDetail.tsx`, confirmed live on 2026-11-24:

```json
{ "contractId": "", "type": "Electricity", "activeFrom": "2020-01-01",
  "tariff": { "product": "ENECO_POWER_FLEX", "productType": "Variable",
              "isSocialTariff": false,
              "currentTariff": [ { "startDate": "2023-01-01T00:00:00+00:00",
                                   "tariffCardByLanguage": [
                                     { "language": "NL", "type": "Normal",
                                       "tariffCard": "https://cdn.eneco.be/downloads/nl/general/tk/BC_032_012210_NL_ENECO_POWER_FLEX.pdf" } ] } ] },
  "meter": { "meterType": "Digital", "service": "CompensatedOfftake",
             "invoiceFrequency": { "current": "Yearly" } },
  "tariffChartStartDate": "2023-01-01", "nextRenewalDate": "2027-01-01" }
```

`tariffCard` names the card the contract is billed on: `012210` is issue 01 of
October 2022. A Flex contract seen on 2026-11-24 was still billed on that card,
formula and fee, for January 2026, when the card Eneco published that month
printed another.

### Enumerations

| Type | Values |
| --- | --- |
| `TimeframeTypeCodeDto` | `TH` single, `HI` day/peak, `LO` night/off peak, `EX` exclusive night |
| `DirectionTypeCodeDto` | `OffTake` (import), `Injection` (export) |
| `EnergyTypeDto` | `Electricity`, `Gas`, `Both`, `Unknown` |
| `MeterType` | `Analogue`, `Digital`, `DigitalNightExclusive`, `Unknown` |
| `MeterRegime` | `NotApplicable`, `Smr1`, `Smr3` |
| `RegisterType` | `Single`, `Day`, `Night`, `ExclusiveNight` (UI form of the timeframe code) |

### Validation

Client side (`ValidationSchema.tsx`): the reading date must not be in the future and not
more than one month in the past; every value must be an integer. The page copy also warns
that readings may not be older than one week, which the server enforces.

Server side validation codes, from `AddMeterReading.tsx`:

| Code | Meaning |
| --- | --- |
| `MeterReading.AlreadyExists` | a reading already exists for that date |
| `MeterReading.DateIsEarlierThanContractStart` | date precedes the contract start |
| `AddMeterReadings.Contract.NotFound` | no contract for this meter |
| `MeterReading.AddingInjectionReadingsToConsumptionOnlyMeter` | meter has no injection register |
| `MeterReading.NewReadingsShouldBeHigher.{TH,HI,LO}.{OffTake,Injection}` | value below the previous reading |
| `MeterReading.NewMeterReadingsToHigh.{TH,HI,LO}.{OffTake,Injection}` | value implausibly high |

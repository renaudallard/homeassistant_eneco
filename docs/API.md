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
[ { "accountNumber": "...", "readingValue": 12345, "ean": "...",
    "meterTimeFrameType": "HI", "registeredDate": "2026-08-21",
    "utilityType": "Electricity", "direction": "OffTake" } ]
```

`readingValue` is an `int32`; the form widget is configured with
`maximumFractionDigits={0}`, so only whole units are accepted. Success is HTTP 200
with an empty body.

A submitted reading comes back from the history endpoint as `meterReadingType`
`CustomerAdded`, with both `date` and `registeredDate` set to the date that was
sent. Eneco does not record a reading of its own every day, so
`MeterReading.AlreadyExists` only fires when a reading already exists for that
exact date, not as a matter of course.

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

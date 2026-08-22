# Copyright (c) 2026 Renaud Allard <renaud@allard.it>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Client for the Eneco Belgium customer portal.

The portal is a Next.js application that brokers an Okta OIDC login through
NextAuth and then talks to a REST API with the resulting bearer token. See
docs/API.md for how the endpoints and payloads below were established.
"""

from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
import json
import logging
import re
from typing import Any, Final

import aiohttp

_LOGGER = logging.getLogger(__name__)

WEB_HOST: Final = "https://eneco.be"
OKTA_HOST: Final = "https://login.eneco.be"
API_BASE: Final = (
    "https://api-prd.be-digitalcore.enecogroup.com/eneco-be/xapi/site/api/v1"
)

CALLBACK_URL: Final = "/my-eneco/dashboard"
DASHBOARD_URL: Final = f"{WEB_HOST}/fr/my-eneco/dashboard/"

REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=45)

# Matches the JSON island Next.js writes into every server rendered page.
_NEXT_DATA_RE: Final = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)

# Wire values of TimeframeTypeCodeDto, mapped to stable keys used for storage.
TIMEFRAME_KEYS: Final[dict[str, str]] = {
    "TH": "single",
    "HI": "day",
    "LO": "night",
    "EX": "exclusive_night",
}
DIRECTION_KEYS: Final[dict[str, str]] = {
    "OffTake": "offtake",
    "Injection": "injection",
}

_TIMEFRAMES_BY_KEY: Final[dict[str, str]] = {v: k for k, v in TIMEFRAME_KEYS.items()}
_DIRECTIONS_BY_KEY: Final[dict[str, str]] = {v: k for k, v in DIRECTION_KEYS.items()}


class EnecoError(Exception):
    """Base class for every error raised by this client."""


class EnecoConnectionError(EnecoError):
    """The portal could not be reached or answered unexpectedly."""


class EnecoAuthError(EnecoError):
    """The credentials were rejected or the session could not be established."""


class EnecoValidationError(EnecoError):
    """The portal rejected a meter reading.

    ``codes`` holds the raw validation codes, for example
    ``MeterReading.NewReadingsShouldBeHigher.HI.OffTake``.
    """

    def __init__(self, codes: list[str]) -> None:
        """Store the codes reported by the portal."""
        super().__init__(", ".join(codes) or "reading rejected")
        self.codes = codes


@dataclass(frozen=True, slots=True)
class Account:
    """A CRM account the logged in user has access to."""

    number: str
    alias: str


@dataclass(frozen=True, slots=True)
class Address:
    """A delivery address belonging to an account."""

    identifier: str
    label: str
    has_active_contracts: bool


@dataclass(frozen=True, slots=True)
class Register:
    """One direction and tariff period a reading can be submitted for."""

    direction: str
    timeframe: str

    @property
    def key(self) -> str:
        """Return a stable identifier such as ``offtake_day``.

        Raises EnecoError for a wire value outside the documented enums. That
        is a last resort: discovery drops such a register before it can reach
        here, so nothing undocumented is ever stored in the options.
        """
        try:
            return f"{DIRECTION_KEYS[self.direction]}_{TIMEFRAME_KEYS[self.timeframe]}"
        except KeyError as err:
            raise EnecoError(
                f"unknown register {self.direction}/{self.timeframe}"
            ) from err

    @classmethod
    def from_key(cls, key: str) -> Register:
        """Return the register a key produced by :attr:`key` refers to."""
        direction, _, timeframe = key.partition("_")
        try:
            return cls(
                direction=_DIRECTIONS_BY_KEY[direction],
                timeframe=_TIMEFRAMES_BY_KEY[timeframe],
            )
        except KeyError as err:
            raise EnecoError(f"unknown register {key}") from err


@dataclass(frozen=True, slots=True)
class Meter:
    """The registers a meter accepts readings for."""

    ean: str
    energy_type: str
    registers: tuple[Register, ...]


@dataclass(frozen=True, slots=True)
class Reading:
    """A meter reading the portal already holds."""

    date: str
    reading_type: str
    values: dict[str, int]


@dataclass(frozen=True, slots=True)
class ServiceDeliveryPoint:
    """A metered connection at a delivery address."""

    ean: str
    energy_type: str


def _known_register(direction: str, timeframe: str) -> Register | None:
    """Return the register a wire pair names, or None if it is undocumented.

    The portal has no public API and no versioning, so meeting a tariff code
    this integration has never seen is a question of when. Dropping the one
    register that cannot be named leaves the rest of the meter usable, where
    carrying it along fails the whole entry the first time its key is read.
    """
    if direction not in DIRECTION_KEYS or timeframe not in TIMEFRAME_KEYS:
        _LOGGER.warning("Ignoring unknown register %s/%s", direction, timeframe)
        return None
    return Register(direction=direction, timeframe=timeframe)


def _unwrap(payload: Any) -> Any:
    """Return the body of a response, which the API wraps in a ``data`` key."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _validation_codes(payload: Any) -> list[str]:
    """Return the validation codes of an error body, which is wrapped twice."""
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    if not isinstance(errors, dict):
        return []
    inner = errors.get("errors")
    if not isinstance(inner, dict):
        return []
    codes = inner.get("Validation")
    if not isinstance(codes, list):
        return []
    return [str(code) for code in codes]


class EnecoClient:
    """Talk to the Eneco Belgium portal on behalf of one account holder.

    The caller owns ``session`` and it must keep cookies, because the whole
    login is carried by NextAuth and Okta cookies.
    """

    def __init__(
        self, session: aiohttp.ClientSession, email: str, password: str
    ) -> None:
        """Initialise the client without contacting the portal."""
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None

    async def async_login(self) -> None:
        """Sign in and obtain a bearer token.

        Raises EnecoAuthError when the credentials are rejected or the account
        needs a step the portal only offers in a browser, such as MFA.
        """
        session_token = await self._async_okta_session_token()
        authorize_url = await self._async_start_oidc_flow()
        await self._async_redeem_session_token(session_token, authorize_url)
        self._token = await self._async_read_access_token()

    async def _async_okta_session_token(self) -> str:
        """Exchange the credentials for a one shot Okta session token."""
        body = {
            "username": self._email,
            "password": self._password,
            "options": {
                "warnBeforePasswordExpired": False,
                "multiOptionalFactorEnroll": False,
            },
        }
        try:
            async with self._session.post(
                f"{OKTA_HOST}/api/v1/authn",
                json=body,
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                payload = await response.json(content_type=None)
                if response.status in (
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.BAD_REQUEST,
                ):
                    raise EnecoAuthError(
                        payload.get("errorSummary", "credentials rejected")
                        if isinstance(payload, dict)
                        else "credentials rejected"
                    )
                response.raise_for_status()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnecoConnectionError(f"Okta sign in failed: {err}") from err
        except ValueError as err:
            raise EnecoConnectionError("Okta did not answer with JSON") from err

        status = payload.get("status") if isinstance(payload, dict) else None
        if status != "SUCCESS":
            raise EnecoAuthError(
                f"Okta requires an interactive sign in (status {status})"
            )
        token = payload.get("sessionToken")
        if not token:
            raise EnecoAuthError("Okta returned no session token")
        return str(token)

    async def _async_start_oidc_flow(self) -> str:
        """Ask NextAuth to build the authorize URL and set its flow cookies."""
        csrf = await self._async_get_json(f"{WEB_HOST}/api/auth/csrf/")
        token = csrf.get("csrfToken") if isinstance(csrf, dict) else None
        if not token:
            raise EnecoAuthError("the portal returned no CSRF token")

        form = {"csrfToken": token, "callbackUrl": CALLBACK_URL, "json": "true"}
        try:
            async with self._session.post(
                f"{WEB_HOST}/api/auth/signin/okta/",
                data=form,
                headers={
                    "Origin": WEB_HOST,
                    "Referer": f"{WEB_HOST}/login/",
                },
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnecoConnectionError(f"could not start the sign in: {err}") from err
        except ValueError as err:
            raise EnecoConnectionError("the portal did not answer with JSON") from err

        url = payload.get("url") if isinstance(payload, dict) else None
        if not url:
            raise EnecoAuthError("the portal returned no authorize URL")
        return str(url)

    async def _async_redeem_session_token(
        self, session_token: str, authorize_url: str
    ) -> None:
        """Turn the session token into a NextAuth session cookie.

        Okta trades the one shot token for a session cookie and then follows
        the authorize URL, which ends at the NextAuth callback.
        """
        try:
            async with self._session.get(
                f"{OKTA_HOST}/login/sessionCookieRedirect",
                params={"token": session_token, "redirectUrl": authorize_url},
                allow_redirects=True,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                if "/api/auth/error" in str(response.url):
                    raise EnecoAuthError("the portal rejected the Okta session")
        except aiohttp.ClientResponseError as err:
            # Never interpolate this URL: the one shot token is a query parameter.
            raise EnecoConnectionError(
                f"could not complete the sign in: HTTP {err.status}"
            ) from None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnecoConnectionError("could not complete the sign in") from err

    async def _async_read_access_token(self) -> str:
        """Read the bearer token out of the NextAuth session."""
        payload = await self._async_get_json(f"{WEB_HOST}/api/auth/session/")
        token = payload.get("accessToken") if isinstance(payload, dict) else None
        if not token:
            raise EnecoAuthError("the portal returned no access token")
        return str(token)

    async def _async_get_json(self, url: str) -> Any:
        """Fetch a JSON document from the web application."""
        try:
            async with self._session.get(url, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnecoConnectionError(f"{url} failed: {err}") from err
        except ValueError as err:
            raise EnecoConnectionError(f"{url} did not answer with JSON") from err

    async def async_get_accounts(self) -> list[Account]:
        """Return the accounts the user may act on.

        No REST endpoint exposes them; they travel in the NextAuth session and
        are rendered into the dashboard page by getServerSideProps.
        """
        try:
            async with self._session.get(
                DASHBOARD_URL, timeout=REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()
                body = await response.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnecoConnectionError(f"could not load the dashboard: {err}") from err

        match = _NEXT_DATA_RE.search(body)
        if match is None:
            raise EnecoAuthError(
                "the dashboard did not render, the session may be gone"
            )
        try:
            data = json.loads(match.group(1))
        except ValueError as err:
            raise EnecoConnectionError("the dashboard payload was not JSON") from err

        raw = data.get("props", {}).get("pageProps", {}).get("accounts")
        if not isinstance(raw, list):
            raise EnecoAuthError("the dashboard listed no accounts")

        accounts = [
            Account(
                number=str(entry["crmAccountNumber"]),
                alias=str(entry.get("alias") or entry["crmAccountNumber"]),
            )
            for entry in raw
            if isinstance(entry, dict) and entry.get("crmAccountNumber")
        ]
        if not accounts:
            raise EnecoAuthError("the dashboard listed no accounts")
        return accounts

    async def async_get_addresses(self, account: str) -> list[Address]:
        """Return the delivery addresses of an account."""
        payload = await self._async_api(
            "GET", f"/accounts/{account}/delivery-addresses"
        )
        addresses = payload.get("addresses") if isinstance(payload, dict) else None
        if not isinstance(addresses, list):
            return []
        return [
            Address(
                identifier=str(entry["id"]),
                label=str(entry.get("address") or entry["id"]),
                has_active_contracts=bool(entry.get("hasActiveContracts")),
            )
            for entry in addresses
            if isinstance(entry, dict) and entry.get("id")
        ]

    async def async_get_delivery_points(
        self, account: str, address: str
    ) -> list[ServiceDeliveryPoint]:
        """Return the metered connections at a delivery address."""
        payload = await self._async_api(
            "GET",
            f"/accounts/{account}/delivery-addresses/{address}/service-delivery-points",
        )
        points = (
            payload.get("serviceDeliveryPoints") if isinstance(payload, dict) else None
        )
        if not isinstance(points, list):
            return []
        return [
            ServiceDeliveryPoint(
                ean=str(entry["ean"]),
                energy_type=str(entry.get("type") or "Unknown"),
            )
            for entry in points
            if isinstance(entry, dict) and entry.get("ean")
        ]

    async def async_get_meter(self, account: str, ean: str) -> Meter:
        """Return the registers a meter accepts readings for."""
        payload = await self._async_api(
            "GET",
            f"/accounts/{account}/meter-readings/registers",
            params={"ean": ean},
        )
        if not isinstance(payload, dict):
            raise EnecoConnectionError("the portal returned no meter registers")

        registers: list[Register] = []
        for entry in payload.get("registers") or []:
            if not isinstance(entry, dict):
                continue
            direction = entry.get("direction")
            timeframe = entry.get("meterTimeFrameType")
            if not direction or not timeframe:
                continue
            register = _known_register(str(direction), str(timeframe))
            if register is not None:
                registers.append(register)

        return Meter(
            ean=str(payload.get("ean") or ean),
            energy_type=str(payload.get("energyType") or "Electricity"),
            registers=tuple(registers),
        )

    async def async_get_readings(self, account: str, ean: str) -> list[Reading]:
        """Return the readings the portal holds for a meter, newest first.

        The integration does not need this, but it is how a portal that has
        changed underneath us gets noticed. See tools/check-portal.py.
        """
        payload = await self._async_api(
            "GET",
            f"/accounts/{account}/meter-readings",
            params=[("eans", ean)],
        )
        if not isinstance(payload, list):
            return []

        readings: list[Reading] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            values: dict[str, int] = {}
            for index in entry.get("indexValues") or []:
                if not isinstance(index, dict):
                    continue
                direction = index.get("direction")
                timeframe = index.get("meterTimeFrameType")
                value = index.get("value")
                if direction is None or timeframe is None or value is None:
                    continue
                register = _known_register(str(direction), str(timeframe))
                if register is None:
                    continue
                values[register.key] = int(value)
            readings.append(
                Reading(
                    date=str(entry.get("date") or ""),
                    reading_type=str(entry.get("meterReadingType") or ""),
                    values=values,
                )
            )
        return sorted(readings, key=lambda r: r.date, reverse=True)

    async def async_submit_readings(
        self,
        account: str,
        ean: str,
        energy_type: str,
        reading_date: date,
        values: dict[Register, int],
    ) -> None:
        """Submit one reading per register.

        Raises EnecoValidationError when the portal rejects the values.
        """
        body = [
            {
                "accountNumber": account,
                "readingValue": int(value),
                "ean": ean,
                "meterTimeFrameType": register.timeframe,
                "registeredDate": reading_date.isoformat(),
                "utilityType": energy_type,
                "direction": register.direction,
            }
            for register, value in values.items()
        ]
        if not body:
            raise EnecoError("no readings to submit")
        await self._async_api("POST", f"/accounts/{account}/meter-readings", json=body)

    async def _async_api(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call the REST API, signing in again once if the token expired."""
        if self._token is None:
            await self.async_login()

        payload, status = await self._async_api_once(method, path, **kwargs)
        if status == HTTPStatus.UNAUTHORIZED:
            _LOGGER.debug("Token rejected, signing in again")
            self._token = None
            await self.async_login()
            payload, status = await self._async_api_once(method, path, **kwargs)

        if status == HTTPStatus.UNAUTHORIZED:
            raise EnecoAuthError("the portal rejected the access token")
        if status >= HTTPStatus.BAD_REQUEST:
            if codes := _validation_codes(payload):
                raise EnecoValidationError(codes)
            _LOGGER.debug("%s %s returned HTTP %s", method, path, status)
            raise EnecoConnectionError(f"the portal returned HTTP {status}")
        return _unwrap(payload)

    async def _async_api_once(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[Any, int]:
        """Perform a single REST call and return its body and status."""
        try:
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            ) as response:
                status = response.status
                if status == HTTPStatus.NO_CONTENT:
                    return None, status
                try:
                    return await response.json(content_type=None), status
                except ValueError:
                    if status < HTTPStatus.BAD_REQUEST:
                        raise
                    # A portal that is refusing the call often says so in an
                    # HTML error page. The status is the whole point of the
                    # answer, so hand it back rather than lose it to the body:
                    # a 401 buried here would never sign in again.
                    _LOGGER.debug(
                        "%s %s returned HTTP %s, no JSON", method, path, status
                    )
                    return None, status
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("%s %s failed: %s", method, path, err)
            # Name the failure, never quote it: aiohttp puts the URL it was
            # given in its own message, and every path here carries the CRM
            # account number. This text is shown in the interface, kept by the
            # recorder, stored, and handed to the repair.
            raise EnecoConnectionError(
                f"could not reach the portal: {type(err).__name__}"
            ) from err
        except ValueError as err:
            _LOGGER.debug("%s %s returned no JSON", method, path)
            raise EnecoConnectionError("the portal returned no JSON") from err

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

import asyncio
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
import json
import logging
import re
from typing import Any, Final

import aiohttp
from yarl import URL

from .api_billing import (
    AdvanceAmounts,
    Invoice,
    MonthVolume,
    TariffCard,
    advance_amounts,
    invoices,
    monthly_volumes,
    tariff_card,
)

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

# The ids a REST path carries, and a query string, which carries the EAN or
# the meter number.
_PATH_ID_RE: Final = re.compile(r"/(accounts|delivery-addresses|contracts)/[^/?#\s]+")
_QUERY_RE: Final = re.compile(r"\?\S*")

# A bill is a few hundred kilobytes; anything far larger is not one.
MAX_DOCUMENT_BYTES: Final = 10 * 1024 * 1024

_MAX_REDIRECTS: Final = 3
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
_SIGNED_OUT: Final = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})

# An error code of the portal's own, such as MeterReading.AlreadyExists, and
# the name of a field in a request, such as Validation or [0].MeterNumber.
_CODE_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)+")
_FIELD_RE: Final = re.compile(r"[$\[\]\d_.]*[A-Za-z][$\[\]\w.]*", re.ASCII)
_MAX_NAME: Final = 100
_MAX_REASONS: Final = 8

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


class EnecoSessionGoneError(EnecoAuthError):
    """The web session behind the dashboard is no longer signed in."""


class EnecoNoAccountError(EnecoAuthError):
    """Signed in, but the login owns no account readings can be sent for."""


class _RefusedError(EnecoAuthError):
    """Okta refused the credentials, which asking again will not change."""


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
    number: str
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
    contract_id: str = ""
    # What the consumption page names the meter by.
    meter_number: str = ""


def _endpoint(value: object) -> str:
    """Return text with the account, address and contract ids and any query taken out.

    Debug logs are what a maintainer asks for in a bug report, and every path
    here is /accounts/<crm account number>/something, often with an address
    id or an EAN after it. Which endpoint answered is the whole point of the
    line; whose account it was is not. This has to cover the exceptions too,
    because aiohttp quotes the URL it was handed, query included, in the text
    of its own errors.
    """
    return _QUERY_RE.sub("?-", _PATH_ID_RE.sub(r"/\1/-", str(value)))


async def _read_capped(response: aiohttp.ClientResponse) -> bytes:
    """Return a response body, refusing one larger than a bill can be."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(65536):
        size += len(chunk)
        if size > MAX_DOCUMENT_BYTES:
            raise EnecoError("the bill is too large")
        chunks.append(chunk)
    return b"".join(chunks)


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


def _error_groups(payload: Any) -> dict[Any, Any]:
    """Return the dict an error body keeps its reasons in, or an empty one.

    The portal's own errors are wrapped twice, as lists of codes by kind:
    ``{"errors": {"errors": {"Validation": [...]}}}``. A body that is wrapped
    once only is returned as it is, whatever it holds.
    """
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, dict):
        return {}
    inner = errors.get("errors")
    return inner if isinstance(inner, dict) else errors


def _validation_codes(payload: Any) -> list[str]:
    """Return the validation codes of an error body."""
    codes = _error_groups(payload).get("Validation")
    if not isinstance(codes, list):
        return []
    return [str(code) for code in codes]


def _error_reasons(payload: Any) -> list[str]:
    """Return the names an error body gives, and none of what it quotes.

    A refusal with no validation code is otherwise reported as a bare status,
    which says nothing about what to change. Only what is shaped like a field
    name or like one of the portal's own codes is taken: a free text message
    may quote the value that was sent, a bare number may be the account or the
    EAN, and this text ends up in the interface, the recorder, the repair and
    the diagnostics.
    """
    reasons: list[str] = []
    for key, value in list(_error_groups(payload).items())[:_MAX_REASONS]:
        if not _is_name(key, _FIELD_RE):
            continue
        listed = value[:_MAX_REASONS] if isinstance(value, list) else []
        codes = [code for code in listed if _is_name(code, _CODE_RE)]
        reasons.append(f"{key} {', '.join(codes)}" if codes else key)
    return reasons


def _is_name(value: Any, shape: re.Pattern[str]) -> bool:
    """Return whether a value from an error body is a name of that shape."""
    return (
        isinstance(value, str)
        and len(value) <= _MAX_NAME
        and shape.fullmatch(value) is not None
    )


class EnecoClient:
    """Talk to the Eneco Belgium portal on behalf of one account holder.

    The caller owns ``session`` and it must keep cookies, because the whole
    login is carried by NextAuth and Okta cookies.

    One client serves every meter of a login, so calls can overlap. A sign in
    is a chain of requests whose cookies build on each other, and two of them
    interleaved on the same cookie jar would leave neither signed in. Only one
    runs at a time, and a call that finds its token already renewed by
    another uses the new one rather than signing in again.
    """

    def __init__(
        self, session: aiohttp.ClientSession, email: str, password: str
    ) -> None:
        """Initialise the client without contacting the portal."""
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._login_lock = asyncio.Lock()
        # Why Okta refused the credentials, once it has. Each try with them
        # is one more failure on the account, and enough of those lock it,
        # so none is made until new ones come with a new client.
        self._refused: str | None = None

    @property
    def refused(self) -> bool:
        """Return whether Okta refused the credentials, so only new ones help."""
        return self._refused is not None

    def _check_session(self) -> None:
        """Raise a portal error for a session closed by unloading the entry.

        Its tasks can still be running, and aiohttp raises RuntimeError for
        it, which no caller catches.
        """
        if self._session.closed:
            raise EnecoConnectionError("the session was closed by a reload")

    async def async_login(self) -> None:
        """Sign in and obtain a bearer token.

        Raises EnecoAuthError when the credentials are rejected or the account
        needs a step the portal only offers in a browser, such as MFA.
        """
        async with self._login_lock:
            await self._async_login_locked()

    async def _async_renew_token(self, stale: str | None) -> None:
        """Sign in again, unless another call already replaced ``stale``."""
        async with self._login_lock:
            if self._token is not None and self._token != stale:
                return
            self._token = None
            await self._async_login_locked()

    async def _async_login_locked(self) -> None:
        """Run the sign in chain; the caller holds the lock."""
        if self._refused is not None:
            raise EnecoAuthError(self._refused)
        try:
            session_token = await self._async_okta_session_token()
        except _RefusedError as err:
            self._refused = str(err)
            raise
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
            self._check_session()
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
                    raise _RefusedError(
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
            raise _RefusedError(
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
            self._check_session()
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
            self._check_session()
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
            self._check_session()
            async with self._session.get(url, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnecoConnectionError(f"{url} failed: {err}") from err
        except ValueError as err:
            raise EnecoConnectionError(f"{url} did not answer with JSON") from err

    async def async_get_accounts(self) -> list[Account]:
        """Return the accounts the user may act on, signing in again once.

        No REST endpoint exposes them; they travel in the NextAuth session and
        are rendered into the dashboard page by getServerSideProps. A client
        kept for every meter of a login can be asked for them long after it
        signed in, by which time that session may have lapsed, and a lapsed
        session answers with a page that never renders.
        """
        token = self._token
        try:
            return await self._async_read_accounts()
        except EnecoSessionGoneError:
            _LOGGER.debug("Dashboard did not render, signing in again")
        await self._async_renew_token(token)
        return await self._async_read_accounts()

    async def _async_read_accounts(self) -> list[Account]:
        """Read the accounts off the dashboard page."""
        try:
            self._check_session()
            async with self._session.get(
                DASHBOARD_URL, timeout=REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()
                body = await response.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnecoConnectionError(f"could not load the dashboard: {err}") from err

        match = _NEXT_DATA_RE.search(body)
        if match is None:
            raise EnecoSessionGoneError(
                "the dashboard did not render, the session may be gone"
            )
        try:
            data = json.loads(match.group(1))
        except ValueError as err:
            raise EnecoConnectionError("the dashboard payload was not JSON") from err

        raw: Any = data
        for key in ("props", "pageProps", "accounts"):
            raw = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(raw, list):
            raise EnecoNoAccountError("the dashboard listed no accounts")

        accounts = [
            Account(
                number=str(entry["crmAccountNumber"]),
                alias=str(entry.get("alias") or entry["crmAccountNumber"]),
            )
            for entry in raw
            if isinstance(entry, dict) and entry.get("crmAccountNumber")
        ]
        if not accounts:
            raise EnecoNoAccountError("the dashboard listed no accounts")
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
                contract_id=str(entry.get("contractId") or ""),
                meter_number=str(entry.get("meterNumber") or ""),
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

        listed = payload.get("registers")
        registers: list[Register] = []
        for entry in listed if isinstance(listed, list) else []:
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
            number=str(payload.get("meterNumber") or ""),
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
            indexes = entry.get("indexValues")
            for index in indexes if isinstance(indexes, list) else []:
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
                try:
                    values[register.key] = int(value)
                except (TypeError, ValueError):  # fmt: skip
                    _LOGGER.warning(
                        "Ignoring register %s, whose index reads %r",
                        register.key,
                        value,
                    )
            readings.append(
                Reading(
                    date=str(entry.get("date") or ""),
                    reading_type=str(entry.get("meterReadingType") or ""),
                    values=values,
                )
            )
        return sorted(readings, key=lambda r: r.date, reverse=True)

    async def async_get_invoices(self, account: str, address: str) -> list[Invoice]:
        """Return every invoice of a delivery address since its contract began.

        No dates are sent: the year of bills shown can reach back further than
        any fixed window would hold.
        """
        return invoices(
            await self._async_api(
                "GET",
                f"/accounts/{account}/delivery-addresses/{address}/invoices",
                params={"ShowActiveContractsOnly": "false"},
            )
        )

    async def async_get_tariff_card(
        self, account: str, contract: str
    ) -> TariffCard | None:
        """Return the tariff card a contract is billed on, if it names one."""
        return tariff_card(
            await self._async_api(
                "GET", f"/accounts/{account}/contracts/{contract}/products"
            )
        )

    async def async_get_advance_amounts(
        self, account: str, address: str
    ) -> AdvanceAmounts | None:
        """Return the monthly advances of a delivery address.

        A GET only: the same path takes the POST that changes the advance,
        which only :meth:`async_set_advance` sends.
        """
        return advance_amounts(
            await self._async_api(
                "GET",
                f"/accounts/{account}/delivery-addresses/{address}/advance-amounts",
            )
        )

    async def async_set_advance(
        self, account: str, address: str, ean: str, amount: float
    ) -> None:
        """Set the monthly advance of one contract, as the portal's edit page does.

        It changes what is paid: only the advance button sends it.
        """
        await self._async_api(
            "POST",
            f"/accounts/{account}/delivery-addresses/{address}/advance-amounts",
            json={"amount": round(amount, 2), "type": "Granular", "ean": ean},
        )

    async def async_get_monthly_volumes(
        self,
        account: str,
        address: str,
        point: ServiceDeliveryPoint,
        start: date,
        end: date,
    ) -> dict[str, MonthVolume]:
        """Return the monthly volumes the consumption page shows for a meter.

        They are the portal's spread of its readings over the months, good
        for the shape of a year rather than for what a period used.
        """
        if not point.meter_number:
            return {}
        return monthly_volumes(
            await self._async_api(
                "GET",
                f"/accounts/{account}/delivery-addresses/{address}/consumption",
                params={
                    "consumptionType": point.energy_type,
                    "displayMode": "Consumption",
                    "timeFrame": "Monthly",
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "meterNumber": point.meter_number,
                },
            )
        )

    async def async_get_document(self, url: str) -> bytes:
        """Download a bill the invoice list links to, signing in again once.

        The link is a route of the website that serves the PDF to the signed
        in session, by its cookies, so it is only ever fetched from there, and
        the bearer token is never sent with it. A session that has lapsed
        answers with something other than a PDF.

        A bill still refused after that is a failure of that bill alone, not
        of the login: raised as a connection error, it does not stop the other
        meters' bills or sign in again every day.
        """
        if not url.startswith(f"{WEB_HOST}/"):
            raise EnecoError("the bill is not on the Eneco website")
        token = self._token
        try:
            return await self._async_get_document_once(url)
        except EnecoSessionGoneError:
            _LOGGER.debug("Bill did not download, signing in again")
        await self._async_renew_token(token)
        try:
            return await self._async_get_document_once(url)
        except EnecoSessionGoneError as err:
            raise EnecoConnectionError(
                f"the website did not serve the bill: {err}"
            ) from None

    async def _async_get_document_once(self, url: str) -> bytes:
        """Fetch one document with the session's cookies.

        Redirects are followed here, and only while they stay on the website:
        the route itself answers 308 to add the trailing slash Next.js wants.
        """
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                self._check_session()
                async with self._session.get(
                    url, timeout=REQUEST_TIMEOUT, allow_redirects=False
                ) as response:
                    if response.status in _REDIRECTS:
                        location = URL(response.headers.get("Location", ""))
                        url = str(response.url.join(location))
                        if not url.startswith(f"{WEB_HOST}/"):
                            raise EnecoError("the bill moved off the Eneco website")
                        continue
                    if response.status in _SIGNED_OUT:
                        raise EnecoSessionGoneError(f"HTTP {response.status}")
                    if response.status != HTTPStatus.OK:
                        raise EnecoConnectionError(
                            f"the bill answered HTTP {response.status}"
                        )
                    body = await _read_capped(response)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EnecoConnectionError(
                    f"could not download the bill: {type(err).__name__}"
                ) from err
            if not body.startswith(b"%PDF"):
                raise EnecoSessionGoneError("the bill is not a PDF")
            return body
        raise EnecoError("the bill redirected too often")

    async def async_submit_readings(
        self,
        account: str,
        ean: str,
        meter_number: str,
        energy_type: str,
        reading_date: date,
        values: dict[Register, int],
    ) -> None:
        """Submit one reading per register.

        ``meter_number`` is the one :meth:`async_get_meter` reports. The
        website has sent it with every reading since Eneco redeployed the
        portal on 28 October 2026, the day a submission without it started
        coming back as a bare HTTP 400.

        Raises EnecoValidationError when the portal rejects the values.
        """
        body = [
            {
                "accountNumber": account,
                "meterNumber": meter_number,
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
            await self._async_renew_token(None)

        token = self._token
        payload, status = await self._async_api_once(method, path, **kwargs)
        if status == HTTPStatus.UNAUTHORIZED:
            _LOGGER.debug("Token rejected, signing in again")
            await self._async_renew_token(token)
            payload, status = await self._async_api_once(method, path, **kwargs)

        if status == HTTPStatus.UNAUTHORIZED:
            raise EnecoAuthError("the portal rejected the access token")
        if status >= HTTPStatus.BAD_REQUEST:
            if codes := _validation_codes(payload):
                raise EnecoValidationError(codes)
            _LOGGER.debug("%s %s returned HTTP %s", method, _endpoint(path), status)
            if reasons := _error_reasons(payload):
                raise EnecoConnectionError(
                    f"the portal returned HTTP {status}: {'; '.join(reasons)}"
                )
            raise EnecoConnectionError(f"the portal returned HTTP {status}")
        return _unwrap(payload)

    async def _async_api_once(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[Any, int]:
        """Perform a single REST call and return its body and status."""
        try:
            self._check_session()
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
                        "%s %s returned HTTP %s, no JSON",
                        method,
                        _endpoint(path),
                        status,
                    )
                    return None, status
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("%s %s failed: %s", method, _endpoint(path), _endpoint(err))
            # Redacted, not dropped: aiohttp quotes the URL it was given, and
            # every path here carries the CRM account number, but the rest of
            # the message is the only thing that says why the portal could not
            # be reached. This text is shown in the interface, kept by the
            # recorder, stored, and handed to the repair.
            raise EnecoConnectionError(
                f"could not reach the portal: {_endpoint(err) or type(err).__name__}"
            ) from err
        except ValueError as err:
            _LOGGER.debug("%s %s returned no JSON", method, _endpoint(path))
            raise EnecoConnectionError("the portal returned no JSON") from err

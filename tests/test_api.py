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

"""Tests for the portal client itself."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import date
from http import HTTPStatus
import json
import re
from typing import Any

from aiohttp import ClientSession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.eneco_be import api_billing
from custom_components.eneco_be.api import (
    EnecoAuthError,
    EnecoClient,
    EnecoConnectionError,
    EnecoValidationError,
    Register,
)
from homeassistant.core import HomeAssistant

from .conftest import (
    ACCOUNT,
    API,
    EAN,
    EMAIL,
    METER_NUMBER,
    PASSWORD,
    dashboard_page,
    mock_login,
)

TODAY = date(2026, 8, 21)


@pytest.fixture
async def session(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> AsyncGenerator[ClientSession]:
    """Return a session bound to the request mocker."""
    client_session = aioclient_mock.create_session(hass.loop)
    yield client_session
    await client_session.close()


def client(session: ClientSession) -> EnecoClient:
    """Return a client for the test account."""
    return EnecoClient(session, EMAIL, PASSWORD)


async def test_mfa_is_refused_clearly(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """An account that needs a second factor cannot be used headlessly."""
    aioclient_mock.post(
        "https://login.eneco.be/api/v1/authn",
        json={"status": "MFA_REQUIRED", "stateToken": "x"},
    )
    with pytest.raises(EnecoAuthError, match="interactive"):
        await client(session).async_login()


async def test_non_json_answer(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """An HTML error page is reported as a connection problem, not a crash."""
    aioclient_mock.post(
        "https://login.eneco.be/api/v1/authn", text="<html>maintenance</html>"
    )
    with pytest.raises(EnecoConnectionError):
        await client(session).async_login()


async def test_missing_access_token(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A session without a token is an authentication problem."""
    aioclient_mock.get("https://eneco.be/api/auth/session/", json={})
    mock_login(aioclient_mock, accounts=[])

    with pytest.raises(EnecoAuthError, match="no access token"):
        await client(session).async_login()


async def test_a_dashboard_that_did_not_render_is_a_session_gone(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A dashboard that did not render means the session is gone."""
    aioclient_mock.get(
        "https://eneco.be/fr/my-eneco/dashboard/", text="<html>login</html>"
    )
    mock_login(aioclient_mock, accounts=[])

    api = client(session)
    await api.async_login()
    with pytest.raises(EnecoAuthError, match="session may be gone"):
        await api.async_get_accounts()


async def test_a_lapsed_session_is_signed_in_again_for_the_accounts(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Adding a meter can come weeks after the client signed in.

    By then the web session can have lapsed, and the dashboard comes back as
    a page that never renders. That is worth one more sign in, not an abort.
    """
    pages = ["<html>login</html>"]

    async def dashboard(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        body = (
            pages.pop(0)
            if pages
            else dashboard_page(
                [{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}]
            )
        )
        return AiohttpClientMockResponse(method, url, text=body)

    aioclient_mock.get("https://eneco.be/fr/my-eneco/dashboard/", side_effect=dashboard)
    mock_login(aioclient_mock, accounts=[])

    api = client(session)
    await api.async_login()
    accounts = await api.async_get_accounts()

    assert [a.number for a in accounts] == [ACCOUNT]
    sign_ins = [
        url
        for method, url, _, _ in aioclient_mock.mock_calls
        if method.lower() == "post" and str(url).endswith("/api/v1/authn")
    ]
    assert len(sign_ins) == 2


async def test_accounts_are_parsed(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Account claims come out of the rendered dashboard."""
    mock_login(
        aioclient_mock,
        accounts=[
            {"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"},
            {"crmAccountNumber": "222", "alias": "", "role": "reader"},
        ],
    )
    api = client(session)
    await api.async_login()
    accounts = await api.async_get_accounts()

    assert [(a.number, a.alias) for a in accounts] == [
        (ACCOUNT, "Home"),
        ("222", "222"),
    ]


async def test_expired_token_is_renewed(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A rejected token triggers one new sign in and one retry."""
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )

    calls: list[str] = []

    async def answer(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        calls.append(method)
        if len(calls) == 1:
            return AiohttpClientMockResponse(
                method, url, status=HTTPStatus.UNAUTHORIZED, json={}
            )
        return AiohttpClientMockResponse(method, url, json={"data": {"addresses": []}})

    aioclient_mock.get(
        f"{API}/accounts/{ACCOUNT}/delivery-addresses", side_effect=answer
    )

    api = client(session)
    await api.async_login()
    assert await api.async_get_addresses(ACCOUNT) == []
    assert len(calls) == 2


async def test_token_rejected_twice_gives_up(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A token the portal keeps refusing surfaces as an auth error."""
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    aioclient_mock.get(
        f"{API}/accounts/{ACCOUNT}/delivery-addresses",
        status=HTTPStatus.UNAUTHORIZED,
        json={},
    )

    api = client(session)
    await api.async_login()
    with pytest.raises(EnecoAuthError):
        await api.async_get_addresses(ACCOUNT)


async def test_validation_codes_are_surfaced(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Both codes of a rejected submission reach the caller."""
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    aioclient_mock.post(
        f"{API}/accounts/{ACCOUNT}/meter-readings",
        status=HTTPStatus.BAD_REQUEST,
        json={
            "errors": {
                "errors": {
                    "Validation": [
                        "MeterReading.NewReadingsShouldBeHigher.HI.OffTake",
                        "MeterReading.AlreadyExists",
                    ]
                }
            }
        },
    )

    api = client(session)
    await api.async_login()
    with pytest.raises(EnecoValidationError) as raised:
        await api.async_submit_readings(
            ACCOUNT,
            EAN,
            METER_NUMBER,
            "Electricity",
            TODAY,
            {Register("OffTake", "HI"): 100},
        )

    assert raised.value.codes == [
        "MeterReading.NewReadingsShouldBeHigher.HI.OffTake",
        "MeterReading.AlreadyExists",
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            {"errors": {"errors": {"Unexpected": ["AddMeterReadings.Failed"]}}},
            "the portal returned HTTP 400: Unexpected AddMeterReadings.Failed",
        ),
        (
            {
                "title": "One or more validation errors occurred.",
                "errors": {
                    "[0].MeterNumber": ["The MeterNumber field is required."],
                    "$[0].readingValue": ["'1SAG1100000000' is not an Int32."],
                    "1234567890": ["MeterReading.AlreadyExists"],
                    "a b": ["MeterReading.AlreadyExists"],
                },
            },
            "the portal returned HTTP 400: [0].MeterNumber; $[0].readingValue",
        ),
        ({"errors": "nope"}, "the portal returned HTTP 400"),
        (["MeterReading.AlreadyExists"], "the portal returned HTTP 400"),
    ],
)
async def test_a_refusal_without_a_validation_code_names_its_reason(
    aioclient_mock: AiohttpClientMocker,
    session: ClientSession,
    body: Any,
    message: str,
) -> None:
    """A bare HTTP 400 is all that was left of the refusal since October.

    The names in the body are kept, and nothing else: the free text quotes
    what was sent, and a key that is only digits may be the account.
    """
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    aioclient_mock.post(
        f"{API}/accounts/{ACCOUNT}/meter-readings",
        status=HTTPStatus.BAD_REQUEST,
        json=body,
    )

    api = client(session)
    await api.async_login()
    with pytest.raises(EnecoConnectionError) as raised:
        await api.async_submit_readings(
            ACCOUNT,
            EAN,
            METER_NUMBER,
            "Electricity",
            TODAY,
            {Register("OffTake", "HI"): 100},
        )

    assert str(raised.value) == message


async def test_a_meter_answer_that_is_not_shaped_right(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """The register list gets the same type check as every other lookup.

    The addresses and the delivery points are both checked for being a list
    before they are walked; the registers were not, so a scalar there raised
    TypeError straight out of the config flow instead of an EnecoError the
    flow knows how to abort on.
    """
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    aioclient_mock.get(
        re.compile(rf"{re.escape(API)}/accounts/{ACCOUNT}/meter-readings/registers.*"),
        json={"data": {"ean": EAN, "energyType": "Electricity", "registers": 7}},
    )

    api = client(session)
    await api.async_login()
    meter = await api.async_get_meter(ACCOUNT, EAN)

    assert meter.registers == ()


async def test_a_reading_whose_index_is_not_a_number(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """int() on a wire value raises outside the client's own error taxonomy."""
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    aioclient_mock.get(
        re.compile(rf"{re.escape(API)}/accounts/{ACCOUNT}/meter-readings.*"),
        json={
            "data": [
                {
                    "date": "2026-08-20",
                    "meterReadingType": "Customer",
                    "indexValues": [
                        {
                            "direction": "OffTake",
                            "meterTimeFrameType": "HI",
                            "value": "not a number",
                        },
                        {
                            "direction": "OffTake",
                            "meterTimeFrameType": "LO",
                            "value": 42,
                        },
                    ],
                }
            ]
        },
    )

    api = client(session)
    await api.async_login()
    readings = await api.async_get_readings(ACCOUNT, EAN)

    assert readings[0].values == {"offtake_night": 42}


async def test_meters_sharing_a_client_sign_in_once(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Two calls refused on the same token renew it once, not twice over.

    A login serves every meter of the account, so a gas and an electricity
    submission can find the token expired at the same moment. Both signing in
    would interleave two Okta flows on one cookie jar.
    """
    issued: list[str] = []

    async def token(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        issued.append(f"t{len(issued) + 1}")
        await asyncio.sleep(0)
        return AiohttpClientMockResponse(method, url, json={"accessToken": issued[-1]})

    async def answer(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        # Recorded just before this runs, and nothing awaits in between.
        refused = aioclient_mock.mock_calls[-1][3]["Authorization"] == "Bearer t1"
        await asyncio.sleep(0)
        if refused:
            return AiohttpClientMockResponse(
                method, url, status=HTTPStatus.UNAUTHORIZED, json={}
            )
        return AiohttpClientMockResponse(method, url, json={"data": {"addresses": []}})

    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
        access_token=token,
    )
    aioclient_mock.get(
        f"{API}/accounts/{ACCOUNT}/delivery-addresses", side_effect=answer
    )

    api = client(session)
    await api.async_login()
    first, second = await asyncio.gather(
        api.async_get_addresses(ACCOUNT), api.async_get_addresses(ACCOUNT)
    )
    assert first == second == []
    assert issued == ["t1", "t2"]


def test_advance_amounts_are_parsed() -> None:
    """The portal's advances page, keeping only what has the shape it should."""
    got = api_billing.advance_amounts(
        {
            "advancePayments": [
                {
                    "meterDetails": {
                        "ean": "541449000000000001",
                        "energyType": "Electricity",
                        "meterReadingMonth": 1,
                        "invoiceFrequency": "Yearly",
                    },
                    "details": {
                        "currentAmount": 56.51,
                        "recommendedAmount": 554.43,
                        "monthsUntilMeterReadingMonth": 3,
                        "amountDueForMeterReadingMonth": 1663.29,
                        "minAmount": 277,
                        "maxAmount": 2500,
                    },
                },
                {"meterDetails": {"energyType": "Gas"}, "details": {}},
                {
                    "meterDetails": {
                        "ean": "541449000000000099",
                        "energyType": "Gas",
                        "meterReadingMonth": 13,
                    },
                    "details": {
                        "currentAmount": "300",
                        "monthsUntilMeterReadingMonth": -1,
                    },
                },
                "nonsense",
            ],
            "totals": {
                "currentAmount": 356.51,
                "recommendedAmount": 861.13,
                "minAmount": 430,
                "maxAmount": 2500,
            },
        }
    )
    assert got == api_billing.AdvanceAmounts(
        current=356.51,
        recommended=861.13,
        contracts=(
            api_billing.ContractAdvance(
                "541449000000000001",
                "Electricity",
                56.51,
                554.43,
                3,
                1663.29,
                reading_month=1,
                minimum=277,
                maximum=2500,
            ),
            api_billing.ContractAdvance(
                "541449000000000099",
                "Gas",
                None,
                None,
                None,
                None,
                blocked="not billed yearly",
            ),
        ),
        minimum=430,
        maximum=2500,
    )
    assert api_billing.advance_amounts(None) is None


def test_monthly_volumes_are_parsed() -> None:
    """Every register of a month adds to what was taken or fed back.

    A month with no value is left out rather than read as nothing used.
    """
    got = api_billing.monthly_volumes(
        {
            "periods": [
                {
                    "startDate": "2026-06-01T00:00:00+02:00",
                    "data": [
                        {
                            "direction": "OffTake",
                            "meterTimeFrameType": "HI",
                            "value": 51.5,
                        },
                        {
                            "direction": "OffTake",
                            "meterTimeFrameType": "LO",
                            "value": 142.5,
                        },
                        {
                            "direction": "Injection",
                            "meterTimeFrameType": "HI",
                            "value": 176,
                        },
                        {
                            "direction": "Injection",
                            "meterTimeFrameType": "LO",
                            "value": None,
                        },
                    ],
                },
                {"startDate": "2026-07-01T00:00:00+02:00", "data": []},
                {"startDate": "bad", "data": [{"value": 1}]},
            ]
        }
    )
    assert got == {"2026-06": api_billing.MonthVolume(194.0, 176.0)}


@pytest.mark.parametrize(
    ("meter", "details", "blocked"),
    [
        ({"invoiceFrequency": "Yearly"}, {}, None),
        ({"invoiceFrequency": "Monthly"}, {}, "not billed yearly"),
        ({"invoiceFrequency": "Yearly"}, {"isDefaulter": True}, "payments are overdue"),
        (
            {"invoiceFrequency": "Yearly"},
            {"notAvailable": True},
            "the advance is not available",
        ),
        (
            {"invoiceFrequency": "Yearly"},
            {"vacancyChangeRequested": True},
            "a vacancy is being requested",
        ),
        (
            {"invoiceFrequency": "Yearly"},
            {"recommendedAmountError": "LastAdvanceDatetimeEqualToBillingDate"},
            "the portal reports LastAdvanceDatetimeEqualToBillingDate",
        ),
        ({"invoiceFrequency": "Yearly"}, {"recommendedAmountError": "Other"}, None),
    ],
)
def test_an_advance_the_portal_would_not_change(
    meter: dict[str, Any], details: dict[str, Any], blocked: str | None
) -> None:
    """The conditions of the portal's own edit page, and a vacancy's end."""
    got = api_billing.advance_amounts(
        {
            "advancePayments": [
                {
                    "meterDetails": {"ean": "541449000000000001", **meter},
                    "details": {"vacancyUntil": "2026-12-31T00:00:00+00:00", **details},
                }
            ]
        }
    )
    assert got is not None
    (contract,) = got.contracts
    assert contract.blocked == blocked
    assert contract.vacancy_until == date(2026, 12, 31)


async def test_a_refused_password_is_not_tried_again(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Each try with a password Okta refused is one more failure on the account.

    Every call waiting on the sign in tried again once it failed, and so
    did every call after, every day, until the password was entered anew:
    enough failures lock the account on the portal too.
    """
    aioclient_mock.post(
        "https://login.eneco.be/api/v1/authn",
        status=HTTPStatus.UNAUTHORIZED,
        json={"errorSummary": "Authentication failed"},
    )
    api = client(session)
    with pytest.raises(EnecoAuthError, match="Authentication failed"):
        await api.async_login()
    results = await asyncio.gather(
        *(api.async_get_addresses(ACCOUNT) for _ in range(3)),
        return_exceptions=True,
    )
    assert all(isinstance(result, EnecoAuthError) for result in results)
    # A lapsed session answers a bill with its login page.
    bill = "https://eneco.be/api/my-eneco/download/X"
    aioclient_mock.get(bill, text="<html>login</html>")
    with pytest.raises(EnecoAuthError, match="Authentication failed"):
        await api.async_get_document(bill)

    tries = [
        url
        for method, url, _, _ in aioclient_mock.mock_calls
        if method.lower() == "post" and str(url).endswith("/api/v1/authn")
    ]
    assert len(tries) == 1


@pytest.mark.parametrize("error", [{"code": "X"}, ["X"]])
def test_an_advance_error_of_another_shape_blocks_the_change(error: Any) -> None:
    """An error that is not a name the portal uses still says something is wrong.

    It raised TypeError, which no caller catches, and the whole read of the
    bills failed with it.
    """
    got = api_billing.advance_amounts(
        {
            "advancePayments": [
                {
                    "meterDetails": {
                        "ean": "541449000000000001",
                        "energyType": "Electricity",
                        "invoiceFrequency": "Yearly",
                    },
                    "details": {
                        "currentAmount": 56.51,
                        "recommendedAmountError": error,
                    },
                }
            ],
            "totals": {"currentAmount": 56.51},
        }
    )
    assert got is not None
    assert got.contracts[0].blocked == "the portal reports an error it does not name"


@pytest.mark.parametrize(
    "payload", [{"props": None}, {"props": {"pageProps": None}}, ["accounts"]]
)
async def test_a_dashboard_of_another_shape_lists_no_account(
    aioclient_mock: AiohttpClientMocker, session: ClientSession, payload: Any
) -> None:
    """It raised AttributeError in the flows instead of saying so."""
    aioclient_mock.get(
        "https://eneco.be/fr/my-eneco/dashboard/",
        text='<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}</script>",
    )
    mock_login(aioclient_mock, accounts=[])
    api = client(session)
    await api.async_login()
    with pytest.raises(EnecoAuthError, match="listed no accounts"):
        await api.async_get_accounts()


async def test_readings_whose_indexes_are_not_a_list_have_none(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A scalar raised TypeError, which no caller catches."""
    mock_login(aioclient_mock, accounts=[])
    aioclient_mock.get(
        f"{API}/accounts/{ACCOUNT}/meter-readings",
        json={"data": [{"date": "2026-11-25", "indexValues": 5}]},
    )
    api = client(session)
    await api.async_login()
    readings = await api.async_get_readings(ACCOUNT, EAN)
    assert [reading.values for reading in readings] == [{}]


async def test_a_session_closed_by_a_reload_is_a_portal_error() -> None:
    """Unloading the entry detaches its session before its tasks are done.

    aiohttp then raises RuntimeError, which no caller catches: a submission
    or an apply between two calls died with a traceback.
    """
    session = ClientSession()
    api = client(session)
    api._token = "bearer"  # noqa: SLF001
    session.detach()
    with pytest.raises(EnecoConnectionError, match="session"):
        await api.async_get_addresses(ACCOUNT)
    with pytest.raises(EnecoConnectionError, match="session"):
        await api.async_login()
    with pytest.raises(EnecoConnectionError, match="session"):
        await api.async_get_document("https://eneco.be/api/my-eneco/download/X")


@pytest.mark.parametrize("value", [10**400, -(10**400)])
def test_an_integer_past_a_float_is_no_amount(value: int) -> None:
    """JSON reads one as an int, and isfinite raised OverflowError on it.

    No caller catches that: a dueAmount like it failed the whole bill read.
    """
    assert api_billing.amount(value) is None

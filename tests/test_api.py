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

from collections.abc import AsyncGenerator
from datetime import date
from http import HTTPStatus
import re
from typing import Any

from aiohttp import ClientSession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.eneco_be.api import (
    EnecoAuthError,
    EnecoClient,
    EnecoConnectionError,
    EnecoValidationError,
    Register,
)
from homeassistant.core import HomeAssistant

from .conftest import ACCOUNT, API, EAN, EMAIL, METER_NUMBER, PASSWORD, mock_login

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


async def test_dashboard_without_accounts(
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

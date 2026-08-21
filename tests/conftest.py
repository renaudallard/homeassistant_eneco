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

"""Shared fixtures for the Eneco Belgium tests."""

import json
import re
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.eneco_be.const import (
    CONF_ACCOUNT,
    CONF_ADDRESS,
    CONF_EAN,
    CONF_REGISTERS,
    CONF_SOURCES,
    CONF_SUBMIT_TIME,
    DOMAIN,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

ACCOUNT = "1234567890"
ADDRESS = "a" * 64
EAN = "541449000000000001"

EMAIL = "someone@example.com"
PASSWORD = "hunter2"

API = "https://api-prd.be-digitalcore.enecogroup.com/eneco-be/xapi/site/api/v1"

REGISTERS = ["offtake_day", "offtake_night", "injection_day", "injection_night"]

SOURCES = {
    "offtake_day": "sensor.import_day",
    "offtake_night": "sensor.import_night",
    "injection_day": "sensor.export_day",
    "injection_night": "sensor.export_night",
}


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load this custom integration."""
    return


def dashboard_page(accounts: list[dict[str, str]]) -> str:
    """Return a dashboard page carrying the given account claims."""
    payload = json.dumps({"props": {"pageProps": {"accounts": accounts}}})
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{payload}"
        "</script></body></html>"
    )


def mock_login(mocker: AiohttpClientMocker, *, accounts: list[dict[str, str]]) -> None:
    """Register the whole sign in sequence."""
    mocker.post(
        "https://login.eneco.be/api/v1/authn",
        json={"status": "SUCCESS", "sessionToken": "one-shot"},
    )
    mocker.get("https://eneco.be/api/auth/csrf/", json={"csrfToken": "csrf"})
    mocker.post(
        "https://eneco.be/api/auth/signin/okta/",
        json={"url": "https://login.eneco.be/oauth2/x/v1/authorize?client_id=y"},
    )
    mocker.get(
        re.compile(r"https://login\.eneco\.be/login/sessionCookieRedirect.*"),
        text="",
    )
    mocker.get("https://eneco.be/api/auth/session/", json={"accessToken": "bearer"})
    mocker.get("https://eneco.be/fr/my-eneco/dashboard/", text=dashboard_page(accounts))


def mock_discovery(
    mocker: AiohttpClientMocker,
    *,
    addresses: list[dict[str, Any]] | None = None,
    points: list[dict[str, Any]] | None = None,
    registers: list[dict[str, str]] | None = None,
) -> None:
    """Register the account, address and meter lookups."""
    mocker.get(
        f"{API}/accounts/{ACCOUNT}/delivery-addresses",
        json={
            "data": {
                "addresses": addresses
                if addresses is not None
                else [
                    {
                        "id": ADDRESS,
                        "address": "Rue de la Loi 1, 1000 Bruxelles",
                        "hasActiveContracts": True,
                    }
                ]
            }
        },
    )
    mocker.get(
        f"{API}/accounts/{ACCOUNT}/delivery-addresses/{ADDRESS}/service-delivery-points",
        json={
            "data": {
                "serviceDeliveryPoints": points
                if points is not None
                else [{"ean": EAN, "type": "Electricity", "meterType": "Digital"}]
            }
        },
    )
    mocker.get(
        re.compile(rf"{re.escape(API)}/accounts/{ACCOUNT}/meter-readings/registers.*"),
        json={
            "data": {
                "ean": EAN,
                "meterNumber": "1SAG1100000000",
                "energyType": "Electricity",
                "meterType": "Digital",
                "meterRegime": "Smr3",
                "hasInjection": True,
                "registers": registers
                if registers is not None
                else [
                    {"direction": "OffTake", "meterTimeFrameType": "HI"},
                    {"direction": "OffTake", "meterTimeFrameType": "LO"},
                    {"direction": "Injection", "meterTimeFrameType": "HI"},
                    {"direction": "Injection", "meterTimeFrameType": "LO"},
                ],
            }
        },
    )


@pytest.fixture
def mock_portal(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Register a portal that answers everything the happy path needs."""
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(aioclient_mock)
    aioclient_mock.post(f"{API}/accounts/{ACCOUNT}/meter-readings", json={"data": None})
    return aioclient_mock


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a configured entry for the meter used in the tests."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Eneco {EAN}",
        unique_id=EAN,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_ACCOUNT: ACCOUNT,
            CONF_ADDRESS: ADDRESS,
            CONF_EAN: EAN,
            CONF_REGISTERS: REGISTERS,
        },
        options={CONF_SOURCES: SOURCES, CONF_SUBMIT_TIME: "23:50:00"},
    )

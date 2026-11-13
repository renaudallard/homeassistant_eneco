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

"""Tests for the Eneco Belgium config flow."""

from datetime import time
from http import HTTPStatus
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.eneco_be.const import (
    CONF_ACCOUNT,
    CONF_ADDRESS,
    CONF_EAN,
    CONF_ENERGY_TYPE,
    CONF_REGISTERS,
    CONF_SOURCES,
    CONF_SUBMIT_TIME,
    DOMAIN,
    SUBENTRY_METER,
)
from custom_components.eneco_be.meter_flow import suggested_submit_time
from custom_components.eneco_be.migrate import login_title
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import (
    ACCOUNT,
    ADDRESS,
    EAN,
    EMAIL,
    METER_ID,
    PASSWORD,
    REGISTERS,
    SOURCES,
    coordinator_of,
    login_entry,
    meter_of,
    mock_discovery,
    mock_login,
)

CREDENTIALS = {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up an entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def logins(mocker: AiohttpClientMocker) -> list[Any]:
    """Return the credentials of every sign in posted to Okta."""
    return [
        data
        for method, url, data, _ in mocker.mock_calls
        if method.lower() == "post" and str(url).endswith("/api/v1/authn")
    ]


async def reconfigure_meter(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Start changing the sources of the meter of the tests."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_METER),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": METER_ID},
    )


async def test_full_flow(hass: HomeAssistant, mock_portal: AiohttpClientMocker) -> None:
    """A single account, address and meter needs only two forms."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    assert result["description_placeholders"] == {"ean": EAN}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SOURCES, CONF_SUBMIT_TIME: "22:15:00"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "s•••@e•••.com"
    assert result["data"] == CREDENTIALS
    entry = result["result"]
    assert entry.unique_id == EMAIL
    (meter,) = entry.subentries.values()
    assert meter.subentry_type == SUBENTRY_METER
    assert meter.unique_id == EAN
    assert meter.title == f"Electricity {EAN}"
    assert meter.data == {
        CONF_ACCOUNT: ACCOUNT,
        CONF_ADDRESS: ADDRESS,
        CONF_EAN: EAN,
        CONF_ENERGY_TYPE: "Electricity",
        CONF_REGISTERS: REGISTERS,
        CONF_SOURCES: SOURCES,
        CONF_SUBMIT_TIME: "22:15:00",
    }


async def test_pickers_shown_when_ambiguous(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Every step with more than one answer is put to the user."""
    mock_login(
        aioclient_mock,
        accounts=[
            {"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"},
            {"crmAccountNumber": "999", "alias": "Office", "role": "owner"},
        ],
    )
    mock_discovery(
        aioclient_mock,
        addresses=[
            {"id": ADDRESS, "address": "Home", "hasActiveContracts": True},
            {"id": "b" * 64, "address": "Shed", "hasActiveContracts": True},
        ],
        points=[
            {"ean": EAN, "type": "Electricity"},
            {"ean": "541449020702479999", "type": "Electricity"},
            {"ean": "541449020702470000", "type": "Gas"},
        ],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["step_id"] == "account"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT: ACCOUNT}
    )
    assert result["step_id"] == "address"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["step_id"] == "meter"
    # Gas is offered alongside electricity.
    assert result["data_schema"] is not None
    options = result["data_schema"].schema[CONF_EAN].config["options"]
    assert [option["value"] for option in options] == [
        EAN,
        "541449020702479999",
        "541449020702470000",
    ]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EAN: EAN}
    )
    assert result["step_id"] == "sources"


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (HTTPStatus.UNAUTHORIZED, {"errorSummary": "nope"}, "invalid_auth"),
        (HTTPStatus.OK, {"status": "MFA_REQUIRED"}, "invalid_auth"),
        (HTTPStatus.INTERNAL_SERVER_ERROR, {}, "cannot_connect"),
    ],
)
async def test_login_failures(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: HTTPStatus,
    payload: dict[str, Any],
    expected: str,
) -> None:
    """A refused or unreachable sign in keeps the user on the form."""
    aioclient_mock.post(
        "https://login.eneco.be/api/v1/authn", status=status, json=payload
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}


async def test_retry_after_wrong_password(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A second attempt with the right password gets its own clean session."""
    attempts: list[str] = []

    async def authn(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        attempts.append(method)
        if len(attempts) == 1:
            return AiohttpClientMockResponse(
                method, url, status=HTTPStatus.UNAUTHORIZED, json={}
            )
        return AiohttpClientMockResponse(
            method, url, json={"status": "SUCCESS", "sessionToken": "one-shot"}
        )

    # Registered first, so it answers before the one mock_login adds.
    aioclient_mock.post("https://login.eneco.be/api/v1/authn", side_effect=authn)
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(aioclient_mock)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    assert len(attempts) == 2


async def test_already_configured(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The same meter cannot be set up twice."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_meters(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An address with nothing metered cannot be configured."""
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(aioclient_mock, points=[{"ean": "5414", "type": "Unknown"}])

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_meters"


async def test_reauth(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A new password is verified and stored on the existing entry."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    # Home Assistant adds its own "name" placeholder to reauth flows.
    assert result["description_placeholders"][CONF_EMAIL] == EMAIL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-secret"

    # The abort reloads the entry, which arms the daily timer.
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reauth_brings_a_failed_login_back(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """An entry whose setup was refused has no listener to reload it."""

    async def authn(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        if data["password"] != "new-secret":
            return AiohttpClientMockResponse(
                method, url, status=HTTPStatus.UNAUTHORIZED, json={}
            )
        return AiohttpClientMockResponse(
            method, url, json={"status": "SUCCESS", "sessionToken": "one-shot"}
        )

    # Registered first, so it answers before the one mock_login adds.
    aioclient_mock.post("https://login.eneco.be/api/v1/authn", side_effect=authn)
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(aioclient_mock)

    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    failed = config_entry.state
    assert failed is ConfigEntryState.SETUP_ERROR

    (flow,) = hass.config_entries.flow.async_progress()
    assert flow["context"]["source"] == "reauth"
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_PASSWORD: "new-secret"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert config_entry.state is ConfigEntryState.LOADED


async def test_changing_the_sources(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Reconfiguring a meter rewrites its sources and time, without a reload."""
    await setup(hass, config_entry)
    client = coordinator_of(config_entry).client
    sign_ins = len(logins(mock_portal))

    result = await reconfigure_meter(hass, config_entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"

    changed = {**SOURCES, "offtake_day": "sensor.other"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**changed, CONF_SUBMIT_TIME: "06:00:00"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert meter_of(config_entry).data[CONF_SOURCES] == changed
    assert meter_of(config_entry).data[CONF_SUBMIT_TIME] == "06:00:00"
    coordinator = coordinator_of(config_entry)
    assert coordinator.client is client
    assert coordinator.submit_time == time(6, 0)
    assert len(logins(mock_portal)) == sign_ins


async def test_reconfigure(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Reconfiguring the login changes its credentials and keeps its meters."""
    await setup(hass, config_entry)
    client = coordinator_of(config_entry).client
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "new-secret"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data == {CONF_EMAIL: EMAIL, CONF_PASSWORD: "new-secret"}
    assert meter_of(config_entry).data[CONF_EAN] == EAN
    assert config_entry.state is ConfigEntryState.LOADED
    # Reloaded on the new password, from the entry's own update listener.
    assert coordinator_of(config_entry).client is not client
    assert logins(mock_portal)[-1]["password"] == "new-secret"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_onto_a_login_without_the_account_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The meters stay, so the login must still reach their account."""
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": "999", "alias": "Office", "role": "owner"}],
    )

    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "other@example.com", CONF_PASSWORD: "x"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "accounts_missing"
    assert config_entry.data[CONF_EMAIL] == EMAIL
    assert config_entry.unique_id == EMAIL


async def test_reconfigure_onto_another_configured_login_is_refused(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Two entries for one login would sign in twice, which is the point."""
    other = login_entry()
    other.add_to_hass(hass)
    hass.config_entries.async_update_entry(other, unique_id="other@example.com")

    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "Other@Example.com", CONF_PASSWORD: "x"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_EMAIL] == EMAIL


async def test_a_login_already_set_up_is_refused_before_signing_in(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Its other meters belong on the entry it has, under Add meter."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL.upper(), CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert logins(mock_portal) == []


async def test_a_login_with_no_account(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Signing in and owning nothing is not a wrong password.

    Reading the accounts raises the same error as a refused sign in, and both
    used to come back as invalid_auth, which told the user to check credentials
    the portal had just accepted.
    """
    mock_login(aioclient_mock, accounts=[])

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_accounts"}


async def test_a_second_flow_for_the_same_login(
    hass: HomeAssistant, mock_portal: AiohttpClientMocker
) -> None:
    """async_set_unique_id aborts on a flow already holding the same login.

    The reason it aborts with had no text anywhere, so the dialog had nothing
    to show but the bare already_in_progress.
    """
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    first = await hass.config_entries.flow.async_configure(
        first["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert first["step_id"] == "sources"

    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"


async def test_reconfigure_offers_the_address_already_on_file(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Reconfigure knows whose account it is walking, so it should say so."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)

    assert result["step_id"] == "user"
    field = next(key for key in result["data_schema"].schema if key == CONF_EMAIL)
    assert field.description == {"suggested_value": EMAIL}


@pytest.mark.parametrize(
    ("email", "title"),
    [
        ("someone@example.com", "s•••@e•••.com"),
        (" Renaud@Mail.Example.be ", "R•••@M•••.be"),
        ("someone@localhost", "s•••@l•••"),
        ("not-an-address", "n•••"),
    ],
)
def test_the_entry_title_keeps_the_address_out_of_the_log(
    email: str, title: str
) -> None:
    """Home Assistant logs entry titles, and users paste those logs."""
    assert login_title(email) == title


def _offered_time(result: Any) -> time:
    """Return the submission time a form is offering."""
    schema = result["data_schema"]
    assert schema is not None
    field = next(key for key in schema.schema if key == CONF_SUBMIT_TIME)
    return time.fromisoformat(field.default())


def test_the_suggested_time_is_scattered_across_the_window() -> None:
    """One fixed default would land every installation on Eneco at once."""
    picks = {suggested_submit_time() for _ in range(300)}

    assert len(picks) > 1
    for pick in picks:
        offered = time.fromisoformat(pick)
        assert time(22, 30) <= offered <= time(23, 50)
        assert offered.second == 0


async def test_a_first_setup_is_offered_a_time_from_the_window(
    hass: HomeAssistant, mock_portal: AiohttpClientMocker
) -> None:
    """A setup with no time of its own gets one drawn from the window.

    Checking the range alone would not notice a fixed default, since the old
    one sat inside the window, so this walks the form several times over and
    asks that they were not all handed the same minute.
    """
    offered = set()
    for _ in range(8):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
        )
        assert result["step_id"] == "sources"
        chosen = _offered_time(result)
        assert time(22, 30) <= chosen <= time(23, 50)
        assert chosen.second == 0
        offered.add(chosen)
        # The next flow would abort as already in progress otherwise, since
        # this one is holding the login's unique id.
        hass.config_entries.flow.async_abort(result["flow_id"])

    assert len(offered) > 1


async def test_changing_the_sources_keeps_the_time_in_use(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Only a new meter is scattered; the rest keep what they chose."""
    await setup(hass, config_entry)

    result = await reconfigure_meter(hass, config_entry)
    assert _offered_time(result) == time(23, 50)

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
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import (
    ACCOUNT,
    ADDRESS,
    EAN,
    EMAIL,
    PASSWORD,
    REGISTERS,
    SOURCES,
    mock_discovery,
    mock_login,
)

CREDENTIALS = {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}


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
    assert result["title"] == f"Eneco {EAN}"
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_ACCOUNT: ACCOUNT,
        CONF_ADDRESS: ADDRESS,
        CONF_EAN: EAN,
        CONF_ENERGY_TYPE: "Electricity",
        CONF_REGISTERS: REGISTERS,
    }
    assert result["options"] == {
        CONF_SOURCES: SOURCES,
        CONF_SUBMIT_TIME: "22:15:00",
    }
    assert result["result"].unique_id == EAN


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
    # The gas connection is not offered.
    assert result["data_schema"] is not None

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


async def test_options_flow(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The options flow rewrites the sources and the submission time."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    changed = {**SOURCES, "offtake_day": "sensor.other"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**changed, CONF_SUBMIT_TIME: "06:00:00"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_SOURCES: changed,
        CONF_SUBMIT_TIME: "06:00:00",
    }


async def test_reconfigure(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Reconfigure walks the whole flow and rewrites the existing entry."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["step_id"] == "sources"

    changed = {**SOURCES, "offtake_day": "sensor.new_import_day"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**changed, CONF_SUBMIT_TIME: "05:00:00"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.options[CONF_SOURCES] == changed
    assert config_entry.data[CONF_EAN] == EAN
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reconfigure_onto_a_different_meter_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """An entry is pinned to its meter, so a different EAN must abort."""
    other = "541449000000000002"
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(
        aioclient_mock, points=[{"ean": other, "type": "Electricity"}], ean=other
    )

    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert config_entry.data[CONF_EAN] == EAN

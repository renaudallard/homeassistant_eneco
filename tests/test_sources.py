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

"""Optional register mapping, and meters other than electricity."""

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

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
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_EMAIL,
    CONF_PASSWORD,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import ACCOUNT, ADDRESS, API, EMAIL, PASSWORD, mock_discovery, mock_login
from .test_init import (
    STATUS,
    press,
    press_entity,
    set_sources,
    setup_entry,
    state_of,
    submissions,
)

GAS_STATUS = "sensor.eneco_541449000000000009_status"

GAS_EAN = "541449000000000009"
GAS_BUTTON = f"button.eneco_{GAS_EAN}_submit_readings"


def gas_entry(**options: Any) -> MockConfigEntry:
    """Return an entry for a single register gas meter."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Eneco {GAS_EAN}",
        unique_id=GAS_EAN,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_ACCOUNT: ACCOUNT,
            CONF_ADDRESS: ADDRESS,
            CONF_EAN: GAS_EAN,
            CONF_ENERGY_TYPE: "Gas",
            CONF_REGISTERS: ["offtake_single"],
        },
        options={CONF_SUBMIT_TIME: "23:50:00", **options},
    )


def mock_gas(mocker: AiohttpClientMocker) -> None:
    """Register a portal whose address has a gas meter."""
    mock_login(
        mocker,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(
        mocker,
        points=[{"ean": GAS_EAN, "type": "Gas"}],
        registers=[{"direction": "OffTake", "meterTimeFrameType": "TH"}],
        ean=GAS_EAN,
        energy_type="Gas",
    )
    mocker.post(f"{API}/accounts/{ACCOUNT}/meter-readings", json={"data": None})


async def test_a_gas_meter_can_be_configured(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Gas uses the same endpoints, so it is offered like any other meter."""
    mock_gas(aioclient_mock)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["step_id"] == "sources"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"offtake_single": "sensor.gas", CONF_SUBMIT_TIME: "23:50:00"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENERGY_TYPE] == "Gas"
    assert result["data"][CONF_EAN] == GAS_EAN
    assert result["data"][CONF_REGISTERS] == ["offtake_single"]


async def test_gas_is_read_and_sent_in_cubic_metres(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A gas index is a volume, not an energy."""
    mock_gas(aioclient_mock)
    entry = gas_entry(**{CONF_SOURCES: {"offtake_single": "sensor.gas"}})
    hass.states.async_set(
        "sensor.gas", "4821.7", {ATTR_UNIT_OF_MEASUREMENT: UnitOfVolume.CUBIC_METERS}
    )
    await setup_entry(hass, entry)
    await press_entity(hass, GAS_BUTTON)

    posted = submissions(aioclient_mock)[0]
    assert len(posted) == 1
    assert posted[0]["utilityType"] == "Gas"
    assert posted[0]["meterTimeFrameType"] == "TH"
    assert posted[0]["readingValue"] == 4822


async def test_gas_units_are_converted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A sensor reporting litres is converted to whole cubic metres."""
    mock_gas(aioclient_mock)
    entry = gas_entry(**{CONF_SOURCES: {"offtake_single": "sensor.gas"}})
    hass.states.async_set(
        "sensor.gas", "4821700", {ATTR_UNIT_OF_MEASUREMENT: UnitOfVolume.LITERS}
    )
    await setup_entry(hass, entry)
    await press_entity(hass, GAS_BUTTON)

    assert submissions(aioclient_mock)[0][0]["readingValue"] == 4822


async def test_an_energy_sensor_is_refused_for_gas(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """KWh is not a volume, so it must not be sent as one."""
    mock_gas(aioclient_mock)
    entry = gas_entry(**{CONF_SOURCES: {"offtake_single": "sensor.gas"}})
    hass.states.async_set("sensor.gas", "4821", {ATTR_UNIT_OF_MEASUREMENT: "kWh"})
    await setup_entry(hass, entry)
    await press_entity(hass, GAS_BUTTON)

    assert submissions(aioclient_mock) == []
    assert state_of(hass, GAS_STATUS).state == "failed"


async def test_a_register_without_a_sensor_is_not_submitted(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A meter can be readable for some registers and not others."""
    set_sources(hass)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            **config_entry.options,
            CONF_SOURCES: {
                "offtake_day": "sensor.import_day",
                "offtake_night": "sensor.import_night",
            },
        },
    )
    await setup_entry(hass, config_entry)
    await press(hass)

    posted = submissions(mock_portal)[0]
    assert {r["meterTimeFrameType"] for r in posted} == {"HI", "LO"}
    assert {r["direction"] for r in posted} == {"OffTake"}
    assert state_of(hass, STATUS).state == "success"


async def test_nothing_mapped_submits_nothing(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A meter still read by hand has no sensor at all, and that is fine."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_SOURCES: {}}
    )
    await setup_entry(hass, config_entry)
    await press(hass)

    assert submissions(mock_portal) == []
    assert state_of(hass, STATUS).state == "nothing_to_submit"
    # Nothing to do is not a failure, so it must not raise a repair.
    assert config_entry.runtime_data.data.failures == 0


async def test_mapped_registers_are_still_all_or_nothing(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Skipping a register you chose would record an incoherent moment."""
    set_sources(hass)
    hass.states.async_set("sensor.export_day", "unavailable", {})
    await setup_entry(hass, config_entry)
    await press(hass)

    assert submissions(mock_portal) == []
    assert state_of(hass, STATUS).state == "failed"

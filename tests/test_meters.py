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

"""Tests for the meters of a login: adding them, sharing a client, removal."""

import asyncio
from collections.abc import Callable
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.eneco_be.const import (
    CONF_EAN,
    CONF_ENERGY_TYPE,
    CONF_REGISTERS,
    CONF_SOURCES,
    CONF_SUBMIT_TIME,
    DOMAIN,
    SUBENTRY_METER,
)
from custom_components.eneco_be.coordinator import REPAIR_AFTER_FAILURES
from homeassistant.components.time.const import DOMAIN as TIME_DOMAIN, SERVICE_SET_VALUE
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_UNIT_OF_MEASUREMENT, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util

from .conftest import (
    ACCOUNT,
    API,
    EAN,
    METER_ID,
    coordinator_of,
    login_entry,
    meter_data,
    mock_discovery,
    mock_login,
    update_meter,
)
from .test_config_flow import logins, setup
from .test_init import SUBMIT_TIME, set_sources, state_of

GAS_EAN = "541449000000000009"
GAS_ID = "01JGASSUBENTRY00000000000"
OTHER_EAN = "541449000000000002"


def mock_address(mocker: AiohttpClientMocker, points: list[dict[str, str]]) -> None:
    """Register a portal whose one address has the given meters.

    The register lookup answers for the gas meter, which is the one the
    tests add.
    """
    mock_login(
        mocker,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(
        mocker,
        points=points,
        registers=[{"direction": "OffTake", "meterTimeFrameType": "TH"}],
        ean=GAS_EAN,
        energy_type="Gas",
    )
    mocker.post(f"{API}/accounts/{ACCOUNT}/meter-readings", json={"data": None})


ELECTRICITY_AND_GAS = [
    {"ean": EAN, "type": "Electricity"},
    {"ean": GAS_EAN, "type": "Gas"},
]


def two_meter_entry() -> MockConfigEntry:
    """Return a login with an electricity and a gas meter."""
    entry = login_entry()
    gas = meter_data(
        **{
            CONF_EAN: GAS_EAN,
            CONF_ENERGY_TYPE: "Gas",
            CONF_REGISTERS: ["offtake_single"],
            CONF_SOURCES: {"offtake_single": "sensor.gas"},
        }
    )
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        subentries_data=[
            *(
                {
                    "data": subentry.data,
                    "subentry_id": subentry_id,
                    "subentry_type": subentry.subentry_type,
                    "title": subentry.title,
                    "unique_id": subentry.unique_id,
                }
                for subentry_id, subentry in entry.subentries.items()
            ),
            {
                "data": gas,
                "subentry_id": GAS_ID,
                "subentry_type": SUBENTRY_METER,
                "title": f"Gas {GAS_EAN}",
                "unique_id": GAS_EAN,
            },
        ],
    )


async def add_meter(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Start adding a meter to a login."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_METER), context={"source": SOURCE_USER}
    )


async def test_the_gas_meter_is_added_without_signing_in_again(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The meter already set up is left out, so the gas one is all there is."""
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    await setup(hass, config_entry)
    client = coordinator_of(config_entry).client
    sign_ins = len(logins(aioclient_mock))

    result = await add_meter(hass, config_entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    assert result["description_placeholders"] == {"ean": GAS_EAN}
    # Walked on the client the entry signed in with.
    assert len(logins(aioclient_mock)) == sign_ins
    assert coordinator_of(config_entry).client is client

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"offtake_single": "sensor.gas", CONF_SUBMIT_TIME: "22:40:00"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    meters = {s.unique_id: s for s in config_entry.subentries.values()}
    assert set(meters) == {EAN, GAS_EAN}
    gas = meters[GAS_EAN]
    assert gas.title == f"Gas {GAS_EAN}"
    assert gas.data[CONF_ENERGY_TYPE] == "Gas"
    assert gas.data[CONF_SOURCES] == {"offtake_single": "sensor.gas"}

    # Reloaded with both meters on one client.
    assert config_entry.state is ConfigEntryState.LOADED
    coordinators = config_entry.runtime_data.coordinators
    assert set(coordinators) == set(config_entry.subentries)
    assert len({id(c.client) for c in coordinators.values()}) == 1
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, GAS_EAN)})
    assert device is not None
    assert device.config_entry_id == config_entry.entry_id
    assert device.config_subentry_id == gas.subentry_id
    assert state_of(hass, f"sensor.eneco_{GAS_EAN}_status").state == "pending"


async def test_the_picker_offers_only_meters_not_set_up(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A meter already submitted for is not offered a second time."""
    mock_address(
        aioclient_mock,
        [*ELECTRICITY_AND_GAS, {"ean": OTHER_EAN, "type": "Electricity"}],
    )
    await setup(hass, config_entry)

    result = await add_meter(hass, config_entry)

    assert result["step_id"] == "meter"
    options = result["data_schema"].schema[CONF_EAN].config["options"]
    assert [option["value"] for option in options] == [GAS_EAN, OTHER_EAN]


async def test_an_address_with_every_meter_set_up(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """There is nothing left to add, and the user is told so."""
    mock_address(aioclient_mock, [{"ean": EAN, "type": "Electricity"}])
    await setup(hass, config_entry)

    result = await add_meter(hass, config_entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "meters_configured"


async def test_a_meter_of_another_login_is_not_offered(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two logins reaching one account must not both submit for its meter."""
    mock_address(aioclient_mock, [{"ean": EAN, "type": "Electricity"}])
    taken = login_entry()
    taken.add_to_hass(hass)
    hass.config_entries.async_update_entry(taken, unique_id="other@example.com")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "someone@example.com", "password": "x"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "meters_configured"


async def test_no_meter_is_added_to_a_login_that_is_not_loaded(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """There is no signed in client to walk the portal with."""
    config_entry.add_to_hass(hass)

    result = await add_meter(hass, config_entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    assert logins(aioclient_mock) == []


async def test_every_meter_of_a_login_shares_one_sign_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """One login, one session, however many meters it submits for."""
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    entry = two_meter_entry()
    await setup(hass, entry)

    assert len(logins(aioclient_mock)) == 1
    coordinators = entry.runtime_data.coordinators
    assert set(coordinators) == {METER_ID, GAS_ID}
    assert coordinators[METER_ID].client is coordinators[GAS_ID].client
    for ean in (EAN, GAS_EAN):
        assert state_of(hass, f"sensor.eneco_{ean}_status").state == "pending"


async def test_moving_the_time_does_not_sign_in_again(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The time control writes the subentry, and that alone is no reload."""
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    await setup(hass, config_entry)
    coordinator = coordinator_of(config_entry)

    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: SUBMIT_TIME, "time": "21:10:00"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert len(logins(aioclient_mock)) == 1
    assert coordinator_of(config_entry) is coordinator
    assert config_entry.subentries[METER_ID].data[CONF_SUBMIT_TIME] == "21:10:00"
    assert state_of(hass, SUBMIT_TIME).state == "21:10:00"


def failing_state() -> dict[str, Any]:
    """Return a stored state with enough failures for a repair."""
    return {"error": "boom", "failures": REPAIR_AFTER_FAILURES}


def repair_id(entry: MockConfigEntry, subentry_id: str) -> str:
    """Return the repair id of one meter of an entry."""
    return f"submission_failed_{entry.entry_id}_{subentry_id}"


async def test_removing_a_meter_takes_its_state_and_repair(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The other meter carries on, and nothing is left of the removed one."""
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    entry = two_meter_entry()
    key = f"{DOMAIN}.{entry.entry_id}.meters"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"meters": {METER_ID: failing_state(), GAS_ID: failing_state()}},
    }
    await setup(hass, entry)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, repair_id(entry, GAS_ID)) is not None

    hass.config_entries.async_remove_subentry(entry, GAS_ID)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.runtime_data.coordinators) == {METER_ID}
    assert issues.async_get_issue(DOMAIN, repair_id(entry, GAS_ID)) is None
    assert issues.async_get_issue(DOMAIN, repair_id(entry, METER_ID)) is not None
    assert set(hass_storage[key]["data"]["meters"]) == {METER_ID}
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{GAS_EAN}_status")
        is None
    )


async def test_a_meter_removed_while_not_loaded_leaves_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """No reload sees it go, so the next setup cleans up after it."""
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    entry = two_meter_entry()
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}.meters"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"meters": {METER_ID: failing_state(), GAS_ID: failing_state()}},
    }
    ir.async_create_issue(
        hass,
        DOMAIN,
        repair_id(entry, GAS_ID),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="submission_failed",
    )

    hass.config_entries.async_remove_subentry(entry, GAS_ID)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, repair_id(entry, GAS_ID)) is None
    assert issues.async_get_issue(DOMAIN, repair_id(entry, METER_ID)) is not None
    assert set(hass_storage[key]["data"]["meters"]) == {METER_ID}


async def test_removing_the_login_takes_every_repair(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Every meter's card goes, along with the one file they shared."""
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    entry = two_meter_entry()
    key = f"{DOMAIN}.{entry.entry_id}.meters"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"meters": {METER_ID: failing_state(), GAS_ID: failing_state()}},
    }
    await setup(hass, entry)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not [i for d, i in ir.async_get(hass).issues if d == DOMAIN]
    assert key not in hass_storage


async def test_a_submission_in_flight_does_not_outlive_its_meter(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A meter removed mid submission must not have its state put back."""
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    entry = two_meter_entry()
    await setup(hass, entry)
    gas = entry.runtime_data.coordinators[GAS_ID]
    gas.data.failures = REPAIR_AFTER_FAILURES

    hass.config_entries.async_remove_subentry(entry, GAS_ID)
    await hass.async_block_till_done()
    # The removed meter's coordinator finishing the update it was inside.
    await gas._async_update_data()  # noqa: SLF001
    await hass.async_block_till_done()

    key = f"{DOMAIN}.{entry.entry_id}.meters"
    assert GAS_ID not in hass_storage.get(key, {}).get("data", {}).get("meters", {})
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair_id(entry, GAS_ID)) is None


async def hold_submissions(
    mocker: AiohttpClientMocker,
) -> tuple[asyncio.Event, list[str]]:
    """Make every submission wait for the returned event.

    Returns the event and the EAN of every submission posted.
    """
    release = asyncio.Event()
    posted: list[str] = []

    async def answer(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        posted.append(data[0]["ean"])
        await release.wait()
        return AiohttpClientMockResponse(method, url, json={"data": None})

    mocker.post(f"{API}/accounts/{ACCOUNT}/meter-readings", side_effect=answer)
    return release, posted


async def until(condition: Callable[[], bool]) -> None:
    """Let the loop run until a condition holds, failing after a while.

    Not async_block_till_done: it would wait for the very submission these
    tests hold back. And not a timed sleep, which the frozen clock never ends.
    """
    for _ in range(5000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def reloaded(entry: MockConfigEntry, before: Any) -> Callable[[], bool]:
    """Return a check that the entry is loaded again on new runtime data."""
    return lambda: (
        entry.state is ConfigEntryState.LOADED and entry.runtime_data is not before
    )


async def test_a_reload_mid_submission_does_not_send_the_date_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Changing one meter reloads them all, and a reload is not a restart.

    The daily trigger is not an entry task, so unloading does not wait for
    it. The coordinator set up next restored a state without today in it and
    caught up, posting a date the portal had just taken.
    """
    release, posted = await hold_submissions(aioclient_mock)
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    entry = two_meter_entry()
    set_sources(hass)
    await setup(hass, entry)

    # Started the way the daily trigger starts it, outside the entry's tasks.
    in_flight = asyncio.create_task(coordinator_of(entry).async_refresh())
    await until(lambda: posted == [EAN])
    # Today's moment is behind us for the next setup, and removing the gas
    # meter is what reloads the electricity one.
    before = entry.runtime_data
    update_meter(hass, entry, **{CONF_SUBMIT_TIME: "06:00:00"})
    hass.config_entries.async_remove_subentry(entry, GAS_ID)
    await until(reloaded(entry, before))
    for _ in range(200):
        await asyncio.sleep(0)
    release.set()
    await in_flight
    await hass.async_block_till_done()

    assert posted == [EAN]


async def test_a_reload_mid_submission_keeps_the_other_meters_state(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The coordinator left over from before a reload saved its own copy.

    That copy predated what the other meter recorded after the reload, and
    writing it back took the other meter's submitted date off the disk.
    """
    release, posted = await hold_submissions(aioclient_mock)
    mock_address(aioclient_mock, ELECTRICITY_AND_GAS)
    entry = two_meter_entry()
    set_sources(hass)
    hass.states.async_set(
        "sensor.gas", "12", {ATTR_UNIT_OF_MEASUREMENT: UnitOfVolume.CUBIC_METERS}
    )
    await setup(hass, entry)

    in_flight = asyncio.create_task(coordinator_of(entry).async_refresh())
    await until(lambda: posted == [EAN])
    before = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "password": "changed"}
    )
    await until(reloaded(entry, before))
    gas = asyncio.create_task(entry.runtime_data.coordinators[GAS_ID].async_refresh())
    await until(lambda: posted == [EAN, GAS_EAN])
    release.set()
    await gas
    await in_flight
    await hass.async_block_till_done()

    today = dt_util.now().date().isoformat()
    meters = hass_storage[f"{DOMAIN}.{entry.entry_id}.meters"]["data"]["meters"]
    assert meters[GAS_ID]["submitted_date"] == today
    assert meters[METER_ID]["submitted_date"] == today

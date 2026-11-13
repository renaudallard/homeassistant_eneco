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

"""Tests for folding version 1 entries into one entry per login."""

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

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
from custom_components.eneco_be.migrate import async_migrate_entries
from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import EAN, EMAIL, PASSWORD, REGISTERS, SOURCES, legacy_entry
from .test_config_flow import logins
from .test_init import press, set_sources, state_of, submissions

GAS_EAN = "541449000000000009"


def legacy_gas(**changes: Any) -> MockConfigEntry:
    """Return a version 1 entry for a gas meter."""
    return legacy_entry(
        GAS_EAN,
        energy_type="Gas",
        registers=["offtake_single"],
        sources={"offtake_single": "sensor.gas"},
        submit_time="22:40:00",
        **changes,
    )


def install(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    entry: MockConfigEntry,
    state: dict[str, Any] | None = None,
    entity_id: str | None = None,
) -> str:
    """Add a version 1 entry the way it was left: device, entity, state, repair.

    Returns the entity id of its status sensor, which is ``entity_id`` when
    given, the way a user renaming it would have left it.
    """
    entry.add_to_hass(hass)
    ean = entry.data[CONF_EAN]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, ean)}
    )
    status = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{ean}_status",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=f"eneco_{ean}_status",
    )
    if entity_id is not None:
        status = er.async_get(hass).async_update_entity(
            status.entity_id, new_entity_id=entity_id
        )
    if state is not None:
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
            "version": 1,
            "key": f"{DOMAIN}.{entry.entry_id}",
            "data": state,
        }
        if state.get("failures", 0) >= REPAIR_AFTER_FAILURES:
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"submission_failed_{entry.entry_id}",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="submission_failed",
            )
    return status.entity_id


def meters(entry: ConfigEntry) -> dict[str, Any]:
    """Return the meter subentries of an entry by EAN."""
    return {
        subentry.data[CONF_EAN]: subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_METER
    }


async def test_the_meters_of_one_login_become_one_entry(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Everything a meter had carries over, and the login signs in once."""
    electricity = legacy_entry()
    # The same login, typed differently.
    gas = legacy_gas(email=EMAIL.upper())
    today = dt_util.now().date().isoformat()
    electricity_status = install(
        hass, hass_storage, electricity, {"submitted_date": today}
    )
    # Renamed by the user: setting the meter up afresh would not bring it back.
    gas_status = install(
        hass,
        hass_storage,
        gas,
        {"error": "boom", "failures": REPAIR_AFTER_FAILURES},
        "sensor.gas_meter_status",
    )
    set_sources(hass)

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.entry_id == electricity.entry_id
    assert entry.version == 2
    assert entry.unique_id == EMAIL
    assert entry.title == "s•••@e•••.com"
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    assert entry.options == {}
    assert len(logins(mock_portal)) == 1

    found = meters(entry)
    assert set(found) == {EAN, GAS_EAN}
    assert found[EAN].title == f"Electricity {EAN}"
    assert found[EAN].data[CONF_SOURCES] == SOURCES
    assert found[EAN].data[CONF_REGISTERS] == REGISTERS
    assert found[EAN].data[CONF_SUBMIT_TIME] == "23:50:00"
    assert found[GAS_EAN].title == f"Gas {GAS_EAN}"
    assert found[GAS_EAN].data[CONF_ENERGY_TYPE] == "Gas"
    assert found[GAS_EAN].data[CONF_SOURCES] == {"offtake_single": "sensor.gas"}
    assert found[GAS_EAN].data[CONF_SUBMIT_TIME] == "22:40:00"

    # The entities kept their ids and moved over with their devices.
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    for entity_id, ean in ((electricity_status, EAN), (gas_status, GAS_EAN)):
        moved = entities.async_get(entity_id)
        assert moved is not None
        assert moved.config_entry_id == entry.entry_id
        assert moved.config_subentry_id == found[ean].subentry_id
        device = devices.async_get_device(identifiers={(DOMAIN, ean)})
        assert device is not None
        assert device.config_entry_id == entry.entry_id
        assert device.config_subentry_id == found[ean].subentry_id
        assert state_of(hass, entity_id).state != "unavailable"

    # The stored state went with them, and the old files are gone.
    assert f"{DOMAIN}.{electricity.entry_id}" not in hass_storage
    assert f"{DOMAIN}.{gas.entry_id}" not in hass_storage
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.meters"]["data"]["meters"]
    assert stored[found[EAN].subentry_id]["submitted_date"] == today
    assert stored[found[GAS_EAN].subentry_id]["failures"] == REPAIR_AFTER_FAILURES

    # The gas card is back under the id of its meter, and only there.
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"submission_failed_{gas.entry_id}") is None
    assert (
        issues.async_get_issue(
            DOMAIN,
            f"submission_failed_{entry.entry_id}_{found[GAS_EAN].subentry_id}",
        )
        is not None
    )

    # Today was already submitted for electricity, and that is remembered.
    await press(hass)
    assert submissions(mock_portal) == []
    assert state_of(hass, electricity_status).state == "already_submitted"


async def test_two_logins_stay_two_entries(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Only the meters of the same login are folded together."""
    install(hass, hass_storage, legacy_entry())
    install(hass, hass_storage, legacy_gas(email="other@example.com"))

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    entries = {e.unique_id: e for e in hass.config_entries.async_entries(DOMAIN)}
    assert set(entries) == {EMAIL, "other@example.com"}
    assert set(meters(entries[EMAIL])) == {EAN}
    assert set(meters(entries["other@example.com"])) == {GAS_EAN}


async def test_a_disabled_meter_does_not_start_submitting(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """It stays an entry of its own, disabled, and the login stays unique."""
    enabled = legacy_entry()
    disabled = legacy_gas(disabled_by=ConfigEntryDisabler.USER)
    # Disabled first, so the order the entries were stored in cannot decide.
    install(hass, hass_storage, disabled)
    install(hass, hass_storage, enabled)

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    entries = {e.entry_id: e for e in hass.config_entries.async_entries(DOMAIN)}
    assert set(entries) == {enabled.entry_id, disabled.entry_id}
    assert entries[enabled.entry_id].unique_id == EMAIL
    assert set(meters(entries[enabled.entry_id])) == {EAN}
    assert entries[disabled.entry_id].unique_id is None
    assert entries[disabled.entry_id].disabled_by is ConfigEntryDisabler.USER
    assert entries[disabled.entry_id].version == 2
    assert set(meters(entries[disabled.entry_id])) == {GAS_EAN}


async def test_a_restored_entry_joins_the_login_already_migrated(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A version 1 entry next to a migrated login becomes one of its meters."""
    install(hass, hass_storage, legacy_entry(), {"failures": 2})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert await hass.config_entries.async_unload(entry.entry_id)

    install(hass, hass_storage, legacy_gas(), {"failures": 1})
    await async_migrate_entries(hass)

    (merged,) = hass.config_entries.async_entries(DOMAIN)
    assert merged.entry_id == entry.entry_id
    assert set(meters(merged)) == {EAN, GAS_EAN}
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.meters"]["data"]["meters"]
    assert stored[meters(merged)[GAS_EAN].subentry_id]["failures"] == 1
    # What the login already knew about its first meter is kept.
    assert stored[meters(merged)[EAN].subentry_id]["failures"] == 2

    # And the store the entry loads from again has the new meter too.
    assert await hass.config_entries.async_setup(merged.entry_id)
    await hass.async_block_till_done()
    gas = merged.runtime_data.coordinators[meters(merged)[GAS_EAN].subentry_id]
    assert gas.data.failures == 1


async def test_migrating_twice_changes_nothing(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Every start runs the migration, and a migrated login is left alone."""
    install(hass, hass_storage, legacy_entry())
    install(hass, hass_storage, legacy_gas())
    await async_migrate_entries(hass)
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    before = (dict(entry.data), dict(entry.subentries), entry.unique_id)

    await async_migrate_entries(hass)

    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert (dict(entry.data), dict(entry.subentries), entry.unique_id) == before


async def test_the_registry_is_moved_by_the_migration_itself(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Not left to come back when the platforms set up again.

    Home Assistant does put a removed entity back when the same unique id is
    added later, but only once something adds it. A meter whose setup fails
    would have its device and entities gone until then.
    """
    install(hass, hass_storage, legacy_entry())
    gas = legacy_gas()
    status = install(hass, hass_storage, gas, entity_id="sensor.gas_meter_status")

    await async_migrate_entries(hass)

    (entry,) = hass.config_entries.async_entries(DOMAIN)
    subentry_id = meters(entry)[GAS_EAN].subentry_id
    moved = er.async_get(hass).async_get(status)
    assert moved is not None
    assert moved.config_entry_id == entry.entry_id
    assert moved.config_subentry_id == subentry_id
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, GAS_EAN)})
    assert device is not None
    assert device.config_entry_id == entry.entry_id
    assert device.config_subentry_id == subentry_id


async def test_a_meter_keeps_the_id_of_the_entry_it_was(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A migration cut short has to land on the same ids the second time.

    The state is written under the meter's subentry id straight away and the
    old file removed, while the entries themselves are saved a moment later.
    Lose power in between and the next start migrates again: fresh random ids
    would find nothing under them, and the dates already submitted with it.
    """
    electricity = legacy_entry()
    gas = legacy_gas()
    install(hass, hass_storage, electricity)
    install(hass, hass_storage, gas)

    await async_migrate_entries(hass)

    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert meters(entry)[EAN].subentry_id == electricity.entry_id
    assert meters(entry)[GAS_EAN].subentry_id == gas.entry_id

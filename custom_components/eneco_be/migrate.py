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

"""Fold the entries of one login into a single entry with a subentry per meter.

Up to version 1 every meter was an entry of its own, signed in on its own.
From version 2 an entry is a login and its meters are subentries, sharing one
client. This has to see every entry of a login at once, so it runs from
async_setup, before any of them is set up.
"""

from collections.abc import Mapping
import logging
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ACCOUNT,
    CONF_ADDRESS,
    CONF_EAN,
    CONF_ENERGY_TYPE,
    CONF_REGISTERS,
    CONF_SOURCES,
    CONF_SUBMIT_TIME,
    DOMAIN,
    ELECTRICITY,
    SUBENTRY_METER,
)
from .coordinator import STORAGE_VERSION, MeterStore

_LOGGER = logging.getLogger(__name__)

VERSION = 2


def login_id(email: str) -> str:
    """Return the unique id of the entry for a login."""
    return email.strip().lower()


def login_title(email: str) -> str:
    """Return the title of the entry for a login, without the address in it.

    Home Assistant writes entry titles into its own log lines, the ones users
    paste into bug reports. Enough is kept to tell two logins apart: the
    first letter of the mailbox and of the domain, and the top level domain.
    """

    def mask(part: str) -> str:
        return f"{part[:1]}•••"

    mailbox, _, domain = email.strip().partition("@")
    name, dot, top = domain.rpartition(".")
    if not domain:
        return mask(mailbox)
    if not dot:
        return f"{mask(mailbox)}@{mask(domain)}"
    return f"{mask(mailbox)}@{mask(name)}.{top}"


def meter_title(energy_type: str, ean: str) -> str:
    """Return the title a meter subentry is listed under."""
    return f"{energy_type} {ean}"


def _meter_data(entry: ConfigEntry) -> dict[str, Any]:
    """Return what a version 1 entry knew about its meter."""
    data: dict[str, Any] = {
        CONF_ACCOUNT: entry.data[CONF_ACCOUNT],
        CONF_ADDRESS: entry.data[CONF_ADDRESS],
        CONF_EAN: entry.data[CONF_EAN],
        # Entries written before gas support have no energy type, and every
        # one of those is an electricity meter.
        CONF_ENERGY_TYPE: entry.data.get(CONF_ENERGY_TYPE, ELECTRICITY),
        CONF_REGISTERS: list(entry.data.get(CONF_REGISTERS, [])),
        CONF_SOURCES: dict(entry.options.get(CONF_SOURCES, {})),
    }
    if CONF_SUBMIT_TIME in entry.options:
        data[CONF_SUBMIT_TIME] = entry.options[CONF_SUBMIT_TIME]
    return data


def _old_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    """Return the store a version 1 entry kept its state in."""
    return Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")


async def async_migrate_entries(hass: HomeAssistant) -> None:
    """Turn every version 1 entry into a meter of the entry for its login.

    Enabled and disabled entries are folded separately: a meter that was
    switched off must not start submitting because another meter of the same
    login was not. Only one entry can hold the login as its unique id, and
    the enabled one is seen first, so a disabled one left over goes without.
    """
    # Enabled entries first, so they are the ones whose login wins.
    entries = sorted(
        hass.config_entries.async_entries(DOMAIN),
        key=lambda entry: entry.disabled_by is not None,
    )
    if not any(entry.version < VERSION for entry in entries):
        return

    parents: dict[tuple[str, bool], ConfigEntry] = {
        (login_id(entry.data[CONF_EMAIL]), entry.disabled_by is not None): entry
        for entry in entries
        if entry.version == VERSION
    }
    states: dict[str, dict[str, dict[str, Any]]] = {}
    old_stores: list[Store[dict[str, Any]]] = []
    removed: list[str] = []

    for entry in entries:
        if entry.version >= VERSION:
            continue
        group = (login_id(entry.data[CONF_EMAIL]), entry.disabled_by is not None)
        parent = parents.setdefault(group, entry)
        meter = _meter_data(entry)
        subentry_id = _async_add_meter(hass, parent, meter, entry.entry_id)
        _async_move_registries(hass, entry, parent, subentry_id)

        old_store = _old_store(hass, entry)
        stored = await old_store.async_load()
        if isinstance(stored, dict):
            states.setdefault(parent.entry_id, {})[subentry_id] = stored
        old_stores.append(old_store)
        # Raised again from the stored failure count once the meter loads.
        ir.async_delete_issue(hass, DOMAIN, f"submission_failed_{entry.entry_id}")

        if parent is entry:
            login = login_id(entry.data[CONF_EMAIL])
            taken = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, login)
            unique_id = None if taken else login
            hass.config_entries.async_update_entry(
                entry,
                title=login_title(entry.data[CONF_EMAIL]),
                unique_id=unique_id,
                data={
                    CONF_EMAIL: entry.data[CONF_EMAIL],
                    CONF_PASSWORD: entry.data[CONF_PASSWORD],
                },
                options={},
                version=VERSION,
            )
        else:
            removed.append(entry.entry_id)
        _LOGGER.debug("Migrated entry %s into %s", entry.entry_id, parent.entry_id)

    for entry_id, meters in states.items():
        store = MeterStore.for_entry(hass, entry_id)
        parent_entry = hass.config_entries.async_get_known_entry(entry_id)
        await store.async_load(parent_entry.subentries)
        for subentry_id, state in meters.items():
            await store.async_save(subentry_id, state)
    for old_store in old_stores:
        await old_store.async_remove()
    for entry_id in removed:
        await hass.config_entries.async_remove(entry_id)


def _async_add_meter(
    hass: HomeAssistant, parent: ConfigEntry, meter: Mapping[str, Any], old_id: str
) -> str:
    """Add a meter to an entry, unless it holds that meter already.

    The meter takes the id of the entry it was. Its state is written under
    that id at once and the old file removed, while the entries are only
    saved a moment later, so a migration cut short in between runs again on
    the next start and has to arrive at the same ids to find that state.
    """
    for subentry_id, subentry in parent.subentries.items():
        if subentry.unique_id == meter[CONF_EAN]:
            return subentry_id
    subentry = ConfigSubentry(
        data=MappingProxyType(dict(meter)),
        subentry_id=old_id,
        subentry_type=SUBENTRY_METER,
        title=meter_title(meter[CONF_ENERGY_TYPE], meter[CONF_EAN]),
        unique_id=meter[CONF_EAN],
    )
    hass.config_entries.async_add_subentry(parent, subentry)
    return subentry.subentry_id


def _async_move_registries(
    hass: HomeAssistant, entry: ConfigEntry, parent: ConfigEntry, subentry_id: str
) -> None:
    """Hand the device and entities of an entry over to a meter of the parent.

    Their unique ids are built from the EAN, which does not change, so entity
    ids, history and dashboards carry on as they were.
    """
    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        entity_registry.async_update_entity(
            entity.entity_id,
            config_entry_id=parent.entry_id,
            config_subentry_id=subentry_id,
        )
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        device_registry.async_update_device(
            device.id,
            new_config_entry_id=parent.entry_id,
            new_config_subentry_id=subentry_id,
        )

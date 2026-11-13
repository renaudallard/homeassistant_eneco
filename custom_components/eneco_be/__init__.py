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

"""Submit electricity and gas meter readings to the Eneco Belgium portal."""

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .api import EnecoAuthError, EnecoClient, EnecoConnectionError
from .const import DOMAIN, SUBENTRY_METER
from .coordinator import EnecoCoordinator, MeterStore, repair_prefix
from .migrate import async_migrate_entries

_LOGGER = logging.getLogger(__name__)

# There is no YAML form of this integration; entries come from the config flow.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR, Platform.TIME]


@dataclass(slots=True)
class EnecoData:
    """What a loaded entry runs on: one signed in client for all its meters."""

    client: EnecoClient
    coordinators: dict[str, EnecoCoordinator]
    # The credentials the client signed in with, to tell a change of those,
    # which needs a new sign in, from a change to one meter, which does not.
    credentials: dict[str, Any]


type EnecoConfigEntry = ConfigEntry[EnecoData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Fold entries from before subentries into one entry per login."""
    await async_migrate_entries(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Refuse an entry that async_setup did not migrate.

    Every entry is migrated before any of them is set up, since merging the
    meters of a login needs all its entries at once. One still at an earlier
    version here was not, and is left alone rather than set up half done.
    """
    _LOGGER.error("Entry %s was not migrated", entry.entry_id)
    return False


def _meter_ids(entry: ConfigEntry) -> list[str]:
    """Return the subentry ids of every meter of an entry."""
    return [
        subentry_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_METER
    ]


async def async_setup_entry(hass: HomeAssistant, entry: EnecoConfigEntry) -> bool:
    """Sign in to the portal once and arm the daily submission of every meter."""
    # A private session: the login lives entirely in NextAuth and Okta cookies,
    # which have no business in the session shared by every integration.
    session = async_create_clientsession(hass)
    client = EnecoClient(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])

    try:
        await client.async_login()
    except EnecoAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except EnecoConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    meters = _meter_ids(entry)
    store = MeterStore.for_entry(hass, entry.entry_id)
    await store.async_load(meters)

    # A meter removed while the entry was not loaded leaves its card behind,
    # and nothing would ever submit for it again to take it down.
    prefix = repair_prefix(entry.entry_id)
    wanted = {f"{prefix}{subentry_id}" for subentry_id in meters}
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id not in wanted:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    coordinators = {
        subentry_id: EnecoCoordinator(hass, entry, subentry_id, client, store)
        for subentry_id in meters
    }
    for coordinator in coordinators.values():
        coordinator.async_review_repair()
    entry.runtime_data = EnecoData(client, coordinators, dict(entry.data))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    for coordinator in coordinators.values():
        coordinator.async_schedule()
        entry.async_on_unload(coordinator.async_cancel_schedule)
        # Not before Home Assistant is running: a catch-up reads the source
        # sensors, and during setup the integrations that own them are usually
        # still loading.
        entry.async_on_unload(async_at_started(hass, coordinator.async_catch_up))
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: EnecoConfigEntry) -> None:
    """Reload for new credentials or meters, and pass any other change on.

    Changing the sources or the time of a meter only needs its trigger armed
    again. Reloading for that would sign in anew each time, and the submit
    time control writes the subentry on every change.
    """
    data = entry.runtime_data
    if dict(entry.data) != data.credentials or set(_meter_ids(entry)) != set(
        data.coordinators
    ):
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    for subentry_id, coordinator in data.coordinators.items():
        meter = entry.subentries[subentry_id].data
        if meter != coordinator.meter:
            coordinator.async_update_meter(meter)


async def async_unload_entry(hass: HomeAssistant, entry: EnecoConfigEntry) -> bool:
    """Unload the entry and everything registered with it.

    The repairs go with it: nothing but a submission takes one down, and an
    unloaded entry makes none. Setting the entry up again puts them straight
    back from the stored failure counts, so only being disabled or removed
    actually leaves them down.
    """
    for coordinator in entry.runtime_data.coordinators.values():
        coordinator.async_clear_repair()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EnecoConfigEntry) -> None:
    """Drop the stored submission history along with the entry."""
    # Unloading normally takes the cards down first, but an entry removed
    # while it was never loaded has them too, and they are persistent.
    prefix = repair_prefix(entry.entry_id)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    await MeterStore.async_remove_for(hass, entry.entry_id)

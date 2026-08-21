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

"""Submit electricity meter readings to the Eneco Belgium portal."""

from datetime import datetime, time
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .api import EnecoAuthError, EnecoClient, EnecoConnectionError
from .const import CONF_SUBMIT_TIME, DEFAULT_SUBMIT_TIME, DOMAIN
from .coordinator import EnecoCoordinator

_LOGGER = logging.getLogger(__name__)

# There is no YAML form of this integration; entries come from the config flow.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

type EnecoConfigEntry = ConfigEntry[EnecoCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EnecoConfigEntry) -> bool:
    """Sign in to the portal and arm the daily submission."""
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

    coordinator = EnecoCoordinator(hass, entry, client)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(_async_schedule_daily(hass, entry, coordinator))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnecoConfigEntry) -> bool:
    """Unload the entry and everything registered with it."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


@callback
def _async_schedule_daily(
    hass: HomeAssistant, entry: EnecoConfigEntry, coordinator: EnecoCoordinator
) -> CALLBACK_TYPE:
    """Submit the readings once a day at the configured local time."""
    at = dt_util.parse_time(
        entry.options.get(CONF_SUBMIT_TIME, DEFAULT_SUBMIT_TIME)
    ) or time.fromisoformat(DEFAULT_SUBMIT_TIME)

    async def _submit(now: datetime) -> None:
        _LOGGER.debug("Daily submission triggered at %s", now)
        await coordinator.async_refresh()

    return async_track_time_change(
        hass, _submit, hour=at.hour, minute=at.minute, second=at.second
    )

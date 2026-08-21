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

"""Collect meter readings from Home Assistant and submit them to Eneco."""

from dataclasses import dataclass, field
from datetime import date, datetime
import logging
from typing import TYPE_CHECKING, override

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import EnecoAuthError, EnecoClient, EnecoError, EnecoValidationError, Register
from .const import CONF_ACCOUNT, CONF_EAN, CONF_SOURCES, DOMAIN

if TYPE_CHECKING:
    from . import EnecoConfigEntry

_LOGGER = logging.getLogger(__name__)

ENERGY_TYPE = "Electricity"


class ReadingUnavailableError(EnecoError):
    """A configured source entity did not offer a usable value."""


@dataclass(slots=True)
class EnecoState:
    """Outcome of the most recent submission attempt."""

    attempted: datetime | None = None
    succeeded: datetime | None = None
    reading_date: date | None = None
    values: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class EnecoCoordinator(DataUpdateCoordinator[EnecoState]):
    """Read the configured sensors and hand their values to the portal.

    Nothing is polled: a refresh means "submit the readings now". Operational
    failures are recorded in the state rather than raised, so the entities that
    report them stay available.
    """

    config_entry: EnecoConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EnecoConfigEntry,
        client: EnecoClient,
    ) -> None:
        """Initialise the coordinator without contacting the portal."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.client = client
        self.data = EnecoState()

    @override
    async def _async_update_data(self) -> EnecoState:
        """Submit today's readings and report what happened."""
        now = dt_util.now()
        reading_date = now.date()
        state = EnecoState(attempted=now, reading_date=reading_date)

        try:
            values = self._collect_values()
        except ReadingUnavailableError as err:
            _LOGGER.warning("Skipping submission: %s", err)
            state.error = str(err)
            return state

        try:
            await self.client.async_submit_readings(
                self.config_entry.data[CONF_ACCOUNT],
                self.config_entry.data[CONF_EAN],
                ENERGY_TYPE,
                reading_date,
                values,
            )
        except EnecoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnecoValidationError as err:
            _LOGGER.warning("Eneco rejected the readings: %s", err)
            state.error = ", ".join(err.codes)
        except EnecoError as err:
            _LOGGER.warning("Could not submit the readings: %s", err)
            state.error = str(err)
        else:
            state.succeeded = now
            state.values = {register.key: value for register, value in values.items()}
            _LOGGER.debug("Submitted %s for %s", state.values, reading_date)

        return state

    def _collect_values(self) -> dict[Register, int]:
        """Return one whole kWh value per configured register.

        A submission is all or nothing: if any source entity cannot be read the
        portal is left alone, because a partial set would record an incoherent
        moment across the registers.
        """
        mapping: dict[str, str] = self.config_entry.options[CONF_SOURCES]
        values: dict[Register, int] = {}
        unusable: list[str] = []

        for key, entity_id in mapping.items():
            value = self._read_kwh(entity_id)
            if value is None:
                unusable.append(entity_id)
                continue
            values[Register.from_key(key)] = value

        if unusable:
            raise ReadingUnavailableError(f"no usable value from {', '.join(unusable)}")
        return values

    def _read_kwh(self, entity_id: str) -> int | None:
        """Return the state of an entity as whole kWh, or None if unusable."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            raw = float(state.state)
        except ValueError:
            return None

        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit == UnitOfEnergy.KILO_WATT_HOUR:
            return round(raw)
        try:
            return round(
                EnergyConverter.convert(raw, unit, UnitOfEnergy.KILO_WATT_HOUR)
            )
        except (HomeAssistantError, TypeError):
            _LOGGER.warning(
                "%s reports %s, which is not an energy unit", entity_id, unit
            )
            return None

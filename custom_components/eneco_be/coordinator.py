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
from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any, override

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import EnecoAuthError, EnecoClient, EnecoError, EnecoValidationError, Register
from .const import (
    CONF_ACCOUNT,
    CONF_EAN,
    CONF_SOURCES,
    CONF_SUBMIT_TIME,
    DEFAULT_SUBMIT_TIME,
    DOMAIN,
)

if TYPE_CHECKING:
    from . import EnecoConfigEntry

_LOGGER = logging.getLogger(__name__)

ENERGY_TYPE = "Electricity"
STORAGE_VERSION = 1


class ReadingUnavailableError(EnecoError):
    """A configured source entity did not offer a usable value."""


@dataclass(slots=True)
class EnecoState:
    """Outcome of the most recent submission attempt."""

    attempted: datetime | None = None
    succeeded: datetime | None = None
    submitted_date: date | None = None
    values: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly form for the store."""
        return {
            "attempted": self.attempted.isoformat() if self.attempted else None,
            "succeeded": self.succeeded.isoformat() if self.succeeded else None,
            "submitted_date": (
                self.submitted_date.isoformat() if self.submitted_date else None
            ),
            "values": self.values,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnecoState:
        """Rebuild a stored state, ignoring anything that no longer parses."""

        def moment(key: str) -> datetime | None:
            raw = data.get(key)
            return dt_util.parse_datetime(raw) if isinstance(raw, str) else None

        raw_date = data.get("submitted_date")
        submitted = dt_util.parse_date(raw_date) if isinstance(raw_date, str) else None
        values = data.get("values")
        return cls(
            attempted=moment("attempted"),
            succeeded=moment("succeeded"),
            submitted_date=submitted,
            values={str(k): int(v) for k, v in values.items()}
            if isinstance(values, dict)
            else {},
            error=data.get("error"),
        )


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
        self._unsub: CALLBACK_TYPE | None = None
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )

    async def async_load(self) -> None:
        """Restore what the last run of Home Assistant knew.

        Without this a restart looks exactly like a fresh install, and the guard
        against submitting a date twice would be gone with it.
        """
        stored = await self._store.async_load()
        if stored:
            self.data = EnecoState.from_dict(stored)
            _LOGGER.debug("Restored %s", self.data)

    @staticmethod
    async def async_remove_store_for(
        hass: HomeAssistant, entry: EnecoConfigEntry
    ) -> None:
        """Forget the stored state of an entry that is being removed."""
        await Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        ).async_remove()

    @property
    def submit_time(self) -> time:
        """Return the local time of day readings are sent."""
        configured = self.config_entry.options.get(
            CONF_SUBMIT_TIME, DEFAULT_SUBMIT_TIME
        )
        return dt_util.parse_time(configured) or time.fromisoformat(DEFAULT_SUBMIT_TIME)

    @property
    def next_submission(self) -> datetime:
        """Return when the next submission is due, in local time."""
        at = self.submit_time
        # The helper works to whole seconds and is inclusive of the moment it is
        # given, so asking from the current second hands back the trigger that
        # has just fired. Start from the next second and the answer is always ahead.
        probe = dt_util.now().replace(microsecond=0) + timedelta(seconds=1)
        return dt_util.find_next_time_expression_time(
            probe, [at.second], [at.minute], [at.hour]
        )

    @callback
    def async_schedule(self) -> None:
        """Arm the daily trigger, replacing any previous one."""
        self.async_cancel_schedule()
        at = self.submit_time

        async def _submit(now: datetime) -> None:
            _LOGGER.debug("Daily submission triggered at %s", now)
            await self.async_refresh()

        self._unsub = async_track_time_change(
            self.hass, _submit, hour=at.hour, minute=at.minute, second=at.second
        )
        _LOGGER.debug("Next submission due %s", self.next_submission)

    @callback
    def async_cancel_schedule(self) -> None:
        """Cancel the daily trigger if one is armed."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def async_set_submit_time(self, value: time) -> None:
        """Change the time of day readings are sent and re-arm the trigger."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            options={
                **self.config_entry.options,
                CONF_SUBMIT_TIME: value.isoformat(),
            },
        )
        self.async_schedule()
        self.async_update_listeners()

    @override
    async def _async_update_data(self) -> EnecoState:
        """Submit, then remember the outcome across restarts."""
        state = await self._async_submit()
        await self._store.async_save(state.as_dict())
        return state

    async def _async_submit(self) -> EnecoState:
        """Submit today's readings and report what happened.

        Never raises: everything that can go wrong is recorded in the state, so
        the entities reporting it stay available and an unexpected exception is
        the only thing that marks the coordinator failed.
        """
        now = dt_util.now()
        today = now.date()
        previous = self.data

        # Carry the last success forward, so one bad night does not erase the
        # timestamp and the values of a submission that did go through.
        state = EnecoState(
            attempted=now,
            succeeded=previous.succeeded,
            submitted_date=previous.submitted_date,
            values=dict(previous.values),
        )

        if previous.submitted_date == today:
            _LOGGER.debug("Readings for %s already submitted, skipping", today)
            return state

        try:
            values = self._collect_values()
        except EnecoError as err:
            _LOGGER.warning("Skipping submission: %s", err)
            state.error = str(err)
            return state

        try:
            await self.client.async_submit_readings(
                self.config_entry.data[CONF_ACCOUNT],
                self.config_entry.data[CONF_EAN],
                ENERGY_TYPE,
                today,
                values,
            )
        except EnecoAuthError as err:
            _LOGGER.warning("Eneco no longer accepts the stored password: %s", err)
            state.error = str(err)
            self.config_entry.async_start_reauth(self.hass)
        except EnecoValidationError as err:
            _LOGGER.warning("Eneco rejected the readings: %s", err)
            state.error = ", ".join(err.codes)
        except EnecoError as err:
            _LOGGER.warning("Could not submit the readings: %s", err)
            state.error = str(err)
        else:
            state.succeeded = now
            state.submitted_date = today
            state.values = {register.key: value for register, value in values.items()}
            _LOGGER.debug("Submitted %s for %s", state.values, today)

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
        except HomeAssistantError, TypeError:
            _LOGGER.warning(
                "%s reports %s, which is not an energy unit", entity_id, unit
            )
            return None

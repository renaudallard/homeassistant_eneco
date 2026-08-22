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
from math import isfinite
from typing import TYPE_CHECKING, Any, override

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import EnecoAuthError, EnecoClient, EnecoError, EnecoValidationError, Register
from .const import (
    CONF_ACCOUNT,
    CONF_EAN,
    CONF_ENERGY_TYPE,
    CONF_SOURCES,
    CONF_SUBMIT_TIME,
    DEFAULT_SUBMIT_TIME,
    DOMAIN,
)

if TYPE_CHECKING:
    from . import EnecoConfigEntry

_LOGGER = logging.getLogger(__name__)

ELECTRICITY = "Electricity"
GAS = "Gas"
STORAGE_VERSION = 1

# The portal keeps a meter index in a signed 32 bit integer.
MAX_INDEX = 2**31 - 1

# How many consecutive failures before the user is told out loud.
REPAIR_AFTER_FAILURES = 3


class ReadingUnavailableError(EnecoError):
    """A configured source entity did not offer a usable value."""


class NothingMappedError(EnecoError):
    """No register has a source entity, so there is nothing to send."""


@dataclass(slots=True)
class EnecoState:
    """Outcome of the most recent submission attempt."""

    attempted: datetime | None = None
    succeeded: datetime | None = None
    submitted_date: date | None = None
    values: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    skipped: bool = False
    nothing_mapped: bool = False
    failures: int = 0

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
            "skipped": self.skipped,
            "nothing_mapped": self.nothing_mapped,
            "failures": self.failures,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnecoState:
        """Rebuild a stored state, ignoring anything that no longer parses."""

        def moment(key: str) -> datetime | None:
            raw = data.get(key)
            return dt_util.parse_datetime(raw) if isinstance(raw, str) else None

        def whole(value: Any) -> int | None:
            """Return an integer, or None for anything that is not one."""
            try:
                return int(value)
            except (TypeError, ValueError):  # fmt: skip
                return None

        raw_date = data.get("submitted_date")
        submitted = dt_util.parse_date(raw_date) if isinstance(raw_date, str) else None
        raw_values = data.get("values")
        values = raw_values if isinstance(raw_values, dict) else {}
        return cls(
            attempted=moment("attempted"),
            succeeded=moment("succeeded"),
            submitted_date=submitted,
            values={
                str(key): reading
                for key, value in values.items()
                if (reading := whole(value)) is not None
            },
            error=data.get("error"),
            skipped=bool(data.get("skipped")),
            nothing_mapped=bool(data.get("nothing_mapped")),
            failures=whole(data.get("failures")) or 0,
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

    @callback
    def async_review_repair(self) -> None:
        """Bring the repair card into line with the restored state."""
        self._async_review_repair(self.data)

    @callback
    def async_clear_repair(self) -> None:
        """Take the repair down with the entry that raised it.

        Only a submission clears it, and an entry that is unloaded makes none.
        Disabling one would otherwise leave the card up for good, naming a
        meter nobody is submitting for any more.
        """
        ir.async_delete_issue(self.hass, DOMAIN, self._repair_id)

    @staticmethod
    async def async_remove_store_for(
        hass: HomeAssistant, entry: EnecoConfigEntry
    ) -> None:
        """Forget the stored state of an entry that is being removed."""
        await Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        ).async_remove()

    @property
    def _repair_id(self) -> str:
        """Return the id of the repair raised for this entry."""
        return f"submission_failed_{self.config_entry.entry_id}"

    @property
    def energy_type(self) -> str:
        """Return the kind of meter this entry submits for."""
        return str(self.config_entry.data.get(CONF_ENERGY_TYPE, ELECTRICITY))

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
        # has just fired. Start from the next second and the answer is always
        # ahead. Add that second in UTC, the way the trigger itself does:
        # adding it to a local time throws the fold away, and inside the hour
        # the autumn clock change repeats that moves the probe an hour back.
        probe = dt_util.as_local(
            dt_util.utcnow().replace(microsecond=0) + timedelta(seconds=1)
        )
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
    def async_catch_up(self, _hass: HomeAssistant) -> None:
        """Submit straight away if today's moment has already gone by.

        A wall clock trigger cannot fire while Home Assistant is down, so
        without this a restart across the configured time costs a whole day.
        The reading is dated today either way, and a date that has already been
        submitted is skipped, so the worst this can do is nothing.

        Called once Home Assistant is running, never during setup: reading a
        source sensor that its own integration has not added yet would spend
        the day's attempt on a value nobody has published. The argument is what
        async_at_started hands its callback; the coordinator has its own hass.
        """
        now = dt_util.now()
        # Whole seconds: the trigger fires for the whole of its own second, and
        # landing here in the middle of it would submit twice over, the second
        # of them reporting the day as already done.
        if now.time().replace(microsecond=0) <= self.submit_time:
            return
        if self.data.submitted_date == now.date():
            return
        _LOGGER.debug("Today's submission was missed, catching up")
        # Not a background task: unloading the entry cancels those, and being
        # cancelled between a reading the portal has taken and the record of
        # it would offer the same date again. A date already submitted cannot
        # be corrected, so unload waits for this instead.
        self.config_entry.async_create_task(
            self.hass, self.async_refresh(), f"{DOMAIN} catch up"
        )

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
        state.failures = self.data.failures + 1 if state.error else 0
        # Unloading waits ten seconds and then carries on without cancelling,
        # and one call can take three quarters of a minute, so this can still
        # be running once the entry is gone. Writing then would put back the
        # store file and the repair that removing it has just taken away.
        if self.hass.config_entries.async_get_entry(self.config_entry.entry_id):
            self._async_review_repair(state)
            await self._store.async_save(state.as_dict())
        return state

    @callback
    def _async_review_repair(self, state: EnecoState) -> None:
        """Raise a repair once failures stop looking like a blip.

        A failed status sensor is easy to never look at, and a household can go
        weeks without noticing that nothing is reaching Eneco.
        """
        if state.failures < REPAIR_AFTER_FAILURES:
            ir.async_delete_issue(self.hass, DOMAIN, self._repair_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._repair_id,
            is_fixable=False,
            # A card that only survives until the next restart is no use here:
            # nothing raises it again until the next submission, and there is
            # exactly one of those a day.
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="submission_failed",
            translation_placeholders={
                "name": self.config_entry.title,
                "count": str(state.failures),
                "error": state.error or "",
            },
        )

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
            state.skipped = True
            return state

        try:
            values = self._collect_values()
        except NothingMappedError:
            _LOGGER.debug("No source entity is mapped, nothing to submit")
            state.nothing_mapped = True
            return state
        except EnecoError as err:
            _LOGGER.warning("Skipping submission: %s", err)
            state.error = str(err)
            return state

        try:
            await self.client.async_submit_readings(
                self.config_entry.data[CONF_ACCOUNT],
                self.config_entry.data[CONF_EAN],
                self.energy_type,
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
        """Return one whole unit value per mapped register.

        A register with no source entity is not part of the submission at all:
        a mechanical meter has no sensor, and a meter can be readable for some
        registers and not others.

        Among the registers that *are* mapped it is all or nothing, because a
        partial set would record an incoherent moment across them and the date
        cannot be submitted again to correct it.
        """
        mapping: dict[str, str] = self.config_entry.options.get(CONF_SOURCES, {})
        mapped = {key: entity for key, entity in mapping.items() if entity}
        if not mapped:
            raise NothingMappedError("no register has a source entity")

        values: dict[Register, int] = {}
        unusable: list[str] = []

        for key, entity_id in mapped.items():
            value = self._read_value(entity_id)
            if value is None:
                unusable.append(entity_id)
                continue
            values[Register.from_key(key)] = value

        if unusable:
            raise ReadingUnavailableError(f"no usable value from {', '.join(unusable)}")
        return values

    def _read_value(self, entity_id: str) -> int | None:
        """Return an entity's state in the meter's own unit, or None.

        Electricity meters are read in whole kWh and gas meters in whole cubic
        metres, which is what the portal stores for each.
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            raw = float(state.state)
        except ValueError:
            return None
        # float() is happy with nan and inf, and round() is not: without this a
        # source stuck on either takes the whole coordinator down with it.
        if not isfinite(raw):
            _LOGGER.warning("%s reports %s, which is not a number", entity_id, raw)
            return None

        # Only a gas meter is read as a volume. The portal also answers Both
        # and Unknown, and the picker offers an energy sensor for either, so
        # asking for cubic metres here would refuse the sensor it just chose.
        gas = self.energy_type == GAS
        wanted = UnitOfVolume.CUBIC_METERS if gas else UnitOfEnergy.KILO_WATT_HOUR
        converter = VolumeConverter if gas else EnergyConverter

        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit == wanted:
            value = round(raw)
        else:
            try:
                value = round(converter.convert(raw, unit, wanted))
            except (HomeAssistantError, TypeError):  # fmt: skip
                _LOGGER.warning(
                    "%s reports %s, which cannot be read as %s", entity_id, unit, wanted
                )
                return None

        # An index is never negative, and the portal keeps it in a 32 bit
        # integer. Beyond that the store cannot hold the number either, and it
        # would fail on the way out after the readings had been accepted.
        if not 0 <= value <= MAX_INDEX:
            _LOGGER.warning(
                "%s reports %s, which is not a meter index", entity_id, value
            )
            return None
        return value

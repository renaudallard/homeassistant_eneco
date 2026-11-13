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

"""Sensors reporting how the last submission went."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnecoConfigEntry
from .const import ATTR_LAST_ERROR, CONF_EAN
from .coordinator import EnecoCoordinator, EnecoState
from .entity import EnecoEntity

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_ALREADY_SUBMITTED = "already_submitted"
STATUS_NOTHING_TO_SUBMIT = "nothing_to_submit"


def _status(state: EnecoState) -> str:
    """Return the outcome of the last attempt as an enum value."""
    if state.attempted is None:
        return STATUS_PENDING
    if state.error:
        return STATUS_FAILED
    if state.nothing_mapped:
        return STATUS_NOTHING_TO_SUBMIT
    if state.skipped:
        return STATUS_ALREADY_SUBMITTED
    return STATUS_SUCCESS


@dataclass(frozen=True, kw_only=True)
class EnecoSensorDescription(SensorEntityDescription):
    """Describes an Eneco sensor and how to read it off the state."""

    value_fn: Callable[[EnecoCoordinator], datetime | str | None]


SENSORS: tuple[EnecoSensorDescription, ...] = (
    EnecoSensorDescription(
        key="last_submission",
        translation_key="last_submission",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.data.succeeded,
    ),
    EnecoSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=[
            STATUS_PENDING,
            STATUS_SUCCESS,
            STATUS_ALREADY_SUBMITTED,
            STATUS_NOTHING_TO_SUBMIT,
            STATUS_FAILED,
        ],
        value_fn=lambda coordinator: _status(coordinator.data),
    ),
    EnecoSensorDescription(
        key="next_submission",
        translation_key="next_submission",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coordinator: coordinator.next_submission,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnecoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors of every meter."""
    for subentry_id, coordinator in entry.runtime_data.coordinators.items():
        async_add_entities(
            (EnecoSensor(coordinator, description) for description in SENSORS),
            config_subentry_id=subentry_id,
        )


class EnecoSensor(EnecoEntity, SensorEntity):
    """Report one aspect of the last submission attempt."""

    _unrecorded_attributes = frozenset({ATTR_LAST_ERROR})

    entity_description: EnecoSensorDescription

    def __init__(
        self, coordinator: EnecoCoordinator, description: EnecoSensorDescription
    ) -> None:
        """Bind the sensor to its description."""
        super().__init__(coordinator)
        self.entity_description = description
        ean = coordinator.meter[CONF_EAN]
        self._attr_unique_id = f"{ean}_{description.key}"

    @property
    @override
    def native_value(self) -> datetime | str | None:
        """Return the value this sensor reports."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose why a submission failed, on the status sensor only."""
        if self.entity_description.key != "status":
            return None
        return {ATTR_LAST_ERROR: self.coordinator.data.error}

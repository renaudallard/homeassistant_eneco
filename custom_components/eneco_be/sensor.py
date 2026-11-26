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

"""Sensors reporting how the last submission went, and the last bill."""

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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnecoConfigEntry
from .bills import BillCoordinator
from .const import ATTR_LAST_ERROR, BILL_DISAGREES, CONF_EAN, LAST_BILL
from .coordinator import EnecoCoordinator, EnecoState
from .entity import BillEntity, EnecoEntity

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_ALREADY_SUBMITTED = "already_submitted"
STATUS_NOTHING_TO_SUBMIT = "nothing_to_submit"

TARIFF_CARD = "tariff_card"
BILL_DIFFERENCE = "bill_difference"


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
    """Set up the sensors of every meter, the bill ones only if asked for."""
    bills = entry.runtime_data.bills
    for subentry_id, coordinator in entry.runtime_data.coordinators.items():
        entities: list[SensorEntity] = [
            EnecoSensor(coordinator, description) for description in SENSORS
        ]
        if bills is not None:
            entities += [
                LastBillSensor(bills, subentry_id, coordinator.meter),
                TariffCardSensor(bills, subentry_id, coordinator.meter),
                BillDifferenceSensor(bills, subentry_id, coordinator.meter),
            ]
        async_add_entities(entities, config_subentry_id=subentry_id)
    if bills is None:
        _remove_bill_sensors(hass, entry)


@callback
def _remove_bill_sensors(hass: HomeAssistant, entry: EnecoConfigEntry) -> None:
    """Take the bill sensors away once reading bills is turned off."""
    registry = er.async_get(hass)
    suffixes = (
        f"_{LAST_BILL}",
        f"_{TARIFF_CARD}",
        f"_{BILL_DIFFERENCE}",
        f"_{BILL_DISAGREES}",
    )
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.domain in ("sensor", "binary_sensor") and item.unique_id.endswith(
            suffixes
        ):
            registry.async_remove(item.entity_id)


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


class BillSensor(BillEntity, SensorEntity):
    """A sensor of what the bill coordinator read for one meter."""


class LastBillSensor(BillSensor):
    """The last full year billed: its total with VAT, and every line."""

    _attr_device_class = SensorDeviceClass.MONETARY
    # ISO 4217, as a monetary sensor wants it, not the € symbol.
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2
    # A year has dozens of lines, the same until the next bill comes.
    _unrecorded_attributes = frozenset({"lines"})

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, LAST_BILL)

    @property
    @override
    def native_value(self) -> float | None:
        """Return what the year cost for the meter, VAT included."""
        year = self._read.get("year")
        return year["total"] if year else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return which bills make the year, its lines, and why not a newer one."""
        year = self._read.get("year") or {}
        return {
            "issued": year.get("issued"),
            "period_start": year.get("period_start"),
            "period_end": year.get("period_end"),
            "balance": year.get("balance"),
            "bills": year.get("bills", []),
            "lines": year.get("lines", []),
            ATTR_LAST_ERROR: self._read.get("error"),
        }


class BillDifferenceSensor(BillSensor):
    """What Eneco billed for the year above what it is priced at.

    The price is the one the electricity or gas price entry checking the
    bill gives the meter's own history over the same days, VAT included.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, BILL_DIFFERENCE)

    @property
    @override
    def native_value(self) -> float | None:
        """Return the year billed less the year priced, or None without both."""
        comparison = self._read.get("comparison") or {}
        difference: float | None = comparison.get("difference")
        return difference

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return what the year is priced at, by which entry, or why not."""
        comparison = self._read.get("comparison") or {}
        return {
            "expected": comparison.get("expected"),
            "covered": comparison.get("covered"),
            "price_entry": comparison.get("price_entry"),
            ATTR_LAST_ERROR: comparison.get("error"),
        }


class TariffCardSensor(BillSensor):
    """The version of the tariff card the meter's contract is billed on."""

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, TARIFF_CARD)

    @property
    @override
    def native_value(self) -> str | None:
        """Return the card version, 012210 for the first issue of 2022-10."""
        card = self._read.get("card")
        return card["version"] if card else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the month the card is for, the product and the card itself."""
        card = self._read.get("card") or {}
        return {
            "card_month": card.get("month"),
            "product": card.get("product"),
            "product_type": card.get("product_type"),
            "urls": card.get("urls", {}),
        }

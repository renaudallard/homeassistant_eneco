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

"""Shared entity base for the Eneco Belgium integration."""

from collections.abc import Mapping
from typing import Any, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WEB_HOST
from .bills import BillCoordinator
from .const import CONF_ADDRESS, CONF_EAN, DOMAIN
from .coordinator import EnecoCoordinator


def meter_device(meter: Mapping[str, Any]) -> DeviceInfo:
    """Return the device that stands for the meter a subentry holds."""
    ean = meter[CONF_EAN]
    return DeviceInfo(
        identifiers={(DOMAIN, ean)},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="Eneco",
        name=f"Eneco {ean}",
        serial_number=ean,
        # No locale prefix: the portal picks one from the visitor's cookie.
        configuration_url=(
            f"{WEB_HOST}/my-eneco/usage/add-meter-readings/"
            f"?ean={ean}&address={meter[CONF_ADDRESS]}"
        ),
    )


def address_id(entry_id: str, address: str) -> str:
    """Return the identifier of the device of a delivery address of a login."""
    return f"{entry_id}_{address}"


def address_device(entry_id: str, address: str, number: int) -> DeviceInfo:
    """Return the device that stands for a delivery address of a login.

    Its advance is paid for every meter there at once. ``number`` tells the
    addresses of one login apart, 0 when it has only one.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, address_id(entry_id, address))},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="Eneco",
        name="Eneco advance" + (f" {number}" if number else ""),
        configuration_url=f"{WEB_HOST}/my-eneco/usage/",
    )


def numbered_addresses(meters: list[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """Return the addresses of the meters, each with the number of its device.

    The number tells the addresses of one login apart, 0 when it has only one.
    """
    addresses = sorted({meter[CONF_ADDRESS] for meter in meters})
    return [
        (address, n + 1 if len(addresses) > 1 else 0)
        for n, address in enumerate(addresses)
    ]


@callback
def remove_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    domains: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> None:
    """Take away the entities of the entry whose unique id ends with a suffix.

    For those an option added, once it is turned off.
    """
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.domain in domains and item.unique_id.endswith(suffixes):
            registry.async_remove(item.entity_id)


class EnecoEntity(CoordinatorEntity[EnecoCoordinator]):
    """An entity describing the meter a subentry submits readings for."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EnecoCoordinator) -> None:
        """Attach the entity to the device that stands for the meter."""
        super().__init__(coordinator)
        self._attr_device_info = meter_device(coordinator.meter)


class BillEntity(CoordinatorEntity[BillCoordinator]):
    """What the bill coordinator read for one meter.

    Available as soon as one daily read went through for the meter, and
    from then on even on a day the portal cannot be reached: the bill does
    not change because it could not be fetched again.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any, key: str
    ) -> None:
        """Bind the entity to one meter."""
        super().__init__(coordinator)
        self._subentry_id = subentry_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{meter[CONF_EAN]}_{key}"
        self._attr_device_info = meter_device(meter)

    @property
    def _read(self) -> dict[str, Any]:
        """Return what was read for the meter."""
        result: dict[str, Any] = self.coordinator.data.get(self._subentry_id, {})
        return result

    @property
    @override
    def available(self) -> bool:
        """Return whether anything was ever read for the meter."""
        return self._subentry_id in self.coordinator.data

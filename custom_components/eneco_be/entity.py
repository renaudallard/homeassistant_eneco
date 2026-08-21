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

from typing import override

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WEB_HOST
from .const import CONF_ADDRESS, CONF_EAN, DOMAIN
from .coordinator import EnecoCoordinator


class EnecoEntity(CoordinatorEntity[EnecoCoordinator]):
    """An entity describing the meter this entry submits readings for."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EnecoCoordinator) -> None:
        """Attach the entity to the device that stands for the meter."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        ean = entry.data[CONF_EAN]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Eneco",
            name=f"Eneco {ean}",
            serial_number=ean,
            # No locale prefix: the portal picks one from the visitor's cookie.
            configuration_url=(
                f"{WEB_HOST}/my-eneco/usage/add-meter-readings/"
                f"?ean={ean}&address={entry.data[CONF_ADDRESS]}"
            ),
        )

    @property
    @override
    def available(self) -> bool:
        """Report availability.

        The coordinator records submission failures in its state instead of
        raising, so these entities stay available to show what went wrong.
        """
        return True

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

"""Button that submits the meter readings straight away."""

from typing import override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnecoConfigEntry
from .const import CONF_EAN
from .coordinator import EnecoCoordinator
from .entity import EnecoEntity

DESCRIPTION = ButtonEntityDescription(key="submit", translation_key="submit")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnecoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the submit button."""
    async_add_entities([EnecoSubmitButton(entry.runtime_data)])


class EnecoSubmitButton(EnecoEntity, ButtonEntity):
    """Submit the configured readings for today."""

    entity_description = DESCRIPTION

    def __init__(self, coordinator: EnecoCoordinator) -> None:
        """Give the button a stable unique id."""
        super().__init__(coordinator)
        ean = coordinator.config_entry.data[CONF_EAN]
        self._attr_unique_id = f"{ean}_{DESCRIPTION.key}"

    @override
    async def async_press(self) -> None:
        """Run a submission now."""
        await self.coordinator.async_refresh()

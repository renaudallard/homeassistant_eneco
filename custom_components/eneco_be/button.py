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

"""Buttons: submit the meter readings now, and set the proposed advances."""

from typing import override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EnecoConfigEntry
from .api import EnecoError
from .bills import BillCoordinator
from .const import CONF_ADDRESS, CONF_ADJUST_ADVANCE, CONF_EAN
from .coordinator import EnecoCoordinator
from .entity import EnecoEntity, address_device, remove_entities

DESCRIPTION = ButtonEntityDescription(key="submit", translation_key="submit")
APPLY_ADVANCE = "apply_advance"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnecoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a submit button for every meter, and the advance button if asked."""
    for subentry_id, coordinator in entry.runtime_data.coordinators.items():
        async_add_entities(
            [EnecoSubmitButton(coordinator)], config_subentry_id=subentry_id
        )
    bills = entry.runtime_data.bills
    if bills is None or not entry.options.get(CONF_ADJUST_ADVANCE):
        remove_entities(hass, entry, ("button",), (f"_{APPLY_ADVANCE}",))
        return
    addresses = sorted(
        {c.meter[CONF_ADDRESS] for c in entry.runtime_data.coordinators.values()}
    )
    async_add_entities(
        ApplyAdvanceButton(
            bills, entry.entry_id, address, n + 1 if len(addresses) > 1 else 0
        )
        for n, address in enumerate(addresses)
    )


class EnecoSubmitButton(EnecoEntity, ButtonEntity):
    """Submit the configured readings for today."""

    entity_description = DESCRIPTION

    def __init__(self, coordinator: EnecoCoordinator) -> None:
        """Give the button a stable unique id."""
        super().__init__(coordinator)
        ean = coordinator.meter[CONF_EAN]
        self._attr_unique_id = f"{ean}_{DESCRIPTION.key}"

    @override
    async def async_press(self) -> None:
        """Run a submission now, and say so when it does not go through.

        The daily run has the status sensor watching it, but someone who asked
        for a submission here and now is owed the answer where they asked for
        it: a button that silently does nothing reads as success, and a script
        pressing it carries on as though the readings had been sent.
        """
        # The coordinator only replaces its data when the update returns, so a
        # refresh that raised leaves the previous attempt's error in place.
        # Reporting that would blame this press for an earlier one's failure.
        before = self.coordinator.data
        await self.coordinator.async_refresh()
        state = self.coordinator.data
        if state is not before and state.error:
            raise HomeAssistantError(state.error)


class ApplyAdvanceButton(CoordinatorEntity[BillCoordinator], ButtonEntity):
    """Set on the portal the advances the proposed advance sensor shows.

    It changes what Eneco debits each month, so it only exists when asked
    for, and only acts when pressed: Eneco is told the new amount of each
    contract that changes, as its own advances page would.
    """

    _attr_has_entity_name = True
    _attr_translation_key = APPLY_ADVANCE

    def __init__(
        self, coordinator: BillCoordinator, entry_id: str, address: str, number: int
    ) -> None:
        """Bind the button to one delivery address of the login."""
        super().__init__(coordinator)
        self._address = address
        self._attr_unique_id = f"{entry_id}_{address}_{APPLY_ADVANCE}"
        self._attr_device_info = address_device(entry_id, address, number)

    @property
    @override
    def available(self) -> bool:
        """Return True: a press reads again first, and says why if that fails.

        Following the last read, one that failed would keep it from being
        pressed for a day, and a script pressing it would pass doing nothing.
        """
        return True

    @override
    async def async_press(self) -> None:
        """Set the proposed advances, and say why when none may be set."""
        try:
            await self.coordinator.async_apply_advance(self._address)
        except EnecoError as err:
            raise HomeAssistantError(str(err)) from err

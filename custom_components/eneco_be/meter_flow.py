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

"""The steps that pick a meter, shared by the first setup and Add meter."""

from collections.abc import Mapping
from datetime import time
import random
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, SubentryFlowResult
from homeassistant.data_entry_flow import FlowHandler
from homeassistant.helpers import selector

from .api import Account, Address, EnecoClient, EnecoError, Meter, ServiceDeliveryPoint
from .const import (
    CONF_ACCOUNT,
    CONF_ADDRESS,
    CONF_EAN,
    CONF_ENERGY_TYPE,
    CONF_REGISTERS,
    CONF_SOURCES,
    CONF_SUBMIT_TIME,
    DOMAIN,
    ELECTRICITY,
    GAS,
    SUGGEST_SUBMIT_FROM,
    SUGGEST_SUBMIT_TO,
)

METERED = (ELECTRICITY, GAS)

# Which sensors are worth offering for each kind of meter.
SOURCE_DEVICE_CLASS = {ELECTRICITY: "energy", GAS: "gas"}


def suggested_submit_time() -> str:
    """Return the time of day to offer a meter that has not chosen one.

    Every meter this integration reads is submitted to the same portal, and a
    single default would have all of them arrive there in the same second. The
    suggestion is drawn from a window of whole minutes instead. It is only a
    suggestion: the field is there to be changed.
    """
    first = time.fromisoformat(SUGGEST_SUBMIT_FROM)
    last = time.fromisoformat(SUGGEST_SUBMIT_TO)
    minute = random.randint(
        first.hour * 60 + first.minute, last.hour * 60 + last.minute
    )
    return time(minute // 60, minute % 60).isoformat()


def sources_schema(
    registers: list[str], defaults: Mapping[str, Any], energy_type: str
) -> vol.Schema:
    """Build the schema that maps registers to source entities.

    Every register is optional: a meter can be read by hand for some registers
    and not others, and a mechanical meter has no sensor at all.
    """
    sources: Mapping[str, str] = defaults.get(CONF_SOURCES, {})
    fields: dict[Any, Any] = {
        vol.Optional(
            key, description={"suggested_value": sources.get(key)}
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                filter=selector.EntityWithDeviceFilterSelectorConfig(
                    domain="sensor",
                    device_class=SOURCE_DEVICE_CLASS.get(energy_type, "energy"),
                )
            )
        )
        for key in registers
    }
    fields[
        vol.Required(
            CONF_SUBMIT_TIME,
            # Only a meter with no time of its own gets a fresh suggestion;
            # changing the sources keeps the one in use.
            default=defaults.get(CONF_SUBMIT_TIME) or suggested_submit_time(),
        )
    ] = selector.TimeSelector()
    return vol.Schema(fields)


class MeterSteps[ResultT: (ConfigFlowResult, SubentryFlowResult)](
    FlowHandler[Any, ResultT, Any]
):
    """Walk a signed in client from its accounts down to one meter.

    The flow using this sets ``_client`` and ``_accounts`` and then hands over
    to the account step. What to do with the meter once its sources are known
    is up to it.
    """

    _client: EnecoClient | None = None
    _accounts: list[Account]
    _addresses: list[Address]
    _points: list[ServiceDeliveryPoint]
    _account: str | None = None
    _address: str | None = None
    _meter: Meter | None = None
    # The meter as it was, when its sources are being changed.
    _current: Mapping[str, Any] | None = None

    async def _async_meter_done(self, meter: dict[str, Any]) -> ResultT:
        """Finish the flow with the meter the user set up."""
        raise NotImplementedError

    def _meter_is_configured(self, ean: str) -> bool:
        """Return whether any login submits for this meter already.

        Two logins can reach the same account, and the second one to submit
        for a date would only ever be told it was already done.
        """
        return any(
            subentry.unique_id == ean
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for subentry in entry.subentries.values()
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ResultT:
        """Pick the account, unless the user only has one."""
        if user_input is not None:
            self._account = user_input[CONF_ACCOUNT]
        elif len(self._accounts) == 1:
            self._account = self._accounts[0].number
        else:
            return self.async_show_form(
                step_id="account",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ACCOUNT): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    selector.SelectOptionDict(
                                        value=account.number,
                                        label=f"{account.alias} ({account.number})",
                                    )
                                    for account in self._accounts
                                ],
                                mode=selector.SelectSelectorMode.LIST,
                            )
                        )
                    }
                ),
            )

        return await self._async_load_addresses()

    async def _async_load_addresses(self) -> ResultT:
        """Fetch the delivery addresses of the chosen account."""
        assert self._client is not None and self._account is not None
        try:
            self._addresses = [
                address
                for address in await self._client.async_get_addresses(self._account)
                if address.has_active_contracts
            ]
        except EnecoError as err:
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"error": str(err)}
            )

        if not self._addresses:
            return self.async_abort(reason="no_addresses")
        return await self.async_step_address()

    async def async_step_address(
        self, user_input: dict[str, Any] | None = None
    ) -> ResultT:
        """Pick the delivery address, unless there is only one."""
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
        elif len(self._addresses) == 1:
            self._address = self._addresses[0].identifier
        else:
            return self.async_show_form(
                step_id="address",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ADDRESS): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    selector.SelectOptionDict(
                                        value=address.identifier, label=address.label
                                    )
                                    for address in self._addresses
                                ],
                                mode=selector.SelectSelectorMode.LIST,
                            )
                        )
                    }
                ),
            )

        return await self._async_load_meters()

    async def _async_load_meters(self) -> ResultT:
        """Fetch the meters at the chosen address that nobody submits for yet.

        Leaving out those already set up means the picker only offers what can
        still be added, and an address with one meter of each kind goes
        straight to the one that is missing.
        """
        assert self._client is not None
        assert self._account is not None and self._address is not None
        try:
            points = [
                point
                for point in await self._client.async_get_delivery_points(
                    self._account, self._address
                )
                if point.energy_type in METERED
            ]
        except EnecoError as err:
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"error": str(err)}
            )

        if not points:
            return self.async_abort(reason="no_meters")
        self._points = [p for p in points if not self._meter_is_configured(p.ean)]
        if not self._points:
            return self.async_abort(reason="meters_configured")
        return await self.async_step_meter()

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ResultT:
        """Pick the meter, unless only one is left to set up."""
        if user_input is not None:
            ean = user_input[CONF_EAN]
        elif len(self._points) == 1:
            ean = self._points[0].ean
        else:
            return self.async_show_form(
                step_id="meter",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_EAN): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    selector.SelectOptionDict(
                                        value=point.ean,
                                        label=f"{point.energy_type} {point.ean}",
                                    )
                                    for point in self._points
                                ],
                                mode=selector.SelectSelectorMode.LIST,
                            )
                        )
                    }
                ),
            )

        return await self._async_load_registers(ean)

    async def _async_load_registers(self, ean: str) -> ResultT:
        """Ask the portal which registers of the meter it takes readings for."""
        assert self._client is not None and self._account is not None
        try:
            self._meter = await self._client.async_get_meter(self._account, ean)
        except EnecoError as err:
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"error": str(err)}
            )

        if not self._meter.registers:
            return self.async_abort(reason="no_registers")
        return await self.async_step_sources()

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ResultT:
        """Map every register of the meter to a Home Assistant sensor."""
        assert self._meter is not None and self._account is not None
        registers = [register.key for register in self._meter.registers]

        if user_input is not None:
            return await self._async_meter_done(
                {
                    CONF_ACCOUNT: self._account,
                    CONF_ADDRESS: self._address,
                    CONF_EAN: self._meter.ean,
                    CONF_ENERGY_TYPE: self._meter.energy_type,
                    CONF_REGISTERS: registers,
                    CONF_SOURCES: {
                        key: user_input[key] for key in registers if user_input.get(key)
                    },
                    CONF_SUBMIT_TIME: user_input[CONF_SUBMIT_TIME],
                }
            )

        return self.async_show_form(
            step_id="sources",
            data_schema=sources_schema(
                registers, self._current or {}, self._meter.energy_type
            ),
            description_placeholders={"ean": self._meter.ean},
        )

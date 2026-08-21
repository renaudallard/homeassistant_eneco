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

"""Config flow for the Eneco Belgium integration."""

from collections.abc import Mapping
import logging
from typing import Any, override

from aiohttp import ClientSession
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    Account,
    Address,
    EnecoAuthError,
    EnecoClient,
    EnecoConnectionError,
    EnecoError,
    Meter,
)
from .const import (
    CONF_ACCOUNT,
    CONF_ADDRESS,
    CONF_EAN,
    CONF_REGISTERS,
    CONF_SOURCES,
    CONF_SUBMIT_TIME,
    DEFAULT_SUBMIT_TIME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ELECTRICITY = "Electricity"


def _new_session(hass: HomeAssistant) -> ClientSession:
    """Return a cookie keeping session this flow is responsible for."""
    return async_create_clientsession(hass, auto_cleanup=False)


CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


def _sources_schema(registers: list[str], defaults: Mapping[str, Any]) -> vol.Schema:
    """Build the schema that maps every register to a source entity."""
    sources: Mapping[str, str] = defaults.get(CONF_SOURCES, {})
    fields: dict[Any, Any] = {
        vol.Required(
            key, description={"suggested_value": sources.get(key)}
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                filter=selector.EntityWithDeviceFilterSelectorConfig(
                    domain="sensor", device_class="energy"
                )
            )
        )
        for key in registers
    }
    fields[
        vol.Required(
            CONF_SUBMIT_TIME,
            default=defaults.get(CONF_SUBMIT_TIME, DEFAULT_SUBMIT_TIME),
        )
    ] = selector.TimeSelector()
    return vol.Schema(fields)


class EnecoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the user from credentials down to a single electricity meter."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing discovered."""
        self._client: EnecoClient | None = None
        self._accounts: list[Account] = []
        self._addresses: list[Address] = []
        self._eans: list[str] = []
        self._account: str | None = None
        self._address: str | None = None
        self._meter: Meter | None = None
        self._credentials: dict[str, Any] = {}
        self._session: ClientSession | None = None

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the portal credentials and sign in."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # A fresh session per attempt: reusing one would leave the previous
            # Okta session cookie in place, and the portal would then happily
            # sign the earlier account back in.
            session = _new_session(self.hass)
            client = EnecoClient(
                session, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            try:
                await client.async_login()
                self._accounts = await client.async_get_accounts()
            except EnecoAuthError as err:
                _LOGGER.debug("Sign in refused: %s", err)
                errors["base"] = "invalid_auth"
                session.detach()
            except EnecoConnectionError as err:
                _LOGGER.debug("Portal unreachable: %s", err)
                errors["base"] = "cannot_connect"
                session.detach()
            except Exception:
                session.detach()
                raise
            else:
                self._replace_session(session)
                self._client = client
                self._credentials = user_input
                return await self.async_step_account()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA,
                {CONF_EMAIL: user_input[CONF_EMAIL]} if user_input else None,
            ),
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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

    async def _async_load_addresses(self) -> ConfigFlowResult:
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
    ) -> ConfigFlowResult:
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

    async def _async_load_meters(self) -> ConfigFlowResult:
        """Fetch the electricity connections at the chosen address."""
        assert self._client is not None
        assert self._account is not None and self._address is not None
        try:
            self._eans = [
                point.ean
                for point in await self._client.async_get_delivery_points(
                    self._account, self._address
                )
                if point.energy_type == ELECTRICITY
            ]
        except EnecoError as err:
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"error": str(err)}
            )

        if not self._eans:
            return self.async_abort(reason="no_electricity_meter")
        return await self.async_step_meter()

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the meter, unless the address has only one."""
        if user_input is not None:
            ean = user_input[CONF_EAN]
        elif len(self._eans) == 1:
            ean = self._eans[0]
        else:
            return self.async_show_form(
                step_id="meter",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_EAN): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=self._eans,
                                mode=selector.SelectSelectorMode.LIST,
                            )
                        )
                    }
                ),
            )

        assert self._client is not None and self._account is not None
        try:
            self._meter = await self._client.async_get_meter(self._account, ean)
        except EnecoError as err:
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"error": str(err)}
            )

        if not self._meter.registers:
            return self.async_abort(reason="no_registers")

        await self.async_set_unique_id(self._meter.ean)
        if self.source == "reconfigure":
            self._abort_if_unique_id_mismatch()
        else:
            self._abort_if_unique_id_configured()
        return await self.async_step_sources()

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Map every register of the meter to a Home Assistant sensor."""
        assert self._meter is not None and self._account is not None
        registers = [register.key for register in self._meter.registers]

        if user_input is not None:
            data = {
                **self._credentials,
                CONF_ACCOUNT: self._account,
                CONF_ADDRESS: self._address,
                CONF_EAN: self._meter.ean,
                CONF_REGISTERS: registers,
            }
            options = {
                CONF_SOURCES: {key: user_input[key] for key in registers},
                CONF_SUBMIT_TIME: user_input[CONF_SUBMIT_TIME],
            }
            if self.source == "reconfigure":
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(), data=data, options=options
                )
            return self.async_create_entry(
                title=f"Eneco {self._meter.ean}", data=data, options=options
            )

        defaults: Mapping[str, Any] = {}
        if self.source == "reconfigure":
            defaults = self._get_reconfigure_entry().options

        return self.async_show_form(
            step_id="sources",
            data_schema=_sources_schema(registers, defaults),
            description_placeholders={"ean": self._meter.ean},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run the whole flow again against the existing entry."""
        return await self.async_step_user(user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle credentials the portal no longer accepts."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and verify it."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            session = _new_session(self.hass)
            client = EnecoClient(
                session, entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            try:
                await client.async_login()
            except EnecoAuthError:
                errors["base"] = "invalid_auth"
            except EnecoConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            finally:
                # Only used to check the password; the reload signs in again.
                session.detach()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    )
                }
            ),
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
            errors=errors,
        )

    @callback
    def _replace_session(self, session: ClientSession) -> None:
        """Adopt a signed in session, releasing any earlier one."""
        if self._session is not None:
            self._session.detach()
        self._session = session

    @callback
    @override
    def async_remove(self) -> None:
        """Release the session when the flow ends, however it ends."""
        if self._session is not None:
            self._session.detach()
            self._session = None

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        """Return the options flow."""
        return EnecoOptionsFlow()


class EnecoOptionsFlow(OptionsFlowWithReload):
    """Change the source entities and the time of day readings are sent."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the register mapping and the submission time."""
        registers: list[str] = self.config_entry.data[CONF_REGISTERS]

        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SOURCES: {key: user_input[key] for key in registers},
                    CONF_SUBMIT_TIME: user_input[CONF_SUBMIT_TIME],
                }
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_sources_schema(registers, self.config_entry.options),
        )

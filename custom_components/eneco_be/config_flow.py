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

"""Config flow for the Eneco Belgium integration.

An entry is a login. Each meter it submits readings for is a subentry: the
first one is picked while setting the login up, and every other one is added
to it afterwards, without signing in again.
"""

from collections.abc import Mapping
import logging
from typing import Any, override

from aiohttp import ClientSession
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EnecoAuthError, EnecoClient, EnecoConnectionError, EnecoError
from .const import (
    CONF_ACCOUNT,
    CONF_ADDRESS,
    CONF_EAN,
    CONF_ENERGY_TYPE,
    DOMAIN,
    SUBENTRY_METER,
)
from .meter_flow import MeterSteps
from .migrate import VERSION, login_id, login_title, meter_title

_LOGGER = logging.getLogger(__name__)


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


class EnecoConfigFlow(MeterSteps[ConfigFlowResult], ConfigFlow, domain=DOMAIN):
    """Sign in, then walk down to the first meter of the login."""

    VERSION = VERSION

    def __init__(self) -> None:
        """Start with nothing discovered."""
        self._accounts = []
        self._credentials: dict[str, Any] = {}
        self._session: ClientSession | None = None

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the portal credentials and sign in."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(login_id(user_input[CONF_EMAIL]))
            if self.source != SOURCE_RECONFIGURE:
                # Before signing in: a login that is set up already is where
                # its other meters go, and there is no point asking Okta.
                self._abort_if_unique_id_configured()
            # A fresh session per attempt: reusing one would leave the previous
            # Okta session cookie in place, and the portal would then happily
            # sign the earlier account back in.
            session = _new_session(self.hass)
            client = EnecoClient(
                session, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            signed_in = False
            try:
                await client.async_login()
                signed_in = True
                self._accounts = await client.async_get_accounts()
            except EnecoAuthError as err:
                # Reading the accounts raises this too, for a dashboard that
                # did not render and for a login that owns none. Neither is the
                # password, and saying it was sends the user round in circles.
                _LOGGER.debug("Sign in refused: %s", err)
                errors["base"] = "no_accounts" if signed_in else "invalid_auth"
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
                if self.source == SOURCE_RECONFIGURE:
                    return self._async_reconfigure_login()
                return await self.async_step_account()

        if user_input is not None:
            known_email = user_input[CONF_EMAIL]
        elif self.source == SOURCE_RECONFIGURE:
            # Asking the user to remember and retype the address the entry
            # already holds serves nobody.
            known_email = self._get_reconfigure_entry().data[CONF_EMAIL]
        else:
            known_email = None

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA,
                {CONF_EMAIL: known_email} if known_email else None,
            ),
            errors=errors,
        )

    @override
    async def _async_meter_done(self, meter: dict[str, Any]) -> ConfigFlowResult:
        """Create the login with the meter just picked as its first."""
        return self.async_create_entry(
            title=login_title(self._credentials[CONF_EMAIL]),
            data=self._credentials,
            subentries=[
                {
                    "data": meter,
                    "subentry_type": SUBENTRY_METER,
                    "title": meter_title(meter[CONF_ENERGY_TYPE], meter[CONF_EAN]),
                    "unique_id": meter[CONF_EAN],
                }
            ],
        )

    def _async_reconfigure_login(self) -> ConfigFlowResult:
        """Store new credentials, if they still reach every meter set up.

        The meters stay as they are, so a login that cannot see the account
        of one of them would leave it failing every night.
        """
        entry = self._get_reconfigure_entry()
        if self.unique_id != entry.unique_id:
            other = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, self.unique_id or ""
            )
            if other is not None:
                return self.async_abort(reason="already_configured")
        reachable = {account.number for account in self._accounts}
        if any(
            subentry.data[CONF_ACCOUNT] not in reachable
            for subentry in entry.subentries.values()
        ):
            return self.async_abort(reason="accounts_missing")
        return self._async_update_and_finish(
            entry,
            unique_id=self.unique_id,
            title=login_title(self._credentials[CONF_EMAIL]),
            data=self._credentials,
        )

    def _async_update_and_finish(
        self, entry: ConfigEntry, **changes: Any
    ) -> ConfigFlowResult:
        """Update the entry and get it running on the change.

        A loaded entry reloads itself from its update listener when its
        credentials change. One that failed to set up has no listener, and
        must be reloaded from here or it stays failed.
        """
        if entry.update_listeners:
            return self.async_update_and_abort(entry, **changes)
        return self.async_update_reload_and_abort(entry, **changes)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the credentials the login signs in with."""
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
                return self._async_update_and_finish(
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

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries a login can have: its meters."""
        return {SUBENTRY_METER: MeterSubentryFlow}


class MeterSubentryFlow(MeterSteps[SubentryFlowResult], ConfigSubentryFlow):
    """Add a meter to a login, or change what one submits and when.

    Both go through the client the loaded entry already signed in with.
    """

    def __init__(self) -> None:
        """Start with nothing discovered."""
        self._accounts = []

    def _signed_in_client(self) -> EnecoClient | None:
        """Return the client of the entry, if it is loaded."""
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return None
        client: EnecoClient = entry.runtime_data.client
        return client

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Read the accounts of the login and walk down to a new meter."""
        self._client = self._signed_in_client()
        if self._client is None:
            return self.async_abort(reason="entry_not_loaded")
        try:
            self._accounts = await self._client.async_get_accounts()
        except EnecoError as err:
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"error": str(err)}
            )
        return await self.async_step_account()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Offer the sources and the time again, for the registers of today.

        The registers are asked for afresh: a meter replaced since it was set
        up can have different ones.
        """
        self._client = self._signed_in_client()
        if self._client is None:
            return self.async_abort(reason="entry_not_loaded")
        meter = self._get_reconfigure_subentry().data
        self._current = meter
        self._account = meter[CONF_ACCOUNT]
        self._address = meter[CONF_ADDRESS]
        return await self._async_load_registers(meter[CONF_EAN])

    @override
    async def _async_meter_done(self, meter: dict[str, Any]) -> SubentryFlowResult:
        """Add the meter, or store its new sources and time."""
        title = meter_title(meter[CONF_ENERGY_TYPE], meter[CONF_EAN])
        if self.source == SOURCE_RECONFIGURE:
            # The entry's update listener takes the change from here: it rearms
            # the trigger without signing in again.
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=title,
                data=meter,
            )
        return self.async_create_entry(
            title=title, data=meter, unique_id=meter[CONF_EAN]
        )

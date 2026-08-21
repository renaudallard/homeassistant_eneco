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

"""Regression tests for defects found by review.

Each test here failed before the fix it guards.
"""

from datetime import date, datetime, time, timedelta
from http import HTTPStatus
from typing import Any

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from multidict import CIMultiDict, CIMultiDictProxy
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.eneco_be.api import (
    EnecoClient,
    EnecoConnectionError,
    EnecoError,
    Register,
)
from custom_components.eneco_be.const import ATTR_LAST_ERROR, CONF_SUBMIT_TIME, DOMAIN
from custom_components.eneco_be.coordinator import EnecoState
from custom_components.eneco_be.diagnostics import async_get_config_entry_diagnostics
from custom_components.eneco_be.sensor import EnecoSensor
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import ACCOUNT, API, EMAIL, PASSWORD, mock_discovery, mock_login
from .test_init import (
    LAST,
    NEXT,
    STATUS,
    press,
    set_sources,
    setup_entry,
    state_of,
    submissions,
)

SESSION_TOKEN = "20111ONE-SHOT-OKTA-TOKEN"


async def test_timeout_becomes_a_client_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A stalled request must not escape as a bare TimeoutError.

    aiohttp raises the builtin TimeoutError, which is an OSError and not an
    aiohttp.ClientError, so it used to sail past every handler in the client.
    """
    aioclient_mock.post("https://login.eneco.be/api/v1/authn", exc=TimeoutError())
    session = aioclient_mock.create_session(hass.loop)
    try:
        with pytest.raises(EnecoConnectionError):
            await EnecoClient(session, EMAIL, PASSWORD).async_login()
    finally:
        await session.close()


async def test_timeout_at_setup_is_retried(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A timeout during setup must leave the entry retrying, not dead.

    SETUP_ERROR is never retried, so the integration would stay broken until the
    user reloaded it by hand.
    """
    aioclient_mock.post("https://login.eneco.be/api/v1/authn", exc=TimeoutError())
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_session_token_never_reaches_the_message(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The one shot Okta token is a query parameter, so it must not be quoted.

    aiohttp puts the full URL into ClientResponseError, which would end up in the
    log and in the entry's failure reason shown by the frontend.
    """
    aioclient_mock.post(
        "https://login.eneco.be/api/v1/authn",
        json={"status": "SUCCESS", "sessionToken": SESSION_TOKEN},
    )
    aioclient_mock.get("https://eneco.be/api/auth/csrf/", json={"csrfToken": "csrf"})
    aioclient_mock.post(
        "https://eneco.be/api/auth/signin/okta/",
        json={"url": "https://login.eneco.be/oauth2/x/v1/authorize?client_id=y"},
    )

    url = "https://login.eneco.be/login/sessionCookieRedirect"

    async def boom(method: str, u: Any, data: Any) -> None:
        raise aiohttp.ClientResponseError(
            request_info=aiohttp.RequestInfo(
                url=u,
                method=method,
                headers=CIMultiDictProxy(CIMultiDict()),
                real_url=u,
            ),
            history=(),
            status=HTTPStatus.BAD_GATEWAY,
            message="Bad Gateway",
        )

    aioclient_mock.get(url, side_effect=boom)

    session = aioclient_mock.create_session(hass.loop)
    try:
        with pytest.raises(EnecoConnectionError) as raised:
            await EnecoClient(session, EMAIL, PASSWORD).async_login()
    finally:
        await session.close()

    assert SESSION_TOKEN not in str(raised.value)
    assert SESSION_TOKEN not in repr(raised.value.__cause__)


async def test_account_number_never_reaches_the_user(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Error text is shown in the UI and kept by the recorder, so keep ids out."""
    aioclient_mock.post(
        f"{API}/accounts/{ACCOUNT}/meter-readings",
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
        json={},
    )
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(aioclient_mock)

    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)

    status = state_of(hass, STATUS)
    assert status.state == "failed"
    assert ACCOUNT not in str(status.attributes["last_error"])


async def test_error_attribute_is_not_recorded(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The free-form error text has no business in the states database."""
    assert ATTR_LAST_ERROR in EnecoSensor._unrecorded_attributes  # noqa: SLF001


async def test_a_failure_keeps_the_last_success(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """One bad night must not erase a submission that did go through."""
    posts: list[int] = []

    async def answer(method: str, url: Any, data: Any) -> Any:
        posts.append(1)
        if len(posts) == 1:
            return AiohttpClientMockResponse(method, url, json={"data": None})
        return AiohttpClientMockResponse(
            method, url, status=HTTPStatus.INTERNAL_SERVER_ERROR, json={}
        )

    aioclient_mock.post(f"{API}/accounts/{ACCOUNT}/meter-readings", side_effect=answer)
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(aioclient_mock)

    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)
    first = state_of(hass, LAST).state
    assert first != "unknown"

    # Force a second attempt for the same day by clearing the guard.
    entry = config_entry
    entry.runtime_data.data.submitted_date = None
    await press(hass)

    assert state_of(hass, STATUS).state == "failed"
    assert state_of(hass, LAST).state == first


async def test_the_same_date_is_not_submitted_twice(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A second run on a date already done is a no op, not a rejected POST."""
    set_sources(hass)
    await setup_entry(hass, config_entry)

    await press(hass)
    assert len(submissions(mock_portal)) == 1
    assert state_of(hass, STATUS).state == "success"

    await press(hass)
    assert len(submissions(mock_portal)) == 1
    assert state_of(hass, STATUS).state == "success"


async def test_next_submission_is_never_in_the_past(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Asked inside the trigger second, the answer must still be ahead."""
    await setup_entry(hass, config_entry)
    coordinator = config_entry.runtime_data

    at = dt_util.now().replace(hour=23, minute=50, second=0, microsecond=278000)
    freezer.move_to(at)
    assert coordinator.next_submission > dt_util.now()

    freezer.move_to(at.replace(microsecond=0))
    assert coordinator.next_submission > dt_util.now()


async def test_the_reading_date_is_local_not_utc(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The date sent is the household's date, which is not the UTC one."""
    brussels = dt_util.get_time_zone("Europe/Brussels")
    assert brussels is not None
    hass.config.time_zone = "Europe/Brussels"
    dt_util.set_default_time_zone(brussels)
    # 23:30 in Brussels is already the next day in neither, but 00:30 local is
    # still the previous day in UTC.
    freezer.move_to(datetime(2026, 8, 21, 22, 30, tzinfo=dt_util.UTC))

    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)

    posted = submissions(mock_portal)[0]
    assert {r["registeredDate"] for r in posted} == {"2026-08-22"}
    assert dt_util.utcnow().date() == date(2026, 8, 21)


async def test_the_trigger_does_not_fire_early(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """One second before the configured time, nothing must have been sent."""
    set_sources(hass)
    await setup_entry(hass, config_entry)

    now = dt_util.now()
    just_before = now.replace(hour=23, minute=49, second=59, microsecond=0)
    if just_before <= now:
        just_before += timedelta(days=1)
    async_fire_time_changed(hass, dt_util.as_utc(just_before))
    await hass.async_block_till_done()

    assert submissions(mock_portal) == []


async def test_diagnostics_survive_an_unloaded_entry(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """runtime_data is gone once unloaded, and the download must still work."""
    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert report["last_attempt"] is None
    assert report["entry"]["data"]["email"] == "**REDACTED**"


def test_the_register_key_round_trip_is_total() -> None:
    """Anything key() accepts, from_key() must read back."""
    for direction in ("OffTake", "Injection"):
        for timeframe in ("TH", "HI", "LO", "EX"):
            register = Register(direction, timeframe)
            assert Register.from_key(register.key) == register

    # An undocumented wire value is refused where it is discovered, rather than
    # being stored in the options and failing every submission afterwards.
    with pytest.raises(EnecoError):
        _ = Register("OffTake", "ZZ").key
    with pytest.raises(EnecoError):
        Register.from_key("offtake_zz")


def test_state_defaults() -> None:
    """A fresh state reports pending, with nothing carried over."""
    state = EnecoState()
    assert state.attempted is None
    assert state.succeeded is None
    assert state.submitted_date is None
    assert state.values == {}
    assert state.error is None


async def test_submit_time_option_is_read_back(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """An unparseable stored time must fall back rather than crash setup."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_SUBMIT_TIME: "nonsense"}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.runtime_data.submit_time == time(23, 50)
    assert state_of(hass, NEXT).state != "unknown"
    assert DOMAIN in hass.config.components

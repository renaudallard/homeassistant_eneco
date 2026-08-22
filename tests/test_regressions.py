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

import ast
from datetime import date, datetime, time, timedelta
from http import HTTPStatus
from pathlib import Path
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
from custom_components.eneco_be.coordinator import REPAIR_AFTER_FAILURES, EnecoState
from custom_components.eneco_be.diagnostics import async_get_config_entry_diagnostics
from custom_components.eneco_be.sensor import EnecoSensor
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_EMAIL,
    CONF_PASSWORD,
    EVENT_HOMEASSISTANT_STARTED,
    UnitOfEnergy,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .conftest import ACCOUNT, API, EAN, EMAIL, PASSWORD, mock_discovery, mock_login
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


@pytest.mark.parametrize("transport", [False, True])
async def test_account_number_never_reaches_the_user(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    transport: bool,
) -> None:
    """Error text is shown in the UI and kept by the recorder, so keep ids out.

    Every REST path carries the CRM account number, and aiohttp quotes the URL
    it was handed in its own message, so a connection that fails rather than
    answers used to hand the number straight to the interface, the store, the
    repair and the diagnostics that take care to redact it.
    """
    url = f"{API}/accounts/{ACCOUNT}/meter-readings"
    if transport:
        aioclient_mock.post(
            url, exc=aiohttp.ConnectionTimeoutError(f"Connection timeout to host {url}")
        )
    else:
        aioclient_mock.post(url, status=HTTPStatus.INTERNAL_SERVER_ERROR, json={})
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
    assert ACCOUNT not in str(status.attributes[ATTR_LAST_ERROR])

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert ACCOUNT not in str(report)

    stored = await config_entry.runtime_data._store.async_load()  # noqa: SLF001
    assert ACCOUNT not in str(stored)


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
    """A second run on a date already done is a no op, and says so."""
    set_sources(hass)
    await setup_entry(hass, config_entry)

    await press(hass)
    assert len(submissions(mock_portal)) == 1
    assert state_of(hass, STATUS).state == "success"

    await press(hass)
    assert len(submissions(mock_portal)) == 1
    # Reporting success would claim something was sent when nothing was.
    assert state_of(hass, STATUS).state == "already_submitted"


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


async def test_state_survives_a_reload(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A restart or a reload must not look like a fresh install."""
    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)

    submitted = state_of(hass, LAST).state
    assert state_of(hass, STATUS).state == "success"

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, LAST).state == submitted
    assert state_of(hass, STATUS).state == "success"


async def test_the_same_date_guard_survives_a_reload(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Restarting on a day already submitted must not post it again."""
    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)
    assert len(submissions(mock_portal)) == 1

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    await press(hass)
    assert len(submissions(mock_portal)) == 1


async def test_stored_state_is_written(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """The outcome of every attempt reaches the store."""
    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)

    stored = hass_storage[f"{DOMAIN}.{config_entry.entry_id}"]["data"]
    assert stored["submitted_date"] == dt_util.now().date().isoformat()
    assert stored["values"]["offtake_day"] == 1235
    assert stored["error"] is None


@pytest.mark.parametrize(
    "stored",
    [
        {},
        {"submitted_date": "not a date", "values": "not a mapping"},
        {"attempted": None, "succeeded": "nonsense", "values": {"offtake_day": 1}},
        # A mapping is not enough: what it maps to has to be a number too.
        {"values": {"offtake_day": "not a number"}},
        {"values": {"offtake_day": None}},
        {"failures": "not a number"},
    ],
)
async def test_unreadable_stored_state_is_ignored(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    stored: dict[str, Any],
) -> None:
    """A store written by another version must not stop the entry loading."""
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "data": stored,
    }
    set_sources(hass)
    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    # Nothing trustworthy was restored, so today is still submittable.
    await press(hass)
    assert len(submissions(mock_portal)) == 1


async def test_removing_the_entry_forgets_the_store(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Nothing of the account should be left behind after a removal."""
    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)
    key = f"{DOMAIN}.{config_entry.entry_id}"
    assert key in hass_storage

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_repair_is_raised_once_failures_persist(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A failed status sensor is easy to miss, a repair card is not."""
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
    issue_id = f"submission_failed_{config_entry.entry_id}"

    for _ in range(REPAIR_AFTER_FAILURES - 1):
        await press(hass)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None

    await press(hass)
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["count"] == str(REPAIR_AFTER_FAILURES)


async def test_the_repair_clears_once_a_submission_lands(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Fixing the cause must take the card away again."""
    failing = True

    async def answer(method: str, url: Any, data: Any) -> Any:
        if failing:
            return AiohttpClientMockResponse(
                method, url, status=HTTPStatus.INTERNAL_SERVER_ERROR, json={}
            )
        return AiohttpClientMockResponse(method, url, json={"data": None})

    aioclient_mock.post(f"{API}/accounts/{ACCOUNT}/meter-readings", side_effect=answer)
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(aioclient_mock)

    set_sources(hass)
    await setup_entry(hass, config_entry)
    issue_id = f"submission_failed_{config_entry.entry_id}"

    for _ in range(REPAIR_AFTER_FAILURES):
        await press(hass)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    failing = False
    await press(hass)

    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    assert state_of(hass, STATUS).state == "success"


async def test_the_catch_up_waits_for_home_assistant_to_start(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A catch-up must not read the sources while they are still loading.

    Setting up an entry is not the moment to read another integration's
    sensors: on the restart this exists for, they are usually not added yet,
    and reading them then spends the day's only attempt on nothing.
    """
    hass.set_state(CoreState.not_running)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_SUBMIT_TIME: "06:00:00"}
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert submissions(mock_portal) == []

    set_sources(hass)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert len(submissions(mock_portal)) == 1
    assert state_of(hass, STATUS).state == "success"


@pytest.mark.parametrize("reported", ["nan", "inf", "-inf", "1e400"])
async def test_a_source_that_is_not_a_number_is_reported(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    reported: str,
) -> None:
    """float() accepts nan and inf where round() does not.

    Letting either through raised out of the coordinator, which took every
    entity of the entry unavailable instead of recording a failed attempt.
    """
    set_sources(hass)
    hass.states.async_set(
        "sensor.import_night",
        reported,
        {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR},
    )
    await setup_entry(hass, config_entry)
    await press(hass)

    assert submissions(mock_portal) == []
    assert state_of(hass, STATUS).state == "failed"
    assert "sensor.import_night" in state_of(hass, STATUS).attributes[ATTR_LAST_ERROR]


async def test_options_open_on_an_entry_without_an_energy_type(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Entries predating gas support have no energy type in their data.

    The coordinator always read it with a default, so such an entry loads and
    submits; only the options form insisted on the key and raised KeyError at
    the user the moment they pressed Configure.
    """
    data = dict(config_entry.data)
    del data["energy_type"]
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data=data)

    set_sources(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM


async def test_a_register_eneco_has_never_shown_us_is_dropped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A tariff code outside the documented enums must not sink the meter.

    Register.key raises for one, and the config flow read the keys of every
    register outside any try, so a single unknown code left the flow raising
    Unknown error with its session still open and the meter unusable.
    """
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(
        aioclient_mock,
        registers=[
            {"direction": "OffTake", "meterTimeFrameType": "HI"},
            {"direction": "OffTake", "meterTimeFrameType": "ZZ"},
        ],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    schema = result["data_schema"]
    assert schema is not None
    assert "offtake_day" in schema.schema
    assert not any(str(key).startswith("offtake_zz") for key in schema.schema)


def test_the_shipped_sources_parse_on_the_previous_python() -> None:
    """Nothing in the integration may need Python 3.14 to be read.

    ruff format writes the PEP 758 form of a multiple exception except clause
    because the target version says 3.14, and it has twice turned a
    parenthesised clause back into one no older parser accepts. Nothing here
    gains anything from the short form, and a file that will not parse is a
    file no older editor, linter or packaging tool can look at, so the clauses
    that ruff would rewrite carry a fmt: skip and this keeps them honest.
    """
    for source in sorted(Path("custom_components/eneco_be").rglob("*.py")):
        ast.parse(source.read_text(encoding="utf-8"), feature_version=(3, 13))


async def test_next_submission_survives_the_autumn_clock_change(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The probe used to lose the fold and land an hour in the past.

    Adding a second to a local time always yields fold zero, so during the
    repeated hour of the autumn change the probe moved backwards in absolute
    time and the helper handed back the moment that had just passed.
    """
    await hass.config.async_set_time_zone("Europe/Brussels")
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_SUBMIT_TIME: "02:30:00"}
    )
    set_sources(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # The second pass through 02:30 on the night the hour repeats.
    freezer.move_to("2026-10-25T01:30:00+00:00")
    assert dt_util.now().fold == 1

    coordinator = config_entry.runtime_data
    assert coordinator.next_submission > dt_util.now()


async def test_setting_up_inside_the_submit_second_reports_success(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The catch-up must leave the trigger's own second to the trigger.

    Arming the schedule inside the configured second fires it right away, and
    the catch-up saw a time that was no longer before the submit time and asked
    for a second refresh. One reading reached Eneco either way, but the second
    pass found the date already done and left that on the status sensor.
    """
    # Half a second into the configured second, so the trigger armed by the
    # setup is already due and the catch-up must not step on it.
    freezer.move_to(
        dt_util.now().replace(hour=23, minute=50, second=0, microsecond=500000)
    )
    set_sources(hass)
    await setup_entry(hass, config_entry)

    assert len(submissions(mock_portal)) == 1
    assert state_of(hass, STATUS).state == "success"


async def test_an_expired_token_is_renewed_even_without_json(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 401 must be acted on whatever the body looks like.

    The status was only handed back once the body had been decoded, so a
    portal answering an expired token with an HTML error page turned into a
    plain connection failure: the token was never dropped, the one retry never
    ran, and nothing ever asked the user to sign in again.
    """
    calls: list[str] = []

    async def answer(method: str, url: Any, data: Any) -> Any:
        calls.append(str(url))
        if len(calls) == 1:
            return AiohttpClientMockResponse(
                method,
                url,
                status=HTTPStatus.UNAUTHORIZED,
                text="<html><body>Unauthorized</body></html>",
            )
        return AiohttpClientMockResponse(method, url, json={"data": None})

    aioclient_mock.post(f"{API}/accounts/{ACCOUNT}/meter-readings", side_effect=answer)
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(aioclient_mock)

    client = EnecoClient(async_get_clientsession(hass), EMAIL, PASSWORD)
    await client.async_login()
    await client.async_submit_readings(
        ACCOUNT,
        EAN,
        "Electricity",
        dt_util.now().date(),
        {Register("OffTake", "HI"): 1},
    )

    assert len(calls) == 2

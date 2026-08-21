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

"""Tests for the setup and the daily submission."""

from datetime import timedelta
from http import HTTPStatus
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.eneco_be.const import CONF_SOURCES, CONF_SUBMIT_TIME, DOMAIN
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.time.const import DOMAIN as TIME_DOMAIN, SERVICE_SET_VALUE
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .conftest import ACCOUNT, API, EAN, SOURCES, mock_discovery, mock_login

BUTTON = "button.eneco_541449000000000001_submit_readings"
STATUS = "sensor.eneco_541449000000000001_status"
LAST = "sensor.eneco_541449000000000001_last_submission"
NEXT = "sensor.eneco_541449000000000001_next_submission"
SUBMIT_TIME = "time.eneco_541449000000000001_submit_at"

READINGS = {
    "sensor.import_day": "1234.6",
    "sensor.import_night": "987.4",
    "sensor.export_day": "55.2",
    "sensor.export_night": "10.9",
}


def set_sources(
    hass: HomeAssistant,
    readings: dict[str, str] | None = None,
    unit: str = UnitOfEnergy.KILO_WATT_HOUR,
) -> None:
    """Give every configured source entity a value."""
    for entity_id, value in (readings or READINGS).items():
        hass.states.async_set(entity_id, value, {ATTR_UNIT_OF_MEASUREMENT: unit})


def submissions(mocker: AiohttpClientMocker) -> list[Any]:
    """Return the bodies posted to the meter readings endpoint."""
    return [
        data
        for method, url, data, _ in mocker.mock_calls
        if method.lower() == "post" and str(url).endswith("/meter-readings")
    ]


async def setup_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Add and set up the entry."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """Return an entity state, failing the test when it does not exist."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state


async def press_entity(hass: HomeAssistant, entity_id: str) -> None:
    """Press a submit button."""
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def press(hass: HomeAssistant) -> None:
    """Press the submit button of the meter used across the tests."""
    await press_entity(hass, BUTTON)


async def test_setup_creates_entities(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A loaded entry exposes the button and both diagnostic sensors."""
    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(BUTTON) is not None
    assert state_of(hass, STATUS).state == "pending"
    assert state_of(hass, LAST).state == "unknown"


async def test_submit_posts_every_register(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Pressing the button posts one whole kWh value per register."""
    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)

    posted = submissions(mock_portal)
    assert len(posted) == 1

    today = dt_util.now().date().isoformat()
    assert sorted(
        posted[0], key=lambda r: (r["direction"], r["meterTimeFrameType"])
    ) == [
        {
            "accountNumber": ACCOUNT,
            "readingValue": 55,
            "ean": EAN,
            "meterTimeFrameType": "HI",
            "registeredDate": today,
            "utilityType": "Electricity",
            "direction": "Injection",
        },
        {
            "accountNumber": ACCOUNT,
            "readingValue": 11,
            "ean": EAN,
            "meterTimeFrameType": "LO",
            "registeredDate": today,
            "utilityType": "Electricity",
            "direction": "Injection",
        },
        {
            "accountNumber": ACCOUNT,
            "readingValue": 1235,
            "ean": EAN,
            "meterTimeFrameType": "HI",
            "registeredDate": today,
            "utilityType": "Electricity",
            "direction": "OffTake",
        },
        {
            "accountNumber": ACCOUNT,
            "readingValue": 987,
            "ean": EAN,
            "meterTimeFrameType": "LO",
            "registeredDate": today,
            "utilityType": "Electricity",
            "direction": "OffTake",
        },
    ]

    assert state_of(hass, STATUS).state == "success"
    assert state_of(hass, LAST).state != "unknown"


async def test_units_are_converted(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A source reporting watt hours is converted to whole kWh."""
    set_sources(
        hass,
        dict.fromkeys(SOURCES.values(), "1500000"),
        unit=UnitOfEnergy.WATT_HOUR,
    )
    await setup_entry(hass, config_entry)
    await press(hass)

    assert {r["readingValue"] for r in submissions(mock_portal)[0]} == {1500}


@pytest.mark.parametrize(
    ("state", "attributes"),
    [
        (STATE_UNAVAILABLE, {}),
        ("not a number", {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR}),
        ("42", {ATTR_UNIT_OF_MEASUREMENT: "°C"}),
    ],
)
async def test_unusable_source_blocks_the_whole_submission(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    state: str,
    attributes: dict[str, Any],
) -> None:
    """Nothing is sent when one register cannot be read."""
    set_sources(hass)
    hass.states.async_set("sensor.import_night", state, attributes)
    await setup_entry(hass, config_entry)
    await press(hass)

    assert submissions(mock_portal) == []
    assert state_of(hass, STATUS).state == "failed"
    assert "sensor.import_night" in state_of(hass, STATUS).attributes["last_error"]


async def test_missing_source_entity(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A source entity that does not exist is reported, not crashed on."""
    set_sources(hass)
    hass.states.async_remove("sensor.export_day")
    await setup_entry(hass, config_entry)
    await press(hass)

    assert submissions(mock_portal) == []
    assert state_of(hass, STATUS).state == "failed"


async def test_portal_rejection_is_reported(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Eneco's validation codes end up on the status sensor."""
    mock_portal.clear_requests()
    mock_login(
        mock_portal,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(mock_portal)
    mock_portal.post(
        f"{API}/accounts/{ACCOUNT}/meter-readings",
        status=HTTPStatus.BAD_REQUEST,
        json={"errors": {"errors": {"Validation": ["MeterReading.AlreadyExists"]}}},
    )

    set_sources(hass)
    await setup_entry(hass, config_entry)
    await press(hass)

    status = state_of(hass, STATUS)
    assert status.state == "failed"
    assert status.attributes["last_error"] == "MeterReading.AlreadyExists"


async def test_daily_schedule(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The submission runs by itself at the configured local time."""
    set_sources(hass)
    await setup_entry(hass, config_entry)
    assert submissions(mock_portal) == []

    now = dt_util.now()
    target = now.replace(hour=23, minute=50, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)

    async_fire_time_changed(hass, dt_util.as_utc(target))
    await hass.async_block_till_done()

    assert len(submissions(mock_portal)) == 1


async def test_auth_failure_triggers_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Credentials the portal refuses put the entry into reauth."""
    aioclient_mock.post(
        "https://login.eneco.be/api/v1/authn",
        status=HTTPStatus.UNAUTHORIZED,
        json={"errorSummary": "Authentication failed"},
    )
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_unload(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Unloading removes the entities and the daily timer."""
    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    # Home Assistant leaves a restored placeholder behind rather than the entity.
    assert state_of(hass, BUTTON).state == STATE_UNAVAILABLE


async def test_options_are_used(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The register mapping in the options decides which sensor is read."""
    hass.states.async_set(
        "sensor.somewhere_else",
        "77",
        {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR},
    )
    set_sources(hass)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            **config_entry.options,
            CONF_SOURCES: {**SOURCES, "offtake_day": "sensor.somewhere_else"},
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await press(hass)

    posted = submissions(mock_portal)[0]
    offtake_day = next(
        r
        for r in posted
        if r["direction"] == "OffTake" and r["meterTimeFrameType"] == "HI"
    )
    assert offtake_day["readingValue"] == 77


async def test_submission_time_is_adjustable_from_the_device(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The time entity shows the schedule and moving it rearms the trigger."""
    set_sources(hass)
    await setup_entry(hass, config_entry)

    assert state_of(hass, SUBMIT_TIME).state == "23:50:00"

    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: SUBMIT_TIME, "time": "06:30:00"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert state_of(hass, SUBMIT_TIME).state == "06:30:00"
    assert config_entry.options[CONF_SUBMIT_TIME] == "06:30:00"

    # The old moment must no longer fire, the new one must.
    now = dt_util.now()
    old = now.replace(hour=23, minute=50, second=0, microsecond=0)
    if old <= now:
        old += timedelta(days=1)
    async_fire_time_changed(hass, dt_util.as_utc(old))
    await hass.async_block_till_done()
    assert submissions(mock_portal) == []

    new = now.replace(hour=6, minute=30, second=0, microsecond=0)
    while new <= dt_util.now():
        new += timedelta(days=1)
    async_fire_time_changed(hass, dt_util.as_utc(new))
    await hass.async_block_till_done()
    assert len(submissions(mock_portal)) == 1


async def test_next_submission_sensor(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The countdown sensor points at the next occurrence of the set time."""
    await setup_entry(hass, config_entry)

    expected = dt_util.parse_datetime(state_of(hass, NEXT).state)
    assert expected is not None
    assert dt_util.as_local(expected).strftime("%H:%M:%S") == "23:50:00"
    assert expected > dt_util.utcnow()

    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: SUBMIT_TIME, "time": "04:05:00"},
        blocking=True,
    )
    await hass.async_block_till_done()

    moved = dt_util.parse_datetime(state_of(hass, NEXT).state)
    assert moved is not None
    assert dt_util.as_local(moved).strftime("%H:%M:%S") == "04:05:00"


async def setup_with_submit_time(
    hass: HomeAssistant, config_entry: MockConfigEntry, at: str
) -> None:
    """Set up the entry with a given submission time."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_SUBMIT_TIME: at}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_a_missed_day_is_caught_up_at_startup(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A wall clock trigger cannot fire while Home Assistant is down."""
    set_sources(hass)
    # The clock is pinned at midday, so 06:00 today has already gone by.
    await setup_with_submit_time(hass, config_entry, "06:00:00")

    assert len(submissions(mock_portal)) == 1
    assert state_of(hass, STATUS).state == "success"


async def test_nothing_is_caught_up_before_the_time(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Today's moment has not passed yet, so there is nothing to catch up."""
    set_sources(hass)
    await setup_with_submit_time(hass, config_entry, "23:50:00")

    assert submissions(mock_portal) == []
    assert state_of(hass, STATUS).state == "pending"


async def test_a_day_already_done_is_not_caught_up(
    hass: HomeAssistant,
    mock_portal: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Restarting after a successful submission must not send it again."""
    set_sources(hass)
    await setup_with_submit_time(hass, config_entry, "06:00:00")
    assert len(submissions(mock_portal)) == 1

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert len(submissions(mock_portal)) == 1
    assert state_of(hass, STATUS).state == "success"

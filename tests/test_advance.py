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

"""Tests for the monthly advance sensors, from the portal to the states."""

from collections.abc import Sequence
from datetime import date
from http import HTTPStatus
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.eneco_be.api import (
    EnecoConnectionError,
    EnecoError,
    ServiceDeliveryPoint,
)
from custom_components.eneco_be.api_billing import AdvanceAmounts, ContractAdvance
from custom_components.eneco_be.bill_compare import RatesUnavailableError
from custom_components.eneco_be.bills import BillCoordinator, _address_state
from custom_components.eneco_be.const import (
    CONF_ACCOUNT,
    CONF_ADDRESS,
    CONF_ADJUST_ADVANCE,
    CONF_EAN,
    CONF_ENERGY_TYPE,
    CONF_READ_BILLS,
    DOMAIN,
    SUBENTRY_METER,
)
from custom_components.eneco_be.sensor import (
    AddressAdvanceSensor,
    AddressProposedAdvanceSensor,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from .bill_pdf import ELECTRICITY
from .conftest import (
    ACCOUNT,
    ADDRESS,
    EAN,
    METER_ID,
    meter_of,
    mock_discovery,
    mock_login,
)
from .test_bills import (
    ADVANCES,
    CONSUMPTION,
    CONTRACT,
    INVOICES,
    NEXT_YEAR,
    OTHER_EAN,
    PRODUCT,
    READINGS,
    billed,
    bills_entry,
    invoice,
    mock_bill,
    setup,
    state_of,
)

# The day after the last reading was sent.
TODAY = "2026-11-26T09:00:00+00:00"
METER = "sensor.eneco_541449000000000001_monthly_advance_needed"
ADDRESS_TOTAL = "sensor.eneco_advance_monthly_advance_needed"
METER_NOW = "sensor.eneco_541449000000000001_current_advance"
ADDRESS_NOW = "sensor.eneco_advance_current_advance"

# The official readings that opened and closed the bill, and the last one sent.
READ = [
    ("2025-02-01", "Official", (5183, 7654, 5473, 2194)),
    ("2026-02-01", "Official", (7722, 11036, 8526, 3355)),
    ("2026-11-25", "CustomerAdded", (9309, 13299, 10137, 6530)),
]
OFFTAKE = (
    660.5,
    558.4,
    356.4,
    250.4,
    198.4,
    209.6,
    256.9,
    325.5,
    507.5,
    698.0,
    949.8,
    949.5,
)


def advance(identifier: str, month: str, energy: str, due: float) -> dict[str, Any]:
    """Return an advance invoice of the invoice list, for ``month``."""
    return invoice(identifier, "AdvanceInvoice", f"{month}-28", energy, due) | {
        "invoicingPeriodStartDate": f"{month}-01T00:00:00+00:00"
    }


def split(
    electricity: str, gas: str, total: str, word: str = "Électricité"
) -> list[str]:
    """Return the rows of an advance PDF for both energies."""
    return [
        "Facture d'acompte",
        f"{word} € 0,00 € {electricity}",
        f"Gaz € 0,00 € {gas}",
        f"txtTotalAmount: {total}",
    ]


def contract(ean: str, energy: str, current: float, due: float) -> dict[str, Any]:
    """Return a contract of the portal's advances page."""
    return {
        "meterDetails": {
            "ean": ean,
            "energyType": energy,
            "meterReadingMonth": 1,
            "invoiceFrequency": "Yearly",
        },
        "details": {
            "currentAmount": current,
            "minAmount": 20,
            "maxAmount": 2500,
            "recommendedAmount": round(due / 3, 2),
            "monthsUntilMeterReadingMonth": 3,
            "amountDueForMeterReadingMonth": due,
        },
    }


def portal(
    mocker: AiohttpClientMocker,
    contracts: list[dict[str, Any]],
    totals: Any,
    read: Sequence[tuple[str, str, tuple[int | None, ...]]] = READ,
    word: str = "Elektriciteit",
    others: Sequence[dict[str, Any]] = (),
) -> None:
    """Register the bill of 2025-2026, three advances, readings and volumes.

    The October advance prints its split with ``word``, which is only read
    in French. ``others`` are more meters at the address.
    """
    mock_login(
        mocker,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(
        mocker,
        points=[
            {
                "ean": EAN,
                "type": "Electricity",
                "contractId": CONTRACT,
                "meterNumber": "M1",
            },
            *others,
        ],
    )
    mocker.get(PRODUCT, json={"data": {}})
    mocker.get(
        INVOICES,
        json={
            "data": {
                "invoices": [
                    invoice("CN2", "YearlyBill", "2026-04-26"),
                    advance("AD1", "2026-02", "Electricity", 100.0),
                    advance("AD2", "2026-04", "Both", 233.24),
                    advance("AD3", "2026-10", "Both", 305.71),
                ]
            }
        },
    )
    mock_bill(mocker, "CN2", ELECTRICITY)
    mock_bill(mocker, "AD2", split("53,40", "179,84", "233,24"))
    # By default a wording not read: split on today's advances instead.
    mock_bill(mocker, "AD3", split("56,51", "249,20", "305,71", word))
    if totals is None:
        mocker.get(ADVANCES, status=HTTPStatus.INTERNAL_SERVER_ERROR)
    else:
        mocker.get(
            ADVANCES, json={"data": {"advancePayments": contracts, "totals": totals}}
        )
    keys = (
        ("OffTake", "HI"),
        ("OffTake", "LO"),
        ("Injection", "HI"),
        ("Injection", "LO"),
    )
    mocker.get(
        READINGS,
        json={
            "data": [
                {
                    "ean": EAN,
                    "date": day,
                    "meterReadingType": kind,
                    "indexValues": [
                        {"direction": d, "meterTimeFrameType": t, "value": v}
                        for (d, t), v in zip(keys, values, strict=True)
                        # None for a register the reading does not name.
                        if v is not None
                    ],
                }
                for day, kind, values in read
            ]
        },
    )
    months = [f"2025-{n:02d}" for n in range(2, 13)] + ["2026-01"]
    mocker.get(
        CONSUMPTION,
        json={
            "data": {
                "periods": [
                    {
                        "startDate": f"{month}-01T00:00:00+01:00",
                        "data": [{"direction": "OffTake", "value": value}],
                    }
                    for month, value in zip(months, OFFTAKE, strict=True)
                ]
            }
        },
    )


async def test_the_advance_a_meter_needs(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A year like the running one at the last bill's prices.

    And what the regularisation should come to with the advances invoiced
    and to come.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [
            contract(EAN, "Electricity", 56.51, 1663.29),
            contract(OTHER_EAN, "Gas", 300.0, 920.10),
        ],
        {"currentAmount": 356.51, "recommendedAmount": 861.13},
    )
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    got = meter.attributes
    assert got["last_error"] is None
    assert meter.attributes["unit_of_measurement"] == "EUR"
    # Until the January reading: what is left to pay over November,
    # December and January.
    assert got["advances_left"] == 3
    assert got["period_end_basis"] == "reading_month"
    assert float(meter.state) == round(
        (got["period_cost"] - got["advances_billed"]) / 3, 2
    )
    # Then a year like this one.
    assert got["after_reading"] == round(got["year_cost"] / 12, 2)
    assert got["after_reading"] == pytest.approx(80, abs=10)
    assert (got["portal_min"], got["portal_max"]) == (20, 2500)
    assert got["period_start"] == "2026-02-01"
    assert got["period_end"] == "2027-01-31"
    assert got["measured_until"] == "2026-11-24"
    assert (got["measured_offtake_kwh"], got["measured_injection_kwh"]) == (
        3850.0,
        4786.0,
    )
    assert (got["volume_basis"], got["energy_price"]) == ("profile", "last_bill")
    assert got["advance_now"] == 56.51
    assert got["difference"] == round(float(meter.state) - 56.51, 2)
    # February on its own invoice, April as its PDF splits it, October on
    # today's share of the advance: 56.51 of 356.51.
    assert got["advances_billed"] == round(100 + 53.40 + 305.71 * 56.51 / 356.51, 2)
    assert got["advance_split"] == "estimated"
    # November, December and January.
    assert got["advances_to_come"] == round(3 * 56.51, 2)
    assert got["expected_regularisation"] == round(
        got["period_cost"] - got["advances_billed"] - got["advances_to_come"], 2
    )

    # The gas contract of the address is not set up here: no total.
    total = state_of(hass, ADDRESS_TOTAL)
    assert total.state == STATE_UNKNOWN
    assert total.attributes["last_error"] == "the meter ending 0099 is not set up here"
    assert total.attributes["advance_now"] == 356.51
    assert total.attributes["eneco_recommended"] == 861.13
    assert total.attributes["eneco_expected_regularisation"] == round(
        1663.29 - 3 * 56.51 + 920.10 - 3 * 300.0, 2
    )

    # What Eneco bills now, as sensors of their own.
    now = state_of(hass, METER_NOW)
    assert now.state == "56.51"
    assert now.attributes["unit_of_measurement"] == "EUR"
    assert now.attributes["eneco_recommended"] == round(1663.29 / 3, 2)
    assert now.attributes["eneco_months_left"] == 3
    assert (now.attributes["portal_min"], now.attributes["portal_max"]) == (20, 2500)
    assert now.attributes["last_error"] is None
    address_now = state_of(hass, ADDRESS_NOW)
    assert address_now.state == "356.51"
    assert address_now.attributes["eneco_recommended"] == 861.13


async def test_the_address_adds_up_its_meters(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every contract of the address set up here: their amounts add up."""
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51, "recommendedAmount": 554.43},
    )
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    total = state_of(hass, ADDRESS_TOTAL)
    assert total.state == meter.state
    assert total.attributes["electricity"] == float(meter.state)
    assert total.attributes["gas"] is None
    assert total.attributes["difference"] == round(float(meter.state) - 56.51, 2)
    assert (
        total.attributes["expected_regularisation"]
        == (meter.attributes["expected_regularisation"])
    )
    assert total.attributes["last_error"] is None


async def test_advances_not_read_leave_the_amount(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The portal's advances page down: a year's advance stands.

    What is paid now and the regularisation are not known, nor what is left
    to pay: an advance whose split is unread cannot be shared by today's.
    """
    freezer.move_to(TODAY)
    portal(aioclient_mock, [], None)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    assert meter.state == STATE_UNKNOWN
    assert meter.attributes["after_reading"] > 0
    assert meter.attributes["period_end_basis"] == "anniversary"
    assert meter.attributes["advance_now"] is None
    assert meter.attributes["advances_to_come"] is None
    assert meter.attributes["expected_regularisation"] is None
    total = state_of(hass, ADDRESS_TOTAL)
    assert total.state == STATE_UNKNOWN
    assert total.attributes["last_error"].startswith("the advances were not read")
    for entity_id in (METER_NOW, ADDRESS_NOW):
        now = state_of(hass, entity_id)
        assert now.state == STATE_UNKNOWN
        assert now.attributes["last_error"].startswith("the advances were not read")


async def test_a_split_not_told_leaves_no_regularisation(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An advance for both energies unread, and no total to share it by.

    The advances billed are short, so there is no regularisation.
    """
    freezer.move_to(TODAY)
    portal(aioclient_mock, [contract(EAN, "Electricity", 56.51, 1663.29)], {})
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    got = meter.attributes
    assert meter.state == STATE_UNKNOWN
    assert got["last_error"] == "the advances invoiced could not all be split by energy"
    assert got["advance_now"] == 56.51
    assert got["advance_split"] == "incomplete"
    assert got["advances_to_come"] == round(3 * 56.51, 2)
    assert got["expected_regularisation"] is None


async def test_a_period_over_and_not_billed_ends_at_its_reading(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The reading that closed the period counts, not the next one's days."""
    freezer.move_to("2027-02-05T09:00:00+00:00")
    read = [
        *READ,
        ("2027-02-01", "Official", (9900, 14500, 10200, 6700)),
        ("2027-02-04", "CustomerAdded", (9950, 14600, 10210, 6710)),
    ]
    portal(aioclient_mock, [contract(EAN, "Electricity", 56.51, 0.0)], {}, read)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    got = state_of(hass, METER).attributes
    assert got["measured_until"] == "2027-01-31"
    assert got["measured_offtake_kwh"] == (9900 - 7722) + (14500 - 11036)
    assert got["measured_injection_kwh"] == (10200 - 8526) + (6700 - 3355)
    assert got["offtake_kwh"] == got["measured_offtake_kwh"]
    # No advance left before the reading: the next period's.
    assert got["advances_left"] == 0
    assert float(state_of(hass, METER).state) == got["after_reading"]


async def test_with_one_advance_left_the_next_period_shows(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """In the reading month the whole gap would fall on one advance.

    The next period's amount shows instead, whatever the split of the
    advances invoiced, and the regularisation says what the bill will ask.
    """
    freezer.move_to("2027-01-15T09:00:00+00:00")
    portal(aioclient_mock, [contract(EAN, "Electricity", 56.51, 0.0)], {})
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    got = meter.attributes
    assert got["advances_left"] == 1
    assert got["advance_split"] == "incomplete"
    assert float(meter.state) == got["after_reading"]
    assert got["last_error"] is None


def test_an_advances_page_down_keeps_the_last_read() -> None:
    """An advance does not change because it was not fetched again."""
    read = _address_state(
        AdvanceAmounts(
            356.51,
            861.13,
            (ContractAdvance(EAN, "Electricity", 56.51, 554.43, 3, 1663.29),),
        )
    )
    assert read["advance_now"] == 356.51
    assert read["contracts"][EAN]["current"] == 56.51
    kept = _address_state("the advances were not read: HTTP 500", read)
    assert kept == read | {"error": "the advances were not read: HTTP 500"}
    assert _address_state("down")["advance_now"] is None


APPLY = "button.eneco_advance_apply_proposed_advance"
PROPOSED = "sensor.eneco_advance_proposed_advance"


async def test_the_proposed_advance_is_applied_on_press(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The one contract of the address is set to what its meter needs."""
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    aioclient_mock.post(ADVANCES, status=HTTPStatus.NO_CONTENT)
    entry = bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    await setup(hass, entry)

    needed = float(state_of(hass, METER).state)
    proposed = state_of(hass, PROPOSED)
    assert float(proposed.state) == needed
    assert proposed.attributes["electricity"] == needed
    assert proposed.attributes["changes"] == ["electricity"]
    assert proposed.attributes["exact"] is True

    await hass.services.async_call(
        "button", "press", {"entity_id": APPLY}, blocking=True
    )
    sent = [
        data
        for method, url, data, _ in aioclient_mock.mock_calls
        if method.lower() == "post" and str(url) == ADVANCES
    ]
    assert sent == [{"amount": needed, "type": "Granular", "ean": EAN}]


async def test_nothing_is_set_when_the_proposal_cannot_be_made(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The gas contract of the address has no meter here: the press says so."""
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [
            contract(EAN, "Electricity", 56.51, 1663.29),
            contract(OTHER_EAN, "Gas", 300.0, 920.10),
        ],
        {"currentAmount": 356.51},
    )
    aioclient_mock.post(ADVANCES, status=HTTPStatus.NO_CONTENT)
    await setup(
        hass, bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    )

    assert state_of(hass, PROPOSED).state == STATE_UNKNOWN
    with pytest.raises(HomeAssistantError, match="ending 0099 is not set up here"):
        await hass.services.async_call(
            "button", "press", {"entity_id": APPLY}, blocking=True
        )
    assert not [
        c
        for c in aioclient_mock.mock_calls
        if c[0].lower() == "post" and str(c[1]) == ADVANCES
    ]


async def test_the_advance_button_needs_its_option(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reading bills alone offers no way to change what is paid."""
    freezer.move_to(TODAY)
    portal(aioclient_mock, [contract(EAN, "Electricity", 56.51, 1663.29)], {})
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    assert hass.states.get(APPLY) is None
    assert hass.states.get(PROPOSED) is None


async def test_an_advance_already_right_is_not_sent(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A contract the proposal leaves as it is is not set again.

    154.87 is what the meter of the tests needs until the reading.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 154.87, 1663.29)],
        {"currentAmount": 154.87},
    )
    aioclient_mock.post(ADVANCES, status=HTTPStatus.NO_CONTENT)
    await setup(
        hass, bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    )

    assert state_of(hass, METER).state == "154.87"
    assert state_of(hass, PROPOSED).attributes["changes"] == []
    await hass.services.async_call(
        "button", "press", {"entity_id": APPLY}, blocking=True
    )
    assert not [
        call
        for call in aioclient_mock.mock_calls
        if call[0].lower() == "post" and str(call[1]) == ADVANCES
    ]


async def test_nothing_is_set_when_the_portal_cannot_be_read_again(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The press reads again first: on failure the old proposal is not sent."""
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    aioclient_mock.post(ADVANCES, status=HTTPStatus.NO_CONTENT)
    entry = bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    await setup(hass, entry)
    bills = entry.runtime_data.bills
    assert bills is not None

    with (
        patch.object(bills, "_async_update_data", side_effect=UpdateFailed("down")),
        pytest.raises(HomeAssistantError, match="could not be read again"),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": APPLY}, blocking=True
        )
    assert not [
        call
        for call in aioclient_mock.mock_calls
        if call[0].lower() == "post" and str(call[1]) == ADVANCES
    ]


@pytest.mark.parametrize(
    ("failing", "why"),
    [
        ("async_get_invoices", "the invoices were not listed"),
        ("async_get_advance_amounts", "the advances were not read"),
        ("async_get_readings", "the readings were not read"),
        # Any other failure reading one meter.
        ("_async_meter", "the portal returned HTTP 500"),
    ],
)
async def test_nothing_is_set_when_part_of_the_portal_cannot_be_read_again(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    failing: str,
    why: str,
) -> None:
    """A read again that keeps an earlier day's data for one part sends nothing.

    Each part that fails leaves what was read before standing, so the
    proposal would be worked out from amounts the portal may no longer hold.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
        word="Électricité",
    )
    aioclient_mock.post(ADVANCES, status=HTTPStatus.NO_CONTENT)
    entry = bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    await setup(hass, entry)
    bills = entry.runtime_data.bills
    assert bills is not None
    assert state_of(hass, PROPOSED).attributes["changes"] == ["electricity"]

    with (
        patch.object(
            bills if failing.startswith("_") else bills.client,
            failing,
            side_effect=EnecoConnectionError("the portal returned HTTP 500"),
        ),
        pytest.raises(HomeAssistantError, match=why),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": APPLY}, blocking=True
        )
    assert not [
        call
        for call in aioclient_mock.mock_calls
        if call[0].lower() == "post" and str(call[1]) == ADVANCES
    ]


async def test_a_change_half_made_says_so(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The second contract refused once the first was set.

    The press names the one that was set, and the sensors are read again
    to show what is billed now.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    await setup(hass, entry)
    bills = entry.runtime_data.bills
    assert bills is not None
    bills.addresses[ADDRESS]["proposal"] = {
        "amounts": {EAN: 100.0, OTHER_EAN: 200.0},
        "energies": {EAN: "Electricity", OTHER_EAN: "Gas"},
        "changes": [EAN, OTHER_EAN],
        "error": None,
    }

    with (
        patch.object(bills, "async_refresh", AsyncMock()) as refresh,
        patch.object(
            bills.client,
            "async_set_advance",
            AsyncMock(
                side_effect=[None, EnecoConnectionError("the portal returned HTTP 500")]
            ),
        ) as sent,
        pytest.raises(
            HomeAssistantError,
            match="the electricity advance was set, but not the gas one: .*HTTP 500",
        ),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": APPLY}, blocking=True
        )
    assert sent.await_count == 2
    assert refresh.await_count == 2


async def test_an_address_left_without_meters_goes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Its device, sensors and button go with the last meter there.

    Nothing else ever adds them again, so they would stay unavailable.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    await setup(hass, entry)
    identifiers = {(DOMAIN, f"{entry.entry_id}_{ADDRESS}")}
    assert device_registry.async_get_device(identifiers=identifiers) is not None
    assert entity_registry.async_get(APPLY) is not None

    assert hass.config_entries.async_remove_subentry(entry, METER_ID)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert device_registry.async_get_device(identifiers=identifiers) is None
    for entity_id in (APPLY, PROPOSED, ADDRESS_TOTAL, ADDRESS_NOW):
        assert entity_registry.async_get(entity_id) is None, entity_id


async def test_the_address_goes_when_bills_are_no_longer_read(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Every entity of the address device is a bill one, so it goes too."""
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)
    identifiers = {(DOMAIN, f"{entry.entry_id}_{ADDRESS}")}
    assert device_registry.async_get_device(identifiers=identifiers) is not None

    hass.config_entries.async_update_entry(entry, options={CONF_READ_BILLS: False})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert device_registry.async_get_device(identifiers=identifiers) is None


async def test_two_meters_of_one_energy_do_not_share_their_advances(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No advance invoice names its meter, so neither may count them all.

    Each meter would take the other's advances as its own, need far less,
    and the advance button would lower both.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [
            contract(EAN, "Electricity", 56.51, 1663.29),
            contract(OTHER_EAN, "Electricity", 56.51, 1663.29),
        ],
        {"currentAmount": 113.02},
        word="Électricité",
        others=[{"ean": OTHER_EAN, "type": "Electricity", "meterNumber": "M2"}],
    )
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    got = state_of(hass, METER).attributes
    assert got["advance_split"] == "incomplete"
    assert got["advances_billed"] == 0
    assert state_of(hass, METER).state == STATE_UNKNOWN
    assert got["last_error"] == (
        "the advances invoiced are not told apart between meters of one energy"
    )


def address_sensor(
    provisions: dict[str, dict[str, Any]], portal: dict[str, Any]
) -> AddressAdvanceSensor:
    """Return the address total over meters of the given EANs and projections."""
    subentries = {
        ean: SimpleNamespace(
            data={"ean": ean, "energy_type": "Electricity", "address": ADDRESS}
        )
        for ean in provisions
    }
    coordinator = SimpleNamespace(
        addresses={ADDRESS: {"eans": sorted(provisions)} | portal},
        data={ean: {"provision": provision} for ean, provision in provisions.items()},
        config_entry=SimpleNamespace(subentries=subentries),
    )
    return AddressAdvanceSensor(coordinator, "entry", ADDRESS, 0)  # type: ignore[arg-type]


def test_two_meters_of_one_energy_both_count_at_the_address() -> None:
    """The total was keyed by energy, so the second meter replaced the first."""
    provision = {
        "monthly_advance": 50.0,
        "after_reading": 50.0,
        "expected_regularisation": 10.0,
        "error": None,
    }
    sensor = address_sensor({EAN: provision, OTHER_EAN: provision}, {})
    attributes = sensor.extra_state_attributes
    assert sensor.native_value == 100.0
    assert attributes["electricity"] == 100.0
    assert attributes["after_reading"] == 100.0
    assert attributes["expected_regularisation"] == 20.0


@pytest.mark.parametrize(
    ("provision_error", "portal_error"),
    [
        ("the readings were not read: HTTP 500", None),
        (None, "the advances were not read"),
    ],
)
def test_the_address_total_says_why_it_was_not_read_again(
    provision_error: str | None, portal_error: str | None
) -> None:
    """A meter's advance left standing from an earlier day still adds up.

    The address total showed it with no error, as if read today, while the
    meter's own sensor said why it was not.
    """
    stale = {"monthly_advance": 50.0, "error": provision_error}
    sensor = address_sensor({EAN: stale}, {"error": portal_error})
    assert sensor.native_value == 50.0
    assert sensor.extra_state_attributes["last_error"] == (
        provision_error or portal_error
    )


async def test_a_reading_of_some_registers_only_is_passed_over(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A meter with a register left unmapped only sends the others.

    The newest reading was taken whatever it named, and naming fewer
    registers than the one that closed the bill, it gave no advance at all.
    """
    freezer.move_to(TODAY)
    read = [*READ, ("2026-11-26", "CustomerAdded", (9320, 13310, None, None))]
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
        read,
    )
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    got = state_of(hass, METER).attributes
    assert got["last_error"] is None
    # The reading of the 25th, every register in it.
    assert got["measured_until"] == "2026-11-24"


async def test_an_address_without_meters_sets_nothing_and_says_so(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Around a reload the button can outlive the meters of its address.

    The account was looked up with next() and no default, and the
    StopIteration came out as a RuntimeError no caller catches.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    await setup(hass, entry)
    bills = entry.runtime_data.bills
    assert bills is not None

    with pytest.raises(EnecoError, match="no meter is set up at the address"):
        await bills.async_apply_advance("b" * 64)


async def test_readings_of_some_registers_only_give_no_advance(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every reading since the bill misses a register: nothing is known used.

    The one naming them all was the official reading closing the bill, so
    the advance was shown from none of the readings since, without a word.
    """
    freezer.move_to(TODAY)
    read = [
        READ[0],
        READ[1],
        ("2026-06-30", "CustomerAdded", (8400, 12000, None, None)),
        ("2026-11-25", "CustomerAdded", (9309, 13299, None, None)),
    ]
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
        read,
    )
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    assert meter.state == STATE_UNKNOWN
    assert meter.attributes["last_error"] == (
        "the readings since the last bill do not name every register"
    )


async def test_the_apply_button_stays_after_a_read_that_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A press reads again first, so it has nothing to wait for.

    Unavailable after one failed read, it could not be pressed for a day,
    and a script pressing it was let through doing nothing.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    await setup(hass, entry)
    bills = entry.runtime_data.bills
    assert bills is not None

    with patch.object(bills, "_async_update_data", side_effect=UpdateFailed("down")):
        await bills.async_refresh()
        await hass.async_block_till_done()

    assert not bills.last_update_success
    assert state_of(hass, APPLY).state != STATE_UNAVAILABLE


async def test_every_entity_has_its_name_in_the_strings(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Most keys are set from constants, which the translations check cannot see.

    A key missing from strings.json shows the entity under the device name
    alone, and nothing else would notice.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True, CONF_ADJUST_ADVANCE: True})
    await setup(hass, entry)

    path = Path(__file__).parent.parent / "custom_components" / DOMAIN / "strings.json"
    strings = json.loads(path.read_text(encoding="utf-8"))["entity"]
    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(entities) > 10
    for item in entities:
        assert item.translation_key in strings.get(item.domain, {}), item.entity_id


async def test_a_bill_shorter_than_a_year_asks_the_profile_a_year_back(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The rest of the period is weighed on the same days a year before.

    The profile was asked from the bill's first month only, so after a bill
    of half a year those days were never fetched, and the rest counted every
    day the same: about a fifth off on a seasonal meter.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)
    bills = entry.runtime_data.bills
    assert bills is not None

    year = {
        "bills": [
            {
                "invoice_type": "YearlyBill",
                "period_start": "2025-08-01",
                "period_end": "2026-01-31",
            }
        ],
        "lines": [],
    }
    volumes = AsyncMock(return_value={})
    with (
        patch.object(bills.client, "async_get_readings", AsyncMock(return_value=[])),
        patch.object(bills.client, "async_get_monthly_volumes", volumes),
    ):
        await bills._async_provision(  # noqa: SLF001
            ACCOUNT,
            ADDRESS,
            meter_of(entry).data,
            ServiceDeliveryPoint(EAN, "Electricity", meter_number="M1"),
            year,
            [],
            "the portal lists no advances",
            date(2026, 4, 2),
        )
    assert volumes.await_args is not None
    start, end = volumes.await_args.args[3:]
    # A year before the period that starts on 1 February 2026.
    assert start == date(2025, 2, 1)
    assert end == date(2026, 1, 31)


async def test_two_accounts_at_one_address_set_nothing(hass: HomeAssistant) -> None:
    """The address state is kept by address, not by account and address.

    The second account's read replaced the first's, and the press sent its
    contract under the first account, whose meter came first.
    """
    address = "f" * 64

    def meter(account: str, ean: str, energy: str) -> ConfigSubentryData:
        return ConfigSubentryData(
            data={
                CONF_ACCOUNT: account,
                CONF_ADDRESS: address,
                CONF_EAN: ean,
                CONF_ENERGY_TYPE: energy,
            },
            subentry_type=SUBENTRY_METER,
            title=ean,
            unique_id=ean,
        )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        subentries_data=[
            meter("A", EAN, "Electricity"),
            meter("B", OTHER_EAN, "Gas"),
        ],
    )
    entry.add_to_hass(hass)
    client = SimpleNamespace(async_set_advance=AsyncMock(), refused=False)
    bills = BillCoordinator(hass, entry, client)  # type: ignore[arg-type]

    async def amounts(account: str, address: str) -> AdvanceAmounts:
        ean, energy = (EAN, "Electricity") if account == "A" else (OTHER_EAN, "Gas")
        return AdvanceAmounts(
            50.0,
            None,
            (ContractAdvance(ean, energy, 50.0, None, 3, None, 1, 20.0, 2500.0),),
        )

    async def provision(account: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"monthly_advance": 100.0, "advances_left": 3, "error": None}

    with (
        patch.object(bills, "_async_list", AsyncMock(return_value=({}, []))),
        patch.object(bills, "_async_advance_amounts", amounts),
        patch.object(bills, "_async_read_advances", AsyncMock()),
        patch.object(
            bills,
            "_async_meter",
            AsyncMock(
                return_value={
                    "card": None,
                    "card_error": None,
                    "year": None,
                    "error": None,
                }
            ),
        ),
        patch.object(bills, "_async_provision", provision),
        pytest.raises(EnecoError, match="two accounts"),
    ):
        await bills.async_apply_advance(address)
    client.async_set_advance.assert_not_awaited()


def test_the_proposal_adds_up_two_contracts_of_one_energy() -> None:
    """Keyed by energy, the second contract replaced the first.

    And the energy came twice in what would change.
    """
    coordinator = SimpleNamespace(
        addresses={
            ADDRESS: {
                "proposal": {
                    "amounts": {EAN: 100.0, OTHER_EAN: 50.0},
                    "energies": {EAN: "Electricity", OTHER_EAN: "Electricity"},
                    "changes": [EAN, OTHER_EAN],
                    "total": 150.0,
                    "exact": True,
                    "error": None,
                }
            }
        }
    )
    sensor = AddressProposedAdvanceSensor(coordinator, "entry", ADDRESS, 0)  # type: ignore[arg-type]
    attributes = sensor.extra_state_attributes
    assert attributes["electricity"] == 150.0
    assert attributes["gas"] is None
    assert attributes["changes"] == ["electricity"]


async def test_the_advance_stands_a_day_the_advances_page_is_down(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without the page there is no reading month, and the period moved.

    The amount was worked out again on a year from the last bill instead,
    another figure for a day, with no error.
    """
    freezer.move_to(TODAY)
    march = contract(EAN, "Electricity", 56.51, 1663.29)
    march["meterDetails"]["meterReadingMonth"] = 3
    portal(aioclient_mock, [march], {"currentAmount": 56.51}, word="Électricité")
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)
    before = state_of(hass, METER)
    assert before.attributes["period_end_basis"] == "reading_month"

    aioclient_mock.clear_requests()
    portal(aioclient_mock, [], None, word="Électricité")
    bills = entry.runtime_data.bills
    assert bills is not None
    await bills.async_refresh()
    await hass.async_block_till_done()

    after = state_of(hass, METER)
    assert after.state == before.state
    assert after.attributes["period_end"] == before.attributes["period_end"]
    assert after.attributes["last_error"].startswith("the advances were not read")


async def test_an_advance_of_the_period_before_is_not_kept(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new bill read the day the readings are not: the old period is over.

    Its amount stood, with its dates, as if it were the running period's.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)
    bills = entry.runtime_data.bills
    assert bills is not None

    year = {
        "bills": [
            {
                "invoice_type": "YearlyBill",
                "period_start": "2025-02-01",
                "period_end": "2026-01-31",
            }
        ],
        "lines": [],
    }
    previous = {"monthly_advance": 50.0, "period_start": "2025-02-01", "error": None}
    with patch.object(
        bills.client,
        "async_get_readings",
        side_effect=EnecoConnectionError("the portal returned HTTP 500"),
    ):
        got = await bills._async_provision(  # noqa: SLF001
            ACCOUNT,
            ADDRESS,
            meter_of(entry).data,
            ServiceDeliveryPoint(EAN, "Electricity", meter_number="M1"),
            year,
            [],
            "the portal lists no advances",
            date(2026, 11, 26),
            previous,
        )
    assert got.get("monthly_advance") is None
    assert got["error"] == "the readings were not read: the portal returned HTTP 500"


async def test_no_advance_is_worked_out_from_a_year_already_billed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The newest yearly bill refused, the year shown is the one before.

    The advance was worked out after it, for a period already billed, with
    nothing said, and that bill's regularisation shown as still to come.
    """
    freezer.move_to("2027-11-26T09:00:00+00:00")
    # Registered first, so it answers before the one portal() adds.
    aioclient_mock.get(
        INVOICES,
        json={
            "data": {
                "invoices": [
                    billed(
                        invoice("CN3", "YearlyBill", "2027-04-26"),
                        "2026-02-01",
                        "2027-02-01",
                    ),
                    billed(
                        invoice("CN2", "YearlyBill", "2026-04-26"),
                        "2025-02-01",
                        "2026-02-01",
                    ),
                    advance("AD1", "2027-02", "Electricity", 100.0),
                ]
            }
        },
    )
    mock_bill(
        aioclient_mock,
        "CN3",
        [row.replace("€ 58,04", "€ 58,40") for row in NEXT_YEAR],
    )
    portal(
        aioclient_mock,
        [contract("541449000000000001", "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51, "recommendedAmount": 554.43},
        read=[
            *READ,
            ("2027-02-01", "Official", (9500, 13600, 10400, 6700)),
            ("2027-11-25", "CustomerAdded", (11000, 15500, 12000, 9000)),
        ],
    )
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    assert meter.state == STATE_UNKNOWN
    assert meter.attributes["last_error"] == (
        "a newer bill than the year shown was not read"
    )


async def test_a_closing_bill_of_another_meter_is_no_newer_year(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A meter of the address removed, its contract ended with a closing bill.

    That bill starts after this meter's year and prints the other EAN, yet
    it was taken for a newer bill of this meter not read, and the advance
    was blank until this meter's next yearly bill.
    """
    freezer.move_to(TODAY)
    aioclient_mock.get(
        INVOICES,
        json={
            "data": {
                "invoices": [
                    billed(
                        invoice("BCL", "ClosingBill", "2026-07-01"),
                        "2026-02-01",
                        "2026-06-15",
                    ),
                    invoice("CN2", "YearlyBill", "2026-04-26"),
                    advance("AD1", "2026-02", "Electricity", 100.0),
                    advance("AD2", "2026-04", "Both", 233.24),
                    advance("AD3", "2026-10", "Both", 305.71),
                ]
            }
        },
    )
    mock_bill(
        aioclient_mock, "BCL", [row.replace(EAN, OTHER_EAN) for row in ELECTRICITY]
    )
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51, "recommendedAmount": 554.43},
    )
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    assert meter.attributes["last_error"] is None
    assert meter.state != STATE_UNKNOWN


async def test_no_gas_advance_from_the_bill_price_when_rates_fail(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The gas price entry checking the bill could not give its rates.

    The bill's price stood in, far below an index's, and the advance was
    worked out, and could be sent, far too low.
    """
    freezer.move_to(TODAY)
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
    )
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)
    bills = entry.runtime_data.bills
    assert bills is not None
    year = bills.data[METER_ID]["year"]

    with patch(
        "custom_components.eneco_be.bills.async_energy_rates",
        AsyncMock(side_effect=RatesUnavailableError("Gas is not loaded")),
    ):
        got = await bills._async_provision(  # noqa: SLF001
            ACCOUNT,
            ADDRESS,
            {**meter_of(entry).data, CONF_ENERGY_TYPE: "Gas"},
            ServiceDeliveryPoint(EAN, "Gas", meter_number="M1"),
            year,
            [],
            "the portal lists no advances",
            date(2026, 11, 26),
        )
    assert got.get("monthly_advance") is None
    assert got["error"] == "the gas prices were not read: Gas is not loaded"


@pytest.mark.parametrize(
    ("energy", "due"),
    [("Electricity", 400.0), ("Electricity", -400.0), ("Both", 400.0)],
)
async def test_an_intermediary_bill_of_the_period_is_counted(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    energy: str,
    due: float,
) -> None:
    """A product change in July: a bill settled February to June already.

    What it asked for, or paid back, was left out, so the advance and the
    regularisation still counted those months as owed. One for both
    energies cannot be split, and gives no advance.
    """
    freezer.move_to(TODAY)
    aioclient_mock.get(
        INVOICES,
        json={
            "data": {
                "invoices": [
                    billed(
                        invoice(
                            "IN3", "IntermediaryInvoice", "2026-07-10", energy, due
                        ),
                        "2026-02-01",
                        "2026-07-01",
                    ),
                    invoice("CN2", "YearlyBill", "2026-04-26"),
                    advance("AD1", "2026-02", "Electricity", 100.0),
                    advance("AD2", "2026-04", "Both", 233.24),
                    advance("AD3", "2026-10", "Both", 305.71),
                ]
            }
        },
    )
    portal(
        aioclient_mock,
        [contract(EAN, "Electricity", 56.51, 1663.29)],
        {"currentAmount": 56.51},
        word="Électricité",
    )
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    meter = state_of(hass, METER)
    got = meter.attributes
    if energy == "Both":
        assert meter.state == STATE_UNKNOWN
        assert got["last_error"] == (
            "an intermediary bill for both energies settles part of the period"
        )
        # Not 0, which would say it settled nothing.
        assert got["settled_by_bills"] is None
        return
    assert got["settled_by_bills"] == due
    assert float(meter.state) == round(
        (got["period_cost"] - got["advances_billed"] - due) / got["advances_left"], 2
    )
    assert got["expected_regularisation"] == round(
        got["period_cost"] - got["advances_billed"] - got["advances_to_come"] - due, 2
    )

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
from http import HTTPStatus
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.eneco_be.const import CONF_READ_BILLS
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .bill_pdf import ELECTRICITY
from .conftest import ACCOUNT, EAN, mock_discovery, mock_login
from .test_bills import (
    ADVANCES,
    CONSUMPTION,
    CONTRACT,
    INVOICES,
    OTHER_EAN,
    PRODUCT,
    READINGS,
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
        "meterDetails": {"ean": ean, "energyType": energy, "meterReadingMonth": 1},
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
    read: Sequence[tuple[str, str, tuple[int, ...]]] = READ,
) -> None:
    """Register the bill of 2025-2026, three advances, readings and volumes."""
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
            }
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
    # A wording not read: split on today's advances instead.
    mock_bill(mocker, "AD3", split("56,51", "249,20", "305,71", "Elektriciteit"))
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

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

"""Tests for reading the bills and tariff cards, an option off by default."""

from datetime import date, timedelta
from http import HTTPStatus
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.eneco_be import api
from custom_components.eneco_be.api import EnecoClient, EnecoError
from custom_components.eneco_be.bills import BillCoordinator
from custom_components.eneco_be.const import CONF_READ_BILLS, DOMAIN
from custom_components.eneco_be.diagnostics import async_get_config_entry_diagnostics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall, State, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .bill_pdf import ELECTRICITY, make_pdf
from .conftest import (
    ACCOUNT,
    ADDRESS,
    API,
    EAN,
    EMAIL,
    METER_ID,
    PASSWORD,
    login_entry,
    mock_discovery,
    mock_login,
)

LAST_BILL = "sensor.eneco_541449000000000001_last_yearly_bill"
TARIFF_CARD = "sensor.eneco_541449000000000001_tariff_card"
BILL_DIFFERENCE = "sensor.eneco_541449000000000001_bill_difference"

CONTRACT = "1-1000"
INVOICES = f"{API}/accounts/{ACCOUNT}/delivery-addresses/{ADDRESS}/invoices"
PRODUCT = f"{API}/accounts/{ACCOUNT}/contracts/{CONTRACT}/products"
DOWNLOAD = "https://eneco.be/api/my-eneco/download"
CARD = "https://cdn.eneco.be/downloads/{lang}/general/tk/BC_032_012210_{LANG}_ENECO_POWER_FLEX.pdf"

OTHER_EAN = "541449000000000099"
# The same bill a cent dearer, to tell which of two is shown.
DEARER = [
    row.replace("€ 58,04", "€ 58,05").replace("€ 350,09", "€ 350,10")
    for row in ELECTRICITY
]


def document(invoice: str) -> str:
    """Return the link the invoice list gives for a bill."""
    return f"{DOWNLOAD}/{invoice}?accountNumber={ACCOUNT}&fileId=SAP/Invoice/{invoice}"


def invoice(
    identifier: str, kind: str, issued: str, energy: str = "Electricity"
) -> dict[str, Any]:
    """Return an entry of the invoice list."""
    return {
        "invoiceId": identifier,
        "invoiceType": kind,
        "invoiceEnergyType": energy,
        "invoiceDate": f"{issued}T00:00:00+00:00",
        "invoicingPeriodStartDate": "2025-02-01T00:00:00+00:00",
        "invoicingPeriodEndDate": "2026-02-01T00:00:00+00:00",
        "dueAmount": -234.09,
        "documents": [
            {"documentType": "Invoice", "url": document(identifier)},
            {"documentType": "SimplifiedBill", "url": document(f"{identifier}-S")},
        ],
    }


def mock_bill(mocker: AiohttpClientMocker, identifier: str, rows: list[str]) -> None:
    """Serve a bill the way the website does, behind a trailing slash redirect."""
    url = document(identifier)
    slashed = url.replace(f"/{identifier}?", f"/{identifier}/?")
    mocker.get(
        url,
        status=HTTPStatus.PERMANENT_REDIRECT,
        headers={"Location": slashed.removeprefix("https://eneco.be")},
    )
    mocker.get(slashed, content=make_pdf(rows))


def downloads(mocker: AiohttpClientMocker, identifier: str, path: str = "/") -> int:
    """Return how many times a bill was downloaded, or its first link asked."""
    return sum(
        1
        for method, url, _, _ in mocker.mock_calls
        if method.lower() == "get" and str(url.path).endswith(f"/{identifier}{path}")
    )


def sign_ins(mocker: AiohttpClientMocker) -> list[Any]:
    """Return every password sign in made."""
    return [
        url
        for method, url, _, _ in mocker.mock_calls
        if method.lower() == "post" and str(url).endswith("/api/v1/authn")
    ]


@pytest.fixture
def portal(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Register a portal with a product and a yearly bill for the meter."""
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(
        aioclient_mock,
        points=[{"ean": EAN, "type": "Electricity", "contractId": CONTRACT}],
    )
    aioclient_mock.get(
        INVOICES,
        json={
            "data": {
                "invoices": [
                    invoice("FA1", "AdvanceInvoice", "2026-07-30", "Both"),
                    invoice("CN2", "YearlyBill", "2026-04-26"),
                    invoice("CN1", "YearlyBill", "2025-02-11"),
                ]
            }
        },
    )
    aioclient_mock.get(
        PRODUCT,
        json={
            "data": {
                "contractId": CONTRACT,
                "tariff": {
                    "product": "ENECO_POWER_FLEX",
                    "productType": "Variable",
                    "currentTariff": [
                        {
                            "tariffCardByLanguage": [
                                {
                                    "language": lang.upper(),
                                    "tariffCard": CARD.format(
                                        lang=lang, LANG=lang.upper()
                                    ),
                                    "type": "Normal",
                                }
                                for lang in ("nl", "fr")
                            ]
                        }
                    ],
                },
            }
        },
    )
    return aioclient_mock


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """Return the state of an entity that has to exist."""
    found = hass.states.get(entity_id)
    assert found is not None, entity_id
    return found


def bills_entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """Return the login of the tests with the given options."""
    entry = login_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options=options)
    return entry


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the login up and let the first read of the bills finish."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_bill_and_card_read(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """A yearly bill of a full year is the year on its own, line by line."""
    mock_bill(portal, "CN2", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.28"
    assert bill.attributes["unit_of_measurement"] == "EUR"
    assert bill.attributes["issued"] == "2026-04-26"
    assert bill.attributes["period_start"] == "2025-02-01"
    assert bill.attributes["period_end"] == "2026-01-31"
    assert bill.attributes["bills"] == [
        {
            "invoice_type": "YearlyBill",
            "issued": "2026-04-26",
            "period_start": "2025-02-01",
            "period_end": "2026-01-31",
        }
    ]
    assert bill.attributes["last_error"] is None
    lines = bill.attributes["lines"]
    assert len(lines) == 11
    assert lines[1] == {
        "section": "energy",
        "label": "Conso. simple",
        "code": "Energiecomponent ElectricityAplusTH",
        "start": "2025-02-01",
        "end": "2025-03-31",
        "quantity": 334.51,
        "unit": "kWh",
        "unit_price": 0.1735,
        "vat": 0.06,
        "total": 58.04,
    }

    card = state_of(hass, TARIFF_CARD)
    assert card.state == "012210"
    assert card.attributes["card_month"] == "2022-10"
    assert card.attributes["product"] == "ENECO_POWER_FLEX"
    assert card.attributes["product_type"] == "Variable"
    assert set(card.attributes["urls"]) == {"nl", "fr"}
    # Only the newest bill, and not its simplified copy.
    assert downloads(portal, "CN2") == 1
    assert downloads(portal, "CN1") == 0


async def test_a_bill_is_downloaded_once(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """The daily read lists the invoices again but keeps a bill it has."""
    mock_bill(portal, "CN2", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done(wait_background_tasks=True)

    listed = [
        url for method, url, _, _ in portal.mock_calls if str(url).startswith(INVOICES)
    ]
    assert len(listed) == 2
    assert downloads(portal, "CN2") == 1
    assert state_of(hass, LAST_BILL).state == "1139.28"


async def test_the_last_bill_survives_a_restart_without_portal(
    hass: HomeAssistant, portal: AiohttpClientMocker, hass_storage: dict[str, Any]
) -> None:
    """What was read is kept, so a portal that is down shows the last bill."""
    mock_bill(portal, "CN2", ELECTRICITY)
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)
    assert f"{DOMAIN}.{entry.entry_id}.bills" in hass_storage

    assert await hass.config_entries.async_unload(entry.entry_id)
    portal.clear_requests()
    mock_login(
        portal,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    portal.get(INVOICES, status=HTTPStatus.INTERNAL_SERVER_ERROR)
    portal.get(
        f"{API}/accounts/{ACCOUNT}/delivery-addresses/{ADDRESS}/service-delivery-points",
        json={"data": {"serviceDeliveryPoints": []}},
    )
    await setup(hass, entry)

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.28"
    assert bill.attributes["last_error"] == "the portal returned HTTP 500"
    assert state_of(hass, TARIFF_CARD).state == "012210"


async def test_a_failed_sign_in_keeps_the_last_bill(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """A refresh that cannot sign in fails as a whole, and the bill stays."""
    mock_bill(portal, "CN2", ELECTRICITY)
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)

    portal.clear_requests()
    mock_login(portal, accounts=[])
    portal.get(INVOICES, status=HTTPStatus.UNAUTHORIZED)
    portal.get(
        f"{API}/accounts/{ACCOUNT}/delivery-addresses/{ADDRESS}/service-delivery-points",
        json={"data": {"serviceDeliveryPoints": []}},
    )
    bills = entry.runtime_data.bills
    assert bills is not None
    await bills.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not bills.last_update_success
    assert state_of(hass, LAST_BILL).state == "1139.28"
    assert state_of(hass, TARIFF_CARD).state == "012210"


async def test_a_bill_of_another_meter_is_passed_over(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """Only a bill printing the meter's own EAN is taken for it."""
    mock_bill(portal, "CN2", [row.replace(EAN, OTHER_EAN) for row in ELECTRICITY])
    mock_bill(portal, "CN1", DEARER)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    assert state_of(hass, LAST_BILL).state == "1139.29"


async def test_a_bill_naming_two_meters_is_refused(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """Lines that are not split by meter are not handed to either."""
    mock_bill(portal, "CN2", [*ELECTRICITY, f"EAN {OTHER_EAN}"])
    mock_bill(portal, "CN1", [row.replace(EAN, OTHER_EAN) for row in ELECTRICITY])
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == STATE_UNKNOWN
    assert bill.attributes["last_error"] == (
        "the bill of 2026-04-26 was not read: the bill covers more than one meter"
    )


async def test_a_refused_bill_leaves_the_one_before(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """A bill that does not add up is refused and named; the older one shows.

    The same on a first read as on a day a newer bill comes in after one was
    already shown: the dates on the sensor say which bill it is.
    """
    mock_bill(portal, "CN2", [row.replace("€ 58,04", "€ 58,40") for row in ELECTRICITY])
    mock_bill(portal, "CN1", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.28"
    assert bill.attributes["issued"] == "2025-02-11"
    assert bill.attributes["last_error"] == (
        "the bill of 2026-04-26 was not read: energy lines add up to 350.45,"
        " the bill says 350,09"
    )

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done(wait_background_tasks=True)
    # Neither is fetched again: one was read, the other refused for good.
    assert downloads(portal, "CN2") == 1
    assert downloads(portal, "CN1") == 1
    assert "350.45" in state_of(hass, LAST_BILL).attributes["last_error"]


async def test_a_bill_that_will_not_download_is_tried_again(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """One bill answering an error does not stop the card or the others.

    It does not sign in again either: a 404 says nothing about the session.
    """
    portal.get(document("CN2"), status=HTTPStatus.NOT_FOUND)
    mock_bill(portal, "CN1", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.28"
    assert bill.attributes["last_error"] == (
        "the bill of 2026-04-26 was not read: the bill answered HTTP 404"
    )
    assert state_of(hass, TARIFF_CARD).state == "012210"
    assert len(sign_ins(portal)) == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert downloads(portal, "CN2", "") == 2
    assert downloads(portal, "CN1") == 1
    assert len(sign_ins(portal)) == 1


async def test_off_by_default(hass: HomeAssistant, portal: AiohttpClientMocker) -> None:
    """Without the option nothing about bills is fetched or shown."""
    await setup(hass, bills_entry(hass))

    assert hass.states.get(LAST_BILL) is None
    assert hass.states.get(TARIFF_CARD) is None
    assert not [
        url for _, url, _, _ in portal.mock_calls if str(url).startswith(INVOICES)
    ]


async def test_options_flow_turns_it_on_and_off(
    hass: HomeAssistant,
    portal: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    hass_storage: dict[str, Any],
) -> None:
    """Turning the option on adds the sensors, turning it off removes them."""
    mock_bill(portal, "CN2", ELECTRICITY)
    entry = bills_entry(hass)
    await setup(hass, entry)

    for value in (True, False):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_READ_BILLS: value}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done(wait_background_tasks=True)

        assert entry.options == {CONF_READ_BILLS: value}
        if value:
            assert state_of(hass, LAST_BILL).state == "1139.28"
            assert BillCoordinator.key_for(entry.entry_id) in hass_storage
        else:
            assert hass.states.get(LAST_BILL) is None
            assert entity_registry.async_get(LAST_BILL) is None
            assert entity_registry.async_get(TARIFF_CARD) is None
            # What was read goes with them.
            assert BillCoordinator.key_for(entry.entry_id) not in hass_storage


async def test_a_bill_moved_off_the_website_is_not_fetched(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """The cookies of the session only ever go to the website."""
    portal.get(
        document("CN2"),
        status=HTTPStatus.FOUND,
        headers={"Location": "https://elsewhere.example/bill.pdf"},
    )
    # Served there too, so that following it would show as a bill read.
    portal.get(
        "https://elsewhere.example/bill.pdf",
        content=make_pdf(DEARER),
    )
    mock_bill(portal, "CN1", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    assert not [
        url for _, url, _, _ in portal.mock_calls if url.host == "elsewhere.example"
    ]
    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.28"
    assert "moved off the Eneco website" in bill.attributes["last_error"]


async def test_a_lapsed_session_is_signed_in_again_for_a_bill(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """The website answers a lapsed session with its login page."""
    answers = [b"<html>login</html>", make_pdf(ELECTRICITY)]

    async def bill(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, response=answers.pop(0))

    portal.get(document("CN2"), side_effect=bill)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    assert state_of(hass, LAST_BILL).state == "1139.28"
    assert len(sign_ins(portal)) == 2


async def test_diagnostics_and_removal(
    hass: HomeAssistant, portal: AiohttpClientMocker, hass_storage: dict[str, Any]
) -> None:
    """Diagnostics tell what was read without the lines; removal drops it all."""
    mock_bill(portal, "CN2", ELECTRICITY)
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)

    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["entry"]["options"] == {CONF_READ_BILLS: True}
    assert report["meters"][0]["bill"] == {
        "card_version": "012210",
        "product": "ENECO_POWER_FLEX",
        "bills": [{"invoice_type": "YearlyBill", "issued": "2026-04-26"}],
        "lines": 11,
        "error": None,
        "comparison": {
            "covered": None,
            "difference": None,
            "error": "no electricity or gas price entry checks this bill",
        },
    }

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert BillCoordinator.key_for(entry.entry_id) not in hass_storage


# The bill of the tests, after an intermediary one: 15/03/2025 to 31/01/2026.
SHORT = [row.replace("01/02/2025", "15/03/2025") for row in ELECTRICITY]


def year_before(end: str) -> list[str]:
    """Return the bill of the tests moved a year back, ending on ``end``."""
    return [
        row.replace("01/02/2025", "15/02/2024")
        .replace("31/03/2025", "31/03/2024")
        .replace("01/04/2025", "01/04/2024")
        .replace("31/01/2026", end)
        for row in ELECTRICITY
    ]


def month_bill(start: date, end: date) -> list[str]:
    """Return a monthly bill of one line per block."""
    first, last = start.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")
    return [
        f"EAN {EAN}",
        f"Conso. simple {first} - {last} 100,00 kWh 10,00 €c 6% € 10,00",
        "Total coût de l’énergie Eneco (hors TVA) € 10,00",
        f"Distribution fixe {first} - {last} 1,00 année 5,00 € 6% € 5,00",
        "Total des tarifs de réseau (hors TVA) € 5,00",
        f"Redevance de raccordement {first} - {last} 100,00 kWh 1,00 €c 0% € 1,00",
        "Total des impôts, taxes et suppléments (hors TVA) € 1,00",
    ]


def mock_months(
    mocker: AiohttpClientMocker, first: date, count: int
) -> list[dict[str, Any]]:
    """Serve ``count`` monthly bills from ``first`` on; return them newest first."""
    listed = []
    for n in range(count):
        month = first.month - 1 + n
        start = date(first.year + month // 12, month % 12 + 1, 1)
        month += 1
        end = date(first.year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        mock_bill(mocker, f"M{n:02d}", month_bill(start, end))
        issued = (end + timedelta(days=5)).isoformat()
        listed.append(invoice(f"M{n:02d}", "MonthlyBill", issued))
    return listed[::-1]


def mock_invoices(mocker: AiohttpClientMocker, invoices: list[dict[str, Any]]) -> None:
    """Register a portal listing the given invoices for the meter."""
    mock_login(
        mocker,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(mocker, points=[{"ean": EAN, "type": "Electricity"}])
    mocker.get(INVOICES, json={"data": {"invoices": invoices}})


async def test_a_short_yearly_bill_is_the_year(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """However many days the readings put in it, a yearly bill is the year."""
    mock_bill(portal, "CN2", SHORT)
    # It follows on: only its kind keeps it out.
    mock_bill(portal, "CN1", year_before("14/03/2025"))
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.28"
    assert bill.attributes["period_start"] == "2025-03-15"
    assert len(bill.attributes["bills"]) == 1
    assert bill.attributes["last_error"] is None
    assert downloads(portal, "CN1") == 0


async def test_an_intermediary_bill_is_part_of_the_year(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The intermediary bills before a yearly one are its year, VAT included."""
    mock_invoices(
        aioclient_mock,
        [
            invoice("CN2", "YearlyBill", "2026-04-26"),
            invoice("IN1", "IntermediaryInvoice", "2025-04-01"),
            invoice("CN1", "YearlyBill", "2024-02-20"),
        ],
    )
    mock_bill(aioclient_mock, "CN2", SHORT)
    mock_bill(aioclient_mock, "IN1", year_before("14/03/2025"))
    mock_bill(aioclient_mock, "CN1", DEARER)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "2278.56"
    assert bill.attributes["issued"] == "2026-04-26"
    assert bill.attributes["period_start"] == "2024-02-15"
    assert bill.attributes["period_end"] == "2026-01-31"
    assert [b["invoice_type"] for b in bill.attributes["bills"]] == [
        "IntermediaryInvoice",
        "YearlyBill",
    ]
    assert len(bill.attributes["lines"]) == 22
    assert bill.attributes["lines"][0]["start"] == "2024-02-15"
    assert bill.attributes["last_error"] is None
    assert downloads(aioclient_mock, "CN1") == 0


async def test_a_bill_of_the_meter_beside_it_does_not_end_the_year(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The other meter's yearly bill is read before it can end the year.

    Two electricity meters at one address: the other one's yearly bill is
    listed between this one's yearly and intermediary bills.
    """
    mock_login(
        aioclient_mock,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    mock_discovery(
        aioclient_mock,
        points=[
            {"ean": EAN, "type": "Electricity"},
            {"ean": OTHER_EAN, "type": "Electricity"},
        ],
    )
    aioclient_mock.get(
        INVOICES,
        json={
            "data": {
                "invoices": [
                    invoice("CN2", "YearlyBill", "2026-04-26"),
                    invoice("NB1", "YearlyBill", "2025-06-01"),
                    invoice("IN1", "IntermediaryInvoice", "2025-04-01"),
                    invoice("CN1", "YearlyBill", "2024-02-20"),
                ]
            }
        },
    )
    mock_bill(aioclient_mock, "CN2", SHORT)
    mock_bill(aioclient_mock, "NB1", [row.replace(EAN, OTHER_EAN) for row in DEARER])
    mock_bill(aioclient_mock, "IN1", year_before("14/03/2025"))
    mock_bill(aioclient_mock, "CN1", DEARER)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "2278.56"
    assert [b["invoice_type"] for b in bill.attributes["bills"]] == [
        "IntermediaryInvoice",
        "YearlyBill",
    ]
    assert bill.attributes["last_error"] is None


async def test_an_intermediary_bill_after_the_year_waits(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """One newer than any yearly bill belongs to a year not billed yet."""
    mock_invoices(
        aioclient_mock,
        [
            invoice("IN2", "IntermediaryInvoice", "2026-07-01"),
            invoice("CN2", "YearlyBill", "2026-04-26"),
        ],
    )
    mock_bill(aioclient_mock, "IN2", month_bill(date(2026, 2, 1), date(2026, 6, 30)))
    mock_bill(aioclient_mock, "CN2", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.28"
    assert len(bill.attributes["bills"]) == 1
    assert bill.attributes["last_error"] is None
    assert downloads(aioclient_mock, "IN2") == 0


async def test_bills_that_do_not_follow_on_are_not_added(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A gap in a year is named, and the year before it shows."""
    mock_invoices(
        aioclient_mock,
        [
            invoice("CN2", "YearlyBill", "2026-04-26"),
            invoice("IN1", "IntermediaryInvoice", "2025-04-01"),
            invoice("CN1", "YearlyBill", "2024-02-20"),
        ],
    )
    mock_bill(aioclient_mock, "CN2", SHORT)
    mock_bill(aioclient_mock, "IN1", year_before("13/03/2025"))
    mock_bill(aioclient_mock, "CN1", DEARER)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.29"
    assert bill.attributes["issued"] == "2024-02-20"
    assert bill.attributes["last_error"] == (
        "the bills up to 2026-01-31 were not used: they start on 2025-03-15"
        " and the bill before them ends on 2025-03-13"
    )


async def test_intermediary_bills_alone_show_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A part of a year is not shown as if it were one."""
    mock_invoices(aioclient_mock, [invoice("IN1", "IntermediaryInvoice", "2025-04-01")])
    mock_bill(aioclient_mock, "IN1", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == STATE_UNKNOWN
    assert bill.attributes["lines"] == []
    assert bill.attributes["last_error"] == (
        "no yearly bill closes the intermediary ones yet"
    )


async def test_monthly_bills_make_a_year_of_twelve(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The last twelve are the year, and the one before is not fetched."""
    mock_invoices(aioclient_mock, mock_months(aioclient_mock, date(2025, 1, 1), 13))
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "202.8"
    assert bill.attributes["period_start"] == "2025-02-01"
    assert bill.attributes["period_end"] == "2026-01-31"
    assert len(bill.attributes["bills"]) == 12
    assert bill.attributes["last_error"] is None
    assert downloads(aioclient_mock, "M00") == 0


async def test_eleven_monthly_bills_are_not_a_year(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Fewer than twelve show nothing, and say how many there are."""
    mock_invoices(aioclient_mock, mock_months(aioclient_mock, date(2025, 1, 1), 11))
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == STATE_UNKNOWN
    assert bill.attributes["last_error"] == (
        "the bills up to 2025-11-30 were not used: 11 monthly bills follow on, not 12"
    )


async def test_monthly_bills_after_a_yearly_one(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Until twelve have come, the yearly bill before them is the year."""
    months = mock_months(aioclient_mock, date(2026, 2, 1), 3)
    mock_invoices(aioclient_mock, [*months, invoice("CN2", "YearlyBill", "2026-02-05")])
    mock_bill(aioclient_mock, "CN2", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    bill = state_of(hass, LAST_BILL)
    assert bill.state == "1139.28"
    assert [b["invoice_type"] for b in bill.attributes["bills"]] == ["YearlyBill"]
    assert bill.attributes["last_error"] == (
        "the bills up to 2026-04-30 were not used: 3 monthly bills follow on, not 12"
    )


async def test_the_invoices_are_listed_without_dates(
    hass: HomeAssistant, portal: AiohttpClientMocker
) -> None:
    """Every invoice is listed, however far back the year has to reach."""
    mock_bill(portal, "CN2", ELECTRICITY)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    queries = [
        dict(url.query)
        for _, url, _, _ in portal.mock_calls
        if str(url).startswith(INVOICES)
    ]
    assert queries == [{"ShowActiveContractsOnly": "false"}]


async def test_a_bill_stored_before_the_year_is_not_shown(
    hass: HomeAssistant, portal: AiohttpClientMocker, hass_storage: dict[str, Any]
) -> None:
    """A bill kept by an older version is not shown as the year it is not."""
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    hass_storage[BillCoordinator.key_for(entry.entry_id)] = {
        "version": 1,
        "minor_version": 1,
        "key": BillCoordinator.key_for(entry.entry_id),
        "data": {
            "meters": {
                METER_ID: {
                    "card": None,
                    "bill": {"invoice_type": "YearlyBill", "total": 1074.87},
                    "error": None,
                }
            },
            "seen": {},
        },
    }
    portal.clear_requests()
    mock_login(
        portal,
        accounts=[{"crmAccountNumber": ACCOUNT, "alias": "Home", "role": "owner"}],
    )
    portal.get(INVOICES, status=HTTPStatus.INTERNAL_SERVER_ERROR)
    portal.get(
        f"{API}/accounts/{ACCOUNT}/delivery-addresses/{ADDRESS}/service-delivery-points",
        json={"data": {"serviceDeliveryPoints": []}},
    )
    await setup(hass, entry)

    bill = state_of(hass, LAST_BILL)
    assert bill.state == STATE_UNKNOWN
    assert bill.attributes["last_error"] == "the portal returned HTTP 500"
    bills = entry.runtime_data.bills
    assert bills is not None
    assert "bill" not in bills.data[METER_ID]


async def test_a_link_off_the_website_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bill is only fetched from the website, whatever the list says."""
    session = aioclient_mock.create_session(hass.loop)
    client = EnecoClient(session, EMAIL, PASSWORD)
    try:
        with pytest.raises(EnecoError, match="not on the Eneco website"):
            await client.async_get_document("https://eneco.be.example/bill.pdf")
    finally:
        await session.close()
    assert aioclient_mock.call_count == 0


async def test_an_oversized_download_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Something far larger than a bill is not read into memory whole."""
    monkeypatch.setattr(api, "MAX_DOCUMENT_BYTES", 1000)
    aioclient_mock.get(document("CN2"), content=b"%PDF" + b"0" * 2000)
    session = aioclient_mock.create_session(hass.loop)
    client = EnecoClient(session, EMAIL, PASSWORD)
    try:
        with pytest.raises(EnecoError, match="too large"):
            await client.async_get_document(document("CN2"))
    finally:
        await session.close()


def price_entry(
    hass: HomeAssistant,
    domain: str = "be_electricity_prices",
    key: str = "eneco_bill",
    checks: str = LAST_BILL,
    state: ConfigEntryState = ConfigEntryState.LOADED,
) -> MockConfigEntry:
    """Add an entry of a price integration naming a bill sensor to check."""
    entry = MockConfigEntry(domain=domain, title="Power Flex", data={key: checks})
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    return entry


def bill_cost(
    hass: HomeAssistant, answer: Any, domain: str = "be_electricity_prices"
) -> list[ServiceCall]:
    """Register a price integration's bill_cost action answering ``answer``."""
    calls: list[ServiceCall] = []

    async def handler(call: ServiceCall) -> Any:
        calls.append(call)
        if isinstance(answer, Exception):
            raise answer
        return answer

    hass.services.async_register(
        domain, "bill_cost", handler, supports_response=SupportsResponse.ONLY
    )
    return calls


@pytest.mark.parametrize(
    ("domain", "key"),
    [("be_electricity_prices", "eneco_bill"), ("be_gas_prices", "bill_entity")],
)
async def test_the_year_is_held_against_the_entry_checking_it(
    hass: HomeAssistant, portal: AiohttpClientMocker, domain: str, key: str
) -> None:
    """The price entry naming the bill sensor prices the year's days."""
    mock_bill(portal, "CN2", ELECTRICITY)
    entry = price_entry(hass, domain, key)
    calls = bill_cost(hass, {"cost": 1100.0, "covered": True}, domain)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    difference = state_of(hass, BILL_DIFFERENCE)
    assert difference.state == "39.28"
    assert difference.attributes["unit_of_measurement"] == "EUR"
    assert difference.attributes["expected"] == 1100.0
    assert difference.attributes["covered"] is True
    assert difference.attributes["price_entry"] == "Power Flex"
    assert difference.attributes["last_error"] is None
    assert [dict(call.data) for call in calls] == [
        {"entry_id": entry.entry_id, "start": "2025-02-01", "end": "2026-01-31"}
    ]


@pytest.mark.parametrize(
    ("link", "answer", "error", "expected"),
    [
        (None, None, "no electricity or gas price entry checks this bill", None),
        ("other", None, "no electricity or gas price entry checks this bill", None),
        ("unloaded", None, "no electricity or gas price entry checks this bill", None),
        ("bill", None, "Power Flex cannot price a bill: update it", None),
        (
            "bill",
            {"cost": 1100.0, "covered": False},
            "the meter's history does not cover every day of the bill",
            1100.0,
        ),
        (
            "bill",
            HomeAssistantError("the recorder is busy"),
            "Power Flex did not price the bill: the recorder is busy",
            None,
        ),
        ("bill", {"cost": None}, "Power Flex priced nothing for the bill", None),
        ("bill", {"cost": True}, "Power Flex priced nothing for the bill", None),
    ],
)
async def test_a_year_not_priced_says_why(
    hass: HomeAssistant,
    portal: AiohttpClientMocker,
    link: str | None,
    answer: Any,
    error: str,
    expected: float | None,
) -> None:
    """No difference is shown unless every day of the year was priced."""
    mock_bill(portal, "CN2", ELECTRICITY)
    if link == "other":
        price_entry(hass, checks="sensor.something_else")
    elif link == "unloaded":
        price_entry(hass, state=ConfigEntryState.NOT_LOADED)
        bill_cost(hass, {"cost": 1100.0, "covered": True})
    elif link == "bill":
        price_entry(hass)
        if answer is not None:
            bill_cost(hass, answer)
    await setup(hass, bills_entry(hass, **{CONF_READ_BILLS: True}))

    difference = state_of(hass, BILL_DIFFERENCE)
    assert difference.state == STATE_UNKNOWN
    assert difference.attributes["last_error"] == error
    assert difference.attributes["expected"] == expected
    # The year itself still shows.
    assert state_of(hass, LAST_BILL).state == "1139.28"


async def test_the_difference_goes_with_the_other_bill_sensors(
    hass: HomeAssistant,
    portal: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
) -> None:
    """Turning bills off takes the difference sensor away too."""
    mock_bill(portal, "CN2", ELECTRICITY)
    entry = bills_entry(hass, **{CONF_READ_BILLS: True})
    await setup(hass, entry)
    assert entity_registry.async_get(BILL_DIFFERENCE) is not None

    hass.config_entries.async_update_entry(entry, options={CONF_READ_BILLS: False})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entity_registry.async_get(BILL_DIFFERENCE) is None

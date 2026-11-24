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

"""The settlement bills and tariff cards of every meter of a login.

Optional and off unless the entry's options turn it on. Once a day the
invoices of every address are listed, and the newest settlement bill of each
meter is read if it was not read already. A bill is only taken for a meter
whose EAN it prints, and only as a whole (see bill.py).

What was read is kept in a file of its own, along with which invoices were
looked at and what they held, so that a PDF is downloaded once and a restart
or a day the portal is down shows the last bill rather than nothing.
"""

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, timedelta
import logging
import re
from typing import Any, Final, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    EnecoAuthError,
    EnecoClient,
    EnecoError,
    Invoice,
    ServiceDeliveryPoint,
    TariffCard,
)
from .bill import BillFormatError, BillLine, parse_bill, pdf_text
from .const import CONF_ACCOUNT, CONF_ADDRESS, CONF_EAN, CONF_ENERGY_TYPE, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1

# Bills come once a year, or once a month for a meter billed monthly.
REFRESH_INTERVAL: Final = timedelta(hours=24)
# Wide enough to always hold the last yearly bill.
INVOICE_WINDOW: Final = timedelta(days=730)

# The invoices that bill what was consumed, as opposed to advances, credit
# notes and reminders.
SETTLEMENT: Final = frozenset(
    {"YearlyBill", "ClosingBill", "IntermediaryInvoice", "MonthlyBill"}
)

# "BC_032_012210_NL_ENECO_POWER_FLEX.pdf": issue 01 of the card of 2022-10.
_CARD_RE: Final = re.compile(r"BC_\d+_(?P<version>\d{2}(?P<yy>\d{2})(?P<mm>\d{2}))_")
_EAN_RE: Final = re.compile(r"(?<!\d)54\d{16}(?!\d)")


def _not_read(invoice: Invoice, reason: str) -> str:
    """Return why a bill was not taken, naming it by the day it was issued."""
    return f"the bill of {invoice.issued} was not read: {reason}"


def card_state(card: TariffCard) -> dict[str, Any]:
    """Return what a meter's sensor shows of its tariff card."""
    match = next(
        (found for url in card.urls.values() if (found := _CARD_RE.search(url))),
        None,
    )
    return {
        "version": match["version"] if match else None,
        "month": f"20{match['yy']}-{match['mm']}" if match else None,
        "product": card.product,
        "product_type": card.product_type,
        "urls": dict(card.urls),
    }


def bill_state(invoice: Invoice, lines: tuple[BillLine, ...]) -> dict[str, Any]:
    """Return what a meter's sensor shows of a bill."""
    return {
        "invoice": invoice.identifier,
        "invoice_type": invoice.kind,
        "issued": invoice.issued.isoformat() if invoice.issued else None,
        "period_start": min(line.start for line in lines).isoformat(),
        "period_end": max(line.end for line in lines).isoformat(),
        "total": round(sum(line.total for line in lines), 2),
        "lines": [line.as_dict() for line in lines],
    }


class BillCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Read the bills of every meter of one login, once a day.

    The data is keyed by meter subentry id, each holding its ``card``, its
    ``bill`` and the ``error`` that kept the newest bill from being read.
    """

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EnecoClient
    ) -> None:
        """Initialise the coordinator without contacting the portal."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} bills",
            update_interval=REFRESH_INTERVAL,
        )
        self.client = client
        self.data = {}
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, self.key_for(entry.entry_id)
        )
        # Invoice id -> the EANs it prints, and its lines or why it could not
        # be read. Kept for the invoices still listed, so a PDF is read once.
        self._seen: dict[str, dict[str, Any]] = {}

    @staticmethod
    def key_for(entry_id: str) -> str:
        """Return the storage key of an entry's bills."""
        return f"{DOMAIN}.{entry_id}.bills"

    @staticmethod
    async def async_remove_for(hass: HomeAssistant, entry_id: str) -> None:
        """Delete the file along with the entry."""
        store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, BillCoordinator.key_for(entry_id)
        )
        await store.async_remove()

    async def async_load(self, meters: Mapping[str, Mapping[str, Any]]) -> None:
        """Restore what the last run read, for the meters the entry has."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        kept = stored.get("meters")
        seen = stored.get("seen")
        self.data = {
            key: value
            for key, value in (kept if isinstance(kept, dict) else {}).items()
            if key in meters and isinstance(value, dict)
        }
        self._seen = seen if isinstance(seen, dict) else {}

    def _meters(self) -> dict[str, Mapping[str, Any]]:
        """Return the meters of the entry, by subentry id."""
        return {
            subentry_id: subentry.data
            for subentry_id, subentry in self.config_entry.subentries.items()
            if CONF_EAN in subentry.data
        }

    @override
    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """List the invoices of every address and read what is new."""
        today = dt_util.now().date()
        groups: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
        meters = self._meters()
        for subentry_id, meter in meters.items():
            groups[(meter[CONF_ACCOUNT], meter[CONF_ADDRESS])].append(subentry_id)

        data: dict[str, dict[str, Any]] = {}
        listed: set[str] = set()
        # Whether every address was listed, without which the cache of what
        # was read cannot tell an invoice gone from one not listed today.
        complete = True
        try:
            for (account, address), subentry_ids in groups.items():
                try:
                    points, invoices = await self._async_list(account, address, today)
                except EnecoAuthError:
                    raise
                except EnecoError as err:
                    complete = False
                    for subentry_id in subentry_ids:
                        data[subentry_id] = self._kept(subentry_id, str(err))
                    continue
                listed.update(invoice.identifier for invoice in invoices)
                for subentry_id in subentry_ids:
                    meter = meters[subentry_id]
                    try:
                        data[subentry_id] = await self._async_meter(
                            account, meter, invoices, points.get(meter[CONF_EAN])
                        )
                    except EnecoAuthError:
                        raise
                    except EnecoError as err:
                        complete = False
                        data[subentry_id] = self._kept(subentry_id, str(err))
        except EnecoAuthError as err:
            raise UpdateFailed(str(err)) from err

        if complete:
            self._seen = {
                key: value for key, value in self._seen.items() if key in listed
            }
        await self._store.async_save({"meters": data, "seen": self._seen})
        return data

    def _kept(self, subentry_id: str, error: str) -> dict[str, Any]:
        """Return what was read for a meter before, with why it was not now."""
        previous = self.data.get(subentry_id) or {"card": None, "bill": None}
        return {**previous, "error": error}

    async def _async_list(
        self, account: str, address: str, today: date
    ) -> tuple[dict[str, ServiceDeliveryPoint], list[Invoice]]:
        """Return the meters of an address and its settlement bills, newest first."""
        points = {
            point.ean: point
            for point in await self.client.async_get_delivery_points(account, address)
        }
        invoices = [
            invoice
            for invoice in await self.client.async_get_invoices(
                account, address, today - INVOICE_WINDOW, today
            )
            if invoice.kind in SETTLEMENT and invoice.document_url
        ]
        invoices.sort(key=lambda i: i.issued or today, reverse=True)
        return points, invoices

    async def _async_meter(
        self,
        account: str,
        meter: Mapping[str, Any],
        invoices: list[Invoice],
        point: ServiceDeliveryPoint | None,
    ) -> dict[str, Any]:
        """Return one meter's card and the newest of its bills that reads.

        A newer bill that was refused, or would not download, is named in the
        error, and the one before it is shown: its dates say which it is. One
        that would not download is not remembered, so it is tried again the
        next day; one that was refused is, and is not fetched again.
        """
        card = None
        if point is not None and point.contract_id:
            card = await self.client.async_get_tariff_card(account, point.contract_id)
        bill = None
        error = None
        ean = meter[CONF_EAN]
        for invoice in invoices:
            if invoice.energy_type not in (meter[CONF_ENERGY_TYPE], "Both"):
                continue
            seen = self._seen.get(invoice.identifier)
            if seen is None:
                try:
                    seen = await self._async_read(invoice)
                except EnecoAuthError:
                    raise
                except EnecoError as err:
                    error = error or _not_read(invoice, str(err))
                    continue
                self._seen[invoice.identifier] = seen
            # Unknown EANs: a PDF that could not be read at all may be ours.
            if seen["eans"] is not None and ean not in seen["eans"]:
                continue
            if seen.get("error") or seen.get("bill") is None:
                error = error or _not_read(invoice, seen.get("error") or "no lines")
                continue
            bill = seen["bill"]
            break
        return {
            "card": card_state(card) if card is not None else None,
            "bill": bill,
            "error": error,
        }

    async def _async_read(self, invoice: Invoice) -> dict[str, Any]:
        """Download and read one bill: the EANs it prints, its lines or why not.

        A bill printing the EAN of more than one meter is refused: its lines
        are not split by meter, and handing them all to each would bill the
        same day twice.
        """
        if not invoice.document_url:
            return {"eans": [], "error": None, "bill": None}
        payload = await self.client.async_get_document(invoice.document_url)
        try:
            text = await self.hass.async_add_executor_job(pdf_text, payload)
        except BillFormatError as err:
            _LOGGER.warning("Could not read the bill of %s: %s", invoice.issued, err)
            return {"eans": None, "error": str(err), "bill": None}
        eans = sorted(set(_EAN_RE.findall(text)))
        if len(eans) > 1:
            return {
                "eans": eans,
                "error": "the bill covers more than one meter",
                "bill": None,
            }
        try:
            lines = parse_bill(text)
        except BillFormatError as err:
            _LOGGER.warning("Could not read the bill of %s: %s", invoice.issued, err)
            return {"eans": eans, "error": str(err), "bill": None}
        return {"eans": eans, "error": None, "bill": bill_state(invoice, lines)}

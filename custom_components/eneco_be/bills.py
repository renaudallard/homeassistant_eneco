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
invoices of every address are listed, and each meter is shown its last full
year: its newest yearly or closing bill with the intermediary bills of the
same year, or its last twelve monthly bills, each read if it was not read
already. A bill is only taken for a meter whose EAN it prints, and only as a
whole (see bill.py).

What was read is kept in a file of its own, along with which invoices were
looked at and what they held, so that a PDF is downloaded once and a restart
or a day the portal is down shows the last year rather than nothing.
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

from .advance_state import ADVANCE, address_state, advance_months, settled_by
from .advice import address_proposal, no_proposal
from .api import EnecoAuthError, EnecoClient, EnecoError, ServiceDeliveryPoint
from .api_billing import AdvanceAmounts, Invoice
from .bill import BillFormatError, parse_advance, parse_bill, pdf_text
from .bill_compare import RatesUnavailableError, async_compare, async_energy_rates
from .bill_year import (
    INTERMEDIARY,
    MONTHLY,
    MONTHS,
    SETTLEMENT,
    bill_state,
    card_state,
    follows,
    not_read,
    not_used,
    ours,
    pass_over,
    settlement,
    too_few,
    year_state,
)
from .const import (
    CONF_ACCOUNT,
    CONF_ADDRESS,
    CONF_EAN,
    CONF_ENERGY_TYPE,
    DOMAIN,
    GAS,
    LAST_BILL,
)
from .provision import (
    ProvisionError,
    advances,
    catch_up,
    period_months,
    profile_range,
    project,
    reading_end,
    year_before,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1

# Bills come once a year, or once a month for a meter billed monthly.
REFRESH_INTERVAL: Final = timedelta(hours=24)
# How far back an advance invoice for both energies is read to split it: a
# billing period, and a little more for one billed late.
ADVANCE_DAYS: Final = 400

_EAN_RE: Final = re.compile(r"(?<!\d)54\d{16}(?!\d)")


class BillCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Read the bills of every meter of one login, once a day.

    The data is keyed by meter subentry id, each holding its ``card``, its
    last full ``year``, the ``error`` that kept a newer one from showing and
    the ``comparison`` of the year with what a price integration prices it at.
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
        # Advance invoice id -> each energy's share of it, empty when the PDF
        # did not give a split that adds up. Read once, like the bills.
        self._advances: dict[str, dict[str, float]] = {}
        # Delivery address -> what the portal says of its monthly advance.
        self.addresses: dict[str, dict[str, Any]] = {}

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
        read = stored.get("advances")
        addresses = stored.get("addresses")
        self.data = {
            key: value
            for key, value in (kept if isinstance(kept, dict) else {}).items()
            if key in meters and isinstance(value, dict)
        }
        self._seen = seen if isinstance(seen, dict) else {}
        self._advances = read if isinstance(read, dict) else {}
        wanted = {meter.get(CONF_ADDRESS) for meter in meters.values()}
        self.addresses = {
            key: value
            for key, value in (addresses if isinstance(addresses, dict) else {}).items()
            if key in wanted and isinstance(value, dict)
        }

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
        # What is shown and set is kept by address: one that two accounts of
        # the login share would show one of them and set the other's.
        accounts: defaultdict[str, set[str]] = defaultdict(set)
        for account, address in groups:
            accounts[address].add(account)

        data: dict[str, dict[str, Any]] = {}
        listed: set[str] = set()
        # Whether every address was listed, without which the cache of what
        # was read cannot tell an invoice gone from one not listed today.
        complete = True
        try:
            for (account, address), subentry_ids in groups.items():
                portal: AdvanceAmounts | str
                try:
                    points, every = await self._async_list(account, address)
                except EnecoAuthError:
                    raise
                except EnecoError as err:
                    complete = False
                    for subentry_id in subentry_ids:
                        data[subentry_id] = self._kept(subentry_id, str(err))
                    # The address stands as last read, with why, and so
                    # nothing is proposed from it.
                    portal = f"the invoices were not listed: {err}"
                else:
                    invoices = settlement(every, today)
                    listed.update(invoice.identifier for invoice in every)
                    portal = await self._async_advance_amounts(account, address)
                    await self._async_read_advances(every, today)
                    for subentry_id in subentry_ids:
                        meter = meters[subentry_id]
                        kind = meter[CONF_ENERGY_TYPE]
                        # The portal's meters of the energy at the address,
                        # or, where it lists none, those set up here: one
                        # set up but not listed is no meter there any more.
                        on_portal = sum(p.energy_type == kind for p in points.values())
                        here = sum(
                            meters[s][CONF_ENERGY_TYPE] == kind for s in subentry_ids
                        )
                        shared = (on_portal or here) > 1
                        point = points.get(meter[CONF_EAN])
                        before = self.data.get(subentry_id) or {}
                        previous = before.get("provision")
                        try:
                            read = await self._async_meter(
                                account, meter, invoices, point, shared=shared
                            )
                            if read["card_error"]:
                                # A card does not change because it was not
                                # fetched again.
                                read["card"] = before.get("card")
                            read["provision"] = await self._async_provision(
                                account,
                                address,
                                meter,
                                point,
                                read.get("year"),
                                every,
                                portal,
                                today,
                                previous,
                                shared=shared,
                            )
                        except EnecoAuthError:
                            raise
                        except EnecoError as err:
                            complete = False
                            read = self._kept(subentry_id, str(err))
                        data[subentry_id] = read
                self.addresses[address] = address_state(
                    portal, self.addresses.get(address)
                )
                self.addresses[address]["proposal"] = (
                    no_proposal("two accounts of the login share the address")
                    if len(accounts[address]) > 1
                    else address_proposal(
                        self.addresses[address],
                        {
                            meters[key][CONF_EAN]: (data[key].get("provision") or {})
                            | {"energy": meters[key][CONF_ENERGY_TYPE]}
                            for key in subentry_ids
                            if key in data
                        },
                        today,
                    )
                )
        except EnecoAuthError as err:
            # A login whose meters submit nothing only signs in here, and
            # nothing else would ask for a new password. Only for a refusal
            # of the password: any other failure passes by the next read.
            if self.client.refused:
                self.config_entry.async_start_reauth(self.hass)
            raise UpdateFailed(str(err)) from err

        # What the price integration checking each meter's bill prices the
        # year at, asked afresh with every read: its cards and history move.
        for subentry_id, read in data.items():
            year = read.get("year")
            read["comparison"] = (
                await async_compare(
                    self.hass, f"{meters[subentry_id][CONF_EAN]}_{LAST_BILL}", year
                )
                if year
                else None
            )

        if complete:
            self._seen = {
                key: value for key, value in self._seen.items() if key in listed
            }
            self._advances = {
                key: value for key, value in self._advances.items() if key in listed
            }
        await self._store.async_save(
            {
                "meters": data,
                "seen": self._seen,
                "advances": self._advances,
                "addresses": self.addresses,
            }
        )
        return data

    def _kept(self, subentry_id: str, error: str) -> dict[str, Any]:
        """Return what was read for a meter before, with why it was not now.

        The advance it needed stands with the error too, so it is shown and
        not proposed.
        """
        previous = self.data.get(subentry_id) or {}
        provision = previous.get("provision")
        return {
            "card": previous.get("card"),
            "card_error": error,
            "year": previous.get("year"),
            "provision": None if provision is None else provision | {"error": error},
            "error": error,
        }

    async def _async_list(
        self, account: str, address: str
    ) -> tuple[dict[str, ServiceDeliveryPoint], list[Invoice]]:
        """Return the meters of an address and all its invoices."""
        points = {
            point.ean: point
            for point in await self.client.async_get_delivery_points(account, address)
        }
        return points, await self.client.async_get_invoices(account, address)

    async def async_apply_advance(self, address: str) -> list[str]:
        """Set on the portal the advances proposed at an address.

        Read again first, so what is set is worked out from today's data, and
        once more after, so the sensors show what the portal now bills.
        Returns the energies changed; raises EnecoError when nothing may be,
        or when one is refused, naming those set before it.
        """
        await self.async_refresh()
        if not self.last_update_success:
            raise EnecoError("the portal could not be read again, so nothing was set")
        proposal = (self.addresses.get(address) or {}).get("proposal") or {}
        if proposal.get("error"):
            raise EnecoError(proposal["error"])
        changes = proposal.get("changes") or []
        account = next(
            (
                meter[CONF_ACCOUNT]
                for meter in self._meters().values()
                if meter[CONF_ADDRESS] == address
            ),
            None,
        )
        if account is None:
            raise EnecoError("no meter is set up at the address any more")
        done: list[str] = []
        for ean in changes:
            amount = proposal["amounts"][ean]
            energy = proposal["energies"][ean]
            try:
                await self.client.async_set_advance(account, address, ean, amount)
            except EnecoError as err:
                if not done:
                    raise
                await self.async_refresh()
                raise EnecoError(
                    f"the {' and '.join(done).lower()} advance was set,"
                    f" but not the {energy.lower()} one: {err}"
                ) from err
            _LOGGER.info("Set the %s advance to %.2f EUR", energy.lower(), amount)
            done.append(energy)
        if done:
            await self.async_refresh()
        return done

    async def _async_advance_amounts(
        self, account: str, address: str
    ) -> AdvanceAmounts | str:
        """Return the portal's advances of an address, or why there are none."""
        try:
            got = await self.client.async_get_advance_amounts(account, address)
        except EnecoAuthError:
            raise
        except EnecoError as err:
            return f"the advances were not read: {err}"
        return got if got is not None else "the portal lists no advances"

    async def _async_read_advances(self, every: list[Invoice], today: date) -> None:
        """Read the split of each recent advance invoice for both energies.

        One that would not download is not remembered, so it is tried again
        the next day.
        """
        since = today - timedelta(days=ADVANCE_DAYS)
        for invoice in every:
            if (
                invoice.kind != ADVANCE
                or invoice.energy_type != "Both"
                or not invoice.document_url
                or invoice.identifier in self._advances
                or invoice.due is None
                or invoice.period_start is None
                or invoice.period_start < since
            ):
                continue
            try:
                payload = await self.client.async_get_document(invoice.document_url)
            except EnecoAuthError:
                raise
            except EnecoError as err:
                _LOGGER.debug("Advance of %s not downloaded: %s", invoice.issued, err)
                continue
            try:
                text = await self.hass.async_add_executor_job(pdf_text, payload)
            except BillFormatError:
                # Downloaded but unreadable: it will not read better tomorrow.
                text = ""
            self._advances[invoice.identifier] = parse_advance(text, invoice.due)

    async def _async_meter(
        self,
        account: str,
        meter: Mapping[str, Any],
        invoices: list[Invoice],
        point: ServiceDeliveryPoint | None,
        *,
        shared: bool = False,
    ) -> dict[str, Any]:
        """Return one meter's card and its last full year of bills.

        The year is the one Eneco billed: the newest yearly or closing bill,
        however many days the meter readings put in it, with the intermediary
        bills before it, each ending the day before the next one starts. On
        monthly billing it is the last MONTHS monthly bills. An intermediary
        bill newer than any yearly one is part of a year not billed yet.

        A bill that was refused or would not download is named in the error
        and passed over: if it was needed, the bills around it leave a gap. A
        gap or an overlap is named too, and the year is made of the bills
        before it: its dates say which it is. One that would not download is
        not remembered, so it is tried again the next day; one that was
        refused is, and is not fetched again.

        A bill's kind is told from the list, so the bill of the year before
        ends the year unread. Where the address has another meter of the
        same energy (``shared``), each bill is read first, since only its EAN
        says whose it is.
        """
        card = None
        card_error = None
        if point is not None and point.contract_id:
            try:
                card = await self.client.async_get_tariff_card(
                    account, point.contract_id
                )
            except EnecoAuthError:
                raise
            except EnecoError as err:
                # Only its own sensor shows it: the bills go on without.
                card_error = f"the tariff card was not read: {err}"
        # The bills making up the year, newest first.
        run: list[dict[str, Any]] = []
        error = None
        # Whether an intermediary bill was passed over for want of a year.
        waiting = False
        ean = meter[CONF_EAN]
        # Where two meters share the energy, a bill that cannot be read, or
        # was not downloaded, may be either's, so once a yearly bill has
        # begun the year it is passed over. One ending the day the year
        # begins may be what closes it: a bill of this meter not following on
        # then shows it was. An intermediary ending then, or given no days,
        # may be missing from it, and is named unless one of this meter's
        # joins the year after it.
        closes: Invoice | None = None
        missing: tuple[Invoice, str] | None = None
        for invoice in invoices:
            if invoice.energy_type not in (meter[CONF_ENERGY_TYPE], "Both"):
                continue
            seen = self._seen.get(invoice.identifier)
            if seen is None and shared:
                seen, failed = await self._async_whose(invoice)
                error = error or failed
            # Which meter's it is cannot be told: named, it says so.
            unsure = shared and seen is not None and seen["eans"] is None
            if unsure and seen is not None:
                skip, closes, missing = pass_over(
                    invoice, seen.get("error") or "unread", run, closes, missing
                )
                if skip:
                    continue
            if seen is not None and not ours(seen, ean):
                continue
            monthly = bool(run) and run[0]["invoice_type"] == MONTHLY
            if run and not monthly and invoice.kind != INTERMEDIARY:
                # The bill of the year before, told from the list unread.
                break
            if monthly and invoice.kind != MONTHLY:
                error = error or too_few(run)
                run = []
            if not run and invoice.kind == INTERMEDIARY:
                waiting = True
                continue
            if seen is None:
                got = await self._async_seen(invoice)
                if isinstance(got, str):
                    error = error or got
                    continue
                seen = got
                if not ours(seen, ean):
                    continue
            if seen.get("error") or seen.get("bill") is None:
                why = seen.get("error") or "no lines"
                error = error or not_read(
                    invoice, f"{why}, so it may be another meter's" if unsure else why
                )
                continue
            bill = seen["bill"]
            if run and not follows(bill, run[-1]) and closes is not None:
                # The bill that could not be read closed the year.
                break
            if run and not follows(bill, run[-1]):
                error = error or not_used(
                    run,
                    f"they start on {run[-1]['period_start']} and the bill"
                    f" before them ends on {bill['period_end']}",
                )
                run = []
                if invoice.kind == INTERMEDIARY:
                    continue
            # What it asked for is taken from today's list, not from what was
            # read of it, which a bill read before this was known lacks.
            run.append({**bill, "due": invoice.due})
            closes = None
            if missing is not None and missing[0].period_end is not None:
                # Ending where the year then began, it was not this meter's.
                # One with no days says nothing of where it falls.
                missing = None
            if run[0]["invoice_type"] == MONTHLY and len(run) == MONTHS:
                break
        else:
            if run and run[0]["invoice_type"] == MONTHLY:
                error = error or too_few(run)
                run = []
            if not run and waiting:
                error = error or "no yearly bill closes the intermediary ones yet"
        if missing is not None and run:
            error = error or not_read(
                missing[0], f"{missing[1]}, so it may be another meter's"
            )
        return {
            "card": card_state(card) if card is not None else None,
            "card_error": card_error,
            "year": year_state(run) if run else None,
            "error": error,
        }

    async def _async_provision(
        self,
        account: str,
        address: str,
        meter: Mapping[str, Any],
        point: ServiceDeliveryPoint | None,
        year: dict[str, Any] | None,
        every: list[Invoice],
        portal: AdvanceAmounts | str,
        today: date,
        previous: dict[str, Any] | None = None,
        *,
        shared: bool = False,
    ) -> dict[str, Any]:
        """Return the monthly advance the meter needs, or why it cannot be told.

        Worked out from the newest bill of the year shown, the official
        readings that opened and closed it, the newest reading since, the
        portal's monthly profile, and the advances invoiced. A portal that
        does not answer leaves ``previous`` standing, with why. ``shared``
        is another meter of the same energy at the address.
        """
        if year is None:
            return {"error": "no yearly bill to work from"}
        newest = year["bills"][-1]
        if newest["invoice_type"] == MONTHLY:
            return {"error": "billed monthly, with no advance"}
        ean = meter[CONF_EAN]
        kind = meter[CONF_ENERGY_TYPE]
        contract = (
            None
            if isinstance(portal, str)
            else next((c for c in portal.contracts if c.ean == ean), None)
        )
        first = date.fromisoformat(newest["period_start"])
        last = date.fromisoformat(newest["period_end"])
        start = last + timedelta(days=1)
        if any(
            i.kind in SETTLEMENT - {INTERMEDIARY, MONTHLY}
            and i.energy_type in (kind, "Both")
            and i.period_start is not None
            and i.period_start >= start
            and ours(self._seen.get(i.identifier) or {"eans": None}, ean)
            for i in every
        ):
            # The year shown is not this meter's newest billed, one was
            # refused or not downloaded: the period after it is over. One
            # read and printing another EAN is another meter's.
            return {"error": "a newer bill than the year shown was not read"}
        # What stands of this period on a day part of it cannot be read; an
        # amount of the period before, a new bill read today, is over.
        kept = (
            previous
            if previous and previous.get("period_start") == start.isoformat()
            else {}
        )
        if isinstance(portal, str) and kept.get("monthly_advance") is not None:
            # The page gives the month of the reading that ends the period,
            # and the advances paid: worked out again without it, the
            # period would move for a day. The amount of this period stands.
            return kept | {"error": portal}
        end, end_basis = reading_end(
            start, contract.reading_month if contract else None
        )
        lines = [
            line for line in year["lines"] if line["start"] >= newest["period_start"]
        ]
        what = "the readings were"
        try:
            readings = await self.client.async_get_readings(account, ean)
            what = "the consumption profile was"
            profile = (
                {}
                if point is None
                else await self.client.async_get_monthly_volumes(
                    account,
                    address,
                    point,
                    # The last period, and the same days a year back as this
                    # one, which reach before it when it was shorter than a
                    # year and past it when this one ends later.
                    *profile_range(
                        min(first, year_before(start)), max(last, year_before(end))
                    ),
                )
            )
        except EnecoAuthError:
            raise
        except EnecoError as err:
            return kept | {"error": f"{what} not read: {err}"}
        official = {r.date: r.values for r in readings if r.reading_type == "Official"}
        opened = official.get(first.isoformat())
        closed = official.get(start.isoformat())
        if opened is None or closed is None:
            return {"error": "no official reading at both ends of the last bill"}
        # Not one past the period's end, the day after its last day: once the
        # period is over and not billed yet, the days after it are the next
        # period's, and the reading that closes it is the one to use.
        closing = (end + timedelta(days=1)).isoformat()
        since = [r for r in readings if r.date <= closing]
        # Of every register the bill closed on: a meter with one unmapped
        # sends readings of the others only.
        newest_reading = next(
            (r for r in since if r.values.keys() == closed.keys()), None
        )
        if newest_reading is None:
            return {"error": "no reading since the last bill"}
        # The reading closing the bill names them all. Fallen back on while
        # newer ones say otherwise, it would count nothing used since.
        if newest_reading.date == start.isoformat() < since[0].date:
            return {
                "error": "the readings since the last bill do not name every register"
            }
        try:
            read_on = date.fromisoformat(newest_reading.date)
        except ValueError:
            return {"error": "the newest reading has no date"}
        rates = None
        if kind == GAS:
            try:
                rates = await async_energy_rates(
                    self.hass, f"{ean}_{LAST_BILL}", start.isoformat(), end.isoformat()
                )
            except RatesUnavailableError as err:
                # Not the bill's price instead: on an indexed contract it can
                # be far below today's, and the advance set far too low.
                return kept | {"error": f"the gas prices were not read: {err}"}
        try:
            result = project(
                energy_type=kind,
                lines=lines,
                bill_period=(first, last),
                bill_readings=(opened, closed),
                latest=(read_on, newest_reading.values),
                profile=profile,
                rates=rates,
                period_end=end,
            )
        except ProvisionError as err:
            return {"error": str(err)}

        advance_now = None if contract is None else contract.current
        share = None
        if not isinstance(portal, str) and advance_now is not None and portal.current:
            share = advance_now / portal.current
        months, estimated, missing = advance_months(
            kind, every, self._advances, share, shared=shared
        )
        paid, coming, left = advances(
            period=(start, end), billed=months, advance_now=advance_now, today=today
        )
        in_period = set(period_months(start, end))
        split = (
            "incomplete"
            if missing & in_period
            else "estimated"
            if estimated & in_period
            else "invoice"
        )
        settled, unsettled = settled_by(every, kind, start, end, shared=shared)
        expected = None
        if coming is not None and split != "incomplete" and settled is not None:
            expected = round(result["period_cost"] - paid - coming - settled, 2)
        # Until the reading, what brings the regularisation to about 0. With
        # one advance left the whole gap would fall on it, too late to set and
        # swinging with each reading, as the portal's own advice does in the
        # reading month: a year like this one for the next period instead.
        error = None
        if left <= 1:
            needed = result["after_reading"]
        elif split == "incomplete":
            needed = None
            error = (
                "the advances invoiced are not told apart between meters of one energy"
                if shared
                else "the advances invoiced could not all be split by energy"
            )
        elif settled is None:
            needed = None
            error = unsettled
        else:
            needed = catch_up(result["period_cost"] - settled, paid, left)
        difference = None
        if advance_now is not None and needed is not None:
            difference = round(needed - advance_now, 2)
        return result | {
            "monthly_advance": needed,
            "period_end_basis": end_basis,
            "advance_now": advance_now,
            "difference": difference,
            "advances_billed": paid,
            "settled_by_bills": None if settled is None else round(settled, 2),
            "advances_to_come": coming,
            "advances_left": left,
            "advance_split": split,
            "expected_regularisation": expected,
            "portal_min": None if contract is None else contract.minimum,
            "portal_max": None if contract is None else contract.maximum,
            # Worked out without the advances page, nothing of the period
            # standing: without its reading month or today's advance.
            "error": error or (portal if isinstance(portal, str) else None),
        }

    async def _async_whose(self, invoice: Invoice) -> tuple[dict[str, Any], str | None]:
        """Read a bill to tell whose it is, and why it did not download.

        One that did not download stands as one that would not read: whose
        it is is not known.
        """
        got = await self._async_seen(invoice)
        if isinstance(got, str):
            return {"eans": None, "error": got, "bill": None}, got
        return got, None

    async def _async_seen(self, invoice: Invoice) -> dict[str, Any] | str:
        """Return what an invoice held, read once, or why it did not download.

        One that did not download is not remembered, so it is tried again
        the next day.
        """
        seen = self._seen.get(invoice.identifier)
        if seen is None:
            try:
                seen = await self._async_read(invoice)
            except EnecoAuthError:
                raise
            except EnecoError as err:
                return not_read(invoice, str(err))
            # One with no document may be given one later.
            if invoice.document_url:
                self._seen[invoice.identifier] = seen
        return seen

    async def _async_read(self, invoice: Invoice) -> dict[str, Any]:
        """Download and read one bill: the EANs it prints, its lines or why not.

        A bill printing the EAN of more than one meter is refused: its lines
        are not split by meter, and handing them all to each would bill the
        same day twice.
        """
        if not invoice.document_url:
            # Whose it is cannot be told, and so it may be the meter's.
            return {"eans": None, "error": "it has no document", "bill": None}
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

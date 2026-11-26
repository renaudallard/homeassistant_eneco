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

"""How a meter's last full year is told from its bills.

What the invoice list says of the settlement bills, and the year a run of
them, newest first, makes. Nothing here does I/O: bills.py reads them.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
import re
from typing import Any, Final

from .api_billing import Invoice, TariffCard
from .bill import BillLine

_CENT: Final = Decimal("0.01")

# The invoices that bill what was consumed, as opposed to advances, credit
# notes and reminders.
SETTLEMENT: Final = frozenset(
    {"YearlyBill", "ClosingBill", "IntermediaryInvoice", "MonthlyBill"}
)
# A meter read between two yearly ones, on a product change for one, bills
# part of the year that the yearly bill after it closes.
INTERMEDIARY: Final = "IntermediaryInvoice"
MONTHLY: Final = "MonthlyBill"
# The monthly bills that make a year, where no yearly bill closes one.
MONTHS: Final = 12

# "BC_032_012210_NL_ENECO_POWER_FLEX.pdf": issue 01 of the card of 2022-10.
_CARD_RE: Final = re.compile(r"BC_\d+_(?P<version>\d{2}(?P<yy>\d{2})(?P<mm>\d{2}))_")


def not_read(invoice: Invoice, reason: str) -> str:
    """Return why a bill was not taken, naming it by the day it was issued."""
    name = f"the bill of {invoice.issued}" if invoice.issued else "an undated bill"
    return f"{name} was not read: {reason}"


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
    """Return what was read of a bill: when, over which days, and its lines."""
    return {
        "invoice": invoice.identifier,
        "invoice_type": invoice.kind,
        "issued": invoice.issued.isoformat() if invoice.issued else None,
        "period_start": min(line.start for line in lines).isoformat(),
        "period_end": max(line.end for line in lines).isoformat(),
        "lines": [line.as_dict() for line in lines],
    }


def follows(older: dict[str, Any], newer: dict[str, Any]) -> bool:
    """Return whether a bill ends the day before a newer one starts."""
    end = date.fromisoformat(older["period_end"])
    return end + timedelta(days=1) == date.fromisoformat(newer["period_start"])


def ours(seen: dict[str, Any], ean: str) -> bool:
    """Return whether a bill read may be the meter's.

    One printing its EAN may, and so may a PDF that could not be read at
    all, whose EANs are unknown.
    """
    return seen["eans"] is None or ean in seen["eans"]


def not_used(run: list[dict[str, Any]], why: str) -> str:
    """Return why a run of bills, newest first, was not shown as the year."""
    return f"the bills up to {run[0]['period_end']} were not used: {why}"


def too_few(run: list[dict[str, Any]]) -> str:
    """Return why monthly bills, newest first, are not a year."""
    return not_used(run, f"{len(run)} monthly bills follow on, not {MONTHS}")


def settlement(every: list[Invoice], today: date) -> list[Invoice]:
    """Return the settlement bills among the invoices, newest first.

    Newest by the days billed, then by when issued: a bill issued late, or
    with no date, would otherwise come before the bills it precedes and
    break the year they make.
    """
    invoices = [i for i in every if i.kind in SETTLEMENT]
    invoices.sort(
        key=lambda i: (i.period_end or i.issued or today, i.issued or today),
        reverse=True,
    )
    return invoices


def year_state(bills: list[dict[str, Any]]) -> dict[str, Any]:
    """Return what a meter's sensor shows of a run of bills, newest first.

    The total is what the lines cost with their VAT, worked out line by
    line, so it can differ by a cent from what a bill prints. The balance is
    what the bills asked for once the advances were taken off, added up as
    the invoice list gives them, or None if one of them gives none.
    """
    run = bills[::-1]
    lines = [line for bill in run for line in bill["lines"]]
    total = sum(
        (
            Decimal(str(line["total"])) * (1 + Decimal(str(line["vat"])))
            for line in lines
        ),
        Decimal(0),
    )
    dues = [bill.get("due") for bill in run]
    balance = (
        None
        if None in dues
        else float(
            sum((Decimal(str(due)) for due in dues), Decimal(0)).quantize(
                _CENT, ROUND_HALF_UP
            )
        )
    )
    return {
        "issued": bills[0]["issued"],
        "period_start": run[0]["period_start"],
        "period_end": run[-1]["period_end"],
        "total": float(total.quantize(_CENT, ROUND_HALF_UP)),
        "balance": balance,
        "bills": [
            {
                key: bill[key]
                for key in ("invoice_type", "issued", "period_start", "period_end")
            }
            for bill in run
        ],
        "lines": lines,
    }


def pass_over(
    invoice: Invoice,
    reason: str,
    run: list[dict[str, Any]],
    closes: Invoice | None,
    missing: tuple[Invoice, str] | None,
) -> tuple[bool, Invoice | None, tuple[Invoice, str] | None]:
    """Weigh a bill of two meters of one energy that cannot be read.

    Return whether it is passed over, the bill that may close the year and
    the one that may be missing from it, as they stand after it. Once a
    yearly bill has begun the year, it is passed over: whose it is cannot be
    told, with no document no more than with one that would not read.
    Ending the day the year begins, a yearly or closing bill may be what
    closes it, and an intermediary may be missing from it; so may one the
    list gives no days. A monthly run passes over any bill of another kind.
    """
    if not run:
        return False, closes, missing
    if run[0]["invoice_type"] == MONTHLY:
        return invoice.kind != MONTHLY, closes, missing
    begins = date.fromisoformat(run[-1]["period_start"])
    if invoice.period_end in (begins, None):
        if invoice.kind != INTERMEDIARY:
            closes = closes or invoice
        elif missing is None:
            missing = (invoice, reason)
    return True, closes, missing

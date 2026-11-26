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

"""What a meter's advances come to, as the invoice list and the portal give them.

The advances page of an address, the advance invoices split by energy, and
what intermediary bills of the period already settled. Nothing here does
I/O: bills.py reads them.
"""

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

from .api_billing import AdvanceAmounts, Invoice
from .bill_year import INTERMEDIARY

ADVANCE: Final = "AdvanceInvoice"


def address_state(
    portal: AdvanceAmounts | str, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return what the address sensors show of the portal's advances.

    The portal's own expected regularisation is turned to this
    integration's sign: positive is still to pay. A page that could not be
    read leaves ``previous`` standing, with why: an advance does not change
    because it was not fetched again.
    """
    if isinstance(portal, str):
        if previous:
            return previous | {"error": portal}
        return {
            "eans": None,
            "contracts": {},
            "advance_now": None,
            "portal_min": None,
            "portal_max": None,
            "eneco_recommended": None,
            "eneco_expected_regularisation": None,
            "error": portal,
        }
    expected = None
    if (
        all(
            c.current is not None and c.months_left is not None and c.due is not None
            for c in portal.contracts
        )
        and portal.contracts
    ):
        expected = round(
            sum(
                (c.due or 0.0) - (c.current or 0.0) * max(c.months_left or 0, 1)
                for c in portal.contracts
            ),
            2,
        )
    return {
        "eans": sorted(c.ean for c in portal.contracts),
        "contracts": {
            c.ean: {
                "current": c.current,
                "recommended": c.recommended,
                "months_left": c.months_left,
                "minimum": c.minimum,
                "maximum": c.maximum,
                "blocked": c.blocked,
                "vacancy_until": c.vacancy_until.isoformat()
                if c.vacancy_until
                else None,
            }
            for c in portal.contracts
        },
        "advance_now": portal.current,
        "portal_min": portal.minimum,
        "portal_max": portal.maximum,
        "eneco_recommended": portal.recommended,
        "eneco_expected_regularisation": expected,
        "error": None,
    }


def settled_by(
    every: list[Invoice], kind: str, start: date, end: date, *, shared: bool
) -> tuple[float | None, str | None]:
    """Return what bills within the period already asked for, or why not.

    An intermediary bill, on a product change, settles its days less their
    advances, so what it asked for, or paid back, is no longer owed at the
    reading. One for both energies cannot be split, and one giving no amount
    cannot be counted. With two meters of one energy the advances are not
    told apart, so neither is what these settled. None is returned then,
    with the reason.
    """
    settling = [
        i
        for i in every
        if i.kind == INTERMEDIARY
        and i.energy_type in (kind, "Both")
        and i.period_start is not None
        and start <= i.period_start <= end
    ]
    if any(i.energy_type == "Both" for i in settling):
        return None, "an intermediary bill for both energies settles part of the period"
    if any(i.due is None for i in settling):
        return None, "an intermediary bill of the period gives no amount"
    if shared and settling:
        return None, (
            "an intermediary bill of the period is not told apart between meters"
            " of one energy"
        )
    return sum(i.due or 0.0 for i in settling), None


def advance_months(
    kind: str,
    every: list[Invoice],
    split: Mapping[str, dict[str, float]],
    share: float | None,
    *,
    shared: bool = False,
) -> tuple[dict[str, float], set[str], set[str]]:
    """Return this energy's share of each month's advance, "YYYY-MM".

    Also the months whose share was estimated and those it could not be told
    for.

    An invoice for this energy alone is its own; one for both is split as
    its PDF prints it, or, where that could not be read, by ``share``, this
    energy's part of today's advance. Without either it is left out. Where
    the address has another meter of the same energy (``shared``), none can
    be told: no invoice names its meter.
    """
    months: dict[str, float] = {}
    estimated: set[str] = set()
    missing: set[str] = set()
    for invoice in every:
        if (
            invoice.kind != ADVANCE
            or invoice.period_start is None
            or invoice.due is None
            or invoice.energy_type not in (kind, "Both")
        ):
            continue
        month = f"{invoice.period_start:%Y-%m}"
        if shared:
            missing.add(month)
            continue
        if invoice.energy_type == kind:
            amount: float | None = invoice.due
        else:
            amount = (split.get(invoice.identifier) or {}).get(kind)
            if amount is None and share is not None:
                amount = invoice.due * share
                estimated.add(month)
            elif amount is None:
                missing.add(month)
        if amount is not None:
            months[month] = months.get(month, 0.0) + amount
    return months, estimated, missing

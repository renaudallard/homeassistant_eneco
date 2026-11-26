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

"""What the portal says about billing: invoices, cards, advances, volumes.

Parsers only, kept apart from the client so it stays readable. Each takes
the payload the REST API returned, already unwrapped, and gives back plain
data, dropping whatever does not have the shape documented in docs/API.md.
"""

from dataclasses import dataclass
from datetime import date
import math
from typing import Any, Final

# The documents of an invoice that are not the bill itself.
_NOT_THE_BILL: Final = frozenset({"DynamicBill", "SimplifiedBill"})


@dataclass(frozen=True, slots=True)
class Invoice:
    """An invoice of a delivery address, without what it billed.

    ``document_url`` is the bill itself, the one the website offers for
    download, and is only fetched with the session's cookies. ``due`` is
    what the invoice asked for in EUR, VAT included: on a settlement bill,
    what it cost less the advances it settles, negative for a refund.
    """

    identifier: str
    kind: str
    energy_type: str
    issued: date | None
    period_start: date | None
    period_end: date | None
    document_url: str | None
    due: float | None


@dataclass(frozen=True, slots=True)
class TariffCard:
    """The tariff card a contract is billed on, as its product says."""

    product: str
    product_type: str
    # Language code -> URL of the card, for the languages it is published in.
    urls: dict[str, str]


@dataclass(frozen=True, slots=True)
class ContractAdvance:
    """The monthly advance of one contract, as the portal's advances page has it.

    ``recommended`` is the portal's own advice: what it expects to be due by
    the next meter reading, divided by the months left until then.
    """

    ean: str
    energy_type: str
    current: float | None
    recommended: float | None
    months_left: int | None
    due: float | None
    # The month of the meter reading that closes the period, 1 to 12.
    reading_month: int | None = None
    # The range the portal lets the advance be set in.
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class AdvanceAmounts:
    """The advances of a delivery address: its total and each contract's."""

    current: float | None
    recommended: float | None
    contracts: tuple[ContractAdvance, ...]
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class MonthVolume:
    """What a meter took and fed back in a month, in kWh, as the portal shows it."""

    offtake: float
    injection: float


def day(value: Any) -> date | None:
    """Return the day of a timestamp the API sends, 2026-04-26T00:00:00+00:00."""
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def amount(value: Any) -> float | None:
    """Return an amount the API sends, or None for one it does not."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _months(value: Any) -> int | None:
    """Return a count of months the API sends, or None for one it does not."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _month_number(value: Any) -> int | None:
    """Return a month the API sends, 1 to 12, or None for one it does not."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        return None
    return value


def invoice(entry: dict[str, Any]) -> Invoice:
    """Return the invoice an entry of the invoice list describes."""
    documents = entry.get("documents")
    url = next(
        (
            str(document["url"])
            for document in (documents if isinstance(documents, list) else [])
            if isinstance(document, dict)
            and document.get("url")
            and document.get("documentType") not in _NOT_THE_BILL
        ),
        None,
    )
    return Invoice(
        identifier=str(entry["invoiceId"]),
        kind=str(entry.get("invoiceType") or ""),
        energy_type=str(entry.get("invoiceEnergyType") or "Unknown"),
        issued=day(entry.get("invoiceDate")),
        period_start=day(entry.get("invoicingPeriodStartDate")),
        period_end=day(entry.get("invoicingPeriodEndDate")),
        document_url=url,
        due=amount(entry.get("dueAmount")),
    )


def invoices(payload: Any) -> list[Invoice]:
    """Return the invoices of an invoice list."""
    listed = payload.get("invoices") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        return []
    return [
        invoice(entry)
        for entry in listed
        if isinstance(entry, dict) and entry.get("invoiceId")
    ]


def tariff_card(payload: Any) -> TariffCard | None:
    """Return the tariff card a contract's products name, if they name one."""
    tariff = payload.get("tariff") if isinstance(payload, dict) else None
    if not isinstance(tariff, dict):
        return None
    current = tariff.get("currentTariff")
    urls: dict[str, str] = {}
    for card in current if isinstance(current, list) else []:
        listed = card.get("tariffCardByLanguage") if isinstance(card, dict) else None
        for entry in listed if isinstance(listed, list) else []:
            if (
                isinstance(entry, dict)
                and entry.get("type") == "Normal"
                and entry.get("language")
                and entry.get("tariffCard")
            ):
                urls.setdefault(
                    str(entry["language"]).lower(), str(entry["tariffCard"])
                )
    if not urls:
        return None
    return TariffCard(
        product=str(tariff.get("product") or ""),
        product_type=str(tariff.get("productType") or ""),
        urls=urls,
    )


def advance_amounts(payload: Any) -> AdvanceAmounts | None:
    """Return the advances of a delivery address, or None without any."""
    if not isinstance(payload, dict):
        return None
    totals = payload.get("totals")
    totals = totals if isinstance(totals, dict) else {}
    listed = payload.get("advancePayments")
    contracts: list[ContractAdvance] = []
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict):
            continue
        meter = entry.get("meterDetails")
        details = entry.get("details")
        if not isinstance(meter, dict) or not isinstance(details, dict):
            continue
        if not meter.get("ean"):
            continue
        contracts.append(
            ContractAdvance(
                ean=str(meter["ean"]),
                energy_type=str(meter.get("energyType") or "Unknown"),
                current=amount(details.get("currentAmount")),
                recommended=amount(details.get("recommendedAmount")),
                months_left=_months(details.get("monthsUntilMeterReadingMonth")),
                due=amount(details.get("amountDueForMeterReadingMonth")),
                reading_month=_month_number(meter.get("meterReadingMonth")),
                minimum=amount(details.get("minAmount")),
                maximum=amount(details.get("maxAmount")),
            )
        )
    return AdvanceAmounts(
        current=amount(totals.get("currentAmount")),
        recommended=amount(totals.get("recommendedAmount")),
        contracts=tuple(contracts),
        minimum=amount(totals.get("minAmount")),
        maximum=amount(totals.get("maxAmount")),
    )


def monthly_volumes(payload: Any) -> dict[str, MonthVolume]:
    """Return a meter's volume of each month the consumption page lists.

    Months are keyed "YYYY-MM". A month listing no value at all is left out
    rather than read as nothing used.
    """
    periods = payload.get("periods") if isinstance(payload, dict) else None
    months: dict[str, MonthVolume] = {}
    for period in periods if isinstance(periods, list) else []:
        if not isinstance(period, dict):
            continue
        start = day(period.get("startDate"))
        data = period.get("data")
        if start is None or not isinstance(data, list):
            continue
        offtake = injection = 0.0
        found = False
        for value in data:
            if not isinstance(value, dict):
                continue
            kwh = amount(value.get("value"))
            if kwh is None:
                continue
            found = True
            if value.get("direction") == "Injection":
                injection += kwh
            else:
                offtake += kwh
        if found:
            months[f"{start:%Y-%m}"] = MonthVolume(offtake, injection)
    return months

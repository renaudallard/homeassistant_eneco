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

"""Read the billing lines off an Eneco settlement bill.

The portal gives the amounts of an invoice and a link to its PDF, never what
was billed on it. The PDF lists every billed line in three blocks, energy,
network and taxes, each closed by its subtotal, and repeats the three for
each part of a period that was split, such as on a change of tariffs:

    Conso. simple 01/01/2026 - 31/01/2026 195,80 kWh 15,89 €c 6% € 31,11
    Tarif prosumer plafonnement 01/01/2026 - 31/01/2026 6% € -36,92
    Total coût de l'énergie Eneco (hors TVA) € 348,51

Only the shape of a line is relied on, not its wording, so a bill is read
the same way whatever its language. Each block has to add up to the
subtotal printed under it, and the blocks have to come in threes, or the
bill is refused: a line misread or missed would otherwise put a wrong figure in
front of the user with nothing to tell it from a right one.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import io
import re
from typing import Any, Final

from pypdf import PdfReader

from .api import EnecoError
from .const import ELECTRICITY, GAS

SECTIONS: Final = ("energy", "network", "taxes")

# Units of a quantity, as Eneco prints them, and what they are.
_UNITS: Final = {"kwh": "kWh", "année": "year", "an": "year"}

_DATE: Final = r"\d{2}/\d{2}/(?:\d{4}|\d{2})"
_AMOUNT: Final = r"-?\d{1,3}(?:\.\d{3})*(?:,\d+)?"

_ITEM_RE: Final = re.compile(
    rf"(?P<label>\S.*?) (?P<start>{_DATE}) - (?P<end>{_DATE})"
    rf"(?: (?P<quantity>{_AMOUNT}) (?P<unit>\S+)"
    rf" (?P<price>{_AMOUNT}) (?P<price_unit>€c|€))?"
    rf" (?P<vat>\d{{1,2}})% € ?(?P<total>{_AMOUNT})(?P<rest>.*)"
)
_SUBTOTAL_RE: Final = re.compile(rf".*€ ?(?P<amount>{_AMOUNT})")
# The electricity bill also carries the line's type in hidden text, such as
# "ItemTypeSys: Distributie variabel (per kWh)", the same in every language.
_CODE_RE: Final = re.compile(r"ItemType(?:Sys|Generic):\s*(?P<code>.+?)(?=\s+txt|$)")

# Subtotals are sums of lines rounded to the cent.
_TOLERANCE: Final = 0.005

# An advance invoice for both energies prints each one's share, before and
# with VAT: "Électricité € 53,32 € 56,51", or in 2025 "Électricité € 94,35
# hors TVA € 100,00 TVA incl.". Only French bills have been seen: another
# wording is not guessed at.
_ADVANCE_RE: Final = re.compile(
    rf"(?P<energy>Électricité|Gaz) € (?P<excl>{_AMOUNT})(?: hors TVA)?"
    rf" € (?P<incl>{_AMOUNT})(?: TVA incl\.)?"
)
_ADVANCE_ENERGY: Final = {"Électricité": ELECTRICITY, "Gaz": GAS}


class BillFormatError(EnecoError):
    """A bill whose billing lines could not be read with confidence."""


@dataclass(frozen=True, slots=True)
class BillLine:
    """One billed line: what, over which days, how much, at what price.

    ``unit_price`` is in EUR per ``unit`` and ``total`` in EUR, both before
    VAT, which is ``vat`` as a fraction. A line billed as a flat amount has
    no quantity and no unit price.
    """

    section: str
    label: str
    code: str | None
    start: date
    end: date
    quantity: float | None
    unit: str | None
    unit_price: float | None
    vat: float
    total: float

    def as_dict(self) -> dict[str, Any]:
        """Return the line as plain JSON values."""
        return {
            "section": self.section,
            "label": self.label,
            "code": self.code,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "quantity": self.quantity,
            "unit": self.unit,
            "unit_price": self.unit_price,
            "vat": self.vat,
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BillLine:
        """Return a line stored by :meth:`as_dict`."""
        return cls(
            section=str(data["section"]),
            label=str(data["label"]),
            code=data.get("code"),
            start=date.fromisoformat(data["start"]),
            end=date.fromisoformat(data["end"]),
            quantity=data.get("quantity"),
            unit=data.get("unit"),
            unit_price=data.get("unit_price"),
            vat=float(data["vat"]),
            total=float(data["total"]),
        )


def _decimal(text: str) -> Decimal:
    """Return a number written the Belgian way, 1.707,52."""
    return Decimal(text.replace(".", "").replace(",", "."))


def _number(text: str) -> float:
    """Return a number written the Belgian way, as a float."""
    return float(_decimal(text))


def _date(text: str) -> date:
    """Return a date written dd/mm/yyyy or dd/mm/yy."""
    day, month, year = (int(part) for part in text.split("/"))
    return date(year + 2000 if year < 100 else year, month, day)


def pdf_text(payload: bytes) -> str:
    """Return the text of a PDF, one printed row per line, blanks collapsed.

    Blocking: run it in the executor.
    """
    try:
        reader = PdfReader(io.BytesIO(payload))
        pages = [page.extract_text(extraction_mode="layout") for page in reader.pages]
    # pypdf reads untrusted bytes and fails on a damaged file with whatever it
    # stumbles on, an AttributeError deep in its layout code among them.
    except Exception as err:
        raise BillFormatError(f"unreadable PDF: {type(err).__name__}") from err
    rows = (" ".join(row.split()) for page in pages for row in page.splitlines())
    return "\n".join(row for row in rows if row)


def _line(match: re.Match[str], section: str) -> BillLine:
    """Return the billed line an item row describes."""
    quantity = match["quantity"]
    price = match["price"]
    unit_price = None
    if price is not None:
        # In Decimal, so that 17,35 €c is 0.1735 and not 0.17350000000000002.
        unit_price = float(
            _decimal(price) / (100 if match["price_unit"] == "€c" else 1)
        )
    unit = match["unit"]
    code = _CODE_RE.search(match["rest"])
    return BillLine(
        section=section,
        label=match["label"],
        code=code["code"] if code else None,
        start=_date(match["start"]),
        end=_date(match["end"]),
        quantity=_number(quantity) if quantity is not None else None,
        unit=_UNITS.get(unit.lower(), unit) if unit is not None else None,
        unit_price=unit_price,
        vat=int(match["vat"]) / 100,
        total=_number(match["total"]),
    )


def parse_bill(text: str) -> tuple[BillLine, ...]:
    """Return every billed line of a bill, or raise BillFormatError.

    Rows between the items of a block, a page footer or hidden text, are
    passed over. The first row after them that ends in an amount closes the
    block and has to be its subtotal.
    """
    lines: list[BillLine] = []
    block: list[BillLine] = []
    blocks = 0
    for row in text.splitlines():
        if item := _ITEM_RE.fullmatch(row):
            try:
                block.append(_line(item, SECTIONS[blocks % len(SECTIONS)]))
            except ValueError as err:
                raise BillFormatError(
                    f"a billing line has no real date: {err}"
                ) from err
            continue
        if not block or (subtotal := _SUBTOTAL_RE.fullmatch(row)) is None:
            continue
        added = sum(line.total for line in block)
        if abs(added - _number(subtotal["amount"])) > _TOLERANCE:
            raise BillFormatError(
                f"{SECTIONS[blocks % len(SECTIONS)]} lines add up to {added:.2f},"
                f" the bill says {subtotal['amount']}"
            )
        lines.extend(block)
        block = []
        blocks += 1
    if block:
        raise BillFormatError(
            f"{SECTIONS[blocks % len(SECTIONS)]} lines have no subtotal"
        )
    if not blocks or blocks % len(SECTIONS):
        raise BillFormatError(f"{blocks} blocks of billing lines, not sets of 3")
    return tuple(lines)


def parse_advance(text: str, total: float) -> dict[str, float]:
    """Return each energy's share of an advance invoice, VAT included.

    Empty unless the shares read add up to ``total``, what the invoice list
    says the invoice asked for: a split that cannot be checked is not used.
    The PDF's own hidden total is no help, as it is what is left to pay once
    a refund due is taken off.
    """
    shares: dict[str, float] = {}
    for row in text.splitlines():
        if found := _ADVANCE_RE.fullmatch(row):
            shares[_ADVANCE_ENERGY[found["energy"]]] = _number(found["incl"])
    if not shares or abs(sum(shares.values()) - total) > _TOLERANCE:
        return {}
    return shares

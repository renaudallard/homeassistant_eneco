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

"""Tests for reading the billing lines off a bill."""

from datetime import date

import pytest

from custom_components.eneco_be import bill
from custom_components.eneco_be.bill import (
    BillFormatError,
    BillLine,
    parse_bill,
    pdf_text,
)

from .bill_pdf import ELECTRICITY, GAS, make_pdf


def _text(rows: list[str]) -> str:
    """Return the rows the way pdf_text hands them over."""
    return "\n".join(" ".join(row.split()) for row in rows)


def test_electricity_bill_lines() -> None:
    """Every line lands in its block, in EUR, with its type where printed."""
    lines = parse_bill(_text(ELECTRICITY))

    assert [line.section for line in lines] == ["energy"] * 4 + ["network"] * 4 + [
        "taxes"
    ] * 3
    assert round(sum(line.total for line in lines), 2) == 1074.87
    energy = lines[1]
    assert energy == BillLine(
        section="energy",
        label="Conso. simple",
        code="Energiecomponent ElectricityAplusTH",
        start=date(2025, 2, 1),
        end=date(2025, 3, 31),
        quantity=334.51,
        unit="kWh",
        unit_price=0.1735,
        vat=0.06,
        total=58.04,
    )
    fee = lines[0]
    assert (fee.unit, fee.unit_price, fee.code) == ("year", 49.59, "Vaste vergoeding")
    # Hidden text after the type is not part of it.
    assert lines[7].code == "Transport variabel (per kWh)"


def test_flat_amount_line() -> None:
    """A line billed as an amount has no quantity and no unit price."""
    prosumer = parse_bill(_text(ELECTRICITY))[6]

    assert prosumer.label == "Tarif prosumer plafonnement"
    assert (prosumer.quantity, prosumer.unit, prosumer.unit_price) == (None, None, None)
    assert prosumer.total == -143.08


def test_split_period_repeats_the_blocks() -> None:
    """A period split in two prints the three blocks twice."""
    lines = parse_bill(_text(GAS))

    assert [line.section for line in lines] == [
        "energy",
        "energy",
        "network",
        "network",
        "taxes",
        "taxes",
        "energy",
        "network",
        "taxes",
    ]
    assert round(sum(line.total for line in lines), 2) == 1404.00
    # Two digit years, a year unit spelled An, a whole quantity.
    assert lines[0].start == date(2023, 12, 20)
    assert lines[0].unit == "year"
    assert (lines[4].quantity, lines[4].total) == (0.0, 0.0)


def test_line_and_back() -> None:
    """A stored line reads back as the same line."""
    for line in parse_bill(_text(ELECTRICITY)):
        assert BillLine.from_dict(line.as_dict()) == line


def test_subtotal_mismatch_refused() -> None:
    """A block that does not add up to its subtotal refuses the whole bill."""
    rows = [row.replace("€ 58,04", "€ 58,40") for row in ELECTRICITY]

    with pytest.raises(BillFormatError, match="energy lines add up to 350.45"):
        parse_bill(_text(rows))


def test_missed_line_refused() -> None:
    """A line the pattern does not take shows up as a wrong subtotal."""
    rows = [
        row.replace("1,00 année 13,06 €", "1,00 mois 13,06 EUR") for row in ELECTRICITY
    ]

    with pytest.raises(BillFormatError, match="network lines"):
        parse_bill(_text(rows))


def test_block_without_subtotal_refused() -> None:
    """Lines left at the end with no subtotal refuse the bill."""
    with pytest.raises(BillFormatError, match="taxes lines have no subtotal"):
        parse_bill(_text(ELECTRICITY[:-2]))


def test_incomplete_set_refused() -> None:
    """Blocks that do not come in threes refuse the bill."""
    taxes = ELECTRICITY.index("Taxes Période Quantité Prix unitaire TVA Total hors TVA")

    with pytest.raises(BillFormatError, match="2 blocks"):
        parse_bill(_text(ELECTRICITY[:taxes]))


def test_no_billing_lines_refused() -> None:
    """An invoice with nothing billed on it is not a bill."""
    with pytest.raises(BillFormatError, match="0 blocks"):
        parse_bill("Facture\nTotal € 12,00")


def test_pdf_text_keeps_rows() -> None:
    """Each printed row comes out as one line, blanks collapsed."""
    text = pdf_text(make_pdf(["Conso.   simple  01/02/2025", "Total € 1,00"]))

    assert text == "Conso. simple 01/02/2025\nTotal € 1,00"


def test_pdf_text_of_a_real_layout() -> None:
    """The text of a bill PDF parses like the bill it prints."""
    lines = parse_bill(pdf_text(make_pdf(GAS)))

    assert round(sum(line.total for line in lines), 2) == 1404.00


def test_impossible_date_refused() -> None:
    """A line whose date cannot be is refused like any unreadable bill."""
    rows = [
        row.replace("01/02/2025 - 31/03/2025", "00/02/2025 - 31/03/2025")
        for row in ELECTRICITY
    ]

    with pytest.raises(BillFormatError, match="no real date"):
        parse_bill(_text(rows))


def test_whatever_pypdf_raises_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A damaged PDF can fail anywhere in pypdf, an AttributeError included."""

    def broken(*args: object, **kwargs: object) -> None:
        raise AttributeError("'NoneType' object has no attribute 'get_object'")

    monkeypatch.setattr(bill, "PdfReader", broken)
    with pytest.raises(BillFormatError, match="unreadable PDF: AttributeError"):
        pdf_text(make_pdf(GAS))


def test_unreadable_pdf_refused() -> None:
    """Bytes that are not a PDF are refused, not raised as something else."""
    with pytest.raises(BillFormatError, match="unreadable PDF"):
        pdf_text(b"%PDF-1.4 not really")


# An advance invoice for both energies, as its PDF rows read.
ADVANCE = [
    "Facture d'acompte",
    "Électricité - ORES Gas - ORES (Brabant Wallon)",
    "Électricité € 53,32 € 56,51",
    "Gaz € 235,11 € 249,20",
    "Sous-total € 288,43",
    "Total € 305,71",
    "txtTotalAmount: 305,71",
]


def test_an_advance_is_split_by_energy() -> None:
    """Each energy's share, VAT included, adding up to what it asked for."""
    expected = {"Electricity": 56.51, "Gas": 249.2}
    assert bill.parse_advance(_text(ADVANCE), 305.71) == expected
    # The hidden total is what is left once a refund is taken off: ignored.
    offset = [
        row.replace("txtTotalAmount: 305,71", "txtTotalAmount: -0,85")
        for row in ADVANCE
    ]
    assert bill.parse_advance(_text(offset), 305.71) == expected
    # As 2025's invoices print it.
    old = [
        "Électricité € 94,35 hors TVA € 100,00 TVA incl.",
        "Gaz € 188,69 hors TVA € 200,00 TVA incl.",
    ]
    assert bill.parse_advance(_text(old), 300.0) == {"Electricity": 100.0, "Gas": 200.0}


@pytest.mark.parametrize(
    "rows",
    [
        # The shares do not add up to the total: a row was misread.
        [row.replace("€ 249,20", "€ 249,02") for row in ADVANCE],
        # One of them was not read.
        [row for row in ADVANCE if not row.startswith("Gaz")],
        # A wording never seen is not guessed at.
        [row.replace("Électricité €", "Elektriciteit €") for row in ADVANCE],
    ],
)
def test_an_advance_split_that_cannot_be_checked_is_not_used(rows: list[str]) -> None:
    """A split not checked against the total could be misread."""
    assert bill.parse_advance(_text(rows), 305.71) == {}

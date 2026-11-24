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

"""Synthetic Eneco bills, as PDFs, for the tests.

Real bills carry a name, an address, an IBAN and the EANs, so none is kept
here. These follow the layouts of the electricity and gas bills of 2025 and
2026 row for row, with made up figures that add up the way a bill does.
"""

from .conftest import EAN

ELECTRICITY = [
    "Facture annuelle Électricité",
    f"EAN {EAN}",
    "Montant de la facture: € -234,09",
    "TVA 6% € -13,20 txtHasPrepaid = false",
    "Données de facturation",
    "Coût de l’énergie Période Quantité Prix unitaire TVA Total hors TVA",
    "Redevance fixe 01/02/2025 - 31/01/2026 1,00 année 49,59 € 6% € 49,59"
    " txtElecBillingBodyItemTypeGeneric: Vaste vergoeding",
    "Conso. simple 01/02/2025 - 31/03/2025 334,51 kWh 17,35 €c 6% € 58,04"
    " txtElecBillingBodyItemTypeGeneric: Energiecomponent ElectricityAplusTH",
    "txtProductRateElec: Variable",
    "Conso. simple 01/04/2025 - 31/01/2026 1.373,01 kWh 14,14 €c 6% € 194,14"
    " txtElecBillingBodyItemTypeGeneric: Energiecomponent ElectricityAplusTH",
    "Coûts énergie verte 01/02/2025 - 31/01/2026 1.707,52 kWh 2,83 €c 6% € 48,32"
    " txtElecBillingBodyItemTypeGeneric: GSC administratiekost",
    "Total coût de l’énergie Eneco (hors TVA) € 350,09",
    "Tarifs de réseau Période Quantité Prix unitaire TVA Total hors TVA",
    "Distribution variable (kWh) 01/02/2025 - 31/01/2026 5.920,50 kWh 10,18 €c 6%"
    " € 602,71 ItemTypeSys: Distributie variabel (per kWh)",
    "Distribution fixe 01/02/2025 - 31/01/2026 1,00 année 13,06 € 6% € 13,06"
    " ItemTypeSys: Distributie vast (per jaar)",
    "Tarif prosumer plafonnement 01/02/2025 - 31/01/2026 6% € -143,08"
    " ItemTypeSys: Prosumententarief plafonnering",
    "Coûts de transmission (kWh) 01/02/2025 - 31/01/2026 5.920,50 kWh 2,81 €c 6%"
    " € 166,37 ItemTypeSys: Transport variabel (per kWh) txtUtilityType1: Elec",
    "Total des tarifs de réseau (hors TVA) € 639,06",
    "Eneco Belgium SA - Battelsesteenweg 455i - 2800 Mechelen 4/7",
    "Taxes Période Quantité Prix unitaire TVA Total hors TVA",
    "Redevance de raccordement 01/02/2025 - 31/01/2026 1.707,52 kWh 0,08 €c 0% € 1,37",
    "Accise spéciale 01/02/2025 - 31/01/2026 1.707,52 kWh 4,75 €c 6% € 81,11",
    "Cotisation sur l'énergie 01/02/2025 - 31/01/2026 1.707,52 kWh 0,19 €c 6% € 3,24",
    "Total des impôts, taxes et suppléments (hors TVA) € 85,72",
    "Sous-total montant de base (hors TVA) € 1.074,87",
]

# Two sets of the three blocks, the period being split on 31/05/24.
GAS = [
    "Raccordements repris sur le décompte:",
    f"EAN {EAN}",
    "Coût de l’énergie Eneco",
    "Période Quantité Prix unitaire TVA Montant hors TVA",
    "Redevance annuelle 20/12/23 - 31/05/24 0,45 An 49,27500 € 6% € 22,14",
    "Consommation 20/12/23 - 31/05/24 11.032,36 kWh 3,90000 €c 6% € 430,26",
    "Sous-total hors TVA € 452,40",
    "Frais de distribution (reversés au gestionnaire de réseau de distribution)",
    "Distribution variable 20/12/23 - 31/05/24 11.032,37 kWh 1,73607 €c 6% € 191,53",
    "Distribution fixe 20/12/23 - 31/05/24 0,45 An 98,72805 € 6% € 44,36",
    "Sous-total hors TVA € 235,89",
    "Taxes (reversées aux autorités)",
    "Accise spéciale 20/12/23 - 31/05/24 0 kWh 0,00000 €c 6% € 0,00",
    "Redevance de raccordement 20/12/23 - 31/05/24 11.032,36 kWh 0,00750 €c 0% € 0,83",
    "Sous-total hors TVA € 0,83",
    "Total consommation gaz naturel (hors TVA) € 689,12",
    "Coût de l’énergie Eneco",
    "Consommation 01/06/24 - 05/01/25 13.404,71 kWh 5,00000 €c 6% € 670,24",
    "Sous-total hors TVA € 670,24",
    "Frais de distribution (reversés au gestionnaire de réseau de distribution)",
    "Coût de transport 01/06/24 - 05/01/25 13.404,70 kWh 0,23320 €c 6% € 31,26",
    "Sous-total hors TVA € 31,26",
    "Taxes (reversées aux autorités)",
    "Cotisation sur l'énergie 01/06/24 - 05/01/25 13.404,70 kWh 0,09978 €c 6% € 13,38",
    "Sous-total hors TVA € 13,38",
    "Total consommation gaz naturel (hors TVA) € 714,88",
]


def _escape(text: str) -> bytes:
    """Return a row as a PDF string literal in WinAnsi."""
    raw = text.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(rows: list[str]) -> bytes:
    """Return a one page landscape PDF printing each row on a line."""
    stream = b"".join(
        b"BT /F1 6 Tf 20 %d Td (%s) Tj ET\n" % (570 - 9 * number, _escape(row))
        for number, row in enumerate(rows)
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%sendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
        b" /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)

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

"""Which advances to set on the portal so their total is what is needed.

Eneco bills one advance per contract, and lets each be set only within a
range of its own, drawn from its own projection: on a compensated household
the electricity floor can sit far above what is needed. A contract can also
be left as it is, even below that floor. So every way of leaving or setting
each contract is weighed:

- first, how close the total comes to what the meters need together;
- then how far each contract ends up from what its own meter needs;
- and on a tie, the way weighed first, leaving a contract before setting it.

Leaving every contract as it is is one of the ways weighed, so what is
proposed never takes the total further from what is needed than what is
paid now. Nothing here does I/O: bills.py gathers the inputs and the advance
button sends what is proposed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from itertools import product
from typing import Any, Final

# Amounts are in euros and cents.
_CENT: Final = 0.005


@dataclass(frozen=True, slots=True)
class Contract:
    """One contract's advance: what is paid, what may be set, what is needed."""

    ean: str
    energy: str
    current: float
    minimum: float
    maximum: float
    need: float


def _fill(contracts: list[Contract], total: float) -> dict[str, float]:
    """Return amounts for ``contracts`` adding up to ``total``, each in its range.

    Each starts at what it needs, held to its range, and the rest is shared
    among those with room left, equally, until none is left to share.
    ``total`` has to lie within the sum of the ranges.
    """
    amounts = {c.ean: min(max(c.need, c.minimum), c.maximum) for c in contracts}
    for _ in contracts:
        left = total - sum(amounts.values())
        if abs(left) < 1e-9:
            break
        room = [
            c
            for c in contracts
            if (left > 0 and amounts[c.ean] < c.maximum)
            or (left < 0 and amounts[c.ean] > c.minimum)
        ]
        if not room:
            break
        share = left / len(room)
        for c in room:
            amounts[c.ean] = min(max(amounts[c.ean] + share, c.minimum), c.maximum)
    return amounts


def _cents(
    amounts: dict[str, float], total: float, changed: list[str]
) -> dict[str, float]:
    """Return ``amounts`` in cents, still adding up to ``total`` in cents.

    What rounding loses or adds goes to the last contract changed.
    """
    out = {ean: round(value, 2) for ean, value in amounts.items()}
    if changed:
        out[changed[-1]] = round(
            out[changed[-1]] + round(total, 2) - sum(out.values()), 2
        )
    return out


def propose(contracts: list[Contract]) -> tuple[dict[str, float], bool]:
    """Return the advance proposed for each contract, by EAN, and if it is exact.

    Exact is the total coming to what the meters need together.
    """
    need = sum(c.need for c in contracts)
    best: tuple[tuple[Any, ...], dict[str, float], bool] | None = None
    for choice in product((False, True), repeat=len(contracts)):
        setting = [c for c, change in zip(contracts, choice, strict=True) if change]
        kept = sum(
            c.current for c, change in zip(contracts, choice, strict=True) if not change
        )
        amounts = {c.ean: c.current for c in contracts}
        if setting:
            low = sum(c.minimum for c in setting)
            high = sum(c.maximum for c in setting)
            amounts |= _fill(setting, min(max(need - kept, low), high))
        total = sum(amounts.values())
        exact = abs(total - need) < _CENT
        changed = [c.ean for c in setting if abs(amounts[c.ean] - c.current) >= _CENT]
        score = (
            round(abs(total - need), 2),
            round(sum(abs(amounts[c.ean] - c.need) for c in contracts), 2),
        )
        if best is None or score < best[0]:
            best = (score, _cents(amounts, total, changed), exact)
    assert best is not None
    _, amounts, exact = best
    return amounts, exact


def address_proposal(
    portal: Mapping[str, Any],
    provisions: Mapping[str, Mapping[str, Any]],
    today: date,
) -> dict[str, Any]:
    """Return what the advance button would set at an address, or why nothing.

    ``portal`` is what bills.py keeps of the address's advances page, and
    ``provisions`` each meter's projection by EAN.
    """
    contracts: list[Contract] = []
    for ean, contract in (portal.get("contracts") or {}).items():
        provision = provisions.get(ean)
        if provision is None:
            return _none(f"the meter ending {ean[-4:]} is not set up here")
        need = provision.get("monthly_advance")
        if need is None:
            return _none(provision.get("error") or "the advance needed is not known")
        if (provision.get("advances_left") or 0) <= 1:
            return _none("one advance or none is left before the reading")
        if contract.get("blocked"):
            return _none(f"the portal would not change it: {contract['blocked']}")
        until = contract.get("vacancy_until")
        if until and date.fromisoformat(until) >= today:
            return _none("the advance is suspended for a vacancy")
        values = [contract.get(k) for k in ("current", "minimum", "maximum")]
        if any(v is None for v in values):
            return _none("the portal gives no range for the advance")
        current, low, high = (float(v) for v in values)
        contracts.append(
            Contract(ean, provision.get("energy", ""), current, low, high, need)
        )
    if not contracts:
        return _none(portal.get("error") or "the portal lists no contract")
    amounts, exact = propose(contracts)
    return {
        "amounts": amounts,
        "energies": {c.ean: c.energy for c in contracts},
        "changes": [
            c.ean for c in contracts if abs(amounts[c.ean] - c.current) >= _CENT
        ],
        "total": round(sum(amounts.values()), 2),
        "exact": exact,
        "error": None,
    }


def _none(why: str) -> dict[str, Any]:
    return {
        "amounts": {},
        "energies": {},
        "changes": [],
        "total": None,
        "exact": False,
        "error": why,
    }

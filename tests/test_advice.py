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

"""Tests for the advances proposed to the portal."""

from datetime import date

import pytest

from custom_components.eneco_be.advice import Contract, address_proposal, propose

ELEC = "541449000000000001"
GAS = "541449000000000099"


def test_a_contract_below_its_floor_is_left_and_the_other_covers() -> None:
    """Electricity cannot go below 277 but sits at 56.51: gas takes the rest.

    The case of 26 November 2026: 137.60 and 206.82 needed, 344.42 in all.
    """
    got = propose(
        [
            Contract(ELEC, "Electricity", 56.51, 277, 2500, 137.60),
            Contract(GAS, "Gas", 300.0, 153, 1533, 206.82),
        ]
    )
    assert got == ({ELEC: 56.51, GAS: 287.91}, True)


def test_advances_that_already_add_up_are_left() -> None:
    """Nothing changes once the advances come to what is needed."""
    got = propose(
        [
            Contract(ELEC, "Electricity", 56.51, 277, 2500, 137.60),
            Contract(GAS, "Gas", 287.91, 153, 1533, 206.82),
        ]
    )
    assert got == ({ELEC: 56.51, GAS: 287.91}, True)


def test_each_is_set_to_its_need_when_the_ranges_allow() -> None:
    """Ranges wide enough: each contract gets exactly what its meter needs."""
    got = propose(
        [
            Contract(ELEC, "Electricity", 56.51, 20, 2500, 137.60),
            Contract(GAS, "Gas", 300.0, 153, 1533, 206.82),
        ]
    )
    assert got == ({ELEC: 137.6, GAS: 206.82}, True)


def test_a_range_too_narrow_is_made_up_by_the_other() -> None:
    """Gas cannot go above 150: electricity carries what it cannot."""
    got = propose(
        [
            Contract(ELEC, "Electricity", 100.0, 20, 2500, 100.0),
            Contract(GAS, "Gas", 100.0, 50, 150, 200.0),
        ]
    )
    assert got == ({ELEC: 150.0, GAS: 150.0}, True)


def test_a_total_out_of_reach_is_not_pushed_further_off() -> None:
    """No change brings the total closer than what is paid.

    100 needed: gas is at its floor, electricity below its own.
    """
    contracts = [
        Contract(ELEC, "Electricity", 56.51, 277, 2500, 50.0),
        Contract(GAS, "Gas", 153.0, 153, 1533, 50.0),
    ]
    assert propose(contracts) == ({ELEC: 56.51, GAS: 153.0}, False)


def test_leaving_both_is_weighed_too() -> None:
    """Both contracts below their floors: any change takes the total further."""
    contracts = [
        Contract(ELEC, "Electricity", 56.51, 277, 2500, 50.0),
        Contract(GAS, "Gas", 100.0, 153, 1533, 50.0),
    ]
    assert propose(contracts) == ({ELEC: 56.51, GAS: 100.0}, False)


def test_a_floor_brings_the_total_closer() -> None:
    """300 needed, 356.51 paid: gas down to its floor gets nearer."""
    contracts = [
        Contract(ELEC, "Electricity", 156.51, 277, 2500, 100.0),
        Contract(GAS, "Gas", 200.0, 153, 1533, 200.0),
    ]
    assert propose(contracts) == ({ELEC: 156.51, GAS: 153.0}, False)


def test_a_total_out_of_reach_is_brought_closer() -> None:
    """300 needed, 600 paid: the floors, 430, are nearer."""
    contracts = [
        Contract(ELEC, "Electricity", 300.0, 277, 2500, 100.0),
        Contract(GAS, "Gas", 300.0, 153, 1533, 200.0),
    ]
    amounts, exact = propose(contracts)
    assert not exact
    assert sum(amounts.values()) == pytest.approx(430.0)


def test_the_total_holds_to_the_cent() -> None:
    """Thirds shared out still add up to the cent."""
    contracts = [
        Contract(ELEC, "Electricity", 10.0, 0, 100, 10.0),
        Contract(GAS, "Gas", 10.0, 0, 100, 10.0),
        Contract("541449000000000077", "Gas", 10.0, 0, 100, 80.0 / 3),
    ]
    amounts, exact = propose(contracts)
    assert exact
    assert round(sum(amounts.values()), 2) == round(20 + 80 / 3, 2)


PORTAL = {
    "contracts": {
        ELEC: {"current": 56.51, "minimum": 277, "maximum": 2500, "blocked": None},
        GAS: {"current": 300.0, "minimum": 153, "maximum": 1533, "blocked": None},
    }
}
NEEDS = {
    ELEC: {"monthly_advance": 137.6, "advances_left": 3, "energy": "Electricity"},
    GAS: {"monthly_advance": 206.82, "advances_left": 3, "energy": "Gas"},
}


def test_an_address_proposal() -> None:
    """The address proposes the amounts, the energies and what changes."""
    got = address_proposal(PORTAL, NEEDS, date(2026, 11, 26))
    assert got == {
        "amounts": {ELEC: 56.51, GAS: 287.91},
        "energies": {ELEC: "Electricity", GAS: "Gas"},
        "changes": [GAS],
        "total": 344.42,
        "exact": True,
        "error": None,
    }


@pytest.mark.parametrize(
    ("portal", "needs", "error"),
    [
        (PORTAL, {ELEC: NEEDS[ELEC]}, "the meter ending 0099 is not set up here"),
        (
            PORTAL,
            NEEDS | {GAS: {"monthly_advance": None, "error": "no reading"}},
            "no reading",
        ),
        (
            PORTAL,
            NEEDS | {GAS: NEEDS[GAS] | {"advances_left": 1}},
            "one advance or none is left before the reading",
        ),
        (
            {
                "contracts": PORTAL["contracts"]
                | {GAS: {"blocked": "payments are overdue"}}
            },
            NEEDS,
            "the portal would not change it: payments are overdue",
        ),
        (
            {
                "contracts": PORTAL["contracts"]
                | {GAS: PORTAL["contracts"][GAS] | {"vacancy_until": "2026-12-31"}}
            },
            NEEDS,
            "the advance is suspended for a vacancy",
        ),
        ({"contracts": {}, "error": "down"}, NEEDS, "down"),
        # A vacancy ending today still holds today.
        (
            {
                "contracts": PORTAL["contracts"]
                | {GAS: PORTAL["contracts"][GAS] | {"vacancy_until": "2026-11-26"}}
            },
            NEEDS,
            "the advance is suspended for a vacancy",
        ),
        (
            PORTAL,
            NEEDS | {GAS: NEEDS[GAS] | {"monthly_advance": None}},
            "the advance needed is not known",
        ),
        (
            {
                "contracts": PORTAL["contracts"]
                | {GAS: PORTAL["contracts"][GAS] | {"maximum": None}}
            },
            NEEDS,
            "the portal gives no range for the advance",
        ),
        ({"contracts": {}}, NEEDS, "the portal lists no contract"),
    ],
)
def test_nothing_is_proposed_without_all_it_needs(
    portal: dict[str, object], needs: dict[str, dict[str, object]], error: str
) -> None:
    """A proposal missing a contract, or one the portal would refuse, is none."""
    got = address_proposal(portal, needs, date(2026, 11, 26))
    assert got["error"] == error
    assert got["amounts"] == {}
    assert got["total"] is None


def test_what_a_full_contract_cannot_take_goes_to_the_others() -> None:
    """30 to share, neither can take it alone: A tops out at 10, B gets 20."""
    contracts = [
        Contract("A", "Gas", 0.0, 0, 10, 0.0),
        Contract("B", "Gas", 0.0, 0, 25, 0.0),
        Contract("C", "Gas", 0.0, 0, 0, 30.0),
    ]
    assert propose(contracts) == ({"A": 10.0, "B": 20.0, "C": 0.0}, True)


def test_a_cent_lost_to_rounding_is_given_back() -> None:
    """0.10 shared by three is 0.03 each once rounded: the last gets 0.04."""
    contracts = [
        Contract("A", "Gas", 0.0, 0, 0.04, 0.0),
        Contract("B", "Gas", 0.0, 0, 0.04, 0.0),
        Contract("C", "Gas", 0.0, 0, 0.04, 0.0),
        Contract("D", "Gas", 0.0, 0, 0, 0.10),
    ]
    amounts, exact = propose(contracts)
    assert exact
    assert round(sum(amounts.values()), 2) == 0.10
    assert sorted(amounts.values()) == [0.0, 0.03, 0.03, 0.04]


def test_a_cent_given_back_stays_in_its_range() -> None:
    """Three contracts: what rounding lost went to the last one changed.

    That one sat on its maximum, and was proposed a cent past it.
    """
    contracts = [
        Contract("e0", "Electricity", 1214.61, 326, 1321, 545.9),
        Contract("e1", "Electricity", 1608.4, 455, 1660, 193.43),
        Contract("e2", "Gas", 63.28, 2, 584, 977.54),
    ]
    amounts, _ = propose(contracts)
    for contract in contracts:
        assert contract.minimum <= amounts[contract.ean] <= contract.maximum
    assert round(sum(amounts.values()), 2) == round(545.9 + 193.43 + 977.54, 2)

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

"""Tests for the monthly advance worked out from the last bill."""

from datetime import date
from typing import Any

import pytest

from custom_components.eneco_be.api_billing import MonthVolume
from custom_components.eneco_be.provision import (
    ProvisionError,
    Volumes,
    _spread,
    advances,
    catch_up,
    cost,
    index_volumes,
    parts,
    profile_range,
    project,
    reading_end,
    year_share,
)


def line(
    section: str,
    label: str,
    start: str,
    end: str,
    quantity: float | None,
    unit: str | None,
    unit_price: float | None,
    vat: float,
    total: float,
) -> dict[str, Any]:
    """Return a bill line as bills.py stores it."""
    return {
        "section": section,
        "label": label,
        "code": None,
        "start": start,
        "end": end,
        "quantity": quantity,
        "unit": unit,
        "unit_price": unit_price,
        "vat": vat,
        "total": total,
    }


def flat(year: int) -> dict[str, MonthVolume]:
    """Return a profile giving every day of ``year`` the same weight."""
    days = (31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return {
        f"{year}-{n:02d}": MonthVolume(float(d), 0.0) for n, d in enumerate(days, 1)
    }


# A year without solar panels: 2000 kWh at 10 c of energy and 5 c of network,
# 100 EUR a year, and a flat amount of -36.50 over the year.
PLAIN = [
    line("energy", "Conso", "2025-01-01", "2025-12-31", 2000, "kWh", 0.10, 0, 200),
    line("energy", "Redevance", "2025-01-01", "2025-12-31", 1, "year", 100, 0, 100),
    line(
        "network", "Distribution", "2025-01-01", "2025-12-31", 2000, "kWh", 0.05, 0, 100
    ),
    line("network", "Plafond", "2025-01-01", "2025-12-31", None, None, None, 0, -36.5),
]


def test_a_year_is_priced_on_the_last_bill() -> None:
    """Half the year measured, the other half the last year's same days."""
    got = project(
        energy_type="Electricity",
        lines=PLAIN,
        bill_period=(date(2025, 1, 1), date(2025, 12, 31)),
        bill_readings=({"offtake_single": 0}, {"offtake_single": 2000}),
        latest=(date(2026, 7, 2), {"offtake_single": 3000}),
        profile=flat(2025),
        rates=None,
        period_end=date(2026, 12, 31),
    )
    # 1000 kWh measured from 1 January to 1 July, and 2 July to 31 December
    # took 183 of the 365 days' 2000 kWh last year.
    offtake = 1000 + 2000 * 183 / 365
    period = offtake * 0.15 + 100 - 36.5
    assert got["period_start"] == "2026-01-01"
    assert got["period_end"] == "2026-12-31"
    assert got["measured_until"] == "2026-07-01"
    assert got["offtake_kwh"] == round(offtake, 1)
    assert got["volume_basis"] == "profile"
    assert got["energy_price"] == "last_bill"
    assert got["period_cost"] == round(period, 2)
    assert got["year_cost"] == round(period, 2)
    assert got["after_reading"] == round(period / 12, 2)


def test_without_a_profile_the_days_count_flat() -> None:
    """The portal's profile missing, each day of last year weighs the same."""
    got = project(
        energy_type="Electricity",
        lines=PLAIN,
        bill_period=(date(2025, 1, 1), date(2025, 12, 31)),
        bill_readings=({"offtake_single": 0}, {"offtake_single": 2000}),
        latest=(date(2026, 7, 2), {"offtake_single": 3000}),
        profile={},
        rates=None,
        period_end=date(2026, 12, 31),
    )
    assert got["volume_basis"] == "days"
    assert got["offtake_kwh"] == round(1000 + 2000 * 183 / 365, 1)


# A compensated year: energy and levies on what was taken less what was fed
# back, the network on what was taken.
COMPENSATED = [
    line("energy", "Conso", "2025-01-01", "2025-12-31", 1000, "kWh", 0.20, 0, 200),
    line(
        "network", "Distribution", "2025-01-01", "2025-12-31", 3000, "kWh", 0.10, 0, 300
    ),
    line("network", "Fixe", "2025-01-01", "2025-12-31", 1, "year", 12, 0, 12),
    line("taxes", "Raccordement", "2025-01-01", "2025-12-31", 3000, "kWh", 0.001, 0, 3),
]


def test_each_line_is_charged_on_the_volume_its_bill_used() -> None:
    """Net, gross or injection, as the line's quantity adds up to on its bill."""
    priced = parts(COMPENSATED, Volumes(3000, 2000), 365)
    assert [(p.label, p.how, p.basis) for p in priced] == [
        ("conso", "kwh", "net"),
        ("distribution", "kwh", "gross"),
        ("fixe", "year", None),
        ("raccordement", "kwh", "gross"),
    ]
    # More fed back than taken: nothing on the energy, the network on all
    # that was taken.
    assert cost(priced, Volumes(500, 800), 365, 0.0) == pytest.approx(500 * 0.101 + 12)
    assert cost(priced, Volumes(900, 400), 365, 0.0) == pytest.approx(
        500 * 0.20 + 900 * 0.101 + 12
    )


def test_a_line_matching_no_volume_is_refused() -> None:
    """A line on no volume of its bill would be priced on a wrong one."""
    odd = [
        *COMPENSATED,
        line("taxes", "Odd", "2025-01-01", "2025-12-31", 1700, "kWh", 0.01, 0, 17),
    ]
    with pytest.raises(ProvisionError, match="Odd matches none"):
        parts(odd, Volumes(3000, 2000), 365)


def test_readings_that_do_not_add_up_are_refused() -> None:
    """Readings that cannot give a volume give no amount."""
    with pytest.raises(ProvisionError, match="went back"):
        index_volumes({"offtake_day": 10}, {"offtake_day": 9})
    with pytest.raises(ProvisionError, match="same registers"):
        index_volumes({"offtake_day": 10}, {"offtake_night": 12})
    with pytest.raises(ProvisionError, match="no reading since"):
        project(
            energy_type="Electricity",
            lines=PLAIN,
            bill_period=(date(2025, 1, 1), date(2025, 12, 31)),
            bill_readings=({"offtake_single": 0}, {"offtake_single": 2000}),
            latest=(date(2025, 12, 31), {"offtake_single": 1990}),
            profile=flat(2025),
            rates=None,
            period_end=date(2026, 12, 31),
        )


GAS = [
    line(
        "energy", "Consommation", "2025-01-01", "2025-12-31", 11000, "kWh", 0.05, 0, 550
    ),
    line("energy", "Redevance", "2025-01-01", "2025-12-31", 1, "year", 50, 0, 50),
    line(
        "network",
        "Distribution",
        "2025-01-01",
        "2025-12-31",
        11000,
        "kWh",
        0.02,
        0,
        220,
    ),
]


def _gas(rates: dict[str, float] | None) -> dict[str, Any]:
    """Return the gas year of the tests, priced on ``rates`` if given."""
    return project(
        energy_type="Gas",
        lines=GAS,
        bill_period=(date(2025, 1, 1), date(2025, 12, 31)),
        bill_readings=({"offtake_single": 100}, {"offtake_single": 1100}),
        latest=(date(2026, 7, 2), {"offtake_single": 1600}),
        profile=flat(2025),
        rates=rates,
        period_end=date(2026, 12, 31),
    )


def test_gas_is_converted_and_priced_on_each_month_rate() -> None:
    """1000 m3 billed as 11000 kWh is 11 kWh a cubic metre.

    The energy line gives way to the rates, each month's kWh at its own.
    """
    on_bill = _gas(None)
    assert on_bill["energy_price"] == "last_bill"
    assert on_bill["measured_offtake_kwh"] == 5500.0
    months = [f"2026-{n:02d}" for n in range(1, 13)]
    low = _gas(dict.fromkeys(months, 0.05))
    high = _gas(dict.fromkeys(months, 0.10))
    assert low["energy_price"] == "price_integration"
    # The bill's own price and the same rate every month agree.
    assert low["period_cost"] == pytest.approx(on_bill["period_cost"], abs=0.02)
    energy = low["offtake_kwh"] * 0.05
    assert high["period_cost"] - low["period_cost"] == pytest.approx(energy, abs=0.05)
    # A year at today's prices takes the newest month's rate.
    rising = _gas({**dict.fromkeys(months, 0.05), "2026-12": 0.10})
    assert rising["year_cost"] == pytest.approx(high["year_cost"], abs=0.02)


def test_the_advances_of_the_period() -> None:
    """An advance whose month opened before the period is the last one's.

    A month gone by with no invoice is not one to come.
    """
    billed = {"2025-12": 50.0, "2026-01": 60.0, "2026-03": 60.0, "2026-10": 70.0}
    paid, coming, left = advances(
        period=(date(2025, 12, 17), date(2026, 12, 16)),
        billed=billed,
        advance_now=80.0,
        today=date(2026, 11, 26),
    )
    assert paid == 190.0
    # November and December 2026.
    assert (coming, left) == (160.0, 2)
    assert advances(
        period=(date(2025, 12, 17), date(2026, 12, 16)),
        billed=billed,
        advance_now=None,
        today=date(2026, 11, 26),
    ) == (190.0, None, 2)


# The yearly electricity bill of 1 February 2025 to 31 January 2026 of a
# compensated household in Wallonia, as read, with its official readings.
BILL_2025 = [
    line(
        "energy",
        "Redevance fixe",
        "2025-02-01",
        "2026-01-31",
        1.0,
        "year",
        49.59,
        0.06,
        49.59,
    ),
    line(
        "energy",
        "Conso. simple",
        "2025-02-01",
        "2025-03-31",
        334.51,
        "kWh",
        0.1735,
        0.06,
        58.04,
    ),
    line(
        "energy",
        "Conso. simple",
        "2025-04-01",
        "2025-06-30",
        354.18,
        "kWh",
        0.1291,
        0.06,
        45.72,
    ),
    line(
        "energy",
        "Conso. simple",
        "2025-07-01",
        "2025-09-30",
        330.06,
        "kWh",
        0.1342,
        0.06,
        44.28,
    ),
    line(
        "energy",
        "Conso. simple",
        "2025-10-01",
        "2025-12-31",
        492.96,
        "kWh",
        0.145,
        0.06,
        71.47,
    ),
    line(
        "energy",
        "Conso. simple",
        "2026-01-01",
        "2026-01-31",
        195.8,
        "kWh",
        0.1589,
        0.06,
        31.11,
    ),
    line(
        "energy",
        "Coûts énergie verte",
        "2025-02-01",
        "2026-01-31",
        1707.52,
        "kWh",
        0.0283,
        0.06,
        48.3,
    ),
    line(
        "network",
        "Distribution variable (kWh)",
        "2025-02-01",
        "2025-02-28",
        596.28,
        "kWh",
        0.1018,
        0.06,
        60.71,
    ),
    line(
        "network",
        "Distribution variable (kWh)",
        "2025-03-01",
        "2025-12-31",
        4602.72,
        "kWh",
        0.1018,
        0.06,
        468.59,
    ),
    line(
        "network",
        "Distribution variable (kWh)",
        "2026-01-01",
        "2026-01-31",
        721.49,
        "kWh",
        0.113,
        0.06,
        81.55,
    ),
    line(
        "network",
        "Distribution fixe",
        "2025-02-01",
        "2025-02-28",
        0.08,
        "year",
        13.04,
        0.06,
        1.0,
    ),
    line(
        "network",
        "Distribution fixe",
        "2025-03-01",
        "2025-12-31",
        0.84,
        "year",
        13.06,
        0.06,
        10.94,
    ),
    line(
        "network",
        "Distribution fixe",
        "2026-01-01",
        "2026-01-31",
        0.08,
        "year",
        13.3,
        0.06,
        1.13,
    ),
    line(
        "network",
        "Tarif prosumer plafonnement",
        "2025-02-01",
        "2025-02-28",
        None,
        None,
        None,
        0.06,
        -24.54,
    ),
    line(
        "network",
        "Tarif prosumer plafonnement",
        "2025-03-01",
        "2025-12-31",
        None,
        None,
        None,
        0.06,
        -81.62,
    ),
    line(
        "network",
        "Tarif prosumer plafonnement",
        "2026-01-01",
        "2026-01-31",
        None,
        None,
        None,
        0.06,
        -36.92,
    ),
    line(
        "network",
        "Coûts de transmission (kWh)",
        "2025-02-01",
        "2025-02-28",
        596.28,
        "kWh",
        0.0302,
        0.06,
        18.0,
    ),
    line(
        "network",
        "Coûts de transmission (kWh)",
        "2025-03-01",
        "2025-12-31",
        4602.72,
        "kWh",
        0.0281,
        0.06,
        129.46,
    ),
    line(
        "network",
        "Coûts de transmission (kWh)",
        "2026-01-01",
        "2026-01-31",
        721.49,
        "kWh",
        0.0259,
        0.06,
        18.67,
    ),
    line(
        "taxes",
        "Redevance de raccordement",
        "2025-02-01",
        "2026-01-31",
        1707.52,
        "kWh",
        0.0008,
        0.0,
        1.27,
    ),
    line(
        "taxes",
        "Accise spéciale",
        "2025-02-01",
        "2026-01-31",
        1707.52,
        "kWh",
        0.0475,
        0.06,
        81.07,
    ),
    line(
        "taxes",
        "Cotisation sur l'énergie",
        "2025-02-01",
        "2026-01-31",
        1707.52,
        "kWh",
        0.0019,
        0.06,
        3.3,
    ),
]
OPENED = {
    "offtake_day": 5183,
    "offtake_night": 7654,
    "injection_day": 5473,
    "injection_night": 2194,
}
CLOSED = {
    "offtake_day": 7722,
    "offtake_night": 11036,
    "injection_day": 8526,
    "injection_night": 3355,
}
NOW = {
    "offtake_day": 9309,
    "offtake_night": 13299,
    "injection_day": 10137,
    "injection_night": 6530,
}
# What the portal's consumption page showed taken each month, in kWh.
OFFTAKE_2025 = dict(
    zip(
        [f"2025-{n:02d}" for n in range(2, 13)] + ["2026-01"],
        (
            660.5,
            558.4,
            356.4,
            250.4,
            198.4,
            209.6,
            256.9,
            325.5,
            507.5,
            698.0,
            949.8,
            949.5,
        ),
        strict=True,
    )
)


def test_a_compensated_household_against_its_own_bills() -> None:
    """The bill's lines on their own volumes, and the running year.

    Held against a projection worked out apart from this code on 26
    November 2026: about 996 EUR for the year, 83 a month.
    """
    assert index_volumes(OPENED, CLOSED) == Volumes(5921, 4214)
    priced = parts(BILL_2025, index_volumes(OPENED, CLOSED), 365)
    assert {p.label: p.basis for p in priced if p.how == "kwh"} == {
        "conso. simple": "net",
        "coûts énergie verte": "net",
        "distribution variable (kwh)": "gross",
        "coûts de transmission (kwh)": "gross",
        "redevance de raccordement": "net",
        "accise spéciale": "net",
        "cotisation sur l'énergie": "net",
    }
    got = project(
        energy_type="Electricity",
        lines=BILL_2025,
        bill_period=(date(2025, 2, 1), date(2026, 1, 31)),
        bill_readings=(OPENED, CLOSED),
        latest=(date(2026, 11, 25), NOW),
        profile={m: MonthVolume(v, 0.0) for m, v in OFFTAKE_2025.items()},
        rates=None,
        period_end=date(2027, 1, 31),
    )
    assert got["measured_offtake_kwh"] == 3850.0
    assert got["measured_injection_kwh"] == 4786.0
    assert got["period_cost"] == pytest.approx(996, rel=0.05)
    assert got["after_reading"] == pytest.approx(83, rel=0.05)


def test_the_profile_is_asked_in_whole_months() -> None:
    """The portal cuts the last month at the end date: ask to its end."""
    assert profile_range(date(2025, 1, 6), date(2025, 12, 16)) == (
        date(2025, 1, 1),
        date(2025, 12, 31),
    )
    assert profile_range(date(2024, 2, 1), date(2025, 2, 3)) == (
        date(2024, 2, 1),
        date(2025, 2, 28),
    )


def test_the_period_ends_with_the_reading_month() -> None:
    """The month the portal gives, the one nearest a year on."""
    assert reading_end(date(2026, 2, 1), 1) == (date(2027, 1, 31), "reading_month")
    # Read on 17 December last time, in January this time: a longer period.
    assert reading_end(date(2025, 12, 17), 1) == (date(2027, 1, 31), "reading_month")
    assert reading_end(date(2026, 2, 1), 2) == (date(2027, 2, 28), "reading_month")
    assert reading_end(date(2025, 12, 17), None) == (date(2026, 12, 16), "anniversary")


def test_the_advance_that_brings_the_regularisation_to_zero() -> None:
    """What is left to pay over the advances left, never below 0."""
    assert catch_up(995.92, 583.13, 3) == 137.6
    assert catch_up(1500.0, 2000.0, 2) == 0.0
    assert catch_up(995.92, 583.13, 0) is None


def test_the_rest_may_reach_past_the_last_period() -> None:
    """Read in January after a bill closed on 16 December.

    The days a year back from 17 December to 31 January weigh in too.
    """
    lines = [
        line("energy", "Conso", "2025-01-01", "2025-12-16", 3500, "kWh", 0.10, 0, 350),
    ]
    profile = {**flat(2025), "2026-01": MonthVolume(31.0, 0.0)}
    got = project(
        energy_type="Electricity",
        lines=lines,
        bill_period=(date(2025, 1, 1), date(2025, 12, 16)),
        bill_readings=({"offtake_single": 0}, {"offtake_single": 3500}),
        latest=(date(2026, 12, 17), {"offtake_single": 7000}),
        profile=profile,
        rates=None,
        period_end=date(2027, 1, 31),
    )
    # The 350 days of the last period took 3500 kWh, 10 a day: 17 December
    # to 31 January a year back is 46 of those days.
    assert got["offtake_kwh"] == 3500 + 460.0
    assert got["period_cost"] == round((3500 + 460) * 0.10, 2)


def test_a_period_longer_than_a_year_is_scaled_by_its_months() -> None:
    """Two Januaries in 13 months weigh more than 365 of their 396 days.

    The year is the weight of its 12 months, not a share of the days as if
    they were alike.
    """
    profile = {f"2025-{n:02d}": 100.0 for n in range(1, 13)} | {"2025-01": 400.0}
    # 1 January 2026 to 31 January 2027 weighs 1500 + 400.
    assert year_share(date(2026, 1, 1), date(2027, 1, 31), profile) == pytest.approx(
        1500 / 1900
    )
    # A whole year scales to itself, and a profile short of a month is flat.
    assert year_share(date(2026, 2, 1), date(2027, 1, 31), profile) == pytest.approx(1)
    assert year_share(date(2026, 1, 1), date(2027, 1, 31), {"2025-01": 1.0}) == (
        pytest.approx(365 / 396)
    )


def test_a_year_from_a_long_period_is_scaled_by_its_months() -> None:
    """The advance after the reading takes a year out of 13 months.

    On the profile, 3800 kWh with two heavy Januaries are 3000 a year.
    """
    lines = [
        line("energy", "Conso", "2025-01-01", "2025-12-31", 3650, "kWh", 0.10, 0, 365)
    ]
    profile = {
        f"2025-{n:02d}": MonthVolume(400.0 if n == 1 else 100.0, 0.0)
        for n in range(1, 13)
    }
    got = project(
        energy_type="Electricity",
        lines=lines,
        bill_period=(date(2025, 1, 1), date(2025, 12, 31)),
        bill_readings=({"offtake_single": 0}, {"offtake_single": 3650}),
        latest=(date(2027, 2, 1), {"offtake_single": 3650 + 3800}),
        profile=profile,
        rates=None,
        period_end=date(2027, 1, 31),
    )
    assert got["period_cost"] == 380.0
    assert got["year_cost"] == pytest.approx(300.0, abs=0.01)
    assert got["after_reading"] == 25.0


@pytest.mark.parametrize(
    "profile",
    [
        # March known, February not: February took 1 against March's 10 a day.
        {"2025-03": MonthVolume(310.0, 0.0)},
        # Nothing taken a year back: the kWh went nowhere and cost nothing.
        {"2025-02": MonthVolume(0.0, 0.0), "2025-03": MonthVolume(0.0, 0.0)},
    ],
)
def test_gas_is_spread_by_days_where_the_profile_cannot_say(
    profile: dict[str, MonthVolume],
) -> None:
    """Each month is priced at its own rate, so it must get its own share."""
    got = _spread(date(2026, 2, 1), date(2026, 3, 31), 590.0, profile)
    assert got == pytest.approx(
        {"2026-02": 590.0 * 28 / 59, "2026-03": 590.0 * 31 / 59}
    )


def test_a_period_from_29_february_runs_a_whole_year() -> None:
    """A year on from 29 February was taken as 28 February, a day short.

    The period then ended on the 27th, and the reading of 1 March that
    closes it fell outside.
    """
    assert reading_end(date(2024, 2, 29), None) == (date(2025, 2, 28), "anniversary")

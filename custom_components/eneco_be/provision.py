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

"""What a meter needs as a monthly advance, worked out from its last bill.

The running billing period starts the day after the last yearly bill ends
and runs a year. What it will cost is that bill's own pricing applied to the
period's volumes:

- each kWh line at its latest unit price, on the volume that bill charged it
  on: net (taken less fed back, under compensation), gross, or injection,
  told by which of the three its quantities add up to;
- each line per year by the day, and each line with no quantity (the
  prosumer cap) at what it came to per day;
- gas energy month by month at the price integration's rate when one checks
  the bill, since an index can move a lot in a year.

The volumes so far are the meter's indexes less the official reading that
closed the last bill, which is how the bill itself counts them. The rest of
the period is the last period's volume times the share of it its remaining
days took: on the portal's monthly profile for what is taken, on a solar
profile for what is fed back.

The advance needed is a year like the running one at today's prices, divided
by twelve. Nothing here does I/O: bills.py gathers the inputs.
"""

from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from .api_billing import MonthVolume
from .const import GAS

# Approximate share of a year's PV yield in each month in Belgium. Only the
# shape counts, so the rest of a period gets the solar share it should and
# not a flat one.
SOLAR: Final = (30, 50, 85, 115, 130, 130, 130, 115, 90, 60, 32, 23)
# How far a line's quantity may be from the volume it is charged on.
MATCH: Final = 0.05


class ProvisionError(Exception):
    """The inputs do not allow a projection; the text says why."""


@dataclass(frozen=True, slots=True)
class Part:
    """One way the last bill charges: per kWh of a volume, per year or per day."""

    label: str
    how: str  # "kwh", "year" or "day"
    basis: str | None  # "net", "gross" or "injection" for "kwh"
    rate: float  # EUR VAT included, per kWh, per year or per day
    energy: bool  # the energy line itself, which a price integration may price


@dataclass(frozen=True, slots=True)
class Volumes:
    """Taken and fed back over some days, in kWh."""

    offtake: float
    injection: float

    @property
    def net(self) -> float:
        """Return what was taken above what was fed back, never below 0."""
        return max(0.0, self.offtake - self.injection)


def _day(text: str) -> date:
    return date.fromisoformat(text)


def _month(day: date) -> str:
    return f"{day:%Y-%m}"


def _shift(day: date, years: int) -> date:
    """The same day ``years`` later, 28 February for a 29th."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def year_after(day: date) -> date:
    """Return the same day a year later."""
    return _shift(day, 1)


def year_before(day: date) -> date:
    """Return the same day a year earlier."""
    return _shift(day, -1)


def index_volumes(
    first: Mapping[str, int], last: Mapping[str, int], factor: float = 1.0
) -> Volumes:
    """Return what was taken and fed back between two readings of a meter."""
    if set(first) != set(last) or not first:
        raise ProvisionError("the readings do not name the same registers")
    taken = fed = 0.0
    for key, value in last.items():
        moved = value - first[key]
        if moved < 0:
            raise ProvisionError(f"the {key} index went back")
        if key.startswith("injection"):
            fed += moved
        else:
            taken += moved
    return Volumes(taken * factor, fed * factor)


def gas_factor(lines: list[dict[str, Any]], cubic_metres: float) -> float:
    """Return the kWh a cubic metre was billed at: the bill's kWh over its m3."""
    kwh: float = sum(
        line["quantity"]
        for line in lines
        if line["section"] == "energy" and line["unit"] == "kWh"
    )
    if cubic_metres <= 0 or kwh <= 0:
        raise ProvisionError("the last bill gives no gas volume to convert with")
    return kwh / cubic_metres


def parts(lines: list[dict[str, Any]], billed: Volumes, days: int) -> list[Part]:
    """Return how the last bill charges, from its lines and its own volumes."""
    candidates = {
        "net": billed.net,
        "gross": billed.offtake,
        "injection": billed.injection,
    }
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for line in lines:
        groups.setdefault((line["section"], line["label"].lower()), []).append(line)
    out: list[Part] = []
    for (section, label), group in groups.items():
        latest = max(group, key=lambda line: line["end"])
        vat = 1 + latest["vat"]
        if latest["unit"] == "kWh" and all(line["unit"] == "kWh" for line in group):
            quantity = sum(line["quantity"] for line in group)
            basis = min(candidates, key=lambda name: abs(quantity - candidates[name]))
            target = candidates[basis]
            if abs(quantity - target) > MATCH * max(target, 1.0):
                raise ProvisionError(
                    f"the bill's {latest['label']} matches none of its volumes"
                )
            out.append(
                Part(
                    label, "kwh", basis, latest["unit_price"] * vat, section == "energy"
                )
            )
        elif latest["unit"] == "year" and latest["unit_price"] is not None:
            out.append(Part(label, "year", None, latest["unit_price"] * vat, False))
        else:
            total = sum(line["total"] * (1 + line["vat"]) for line in group)
            out.append(Part(label, "day", None, total / days, False))
    return out


def _weight(day: date, profile: Mapping[str, float]) -> float | None:
    """Return a day's share of its month in ``profile``, keyed "YYYY-MM"."""
    value = profile.get(_month(day))
    return None if value is None else value / monthrange(day.year, day.month)[1]


def _share(
    first: date, last: date, rest: date, profile: Mapping[str, float]
) -> float | None:
    """Return the share of the days ``first`` to ``last`` from ``rest`` on.

    Each day weighs its month's value in ``profile`` over the days of that
    month; None if the profile misses one of the months.
    """
    whole = part = 0.0
    day = first
    while day <= last:
        weight = _weight(day, profile)
        if weight is None:
            return None
        whole += weight
        if day >= rest:
            part += weight
        day += timedelta(days=1)
    return part / whole if whole > 0 else None


def _months_of(first: date, last: date) -> list[str]:
    """Return the months the days ``first`` to ``last`` fall in, "YYYY-MM"."""
    months: list[str] = []
    day = first.replace(day=1)
    while day <= last:
        months.append(_month(day))
        day = (day + timedelta(days=32)).replace(day=1)
    return months


def _solar(first: date, last: date) -> dict[str, float]:
    """Return the solar profile over the months from ``first`` to ``last``."""
    return {
        month: float(SOLAR[int(month[5:]) - 1]) for month in _months_of(first, last)
    }


def rest_of_period(
    last_period: tuple[date, date],
    rest_from: date,
    billed: Volumes,
    profile: Mapping[str, MonthVolume],
) -> tuple[Volumes, str]:
    """Return the volume of the days left, read off the last period's same days.

    ``rest_from`` is the first day left of the running period, and the last
    period is the year before it. Without the portal's profile for every
    month of the last period, the days are counted flat, and the basis says
    so.
    """
    first, last = last_period
    rest = year_before(rest_from)
    if rest > last:
        return Volumes(0.0, 0.0), "measured"
    rest = max(rest, first)
    taken = _share(first, last, rest, {k: v.offtake for k, v in profile.items()})
    basis = "profile"
    if taken is None:
        basis = "days"
        taken = ((last - rest).days + 1) / ((last - first).days + 1)
    fed = _share(first, last, rest, _solar(first, last)) or 0.0
    return Volumes(billed.offtake * taken, billed.injection * fed), basis


def cost(
    priced: list[Part],
    volumes: Volumes,
    days: int,
    energy: float,
) -> float:
    """Return what ``volumes`` over ``days`` cost.

    ``energy`` is the gas energy priced apart, 0 when the bill's own energy
    line is among ``priced``.
    """
    on = {"net": volumes.net, "gross": volumes.offtake, "injection": volumes.injection}
    total = energy
    for part in priced:
        if part.how == "kwh" and part.basis is not None:
            total += part.rate * on[part.basis]
        elif part.how == "year":
            total += part.rate * days / 365
        else:
            total += part.rate * days
    return total


def _spread(
    first: date, last: date, kwh: float, profile: Mapping[str, MonthVolume]
) -> dict[str, float]:
    """Spread ``kwh`` over the months of the days ``first`` to ``last``.

    Each day weighs what the profile has for the same day a year earlier,
    and counts flat where it has nothing.
    """
    if last < first or kwh <= 0:
        return {}
    weights: dict[str, float] = {}
    day = first
    while day <= last:
        back = year_before(day)
        known = profile.get(_month(back))
        weight = (
            known.offtake / monthrange(back.year, back.month)[1]
            if known is not None
            else 1.0
        )
        weights[_month(day)] = weights.get(_month(day), 0.0) + weight
        day += timedelta(days=1)
    whole = sum(weights.values())
    return (
        {month: kwh * weight / whole for month, weight in weights.items()}
        if whole
        else {}
    )


def project(
    *,
    energy_type: str,
    lines: list[dict[str, Any]],
    bill_period: tuple[date, date],
    bill_readings: tuple[Mapping[str, int], Mapping[str, int]],
    latest: tuple[date, Mapping[str, int]],
    profile: Mapping[str, MonthVolume],
    rates: Mapping[str, float] | None,
) -> dict[str, Any]:
    """Return the running period's volumes and cost, and the advance needed.

    ``bill_readings`` are the official readings that opened and closed the
    last bill, ``latest`` the newest reading and its date, ``rates`` the gas
    energy price of each month of the period, VAT included, if a price
    integration gave them.
    """
    bill_first, bill_last = bill_period
    bill_days = (bill_last - bill_first).days + 1
    opened, closed = bill_readings
    factor = 1.0
    if energy_type == GAS:
        cubic = index_volumes(opened, closed)
        factor = gas_factor(lines, cubic.offtake)
    billed = index_volumes(opened, closed, factor)
    priced = parts(lines, billed, bill_days)

    start = bill_last + timedelta(days=1)
    end = year_after(start) - timedelta(days=1)
    days = (end - start).days + 1
    # A reading dated D is the index as D starts, as the official reading of
    # 1 February closes a bill ending on 31 January: the meter has measured
    # the days before D, and D on are left.
    read_on, values = latest
    if read_on < start:
        raise ProvisionError("no reading since the last bill")
    measured = index_volumes(closed, values, factor)
    rest_from = min(read_on, end + timedelta(days=1))
    measured_to = rest_from - timedelta(days=1)
    rest, basis = rest_of_period(bill_period, rest_from, billed, profile)
    volumes = Volumes(
        measured.offtake + rest.offtake, measured.injection + rest.injection
    )

    energy = 0.0
    year_energy = 0.0
    priced_by = "last_bill"
    if energy_type == GAS and rates:
        # The gas energy at each month's rate: the bill's own energy line is
        # taken out, and the kWh spread over the months they fall in.
        energy_parts = [part for part in priced if part.energy and part.how == "kwh"]
        if energy_parts:
            priced = [part for part in priced if part not in energy_parts]
            months = _spread(start, measured_to, measured.offtake, profile)
            for month, kwh in _spread(rest_from, end, rest.offtake, profile).items():
                months[month] = months.get(month, 0.0) + kwh
            latest_rate = rates[max(rates)]
            energy = sum(
                kwh * rates.get(month, latest_rate) for month, kwh in months.items()
            )
            year_energy = volumes.offtake * 365 / days * latest_rate
            priced_by = "price_integration"

    period_cost = cost(priced, volumes, days, energy)
    scale = 365 / days
    year = Volumes(volumes.offtake * scale, volumes.injection * scale)
    year_cost = cost(priced, year, 365, year_energy)
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "measured_until": measured_to.isoformat(),
        "offtake_kwh": round(volumes.offtake, 1),
        "injection_kwh": round(volumes.injection, 1),
        "net_kwh": round(volumes.net, 1),
        "measured_offtake_kwh": round(measured.offtake, 1),
        "measured_injection_kwh": round(measured.injection, 1),
        "volume_basis": basis,
        "energy_price": priced_by,
        "period_cost": round(period_cost, 2),
        "year_cost": round(year_cost, 2),
        "monthly_advance": round(year_cost / 12, 2),
    }


def period_months(start: date, end: date) -> list[str]:
    """Return the months whose advance belongs to the period, "YYYY-MM".

    An advance is for a calendar month; one whose month starts before the
    period opened belongs to the period before.
    """
    return [m for m in _months_of(start, end) if date.fromisoformat(f"{m}-01") >= start]


def advances(
    *,
    period: tuple[date, date],
    billed: Mapping[str, float],
    advance_now: float | None,
    today: date,
) -> tuple[float, float | None]:
    """Return the advances billed in the period and those still to come.

    ``billed`` maps the month each advance invoice is for, "YYYY-MM", to this
    meter's share of it. Every month of the period from this one on that has
    no invoice yet will be billed ``advance_now``; None when that is unknown.
    """
    in_period = period_months(*period)
    paid = sum(billed.get(month, 0.0) for month in in_period)
    coming = [m for m in in_period if m not in billed and m >= _month(today)]
    if not coming:
        return round(paid, 2), 0.0
    if advance_now is None:
        return round(paid, 2), None
    return round(paid, 2), round(advance_now * len(coming), 2)

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

"""The year billed held against what the price integration prices it at.

The electricity and gas price integrations (be_electricity_prices,
be_gas_prices) can check an Eneco bill: an entry of theirs names this
meter's bill sensor in its settings. That entry's bill_cost action prices
the meter's own history over the days the year covers, and the difference
is what Eneco billed above it. Nothing is asked when no entry names the
sensor, and nothing is set here to link them: the price entry's link is it.
"""

import logging
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .api_billing import amount
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The price integrations, and the key of their entry data naming the bill
# sensor they check.
PRICE_LINKS: Final = {
    "be_electricity_prices": "eneco_bill",
    "be_gas_prices": "bill_entity",
}
SERVICE: Final = "bill_cost"
# The action giving the energy price of each month, to project a bill with.
RATES: Final = "energy_rate"


def _price_entry(hass: HomeAssistant, entity_id: str) -> ConfigEntry | None:
    """Return the loaded price entry checking the bill sensor, if one does."""
    for domain, key in PRICE_LINKS.items():
        for entry in hass.config_entries.async_loaded_entries(domain):
            if entry.data.get(key) == entity_id:
                return entry
    return None


class RatesUnavailableError(Exception):
    """The price entry checking the bill gave no rates; the text says why."""


async def async_energy_rates(
    hass: HomeAssistant, unique_id: str, start: str, end: str
) -> dict[str, float] | None:
    """Return the energy price of each month, VAT included.

    Given by the entry checking the bill sensor, or None when none does, or
    one too old to price by the month: the bill's own price then stands.
    One that does but cannot answer raises RatesUnavailableError, since the
    bill's price can be far from today's, and an advance set from it far
    too low.
    """
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    # One the user disabled is no entry checking the bill.
    linked = [
        entry
        for domain, key in PRICE_LINKS.items()
        for entry in hass.config_entries.async_entries(domain)
        if entity_id is not None
        and entry.data.get(key) == entity_id
        and entry.disabled_by is None
    ]
    entry = next(
        (e for e in linked if e.state is ConfigEntryState.LOADED),
        linked[0] if linked else None,
    )
    if entry is None:
        return None
    if entry.state is not ConfigEntryState.LOADED:
        raise RatesUnavailableError(f"{entry.title} is not loaded")
    if not hass.services.has_service(entry.domain, RATES):
        return None
    try:
        answer = await hass.services.async_call(
            entry.domain,
            RATES,
            {"entry_id": entry.entry_id, "start": start, "end": end},
            blocking=True,
            return_response=True,
        )
    # Another integration's failure, whatever it raised: the next read asks
    # again.
    except Exception as err:
        _LOGGER.debug("%s gave no energy rates: %r", entry.title, err)
        raise RatesUnavailableError(f"{entry.title} gave no rates: {err}") from err
    months = answer.get("months") if answer is not None else None
    rates: dict[str, float] = {}
    for row in months if isinstance(months, list) else []:
        month = row.get("month") if isinstance(row, dict) else None
        price = amount(row.get("eur_per_kwh")) if isinstance(row, dict) else None
        if not isinstance(month, str) or price is None:
            raise RatesUnavailableError(f"{entry.title} gave a rate that is no price")
        rates[month] = price
    if not rates:
        raise RatesUnavailableError(f"{entry.title} priced no month")
    return rates


def _result(error: str, **found: Any) -> dict[str, Any]:
    return {
        "expected": None,
        "difference": None,
        "covered": None,
        "price_entry": None,
        "entry_id": None,
        "error": error,
    } | found


async def async_compare(
    hass: HomeAssistant, unique_id: str, year: dict[str, Any]
) -> dict[str, Any]:
    """Return what the year is priced at by the entry checking its sensor.

    The difference is only given when the meter's history covers every day
    of the year: priced over fewer days, the cost would pass for a bill far
    above what was used.
    """
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    entry = None if entity_id is None else _price_entry(hass, entity_id)
    if entry is None:
        return _result("no electricity or gas price entry checks this bill")
    if not hass.services.has_service(entry.domain, SERVICE):
        return _result(
            f"{entry.title} cannot price a bill: update it",
            price_entry=entry.title,
            entry_id=entry.entry_id,
        )
    try:
        answer = await hass.services.async_call(
            entry.domain,
            SERVICE,
            {
                "entry_id": entry.entry_id,
                "start": year["period_start"],
                "end": year["period_end"],
            },
            blocking=True,
            return_response=True,
        )
    # Another integration's failure, whatever it raised: the bill read
    # stands, and the next read asks again.
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("%s did not price the bill: %r", entry.title, err)
        return _result(
            f"{entry.title} did not price the bill: {err}",
            price_entry=entry.title,
            entry_id=entry.entry_id,
        )
    cost = amount(answer.get("cost")) if answer is not None else None
    if answer is None or cost is None:
        return _result(
            f"{entry.title} priced nothing for the bill",
            price_entry=entry.title,
            entry_id=entry.entry_id,
        )
    expected = round(cost, 2)
    if answer.get("covered") is not True:
        return _result(
            "the meter's history does not cover every day of the bill",
            expected=expected,
            covered=False,
            price_entry=entry.title,
            entry_id=entry.entry_id,
        )
    return {
        "expected": expected,
        "difference": round(year["total"] - expected, 2),
        "covered": True,
        "price_entry": entry.title,
        "entry_id": entry.entry_id,
        "error": None,
    }

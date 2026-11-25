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

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The price integrations, and the key of their entry data naming the bill
# sensor they check.
PRICE_LINKS: Final = {
    "be_electricity_prices": "eneco_bill",
    "be_gas_prices": "bill_entity",
}
SERVICE: Final = "bill_cost"


def _price_entry(hass: HomeAssistant, entity_id: str) -> ConfigEntry | None:
    """Return the loaded price entry checking the bill sensor, if one does."""
    for domain, key in PRICE_LINKS.items():
        for entry in hass.config_entries.async_loaded_entries(domain):
            if entry.data.get(key) == entity_id:
                return entry
    return None


def _result(error: str, **found: Any) -> dict[str, Any]:
    return {
        "expected": None,
        "difference": None,
        "covered": None,
        "price_entry": None,
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
            f"{entry.title} cannot price a bill: update it", price_entry=entry.title
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
            f"{entry.title} did not price the bill: {err}", price_entry=entry.title
        )
    cost = answer.get("cost") if answer is not None else None
    if answer is None or isinstance(cost, bool) or not isinstance(cost, int | float):
        return _result(
            f"{entry.title} priced nothing for the bill", price_entry=entry.title
        )
    expected = round(float(cost), 2)
    if answer.get("covered") is not True:
        return _result(
            "the meter's history does not cover every day of the bill",
            expected=expected,
            covered=False,
            price_entry=entry.title,
        )
    return {
        "expected": expected,
        "difference": round(year["total"] - expected, 2),
        "covered": True,
        "price_entry": entry.title,
        "error": None,
    }

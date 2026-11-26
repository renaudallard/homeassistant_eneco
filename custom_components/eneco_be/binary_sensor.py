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

"""Whether the year billed disagrees with what it is priced at.

Two things are held to the same threshold, DIFFERENCE_EUR or
DIFFERENCE_SHARE of the bill, whichever is more: what the lines the price
entry's Bill check finds billed at another unit price move the bill by, and
how far the year billed is from what the entry prices the meter's history
at. On when either goes past it, off when neither does, unknown while it
cannot be told. A line is weighed by its gap in unit price times its
quantity on the bill, VAT included, so a rate a card prints a hair off does
not make a bill disagree, and a wrong rate on a large volume does.
"""

from collections.abc import Mapping
import math
from typing import Any, Final, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import EnecoConfigEntry
from .bills import BillCoordinator
from .const import ATTR_LAST_ERROR, BILL_DISAGREES
from .entity import BillEntity

# A total within this much of the price is put down to the meter Home
# Assistant reads not being the one the network operator reads, and to
# rounding: 5 EUR, or 1% of a larger bill.
DIFFERENCE_EUR: Final = 5.0
DIFFERENCE_SHARE: Final = 0.01


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnecoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one per meter, with the bill sensors."""
    bills = entry.runtime_data.bills
    if bills is None:
        # The sensor platform takes the entities away.
        return
    for subentry_id, coordinator in entry.runtime_data.coordinators.items():
        async_add_entities(
            [BillDisagreesSensor(bills, subentry_id, coordinator.meter)],
            config_subentry_id=subentry_id,
        )


def threshold(total: float) -> float:
    """Return how far a year's total may be from its price, in EUR."""
    return max(DIFFERENCE_EUR, abs(total) * DIFFERENCE_SHARE)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _gap(billed: float, priced: Any) -> float | None:
    """Return how far a billed unit price is from the one priced.

    Where a range is priced, from its nearest end.
    """
    if isinstance(priced, list | tuple) and len(priced) == 2:
        low, high = (_number(value) for value in priced)
        if low is None or high is None:
            return None
        return max(low - billed, billed - high, 0.0)
    price = _number(priced)
    return None if price is None else abs(billed - price)


def line_amount(line: Mapping[str, Any], billed_lines: list[Any]) -> float | None:
    """Return what a line found off moves the bill by, VAT included.

    Its gap in unit price times its quantity, from the same line of the
    bill. None when the bill holds no such line or the prices are not
    numbers.
    """
    billed = _number(line.get("billed"))
    if billed is None:
        return None
    gap = _gap(billed, line.get("priced"))
    if gap is None:
        return None
    key = (line.get("label"), line.get("section"), line.get("start"), line.get("end"))
    for raw in billed_lines:
        if not isinstance(raw, Mapping):
            continue
        same = (raw.get("label"), raw.get("section"), raw.get("start"), raw.get("end"))
        if same != key or _number(raw.get("unit_price")) != billed:
            continue
        quantity = _number(raw.get("quantity"))
        vat = _number(raw.get("vat")) or 0.0
        if quantity is None:
            return None
        return abs(gap * quantity) * (1 + vat)
    return None


class BillDisagreesSensor(BillEntity, BinarySensorEntity):
    """On when the bill disagrees with the price entry checking it."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, BILL_DISAGREES)
        # The price entry's Bill check sensor, followed while it is the one.
        self._check: str | None = None
        self._unsubscribe: Any = None

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the Bill check sensor of the entry the comparison names."""
        await super().async_added_to_hass()
        self._follow()
        self.async_on_remove(self._stop)

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Follow another Bill check sensor when the comparison moves."""
        self._follow()
        super()._handle_coordinator_update()

    @callback
    def _follow(self) -> None:
        check = self._check_entity()
        if check == self._check:
            return
        self._stop()
        self._check = check
        if check is not None:

            @callback
            def _changed(_event: Event[EventStateChangedData]) -> None:
                self.async_write_ha_state()

            self._unsubscribe = async_track_state_change_event(
                self.hass, check, _changed
            )

    @callback
    def _stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _comparison(self) -> dict[str, Any]:
        comparison: dict[str, Any] = self._read.get("comparison") or {}
        return comparison

    def _check_entity(self) -> str | None:
        """Return the Bill check sensor of the price entry comparing the year."""
        entry_id = self._comparison().get("entry_id")
        entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is None:
            return None
        return er.async_get(self.hass).async_get_entity_id(
            "sensor", entry.domain, f"{entry_id}_bill_check"
        )

    def _lines_off(self) -> list[dict[str, Any]] | None:
        """Return each line the Bill check finds off and what it moves.

        None before the price entry's check has counted.
        """
        state = self.hass.states.get(self._check) if self._check else None
        if state is None:
            return None
        try:
            int(state.state)
        except ValueError:
            return None
        year = self._read.get("year") or {}
        for key in ("period_start", "period_end"):
            checked = state.attributes.get(key)
            if checked is not None and str(checked)[:10] != year.get(key):
                # A check of another year, until it catches up with this
                # one: its lines would match none here.
                return None
        lines = state.attributes.get("lines")
        billed = year.get("lines") or []
        out: list[dict[str, Any]] = []
        for line in lines if isinstance(lines, list) else []:
            if not isinstance(line, Mapping) or line.get("status") != "off":
                continue
            amount = line_amount(line, billed)
            out.append(
                {
                    "label": line.get("label"),
                    "start": line.get("start"),
                    "end": line.get("end"),
                    "eur": None if amount is None else round(amount, 2),
                }
            )
        return out

    def _verdict(self) -> tuple[bool | None, str | None]:
        """Return whether the bill disagrees, and why it cannot be told."""
        year = self._read.get("year")
        if not year:
            return None, "no year billed yet"
        limit = threshold(year["total"])
        comparison = self._comparison()
        difference = comparison.get("difference")
        lines = self._lines_off()
        if lines is not None:
            # A line that cannot be weighed is not taken for a small one.
            if any(line["eur"] is None for line in lines):
                return True, None
            if sum(line["eur"] for line in lines) > limit:
                # Prices billed off are a disagreement whatever the total says.
                return True, None
        if difference is None:
            return None, comparison.get("error") or "the year is not priced yet"
        if abs(difference) > limit:
            return True, None
        if lines is None:
            return None, "the price entry's Bill check has not checked this year yet"
        return False, None

    @property
    @override
    def is_on(self) -> bool | None:
        """Return whether the bill disagrees, or None while it cannot be told."""
        return self._verdict()[0]

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return what disagrees and by how much, or why it is not known."""
        year = self._read.get("year") or {}
        total = year.get("total")
        lines = self._lines_off()
        return {
            "lines_off": lines,
            "lines_off_eur": (
                None
                if lines is None or any(line["eur"] is None for line in lines)
                else round(sum(line["eur"] for line in lines), 2)
            ),
            "difference": self._comparison().get("difference"),
            "threshold": None if total is None else round(threshold(total), 2),
            ATTR_LAST_ERROR: self._verdict()[1],
        }

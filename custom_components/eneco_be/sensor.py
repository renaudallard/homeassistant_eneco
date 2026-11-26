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

"""Sensors reporting how the last submission went, and the last bill."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EnecoConfigEntry
from .bills import BillCoordinator
from .const import (
    ATTR_LAST_ERROR,
    BILL_DISAGREES,
    CONF_ADDRESS,
    CONF_ADJUST_ADVANCE,
    CONF_EAN,
    CONF_ENERGY_TYPE,
    LAST_BILL,
)
from .coordinator import EnecoCoordinator, EnecoState
from .entity import BillEntity, EnecoEntity, address_device, remove_entities

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_ALREADY_SUBMITTED = "already_submitted"
STATUS_NOTHING_TO_SUBMIT = "nothing_to_submit"

TARIFF_CARD = "tariff_card"
BILL_DIFFERENCE = "bill_difference"
MONTHLY_ADVANCE = "monthly_advance"
CURRENT_ADVANCE = "current_advance"
PROPOSED_ADVANCE = "proposed_advance"
# What a meter's advance sensor shows of its projection, besides the amount.
PROVISION_ATTRIBUTES = (
    "advance_now",
    "difference",
    "after_reading",
    "advances_left",
    "portal_min",
    "portal_max",
    "year_cost",
    "period_start",
    "period_end",
    "period_end_basis",
    "period_cost",
    "expected_regularisation",
    "advances_billed",
    "advances_to_come",
    "measured_until",
    "offtake_kwh",
    "injection_kwh",
    "net_kwh",
    "measured_offtake_kwh",
    "measured_injection_kwh",
    "volume_basis",
    "energy_price",
    "advance_split",
)


def _status(state: EnecoState) -> str:
    """Return the outcome of the last attempt as an enum value."""
    if state.attempted is None:
        return STATUS_PENDING
    if state.error:
        return STATUS_FAILED
    if state.nothing_mapped:
        return STATUS_NOTHING_TO_SUBMIT
    if state.skipped:
        return STATUS_ALREADY_SUBMITTED
    return STATUS_SUCCESS


@dataclass(frozen=True, kw_only=True)
class EnecoSensorDescription(SensorEntityDescription):
    """Describes an Eneco sensor and how to read it off the state."""

    value_fn: Callable[[EnecoCoordinator], datetime | str | None]
    # Read off the meter's settings rather than the last attempt, so it
    # stays available when an attempt fails unexpectedly.
    from_settings: bool = False


SENSORS: tuple[EnecoSensorDescription, ...] = (
    EnecoSensorDescription(
        key="last_submission",
        translation_key="last_submission",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.data.succeeded,
    ),
    EnecoSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=[
            STATUS_PENDING,
            STATUS_SUCCESS,
            STATUS_ALREADY_SUBMITTED,
            STATUS_NOTHING_TO_SUBMIT,
            STATUS_FAILED,
        ],
        value_fn=lambda coordinator: _status(coordinator.data),
    ),
    EnecoSensorDescription(
        key="next_submission",
        translation_key="next_submission",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coordinator: coordinator.next_submission,
        from_settings=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnecoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of every meter, the bill ones only if asked for."""
    bills = entry.runtime_data.bills
    for subentry_id, coordinator in entry.runtime_data.coordinators.items():
        entities: list[SensorEntity] = [
            EnecoSensor(coordinator, description) for description in SENSORS
        ]
        if bills is not None:
            entities += [
                LastBillSensor(bills, subentry_id, coordinator.meter),
                TariffCardSensor(bills, subentry_id, coordinator.meter),
                BillDifferenceSensor(bills, subentry_id, coordinator.meter),
                MonthlyAdvanceSensor(bills, subentry_id, coordinator.meter),
                CurrentAdvanceSensor(bills, subentry_id, coordinator.meter),
            ]
        async_add_entities(entities, config_subentry_id=subentry_id)
    if bills is None:
        remove_entities(hass, entry, ("sensor", "binary_sensor"), BILL_SUFFIXES)
        return
    addresses = sorted(
        {c.meter[CONF_ADDRESS] for c in entry.runtime_data.coordinators.values()}
    )
    adjust = bool(entry.options.get(CONF_ADJUST_ADVANCE))
    per_address: list[SensorEntity] = []
    for n, address in enumerate(addresses):
        number = n + 1 if len(addresses) > 1 else 0
        per_address += [
            AddressAdvanceSensor(bills, entry.entry_id, address, number),
            AddressCurrentAdvanceSensor(bills, entry.entry_id, address, number),
        ]
        if adjust:
            per_address.append(
                AddressProposedAdvanceSensor(bills, entry.entry_id, address, number)
            )
    async_add_entities(per_address)
    if not adjust:
        remove_entities(hass, entry, ("sensor",), (f"_{PROPOSED_ADVANCE}",))


# The ends of the unique ids of every entity reading bills adds.
BILL_SUFFIXES = (
    f"_{LAST_BILL}",
    f"_{TARIFF_CARD}",
    f"_{BILL_DIFFERENCE}",
    f"_{BILL_DISAGREES}",
    f"_{MONTHLY_ADVANCE}",
    f"_{CURRENT_ADVANCE}",
    f"_{PROPOSED_ADVANCE}",
)


class EnecoSensor(EnecoEntity, SensorEntity):
    """Report one aspect of the last submission attempt."""

    _unrecorded_attributes = frozenset({ATTR_LAST_ERROR})

    entity_description: EnecoSensorDescription

    def __init__(
        self, coordinator: EnecoCoordinator, description: EnecoSensorDescription
    ) -> None:
        """Bind the sensor to its description."""
        super().__init__(coordinator)
        self.entity_description = description
        ean = coordinator.meter[CONF_EAN]
        self._attr_unique_id = f"{ean}_{description.key}"

    @property
    @override
    def available(self) -> bool:
        """Return whether the value can be read, always for a setting."""
        return self.entity_description.from_settings or super().available

    @property
    @override
    def native_value(self) -> datetime | str | None:
        """Return the value this sensor reports."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose why a submission failed, on the status sensor only."""
        if self.entity_description.key != "status":
            return None
        return {ATTR_LAST_ERROR: self.coordinator.data.error}


class BillSensor(BillEntity, SensorEntity):
    """A sensor of what the bill coordinator read for one meter."""


class MoneySensor(BillSensor):
    """A sensor of one meter's amount in euros, VAT included."""

    _attr_device_class = SensorDeviceClass.MONETARY
    # ISO 4217, as a monetary sensor wants it, not the € symbol.
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2


class LastBillSensor(MoneySensor):
    """The last full year billed: its total with VAT, and every line."""

    # A year has dozens of lines, the same until the next bill comes.
    _unrecorded_attributes = frozenset({"lines"})

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, LAST_BILL)

    @property
    @override
    def native_value(self) -> float | None:
        """Return what the year cost for the meter, VAT included."""
        year = self._read.get("year")
        return year["total"] if year else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return which bills make the year, its lines, and why not a newer one."""
        year = self._read.get("year") or {}
        return {
            "issued": year.get("issued"),
            "period_start": year.get("period_start"),
            "period_end": year.get("period_end"),
            "balance": year.get("balance"),
            "bills": year.get("bills", []),
            "lines": year.get("lines", []),
            ATTR_LAST_ERROR: self._read.get("error"),
        }


class BillDifferenceSensor(MoneySensor):
    """What Eneco billed for the year above what it is priced at.

    The price is the one the electricity or gas price entry checking the
    bill gives the meter's own history over the same days, VAT included.
    """

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, BILL_DIFFERENCE)

    @property
    @override
    def native_value(self) -> float | None:
        """Return the year billed less the year priced, or None without both."""
        comparison = self._read.get("comparison") or {}
        difference: float | None = comparison.get("difference")
        return difference

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return what the year is priced at, by which entry, or why not."""
        comparison = self._read.get("comparison") or {}
        return {
            "expected": comparison.get("expected"),
            "covered": comparison.get("covered"),
            "price_entry": comparison.get("price_entry"),
            ATTR_LAST_ERROR: comparison.get("error"),
        }


class MonthlyAdvanceSensor(MoneySensor):
    """The monthly advance the meter needs until its next reading.

    What brings the coming regularisation to about 0; once one advance or
    none is left before the reading, a year like the running one for the
    next period, which ``after_reading`` always gives. The attributes say what
    the period will cost and what the regularisation would come to at
    today's advance, positive still to pay.
    """

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, MONTHLY_ADVANCE)

    @property
    def _provision(self) -> dict[str, Any]:
        provision: dict[str, Any] = self._read.get("provision") or {}
        return provision

    @property
    @override
    def native_value(self) -> float | None:
        """Return the advance needed each month, VAT included."""
        value: float | None = self._provision.get("monthly_advance")
        return value

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the projection behind the amount, or why there is none."""
        provision = self._provision
        return {key: provision.get(key) for key in PROVISION_ATTRIBUTES} | {
            ATTR_LAST_ERROR: provision.get("error")
        }


class AddressSensor(CoordinatorEntity[BillCoordinator], SensorEntity):
    """A sensor of one delivery address of the login, in EUR.

    Eneco bills one advance per address, for every energy there.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: BillCoordinator,
        entry_id: str,
        address: str,
        number: int,
        key: str,
    ) -> None:
        """Bind the sensor to one delivery address of the login."""
        super().__init__(coordinator)
        self._address = address
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_id}_{address}_{key}"
        self._attr_device_info = address_device(entry_id, address, number)

    @property
    def _portal(self) -> dict[str, Any]:
        portal: dict[str, Any] = self.coordinator.addresses.get(self._address, {})
        return portal

    @property
    @override
    def available(self) -> bool:
        """Return whether the address was ever read."""
        return self._address in self.coordinator.addresses


class AddressCurrentAdvanceSensor(AddressSensor):
    """What Eneco bills the address each month, every energy together."""

    def __init__(
        self, coordinator: BillCoordinator, entry_id: str, address: str, number: int
    ) -> None:
        """Bind the sensor to one delivery address of the login."""
        super().__init__(coordinator, entry_id, address, number, CURRENT_ADVANCE)

    @property
    @override
    def native_value(self) -> float | None:
        """Return the advance billed each month, VAT included."""
        value: float | None = self._portal.get("advance_now")
        return value

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the range the portal accepts and its own advice."""
        portal = self._portal
        return {
            "portal_min": portal.get("portal_min"),
            "portal_max": portal.get("portal_max"),
            "eneco_recommended": portal.get("eneco_recommended"),
            ATTR_LAST_ERROR: portal.get("error"),
        }


class AddressProposedAdvanceSensor(AddressSensor):
    """What the advance button would set at the address, every energy together.

    The amounts the portal accepts that come closest to what the meters
    need, leaving a contract as it is where its range cannot help.
    """

    def __init__(
        self, coordinator: BillCoordinator, entry_id: str, address: str, number: int
    ) -> None:
        """Bind the sensor to one delivery address of the login."""
        super().__init__(coordinator, entry_id, address, number, PROPOSED_ADVANCE)

    @property
    def _proposal(self) -> dict[str, Any]:
        proposal: dict[str, Any] = self._portal.get("proposal") or {}
        return proposal

    @property
    @override
    def native_value(self) -> float | None:
        """Return the total the proposed advances come to, VAT included."""
        total: float | None = self._proposal.get("total")
        return total

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return each energy's proposed advance and which ones would change."""
        proposal = self._proposal
        energies: dict[str, str] = proposal.get("energies") or {}
        amounts: dict[str, float] = proposal.get("amounts") or {}
        # Summed: an address can have two contracts of one energy.
        by_energy: dict[str, float] = {}
        for ean, amount in amounts.items():
            kind = energies[ean].lower()
            by_energy[kind] = round(by_energy.get(kind, 0.0) + amount, 2)
        return {
            "electricity": by_energy.get("electricity"),
            "gas": by_energy.get("gas"),
            "changes": sorted(
                {energies[ean].lower() for ean in proposal.get("changes") or []}
            ),
            "exact": proposal.get("exact"),
            ATTR_LAST_ERROR: proposal.get("error"),
        }


class AddressAdvanceSensor(AddressSensor):
    """The monthly advance a delivery address needs, every energy together.

    The amount is unknown unless each contract the portal lists there has
    its meter set up here with an amount: a total missing one would pass
    for the whole.
    """

    def __init__(
        self, coordinator: BillCoordinator, entry_id: str, address: str, number: int
    ) -> None:
        """Bind the sensor to one delivery address of the login."""
        super().__init__(coordinator, entry_id, address, number, MONTHLY_ADVANCE)

    def _meters(self) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
        """Return each contract's energy and projection, or why one is missing.

        A list, not keyed by energy: an address can have two meters of one.
        """
        eans = self._portal.get("eans")
        if not eans:
            return [], self._portal.get("error") or "the portal lists no contract"
        found = {
            subentry.data[CONF_EAN]: (subentry.data, self.coordinator.data.get(key))
            for key, subentry in self.coordinator.config_entry.subentries.items()
            if CONF_EAN in subentry.data
        }
        meters: list[tuple[str, dict[str, Any]]] = []
        for ean in eans:
            meter, read = found.get(ean, (None, None))
            if meter is None:
                return meters, f"the meter ending {ean[-4:]} is not set up here"
            provision = (read or {}).get("provision") or {}
            if provision.get("monthly_advance") is None:
                return meters, provision.get("error") or "not worked out yet"
            meters.append((meter[CONF_ENERGY_TYPE].lower(), provision))
        return meters, None

    @staticmethod
    def _energy(meters: list[tuple[str, dict[str, Any]]], energy: str) -> float | None:
        """Return what the meters of one energy need together, if any is there."""
        amounts = [float(p["monthly_advance"]) for kind, p in meters if kind == energy]
        return round(sum(amounts), 2) if amounts else None

    @property
    @override
    def native_value(self) -> float | None:
        """Return the advance needed each month at the address, VAT included."""
        meters, error = self._meters()
        if error is not None:
            return None
        return round(sum(float(p["monthly_advance"]) for _, p in meters), 2)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return each energy's share, what is paid now, and the portal's advice."""
        meters, error = self._meters()
        portal = self._portal
        total = self.native_value
        now = portal.get("advance_now")
        expected = [p.get("expected_regularisation") for _, p in meters]
        after = [p.get("after_reading") for _, p in meters]
        return {
            "advance_now": now,
            "difference": None
            if total is None or now is None
            else round(total - now, 2),
            "after_reading": None
            if error is not None or None in after
            else round(sum(float(a) for a in after if a is not None), 2),
            "portal_min": portal.get("portal_min"),
            "portal_max": portal.get("portal_max"),
            "electricity": self._energy(meters, "electricity"),
            "gas": self._energy(meters, "gas"),
            "expected_regularisation": None
            if error is not None or None in expected
            else round(sum(float(e) for e in expected if e is not None), 2),
            "eneco_recommended": portal.get("eneco_recommended"),
            "eneco_expected_regularisation": portal.get(
                "eneco_expected_regularisation"
            ),
            # Else why a meter's advance, or the page's, stands from an
            # earlier day: it is still added up.
            ATTR_LAST_ERROR: error
            or next((p["error"] for _, p in meters if p.get("error")), None)
            or portal.get("error"),
        }


class CurrentAdvanceSensor(MoneySensor):
    """What Eneco bills the meter's contract each month, as its advances page says."""

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, CURRENT_ADVANCE)
        self._ean = meter[CONF_EAN]
        self._address = meter[CONF_ADDRESS]

    @property
    def _contract(self) -> tuple[dict[str, Any], str | None]:
        """Return the contract's advance, and why it is not known if not."""
        portal = self.coordinator.addresses.get(self._address) or {}
        contract: dict[str, Any] | None = (portal.get("contracts") or {}).get(self._ean)
        if contract is None:
            return {}, portal.get("error") or "the advances page lists no such contract"
        return contract, portal.get("error")

    @property
    @override
    def native_value(self) -> float | None:
        """Return the advance billed each month for the meter, VAT included."""
        value: float | None = self._contract[0].get("current")
        return value

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the range the portal accepts and its own advice."""
        contract, error = self._contract
        return {
            "portal_min": contract.get("minimum"),
            "portal_max": contract.get("maximum"),
            "eneco_recommended": contract.get("recommended"),
            "eneco_months_left": contract.get("months_left"),
            ATTR_LAST_ERROR: error,
        }


class TariffCardSensor(BillSensor):
    """The version of the tariff card the meter's contract is billed on."""

    def __init__(
        self, coordinator: BillCoordinator, subentry_id: str, meter: Any
    ) -> None:
        """Bind the sensor to one meter."""
        super().__init__(coordinator, subentry_id, meter, TARIFF_CARD)

    @property
    @override
    def native_value(self) -> str | None:
        """Return the card version, 012210 for the first issue of 2022-10."""
        card = self._read.get("card")
        return card["version"] if card else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the month the card is for, the product, the card, and why not."""
        card = self._read.get("card") or {}
        return {
            "card_month": card.get("month"),
            "product": card.get("product"),
            "product_type": card.get("product_type"),
            "urls": card.get("urls", {}),
            ATTR_LAST_ERROR: self._read.get("card_error"),
        }

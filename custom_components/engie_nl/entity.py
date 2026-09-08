"""Base entities: one device per metering point, one for the account."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import EanData, EngieCoordinator


class EngieAccountEntity(CoordinatorEntity[EngieCoordinator]):
    """An entity that hangs off the account device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EngieCoordinator, entry_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        # Keyed on the customer number, not the entry id. The EAN entities are
        # already keyed on the EAN and survive the account being removed and
        # added again; account entities keyed on the entry id do not, and every
        # one of them comes back as a "_2" duplicate with the old ones orphaned.
        customer_id = coordinator.data.user.customer_id or entry_id
        self._attr_unique_id = f"account_{customer_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """The account device, named after the customer number."""
        customer_id = self.coordinator.data.user.customer_id or self._entry_id
        return DeviceInfo(
            identifiers={(DOMAIN, f"account_{customer_id}")},
            name=f"ENGIE {customer_id}",
            manufacturer=MANUFACTURER,
            model="Mijn ENGIE account",
            configuration_url="https://mijn.engie.nl",
        )


class EngieEanEntity(CoordinatorEntity[EngieCoordinator]):
    """An entity that hangs off one metering point's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EngieCoordinator, entry_id: str, ean: str, key: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._ean = ean
        # The EAN is stable for the life of the connection, so the unique id
        # survives re-adding the account.
        self._attr_unique_id = f"{ean}_{key}"

    @property
    def ean_data(self) -> EanData | None:
        """This entity's metering point data for the current poll."""
        return self.coordinator.data.eans.get(self._ean)

    @property
    def available(self) -> bool:
        """Unavailable when the EAN dropped out of the account."""
        return super().available and self.ean_data is not None

    @property
    def device_info(self) -> DeviceInfo:
        """One device per EAN, linked to the account device."""
        data = self.ean_data
        energy = (data.energy if data else None) or "meter"
        label = {"electricity": "Elektriciteit", "gas": "Gas"}.get(energy, "Meter")
        customer_id = self.coordinator.data.user.customer_id or self._entry_id
        return DeviceInfo(
            identifiers={(DOMAIN, self._ean)},
            name=f"ENGIE {label} {self._ean[-4:]}",
            manufacturer=MANUFACTURER,
            model=f"Aansluiting {label.lower()}",
            serial_number=self._ean,
            via_device=(DOMAIN, f"account_{customer_id}"),
        )

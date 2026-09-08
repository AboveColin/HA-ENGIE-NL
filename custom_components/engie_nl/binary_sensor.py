"""Binary sensors for the state of a connection and of the account.

These answer questions the numeric sensors cannot. "Delivering" is the one that
matters at a switchover: ENGIE reports a connection as a smart, readable meter
long before it supplies it, and every data endpoint refuses until it does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EngieConfigEntry
from .coordinator import EanData, EngieCoordinator, EngieData
from .entity import EngieAccountEntity, EngieEanEntity


@dataclass(frozen=True, kw_only=True)
class EanBinaryDescription(BinarySensorEntityDescription):
    """A binary sensor on a metering point."""

    value_fn: Callable[[EanData], bool | None]
    attr_fn: Callable[[EanData], dict[str, Any]] | None = None


def _delivery_attrs(data: EanData) -> dict[str, Any]:
    point = data.point
    product = point.current_product or point.next_product
    return {
        "status_code": point.status_code,
        "supply_starts": point.start_date.isoformat() if point.start_date else None,
        "supply_ends": point.end_date.isoformat() if point.end_date else None,
        "product": product.name if product else None,
        "data_from": point.data_from.isoformat() if point.data_from else None,
        "data_to": point.data_to.isoformat() if point.data_to else None,
    }


EAN_BINARY_SENSORS: tuple[EanBinaryDescription, ...] = (
    EanBinaryDescription(
        key="delivering",
        translation_key="delivering",
        # has_data is what the gateway itself checks: on a connection where it
        # is False, /consumptions, /mandates and /tariffs each refuse. Measured
        # 2026-09-07 on an account two days before its contract started, with
        # smart and readable both True at the same time.
        value_fn=lambda d: d.point.has_data,
        attr_fn=_delivery_attrs,
    ),
    EanBinaryDescription(
        key="smart_meter",
        translation_key="smart_meter",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.point.smart,
    ),
    EanBinaryDescription(
        key="data_mandate",
        translation_key="data_mandate",
        entity_category=EntityCategory.DIAGNOSTIC,
        # The mandate record when the gateway served one, else what the metering
        # point claims. They disagree while a mandate is being set up.
        value_fn=lambda d: d.mandate.active if d.mandate is not None else d.point.readable,
        attr_fn=lambda d: {
            "approval_version": d.mandate.approval_version,
            "start_date": d.mandate.start_date.isoformat() if d.mandate.start_date else None,
            "end_date": d.mandate.end_date.isoformat() if d.mandate.end_date else None,
        } if d.mandate is not None else {},
    ),
)


@dataclass(frozen=True, kw_only=True)
class AccountBinaryDescription(BinarySensorEntityDescription):
    """A binary sensor on the account."""

    value_fn: Callable[[EngieData], bool | None]
    attr_fn: Callable[[EngieData], dict[str, Any]] | None = None


ACCOUNT_BINARY_SENSORS: tuple[AccountBinaryDescription, ...] = (
    AccountBinaryDescription(
        key="outage",
        translation_key="outage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        # /api/v1/outages takes a customerId and answered 8 messages for this
        # account on 2026-09-08, so the list is not filtered down to outages
        # that affect the customer. Treat it as "ENGIE has something to say".
        value_fn=lambda d: bool(d.outages),
        attr_fn=lambda d: {"titles": [o.title for o in d.outages]},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EngieConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """One binary sensor per description per device."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        EngieEanBinarySensor(coordinator, entry.entry_id, ean, desc)
        for ean in coordinator.data.eans
        for desc in EAN_BINARY_SENSORS
    ]
    entities.extend(
        EngieAccountBinarySensor(coordinator, entry.entry_id, desc)
        for desc in ACCOUNT_BINARY_SENSORS
    )
    async_add_entities(entities)


class EngieEanBinarySensor(EngieEanEntity, BinarySensorEntity):
    """A metering-point binary sensor."""

    entity_description: EanBinaryDescription

    def __init__(
        self,
        coordinator: EngieCoordinator,
        entry_id: str,
        ean: str,
        description: EanBinaryDescription,
    ) -> None:
        super().__init__(coordinator, entry_id, ean, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        data = self.ean_data
        return self.entity_description.value_fn(data) if data is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.ean_data
        fn = self.entity_description.attr_fn
        return fn(data) if (fn and data is not None) else None


class EngieAccountBinarySensor(EngieAccountEntity, BinarySensorEntity):
    """An account-level binary sensor."""

    entity_description: AccountBinaryDescription

    def __init__(
        self, coordinator: EngieCoordinator, entry_id: str, description: AccountBinaryDescription
    ) -> None:
        super().__init__(coordinator, entry_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attr_fn
        return fn(self.coordinator.data) if fn else None


__all__ = ["async_setup_entry", "EAN_BINARY_SENSORS", "ACCOUNT_BINARY_SENSORS"]

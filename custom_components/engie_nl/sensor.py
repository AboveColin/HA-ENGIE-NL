"""Sensors for the ENGIE Energie NL integration.

Per metering point: cumulative meter readings per register (kWh or m3,
``total_increasing``, so the Energy dashboard can use them), the most recent
day's consumption and return, and the date of the latest reading.

Per account: the current termijnbedrag and ENGIE's advice, the projected
year total, the open amount across invoices, the last transaction, and, when
enabled in the options, the current day-ahead prices.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from engie_nl import Consumption, DayAheadPrice, Register, TransactionStatus

from . import EngieConfigEntry
from .coordinator import EanData, EngieCoordinator, EngieData
from .entity import EngieAccountEntity, EngieEanEntity

# --- register classification --------------------------------------------------
#
# The gateway names registers in Dutch and marks the direction in ``type``. The
# exact strings are not enumerated in the app, so the match is by keyword with
# the register sequence as the fallback: sequence 1 is normaal, 2 is dal.

_LOW_WORDS = ("dal", "laag", "low", "off")
_RETURN_WORDS = ("terug", "return", "lever", "production", "opwek")


def classify(register: Register) -> tuple[str, str]:
    """Return ``(rate, direction)``: rate in normal/low, direction in consumption/return."""
    name = (register.name or "").lower()
    kind = (register.kind or "").lower()
    direction = "return" if any(w in kind or w in name for w in _RETURN_WORDS) else "consumption"
    if any(w in name for w in _LOW_WORDS):
        rate = "low"
    elif register.sequence == 2 and not name:
        rate = "low"
    else:
        rate = "normal"
    return rate, direction


def latest_reading(data: EanData, rate: str, direction: str) -> int | None:
    """Latest reading of the register matching rate and direction, if any."""
    if data.readings is None:
        return None
    for reg in data.readings.registers:
        if classify(reg) == (rate, direction) and reg.latest is not None:
            return reg.latest.value
    return None


def gas_reading(data: EanData) -> int | None:
    """Gas has one register; take the freshest reading across all of them."""
    if data.readings is None:
        return None
    best: tuple[date, int] | None = None
    for reg in data.readings.registers:
        r = reg.latest
        if r is not None and r.day is not None and r.value is not None and (best is None or r.day > best[0]):
            best = (r.day, r.value)
    return best[1] if best else None


def latest_reading_date(data: EanData) -> date | None:
    if data.readings is None:
        return None
    days = [reg.latest.day for reg in data.readings.registers if reg.latest is not None and reg.latest.day]
    return max(days) if days else None


def last_day(data: EanData) -> Consumption | None:
    """The most recent day with a consumption value; P4 lags, so not always yesterday."""
    if data.consumptions is None:
        return None
    rows = [c for c in data.consumptions.data if c.day is not None and c.total is not None]
    return max(rows, key=lambda c: c.day or date.min) if rows else None


def _day_attrs(data: EanData) -> dict[str, Any]:
    row = last_day(data)
    if row is None:
        return {"error": data.consumptions.error if data.consumptions else None}
    return {
        "date": row.day.isoformat() if row.day else None,
        "normal": row.normal,
        "low": row.low,
        "return_normal": row.return_normal,
        "return_low": row.return_low,
    }


@dataclass(frozen=True, kw_only=True)
class EanSensorDescription(SensorEntityDescription):
    """A sensor on a metering point."""

    energy: str  # electricity or gas
    value_fn: Callable[[EanData], Any]
    attr_fn: Callable[[EanData], dict[str, Any]] | None = None


_KWH = UnitOfEnergy.KILO_WATT_HOUR
_M3 = UnitOfVolume.CUBIC_METERS

EAN_SENSORS: tuple[EanSensorDescription, ...] = (
    EanSensorDescription(
        key="reading_normal", translation_key="reading_normal", energy="electricity",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=_KWH, value_fn=lambda d: latest_reading(d, "normal", "consumption"),
    ),
    EanSensorDescription(
        key="reading_low", translation_key="reading_low", energy="electricity",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=_KWH, value_fn=lambda d: latest_reading(d, "low", "consumption"),
    ),
    EanSensorDescription(
        key="reading_return_normal", translation_key="reading_return_normal", energy="electricity",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=_KWH, value_fn=lambda d: latest_reading(d, "normal", "return"),
    ),
    EanSensorDescription(
        key="reading_return_low", translation_key="reading_return_low", energy="electricity",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=_KWH, value_fn=lambda d: latest_reading(d, "low", "return"),
    ),
    EanSensorDescription(
        key="consumption_last_day", translation_key="consumption_last_day", energy="electricity",
        device_class=SensorDeviceClass.ENERGY, native_unit_of_measurement=_KWH,
        value_fn=lambda d: (row.total if (row := last_day(d)) else None), attr_fn=_day_attrs,
    ),
    EanSensorDescription(
        key="return_last_day", translation_key="return_last_day", energy="electricity",
        device_class=SensorDeviceClass.ENERGY, native_unit_of_measurement=_KWH,
        value_fn=lambda d: (row.total_return if (row := last_day(d)) else None), attr_fn=_day_attrs,
    ),
    EanSensorDescription(
        key="reading_gas", translation_key="reading_gas", energy="gas",
        device_class=SensorDeviceClass.GAS, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=_M3, value_fn=gas_reading,
    ),
    EanSensorDescription(
        key="consumption_last_day_gas", translation_key="consumption_last_day", energy="gas",
        device_class=SensorDeviceClass.GAS, native_unit_of_measurement=_M3,
        value_fn=lambda d: (row.total if (row := last_day(d)) else None), attr_fn=_day_attrs,
    ),
    EanSensorDescription(
        key="last_reading_date", translation_key="last_reading_date", energy="any",
        device_class=SensorDeviceClass.DATE, entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=latest_reading_date,
    ),
)


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    """A sensor on the account."""

    value_fn: Callable[[EngieData], Any]
    attr_fn: Callable[[EngieData], dict[str, Any]] | None = None
    needs_day_ahead: bool = False


def _open_amount(data: EngieData) -> float | None:
    open_tx = [t.amount for t in data.transactions if t.status is TransactionStatus.OPEN and t.amount is not None]
    return round(sum(open_tx), 2) if open_tx else 0.0 if data.transactions else None


def _last_tx(data: EngieData):
    dated = [t for t in data.transactions if t.day is not None]
    return max(dated, key=lambda t: t.day or date.min) if dated else None


def _price_now(prices: list[DayAheadPrice] | None) -> float | None:
    if not prices:
        return None
    now = dt_util.utcnow()
    for p in prices:
        if p.start and p.end and p.start <= now < p.end:
            return p.price
    return None


def _price_attrs(prices: list[DayAheadPrice] | None) -> dict[str, Any]:
    if not prices:
        return {}
    return {
        "prices": [
            {"start": p.start.isoformat() if p.start else None, "price": p.price, "price_ex": p.price_ex}
            for p in prices
        ]
    }


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="prepayment_current", translation_key="prepayment_current",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=CURRENCY_EURO,
        value_fn=lambda d: d.estimations.prepayment_amount_current if d.estimations else None,
    ),
    AccountSensorDescription(
        key="prepayment_advice", translation_key="prepayment_advice",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=CURRENCY_EURO,
        value_fn=lambda d: d.estimations.prepayment_amount_advice if d.estimations else None,
        attr_fn=lambda d: {
            "min": d.estimations.prepayment_amount_min, "max": d.estimations.prepayment_amount_max,
            "prepayments_left": d.estimations.number_of_prepayments_left,
        } if d.estimations else {},
    ),
    AccountSensorDescription(
        key="estimated_year_total", translation_key="estimated_year_total",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=CURRENCY_EURO,
        value_fn=lambda d: d.estimations.total_estimated_nota_amount if d.estimations else None,
        attr_fn=lambda d: {
            "to_pay_balance": d.estimations.to_pay_balance_amount,
            "paid_prepayments": d.estimations.total_paid_pre_payment_amount,
        } if d.estimations else {},
    ),
    AccountSensorDescription(
        key="open_amount", translation_key="open_amount",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=CURRENCY_EURO,
        value_fn=_open_amount,
    ),
    AccountSensorDescription(
        key="last_transaction", translation_key="last_transaction",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=CURRENCY_EURO,
        value_fn=lambda d: (t.amount if (t := _last_tx(d)) else None),
        attr_fn=lambda d: {
            "date": t.day.isoformat() if t.day else None, "description": t.description,
            "status": str(t.status), "type": t.kind,
        } if (t := _last_tx(d)) else {},
    ),
    AccountSensorDescription(
        key="day_ahead_electricity", translation_key="day_ahead_electricity", needs_day_ahead=True,
        native_unit_of_measurement=f"{CURRENCY_EURO}/{_KWH}", suggested_display_precision=4,
        value_fn=lambda d: _price_now(d.day_ahead.get("electricity")),
        attr_fn=lambda d: _price_attrs(d.day_ahead.get("electricity")),
    ),
    AccountSensorDescription(
        key="day_ahead_gas", translation_key="day_ahead_gas", needs_day_ahead=True,
        native_unit_of_measurement=f"{CURRENCY_EURO}/{_M3}", suggested_display_precision=4,
        value_fn=lambda d: _price_now(d.day_ahead.get("gas")),
        attr_fn=lambda d: _price_attrs(d.day_ahead.get("gas")),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: EngieConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Create one sensor per description per matching device."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for ean, data in coordinator.data.eans.items():
        for desc in EAN_SENSORS:
            if desc.energy in ("any", data.energy):
                entities.append(EngieEanSensor(coordinator, entry.entry_id, ean, desc))
    for desc in ACCOUNT_SENSORS:
        if desc.needs_day_ahead and not coordinator.include_day_ahead:
            continue
        entities.append(EngieAccountSensor(coordinator, entry.entry_id, desc))
    async_add_entities(entities)


class EngieEanSensor(EngieEanEntity, SensorEntity):
    """A metering-point sensor."""

    entity_description: EanSensorDescription

    def __init__(self, coordinator: EngieCoordinator, entry_id: str, ean: str, description: EanSensorDescription) -> None:
        super().__init__(coordinator, entry_id, ean, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        data = self.ean_data
        return self.entity_description.value_fn(data) if data is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.ean_data
        fn = self.entity_description.attr_fn
        return fn(data) if (fn and data is not None) else None


class EngieAccountSensor(EngieAccountEntity, SensorEntity):
    """An account-level sensor."""

    entity_description: AccountSensorDescription

    def __init__(self, coordinator: EngieCoordinator, entry_id: str, description: AccountSensorDescription) -> None:
        super().__init__(coordinator, entry_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attr_fn
        return fn(self.coordinator.data) if fn else None


__all__ = ["async_setup_entry", "classify", "EAN_SENSORS", "ACCOUNT_SENSORS", "datetime"]

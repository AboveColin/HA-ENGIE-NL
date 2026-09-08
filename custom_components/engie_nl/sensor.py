"""Sensors for the ENGIE Energie NL integration.

Per metering point: cumulative meter readings per register (kWh or m3,
``total_increasing``, so the Energy dashboard can use them), the most recent
day's consumption and return, and the date of the latest reading.

Per metering point also: the contract's own rates and standing charge, and
which product supplies it from when.

Per account: the current termijnbedrag and ENGIE's advice, the projected
year total, the open amount across invoices, the last transaction, and, when
enabled in the options, the current day-ahead prices. Diagnostics cover
documents, outages, monthly reports, the house ENGIE has on file and the daily
welcome message, which carries the cheapest hour of tomorrow.
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
from .coordinator import EanData, EngieCoordinator, EngieData, TariffView
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


def _tariffs(data: EanData) -> TariffView | None:
    return data.tariffs


def _rate(data: EanData, field_name: str) -> float | None:
    view = data.tariffs
    return getattr(view, field_name) if view is not None else None


def _tariff_attrs(data: EanData) -> dict[str, Any]:
    """The parts ENGIE priced separately, so the sum can be checked.

    The grouping that produced these fields is unverified: see
    :class:`~custom_components.engie_nl.coordinator.TariffView`. Publishing the
    components means a wrong grouping is visible rather than silent.
    """
    view = data.tariffs
    if view is None:
        return {}
    return {
        "entries": [
            {
                "type": e.tariff_type,
                "unit": e.unit_of_measure,
                "feed_in": e.use_for_feed_in,
                "price_ex": e.price_ex,
                "tax": e.tax,
                "from": e.date_start.isoformat() if e.date_start else None,
                "to": e.date_end.isoformat() if e.date_end else None,
                "description": e.description,
            }
            for e in view.entries
        ]
    }


def _product(data: EanData):
    """The product supplying this connection now, or the one that will."""
    point = data.point
    return point.current_product or point.next_product


def _product_attrs(data: EanData) -> dict[str, Any]:
    product = _product(data)
    point = data.point
    return {
        "start_date": product.start_date.isoformat() if product and product.start_date else None,
        "end_date": product.end_date.isoformat() if product and product.end_date else None,
        "signed_date": product.signed_date.isoformat() if product and product.signed_date else None,
        "is_current": point.current_product is not None,
        "status_code": point.status_code,
        "single_tariff": point.single_tariff,
        "market_segment": point.market_segment,
        "grid_owner": point.grid_owner_name,
        "annual_estimate_normal": point.sjv_normal,
        "annual_estimate_low": point.sjv_low,
        "annual_estimate_single": point.sjv_single,
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
    # --- contract rates, from GET /api/v1/tariffs ---------------------------
    EanSensorDescription(
        key="tariff_normal", translation_key="tariff_normal", energy="electricity",
        native_unit_of_measurement=f"{CURRENCY_EURO}/{_KWH}", suggested_display_precision=5,
        value_fn=lambda d: _rate(d, "any_rate"), attr_fn=_tariff_attrs,
    ),
    EanSensorDescription(
        key="tariff_low", translation_key="tariff_low", energy="electricity",
        native_unit_of_measurement=f"{CURRENCY_EURO}/{_KWH}", suggested_display_precision=5,
        value_fn=lambda d: _rate(d, "low"),
    ),
    EanSensorDescription(
        key="tariff_feed_in", translation_key="tariff_feed_in", energy="electricity",
        native_unit_of_measurement=f"{CURRENCY_EURO}/{_KWH}", suggested_display_precision=5,
        value_fn=lambda d: _rate(d, "feed_in"),
    ),
    EanSensorDescription(
        key="tariff_gas", translation_key="tariff_gas", energy="gas",
        native_unit_of_measurement=f"{CURRENCY_EURO}/{_M3}", suggested_display_precision=5,
        value_fn=lambda d: _rate(d, "any_rate"), attr_fn=_tariff_attrs,
    ),
    EanSensorDescription(
        key="standing_charge", translation_key="standing_charge", energy="any",
        native_unit_of_measurement=f"{CURRENCY_EURO}/d", suggested_display_precision=5,
        value_fn=lambda d: _rate(d, "standing_charge"),
    ),
    EanSensorDescription(
        key="product", translation_key="product", energy="any",
        value_fn=lambda d: (p.name if (p := _product(d)) else None), attr_fn=_product_attrs,
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


def _next_document(data: EngieData):
    dated = [d for d in data.documents if d.day is not None]
    return max(dated, key=lambda d: d.day or date.min) if dated else None


def _outage_attrs(data: EngieData) -> dict[str, Any]:
    return {
        "messages": [
            {
                "title": o.title,
                "description": o.description,
                "start": o.start.isoformat() if o.start else None,
                "end": o.end.isoformat() if o.end else None,
                "link": o.link_url,
            }
            for o in data.outages
        ]
    }


def _welcome_attrs(data: EngieData) -> dict[str, Any]:
    """The weather ENGIE quotes alongside its daily message.

    The message itself names the cheapest hour of tomorrow, which is the part
    worth automating on, but ENGIE writes it as prose rather than a field, so
    it stays a string.
    """
    welcome = data.welcome
    if welcome is None or welcome.meteorological_context is None:
        return {}
    weather = welcome.meteorological_context
    # sunrise_at and sunset_at are declared String in the app, not a timestamp
    # type, so they arrive as ISO text already. Calling isoformat on them
    # raises, and the entity then fails to be added at all.
    return {
        "weather": weather.weather_description,
        "sunrise": weather.sunrise_at,
        "sunset": weather.sunset_at,
    }


def _house_attrs(data: EngieData) -> dict[str, Any]:
    house = data.house
    if house is None:
        return {}
    return {
        "construction_year": house.construction_year,
        "type": house.type,
        "surface_size": house.surface_size,
        "tenure": house.sale_rent,
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
    AccountSensorDescription(
        key="welcome_message", translation_key="welcome_message",
        value_fn=lambda d: (d.welcome.message[:255] if d.welcome and d.welcome.message else None),
        attr_fn=_welcome_attrs,
    ),
    AccountSensorDescription(
        key="outages", translation_key="outages", entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.outages), attr_fn=_outage_attrs,
    ),
    AccountSensorDescription(
        key="documents", translation_key="documents", entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.documents),
        attr_fn=lambda d: {
            "latest_title": doc.title, "latest_date": doc.day.isoformat() if doc.day else None,
        } if (doc := _next_document(d)) else {},
    ),
    AccountSensorDescription(
        key="monthly_reports", translation_key="monthly_reports",
        entity_category=EntityCategory.DIAGNOSTIC, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.mer_periods),
    ),
    AccountSensorDescription(
        key="energy_label", translation_key="energy_label",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.house.energy_label if d.house else None, attr_fn=_house_attrs,
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

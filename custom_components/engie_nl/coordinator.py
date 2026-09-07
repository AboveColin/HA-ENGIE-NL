"""Polling coordinator for the ENGIE Energie NL integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from engie_nl import (
    ConsumptionSeries,
    DayAheadPrice,
    EnergyType,
    EngieApiError,
    EngieAuthError,
    EngieClient,
    EngieError,
    EngieNetworkError,
    EstimationCosts,
    MeteringPoint,
    MeterReadings,
    Transaction,
    User,
)

from .const import (
    CONF_INCLUDE_DAY_AHEAD,
    CONF_SCAN_INTERVAL_MINUTES,
    CONSUMPTION_DAYS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    ELECTRICITY_KINDS,
    GAS_KINDS,
    MIN_SCAN_INTERVAL_MINUTES,
    READINGS_DAYS,
)

_LOGGER = logging.getLogger(__name__)


def energy_kind(point: MeteringPoint) -> str | None:
    """Return ``"electricity"``, ``"gas"`` or ``None`` for a metering point."""
    kind = (point.kind or "").strip().lower()
    if kind in ELECTRICITY_KINDS:
        return "electricity"
    if kind in GAS_KINDS:
        return "gas"
    return None


@dataclass
class EanData:
    """Everything the coordinator knows about one metering point."""

    point: MeteringPoint
    energy: str | None
    consumptions: ConsumptionSeries | None = None
    readings: MeterReadings | None = None


@dataclass
class EngieData:
    """One poll's worth of data."""

    user: User
    eans: dict[str, EanData] = field(default_factory=dict)
    estimations: EstimationCosts | None = None
    transactions: list[Transaction] = field(default_factory=list)
    day_ahead: dict[str, list[DayAheadPrice]] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=dt_util.utcnow)


class EngieCoordinator(DataUpdateCoordinator[EngieData]):
    """Reads the account once per interval.

    The user record and the per-EAN consumption and readings are essential:
    if they fail the poll fails. Estimations, transactions and day-ahead
    prices are extras; a failure there is logged and the field stays ``None``
    so the energy sensors keep updating.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: EngieClient) -> None:
        minutes = max(
            MIN_SCAN_INTERVAL_MINUTES,
            int(entry.options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES)),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes),
            config_entry=entry,
        )
        self.client = client
        self.include_day_ahead: bool = bool(entry.options.get(CONF_INCLUDE_DAY_AHEAD, False))

    async def _async_update_data(self) -> EngieData:
        try:
            user = await self.client.get_user()
        except EngieAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (EngieNetworkError, EngieApiError) as err:
            raise UpdateFailed(f"ENGIE gateway: {err}") from err

        data = EngieData(user=user)
        for point in user.metering_points:
            if point.ean:
                data.eans[point.ean] = EanData(point=point, energy=energy_kind(point))
        eans = list(data.eans)
        if not eans:
            _LOGGER.warning("ENGIE account %s has no metering points", user.customer_id)
            return data

        today = dt_util.now().date()
        try:
            consumptions, readings = await asyncio.gather(
                self.client.get_consumptions(eans, start=today - timedelta(days=CONSUMPTION_DAYS), end=today),
                self.client.get_meter_readings(eans, start=today - timedelta(days=READINGS_DAYS), end=today),
            )
        except EngieAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (EngieNetworkError, EngieApiError) as err:
            raise UpdateFailed(f"ENGIE gateway: {err}") from err
        for series in consumptions:
            if series.ean in data.eans:
                data.eans[series.ean].consumptions = series
        for reading in readings:
            if reading.ean in data.eans:
                data.eans[reading.ean].readings = reading

        # Extras. The amount for /estimations is the current termijnbedrag, which
        # the app reads from the metering point; 0 asks the gateway for advice only.
        amount = next((int(p.prepayment_amount) for p in user.metering_points if p.prepayment_amount), 0)
        extras = [
            ("estimations", self.client.get_estimations(eans, amount=amount)),
            ("transactions", self.client.get_transactions()),
        ]
        if self.include_day_ahead:
            extras.append(("day_ahead_E", self.client.get_day_ahead_prices(EnergyType.ELECTRICITY)))
            extras.append(("day_ahead_G", self.client.get_day_ahead_prices(EnergyType.GAS)))
        results = await asyncio.gather(*(call for _, call in extras), return_exceptions=True)
        for (name, _), result in zip(extras, results):
            if isinstance(result, EngieAuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, EngieError):
                _LOGGER.debug("ENGIE %s unavailable this poll: %s", name, result)
                continue
            if isinstance(result, BaseException):
                raise result
            if name == "estimations":
                data.estimations = result
            elif name == "transactions":
                data.transactions = result
            elif name == "day_ahead_E":
                data.day_ahead["electricity"] = result
            elif name == "day_ahead_G":
                data.day_ahead["gas"] = result
        return data

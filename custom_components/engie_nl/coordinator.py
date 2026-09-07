"""Polling coordinator for the ENGIE Energie NL integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

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

    Only the user record is essential. It proves the session works and it
    carries the devices, so if it fails the poll fails. Every other read is
    best effort: a failure is logged at debug level and that field keeps its
    previous value, so one unhappy endpoint cannot blank out the sensors that
    did answer. The gateway makes this necessary rather than merely tidy:
    measured 2026-09-07 it answered 400 for /consumptions and /mandates, 424
    for /estimations and 500 for /mer_periods on a healthy account, all in the
    same minute that /user, /meterstands and /transactions answered 200.
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
        if not data.eans:
            _LOGGER.warning("ENGIE account %s has no metering points", user.customer_id)
            return data

        # ENGIE answers 400 "not-owned" for a connection it does not supply
        # yet, and the user record says so first: has_data is False until
        # delivery starts. Asking anyway turns a normal account waiting for its
        # start date into an entry that logs an error every hour. Measured
        # 2026-09-07 on an account whose contract began two days later.
        eans = [ean for ean, ean_data in data.eans.items() if ean_data.point.has_data is not False]
        if not eans:
            _LOGGER.debug(
                "ENGIE account %s has %d connection(s), none delivering data yet",
                user.customer_id,
                len(data.eans),
            )
            return data

        today = dt_util.now().date()
        reads: list[tuple[str, Any]] = [
            (
                "consumptions",
                self.client.get_consumptions(
                    eans, start=today - timedelta(days=CONSUMPTION_DAYS), end=today
                ),
            ),
            (
                "meterstands",
                self.client.get_meter_readings(
                    eans, start=today - timedelta(days=READINGS_DAYS), end=today
                ),
            ),
            ("estimations", self.client.get_estimations(eans, amount=_prepayment_amount(user))),
            ("transactions", self.client.get_transactions()),
        ]
        if self.include_day_ahead:
            reads.append(("day_ahead_E", self.client.get_day_ahead_prices(EnergyType.ELECTRICITY)))
            reads.append(("day_ahead_G", self.client.get_day_ahead_prices(EnergyType.GAS)))

        results = await asyncio.gather(*(call for _, call in reads), return_exceptions=True)
        for (name, _), result in zip(reads, results):
            if isinstance(result, EngieAuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, EngieError):
                _LOGGER.debug("ENGIE %s unavailable this poll: %s", name, result)
                continue
            if isinstance(result, BaseException):
                raise result
            _store(data, name, result)
        return data


def _prepayment_amount(user: User) -> int:
    """The termijnbedrag to price an estimation against; 0 asks for advice only."""
    return next((int(p.prepayment_amount) for p in user.metering_points if p.prepayment_amount), 0)


def _store(data: EngieData, name: str, result: Any) -> None:
    """File one best-effort read into this poll's data."""
    if name == "consumptions":
        for series in result:
            if series.ean in data.eans:
                data.eans[series.ean].consumptions = series
    elif name == "meterstands":
        for reading in result:
            if reading.ean in data.eans:
                data.eans[reading.ean].readings = reading
    elif name == "estimations":
        data.estimations = result
    elif name == "transactions":
        data.transactions = result
    elif name == "day_ahead_E":
        data.day_ahead["electricity"] = result
    elif name == "day_ahead_G":
        data.day_ahead["gas"] = result

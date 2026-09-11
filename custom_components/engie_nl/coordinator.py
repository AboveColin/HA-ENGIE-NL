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
    DocumentRef,
    EnergyType,
    EngieApiError,
    EngieAuthError,
    EngieClient,
    EngieError,
    EngieNetworkError,
    EstimationCosts,
    Mandate,
    MerPeriod,
    MeteringPoint,
    MeterReadings,
    OutageMessage,
    Transaction,
    User,
)
from engie_nl.generated import AddressMetaData, HappyHoursResponse, MGWTariff, WarmWelcomeResponse

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
    TARIFF_WINDOW_DAYS,
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
class TariffView:
    """The contract rates for one connection, read out of ``GET /api/v1/tariffs``.

    **The grouping below is unverified.** The endpoint refuses an EAN the
    customer does not supply yet, so on 2026-09-08 it could not be called on a
    real contract; the rules come from the app's model, not from a response.
    Each field is None when nothing matched, never a guessed number, so a wrong
    rule shows up as an unavailable sensor rather than a wrong price.

    ``price_ex`` and ``tax`` are separate on the wire, so the all-in rate is
    their sum. ``unit_of_measure`` separates a per-unit rate from a standing
    charge, and ``use_for_feed_in`` marks the teruglevering rate.
    """

    normal: float | None = None
    low: float | None = None
    single: float | None = None
    feed_in: float | None = None
    standing_charge: float | None = None
    entries: list[MGWTariff] = field(default_factory=list)

    @property
    def any_rate(self) -> float | None:
        """The rate to show when a connection has one tariff, not two."""
        return self.single if self.single is not None else self.normal


@dataclass
class EanData:
    """Everything the coordinator knows about one metering point."""

    point: MeteringPoint
    energy: str | None
    consumptions: ConsumptionSeries | None = None
    readings: MeterReadings | None = None
    tariffs: TariffView | None = None
    mandate: Mandate | None = None


@dataclass
class EngieData:
    """One poll's worth of data."""

    user: User
    eans: dict[str, EanData] = field(default_factory=dict)
    estimations: EstimationCosts | None = None
    transactions: list[Transaction] = field(default_factory=list)
    day_ahead: dict[str, list[DayAheadPrice]] = field(default_factory=dict)
    # None means the read failed this poll, [] means it answered with nothing.
    # Three sensors count these and one binary sensor reports a problem from
    # them, so collapsing the two into an empty list files a dead endpoint as a
    # real 0 and as "no outage".
    documents: list[DocumentRef] | None = None
    outages: list[OutageMessage] | None = None
    mer_periods: list[MerPeriod] | None = None
    welcome: WarmWelcomeResponse | None = None
    happy_hours: HappyHoursResponse | None = None
    house: AddressMetaData | None = None
    fetched_at: datetime = field(default_factory=dt_util.utcnow)


class EngieCoordinator(DataUpdateCoordinator[EngieData]):
    """Reads the account once per interval.

    Only the user record is essential. It proves the session works and it
    carries the devices, so if it fails the poll fails. Every other read is
    best effort: a failure is logged at debug level and that field stays at the
    empty value this poll started with, so one unhappy endpoint cannot blank
    out the sensors that did answer. Each poll builds a fresh ``EngieData``, so
    nothing is carried over from the previous one; a field whose read failed
    reads as None, which the entities render as unknown rather than as a zero.
    The gateway makes this necessary rather than merely tidy:
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
        today = dt_util.now().date()

        # Reads that ask about the account, not about a connection. They answer
        # whether or not supply has started, so they are never skipped: measured
        # 2026-09-08, /documents, /outages, /user/welcome and /address-metadata
        # all answered 200 on an account whose two connections were both still
        # has_data: false.
        reads: list[tuple[str, Any]] = [
            ("transactions", self.client.get_transactions()),
            ("documents", self.client.get_documents()),
            ("mer_periods", self.client.get_mer_periods()),
            ("outages", self.client.get_outages(user.customer_id)),
            ("welcome", self.client.account.welcome()),
            ("happy_hours", self.client.happy_hour.hours()),
        ]
        address = next((a for a in user.delivery_addresses if a.zip_code and a.house_nr), None)
        if address is not None:
            reads.append(
                (
                    "house",
                    self.client.address.metadata(
                        zip_code=(address.zip_code or "").replace(" ", "").upper(),
                        house_nr=str(address.house_nr),
                        addition=address.house_nr_addition or "",
                    ),
                )
            )
        if self.include_day_ahead:
            reads.append(("day_ahead_E", self.client.get_day_ahead_prices(EnergyType.ELECTRICITY)))
            reads.append(("day_ahead_G", self.client.get_day_ahead_prices(EnergyType.GAS)))

        # Reads that name an EAN. ENGIE answers 400 "not-owned" for a connection
        # it does not supply yet, and the user record says so first in has_data,
        # so asking anyway would log an error every hour on a healthy account
        # waiting for its start date.
        eans = [ean for ean, ean_data in data.eans.items() if ean_data.point.has_data is not False]
        if eans:
            reads.extend(
                [
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
                    ("mandates", self.client.get_mandates(eans)),
                    (
                        "tariffs",
                        self.client.tariffs.get(
                            eans, start=today, end=today + timedelta(days=TARIFF_WINDOW_DAYS)
                        ),
                    ),
                ]
            )
        else:
            _LOGGER.debug(
                "ENGIE account %s has %d connection(s), none delivering data yet",
                user.customer_id,
                len(data.eans),
            )

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


# Each best-effort read files itself into the poll's data. A dict of small
# functions rather than a chain of elifs, so adding an endpoint is one line.
_PER_EAN = {
    "consumptions": lambda ean_data, value: setattr(ean_data, "consumptions", value),
    "meterstands": lambda ean_data, value: setattr(ean_data, "readings", value),
    "mandates": lambda ean_data, value: setattr(ean_data, "mandate", value),
}

_ON_ACCOUNT = {
    "estimations": "estimations",
    "transactions": "transactions",
    "documents": "documents",
    "mer_periods": "mer_periods",
    "outages": "outages",
    "welcome": "welcome",
    "happy_hours": "happy_hours",
    "house": "house",
}


def _store(data: EngieData, name: str, result: Any) -> None:
    """File one best-effort read into this poll's data."""
    if name in _PER_EAN:
        assign = _PER_EAN[name]
        for row in result:
            if row.ean in data.eans:
                assign(data.eans[row.ean], row)
    elif name in _ON_ACCOUNT:
        setattr(data, _ON_ACCOUNT[name], result)
    elif name == "tariffs":
        _store_tariffs(data, result)
    elif name == "day_ahead_E":
        data.day_ahead["electricity"] = result
    elif name == "day_ahead_G":
        data.day_ahead["gas"] = result


def _store_tariffs(data: EngieData, result: Any) -> None:
    """Group the flat tariff list by EAN and by what each entry prices."""
    if result is None:
        return
    for entry in result.tariffs:
        ean_data = data.eans.get(entry.ean or "")
        if ean_data is None:
            continue
        if ean_data.tariffs is None:
            ean_data.tariffs = TariffView()
        _apply_tariff(ean_data.tariffs, entry)


def _apply_tariff(view: TariffView, entry: MGWTariff) -> None:
    """Put one tariff entry on the field it prices. See TariffView for the caveat."""
    view.entries.append(entry)
    rate = _all_in(entry)
    if rate is None:
        return
    unit = str(entry.unit_of_measure or "").upper()
    feed_in = str(entry.use_for_feed_in or "").upper()
    kind = str(entry.tariff_type or "").upper()
    if unit == "DAY":
        view.standing_charge = rate
    elif feed_in == "YES":
        view.feed_in = rate
    elif kind == "OFFPEAK":
        view.low = rate
    elif kind == "PEAK":
        view.normal = rate
    elif kind == "SINGLE":
        view.single = rate


def _all_in(entry: MGWTariff) -> float | None:
    """``price_ex`` plus ``tax``; None when the entry carries neither."""
    if entry.price_ex is None and entry.tax is None:
        return None
    return round((entry.price_ex or 0.0) + (entry.tax or 0.0), 6)

"""Diagnostics for the ENGIE Energie NL integration, with the personal data removed.

``async_redact_data`` matches keys exactly, so every spelling that carries a
token or an identifier is listed. The EAN is also a device serial number and
the unique-id prefix of every metering-point entity, so it is redacted too.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EngieConfigEntry

TO_REDACT = {
    "tokens",
    "access_token",
    "refresh_token",
    "id_token",
    "username",
    "password",
    "customer_id",
    "contact_id",
    "customer_number",
    "customer_name",
    "customer_email",
    "email",
    "first_name",
    "middle_name",
    "last_name",
    "phone",
    "mobile",
    "bank_account",
    "street",
    "house_nr",
    "house_nr_addition",
    "zip_code",
    "city",
    "ean",
    "ean_code",
    "ean_grid",
    "meter_id",
    "agreement_id",
    "address_id",
    "id",
    "reference",
    "parent_reference",
}


def _raw(obj: Any) -> Any:
    raw = getattr(obj, "raw", None)
    return raw if isinstance(raw, dict) else None


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EngieConfigEntry) -> dict[str, Any]:
    """Return the entry, the options, and this poll's raw gateway payloads, redacted."""
    coordinator = entry.runtime_data
    data = coordinator.data
    payload: dict[str, Any] = {
        # The title carries the customer number, so it stays out.
        "entry": {"data": entry.data, "options": dict(entry.options)},
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "include_day_ahead": coordinator.include_day_ahead,
            "fetched_at": data.fetched_at.isoformat(),
        },
        "user": _raw(data.user),
        "eans": {
            f"ean_{i}": {
                "energy": ean.energy,
                "point": _raw(ean.point),
                "consumptions": _raw(ean.consumptions),
                "readings": _raw(ean.readings),
            }
            for i, ean in enumerate(data.eans.values())
        },
        "estimations": _raw(data.estimations),
        "transactions": [_raw(t) for t in data.transactions],
        "day_ahead": {k: [_raw(p) for p in v] for k, v in data.day_ahead.items()},
    }
    return async_redact_data(payload, TO_REDACT)

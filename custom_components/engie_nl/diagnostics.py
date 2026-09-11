"""Diagnostics for the ENGIE Energie NL integration, with the personal data removed.

``async_redact_data`` matches keys exactly, so every spelling that carries a
token or an identifier is listed. The EAN is also a device serial number and
the unique-id prefix of every metering-point entity, so it is redacted too.

Two payloads are personal in ways a key name cannot catch.
``/api/v1/user/welcome`` greets the customer by first name inside its
``message``, so that key is redacted. ``/api/v1/address-metadata`` describes
the house itself, and its most identifying field arrives under the key
``type``, which elsewhere holds the harmless ``ELK``/``GAS``. Redacting
``type`` would hide that too, so the house payload is reported as fetched or
not and never included.
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
    "phone_number",
    "mobile",
    "gender",
    "bank_account",
    "payment_method",
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
    "external_contract_id",
    # A document's display_name spells out the customer's surname, where its
    # title does not: "Termijnnota" against "Termijnnota <name> september".
    "display_name",
    # /api/v1/user/welcome greets the customer by name inside the message.
    "message",
    # The standaardjaarverbruik per register. Not an identifier on its own, but
    # six numbers describing one household's yearly energy use are a fingerprint,
    # and the estimation sensors can be debugged from the estimation itself.
    "sjv_normal",
    "sjv_low",
    "sjv_single",
    "sjv_return_normal",
    "sjv_return_low",
    "sjv_return_single",
    "profile_category",
}

# grid_owner_name, grid_owner_ean and grid_owner_phone stay readable. They name
# the netbeheerder, a public company, and its EAN is the operator's published
# party code, not this connection's. Behaviour differs per operator, so it is
# the field a "why does this connection answer 400" report is read for. It
# narrows the address to a grid area at worst, which is several provinces.


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
                "mandate": _raw(ean.mandate),
                "tariffs": [_raw(t) for t in ean.tariffs.entries] if ean.tariffs else None,
            }
            for i, ean in enumerate(data.eans.values())
        },
        "estimations": _raw(data.estimations),
        "transactions": [_raw(t) for t in data.transactions],
        "day_ahead": {k: [_raw(p) for p in v] for k, v in data.day_ahead.items()},
        # None rather than [] when the read failed, so a dump shows which
        # endpoint was silent instead of an account that owns nothing.
        "documents": None if data.documents is None else [_raw(d) for d in data.documents],
        "outages": None if data.outages is None else [_raw(o) for o in data.outages],
        "mer_periods": None if data.mer_periods is None else [_raw(m) for m in data.mer_periods],
        "welcome": _raw(data.welcome),
        "happy_hours": _raw(data.happy_hours),
        "house": {"fetched": data.house is not None},
    }
    return async_redact_data(payload, TO_REDACT)

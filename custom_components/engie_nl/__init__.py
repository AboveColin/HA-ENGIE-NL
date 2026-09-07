"""The ENGIE Energie NL integration.

Reads a Mijn ENGIE account through the same gateway the ENGIE app uses. The
config entry holds only the Okta token pair; the password is used once in the
config flow and never stored. When the client refreshes the pair, the entry is
updated so a restart continues where it left off.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from engie_nl import EngieClient, OktaAuth, TokenSet

from .const import CONF_TOKENS, DOMAIN, MANUFACTURER
from .coordinator import EngieCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

type EngieConfigEntry = ConfigEntry[EngieCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EngieConfigEntry) -> bool:
    """Set up ENGIE from a config entry."""
    stored = entry.data.get(CONF_TOKENS)
    if not isinstance(stored, dict) or not stored.get("access_token"):
        raise ConfigEntryAuthFailed("no stored ENGIE session; log in again")
    tokens = TokenSet.from_dict(stored)

    session = async_get_clientsession(hass)
    auth = OktaAuth(session=session)

    @callback
    def _tokens_updated(new: TokenSet) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKENS: new.to_dict()})

    client = EngieClient(tokens, auth=auth, session=session, on_tokens_updated=_tokens_updated)
    coordinator = EngieCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    # The metering-point devices point at the account with via_device, and HA
    # requires the target to exist before the first entity references it.
    customer_id = coordinator.data.user.customer_id or entry.entry_id
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"account_{customer_id}")},
        name=f"ENGIE {customer_id}",
        manufacturer=MANUFACTURER,
        model="Mijn ENGIE account",
        configuration_url="https://mijn.engie.nl",
    )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: EngieConfigEntry) -> None:
    """Options changed: rebuild the coordinator with the new interval and extras."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EngieConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


__all__ = ["DOMAIN", "EngieConfigEntry", "async_setup_entry", "async_unload_entry"]

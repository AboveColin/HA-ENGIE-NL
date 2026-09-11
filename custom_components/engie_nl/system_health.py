"""System health for the ENGIE Energie NL integration.

Two different hosts can fail here and they fail differently. Okta is only
needed to log in and to refresh, so losing it shows up as a reauth days later.
The gateway is what every poll reads. Reporting them separately turns "ENGIE is
broken" into which half, before anyone reads a log.
"""

from __future__ import annotations

from typing import Any

from engie_nl.constants import GATEWAY_URL, OKTA_ORG_URL
from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register the system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Report the two hosts and whether the last poll of each account worked."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    info: dict[str, Any] = {
        "gateway_reachable": system_health.async_check_can_reach_url(hass, GATEWAY_URL),
        "okta_reachable": system_health.async_check_can_reach_url(hass, OKTA_ORG_URL),
        "configured_accounts": len(entries),
    }
    for index, entry in enumerate(entries):
        coordinator = entry.runtime_data
        info[f"account_{index}_last_update_success"] = coordinator.last_update_success
    return info

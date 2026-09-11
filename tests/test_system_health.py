"""Tests for the ENGIE system health panel."""

from __future__ import annotations

from unittest.mock import MagicMock

from engie_nl.constants import GATEWAY_URL, OKTA_ORG_URL
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.engie_nl.const import DOMAIN


async def _get_info(hass: HomeAssistant) -> dict:
    """Run the registered system health callback."""
    from homeassistant.components.system_health import DOMAIN as SH_DOMAIN  # noqa: PLC0415

    return await hass.data[SH_DOMAIN][DOMAIN].info_callback(hass)


async def test_nothing_configured_still_reports_both_hosts(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The two hosts are worth checking before the first account is added."""
    assert await async_setup_component(hass, "system_health", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    aioclient_mock.get(GATEWAY_URL, text="")
    aioclient_mock.get(OKTA_ORG_URL, text="")

    info = await _get_info(hass)
    assert info["configured_accounts"] == 0
    assert await info["gateway_reachable"] == "ok"
    assert await info["okta_reachable"] == "ok"


async def test_both_hosts_are_reported_separately(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock,
    config_entry: MockConfigEntry, aioclient_mock,
) -> None:
    """Okta signs in and the gateway serves the reads, so they are two answers."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, "system_health", {})
    await hass.async_block_till_done()
    aioclient_mock.get(GATEWAY_URL, text="")
    aioclient_mock.get(OKTA_ORG_URL, text="")

    info = await _get_info(hass)
    assert info["configured_accounts"] == 1
    assert info["account_0_last_update_success"] is True
    assert await info["gateway_reachable"] == "ok"
    assert await info["okta_reachable"] == "ok"

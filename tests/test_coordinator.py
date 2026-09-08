"""What the coordinator does when the gateway only half answers.

Both cases here are the live gateway's behaviour on 2026-09-07, two days
before the account started delivering: /consumptions and /mandates answered
400 "not-owned", /estimations 424 and /mer_periods 500, while /user,
/meterstands and /transactions answered 200 in the same minute.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from engie_nl import EngieApiError, User

from tests.conftest import EAN_E, EAN_G, make_user


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _user_without_data() -> User:
    """The user record ENGIE serves before a contract's start date."""
    raw = make_user().raw
    for address in raw["delivery_addresses"]:
        for point in address["metering_points"]:
            point["has_data"] = False
            point["status_code"] = "RECEIVED"
    return User.from_api(raw)


async def test_a_connection_without_data_is_not_asked_for_any(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    mock_client.get_user = AsyncMock(return_value=_user_without_data())
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    mock_client.get_consumptions.assert_not_called()
    mock_client.get_meter_readings.assert_not_called()
    mock_client.get_estimations.assert_not_called()


async def test_one_failing_read_does_not_lose_the_others(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    mock_client.get_consumptions = AsyncMock(
        side_effect=EngieApiError("GET /api/v1/consumptions failed", status=400, body={"message": "not-owned"})
    )
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    data = config_entry.runtime_data.data
    assert data.eans[EAN_E].consumptions is None
    assert data.eans[EAN_E].readings is not None
    assert data.eans[EAN_G].readings is not None
    assert data.estimations is not None
    assert data.transactions


async def test_the_user_record_is_still_essential(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    mock_client.get_user = AsyncMock(
        side_effect=EngieApiError("GET /api/v1/user failed", status=500, body={})
    )
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_account_reads_run_even_when_nothing_is_delivered(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    """Documents, outages and the daily message do not name an EAN.

    They answered 200 on the live account while both connections were still
    has_data: false, so skipping them with the per-EAN reads left five sensors
    empty for no reason.
    """
    mock_client.get_user = AsyncMock(return_value=_user_without_data())
    await _setup(hass, config_entry)

    data = config_entry.runtime_data.data
    assert data.documents, "documents should still be read"
    assert data.outages, "outages should still be read"
    assert data.welcome is not None
    assert data.house is not None
    mock_client.get_consumptions.assert_not_called()
    mock_client.get_estimations.assert_not_called()
    mock_client.tariffs.get.assert_not_called()

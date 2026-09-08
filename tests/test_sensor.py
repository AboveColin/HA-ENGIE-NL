"""Setup, entities and their values, token persistence, and diagnostics redaction."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from engie_nl import EngieApiError, EngieAuthError, TokenSet

from custom_components.engie_nl.const import CONF_TOKENS, DOMAIN
from custom_components.engie_nl.diagnostics import async_get_config_entry_diagnostics
from tests.conftest import CUSTOMER, EAN_E, EAN_G


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_entities_and_values(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    # electricity sensors: 4 readings + 2 last-day + date + 3 tariffs + standing
    # charge + product = 12. Gas: reading + last-day + date + tariff + standing
    # charge + product = 6. Account sensors with day-ahead off: 10. Binary: 3
    # per EAN plus 1 on the account = 7. Total 35.
    assert len(entries) == 35

    def state_of(unique_id: str) -> str:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id, unique_id
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        return state.state

    assert state_of(f"{EAN_E}_reading_normal") == "12000"
    assert state_of(f"{EAN_E}_reading_low") == "9000"
    assert state_of(f"{EAN_E}_reading_return_normal") == "3000"
    assert state_of(f"{EAN_E}_reading_return_low") == "1000"
    assert state_of(f"{EAN_E}_consumption_last_day") == "7.0"
    assert state_of(f"{EAN_E}_return_last_day") == "2.0"
    assert state_of(f"{EAN_E}_last_reading_date") == "2026-09-07"
    assert state_of(f"{EAN_G}_reading_gas") == "700"
    assert state_of(f"{EAN_G}_consumption_last_day_gas") == "1.5"
    assert state_of(f"{config_entry.entry_id}_prepayment_current") == "187.0"
    assert state_of(f"{config_entry.entry_id}_prepayment_advice") == "195.5"
    assert state_of(f"{config_entry.entry_id}_estimated_year_total") == "2244.0"
    assert state_of(f"{config_entry.entry_id}_open_amount") == "-187.0"
    assert state_of(f"{config_entry.entry_id}_last_transaction") == "-187.0"

    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{EAN_E}_consumption_last_day")
    attrs = hass.states.get(entity_id).attributes
    assert attrs["date"] == "2026-09-06"
    assert attrs["normal"] == 5.0 and attrs["low"] == 2.0

    devices = dr.async_get(hass)
    e_device = devices.async_get_device(identifiers={(DOMAIN, EAN_E)})
    assert e_device is not None and e_device.name == f"ENGIE Elektriciteit {EAN_E[-4:]}"
    account = devices.async_get_device(identifiers={(DOMAIN, f"account_{CUSTOMER}")})
    assert account is not None
    assert e_device.via_device_id == account.id

    # One call per endpoint per poll, with every EAN in one request.
    mock_client.get_consumptions.assert_awaited_once()
    assert list(mock_client.get_consumptions.await_args.args[0]) == [EAN_E, EAN_G]
    mock_client.get_estimations.assert_awaited_once()
    assert mock_client.get_estimations.await_args.kwargs["amount"] == 187
    mock_client.get_day_ahead_prices.assert_not_awaited()


async def test_day_ahead_sensors_only_when_enabled(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=CUSTOMER, title="x",
        data={"username": "u", "customer_id": CUSTOMER, CONF_TOKENS: TokenSet("a", "r", 9_999_999_999.0).to_dict()},
        options={"include_day_ahead": True},
    )
    await _setup(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_day_ahead_electricity")
    assert mock_client.get_day_ahead_prices.await_count == 2


async def test_extras_failure_keeps_energy_sensors(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    mock_client.get_estimations.side_effect = EngieApiError("boom", status=500)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    reading = hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, f"{EAN_E}_reading_normal"))
    assert reading.state == "12000"
    advice = hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, f"{config_entry.entry_id}_prepayment_advice"))
    assert advice.state == "unknown"


async def test_auth_error_triggers_reauth(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    mock_client.get_user.side_effect = EngieAuthError("expired")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_refreshed_tokens_are_persisted(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    # The client calls back with the new pair; the entry must hold it afterwards.
    from custom_components.engie_nl import EngieClient  # noqa: PLC0415  (patched constructor)

    callback = EngieClient.call_args.kwargs["on_tokens_updated"]
    callback(TokenSet(access_token="fresh", refresh_token="fresh-r", expires_at=9_999_999_999.0))
    await hass.async_block_till_done()
    assert config_entry.data[CONF_TOKENS]["access_token"] == "fresh"


async def test_diagnostics_redact(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = str(diag)
    assert "okta-access" not in text and "okta-refresh" not in text
    assert CUSTOMER not in text
    assert EAN_E not in text and EAN_G not in text
    assert "klant@example.com" not in text
    assert "Straat" not in text
    assert diag["coordinator"]["last_update_success"] is True
    assert diag["eans"]["ean_0"]["energy"] == "electricity"


async def test_unload(hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_contract_tariffs_are_grouped_per_ean(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    """The grouping of GET /api/v1/tariffs is unverified, so pin it to a fixture.

    The fixture uses the signed ENGIE Opgewekt rates. If ENGIE's real response
    groups differently, this test still passes and the live values will be
    wrong, which is why the components are published as attributes too.
    """
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    def state_of(unique_id: str) -> str:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id, unique_id
        state = hass.states.get(entity_id)
        assert state
        return state.state

    assert float(state_of(f"{EAN_E}_tariff_normal")) == pytest.approx(0.27575)
    assert float(state_of(f"{EAN_E}_tariff_low")) == pytest.approx(0.24575)
    assert float(state_of(f"{EAN_E}_tariff_feed_in")) == pytest.approx(0.05)
    assert float(state_of(f"{EAN_E}_standing_charge")) == pytest.approx(0.36838)
    assert float(state_of(f"{EAN_G}_tariff_gas")) == pytest.approx(1.50364)
    assert float(state_of(f"{EAN_G}_standing_charge")) == pytest.approx(0.23578)
    assert state_of(f"{EAN_E}_product") == "ENGIE Opgewekt"


async def test_delivering_reflects_has_data(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    """has_data is what the gateway checks before it answers any data endpoint."""
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{EAN_E}_delivering")
    assert entity_id
    state = hass.states.get(entity_id)
    assert state and state.state == "on"
    assert state.attributes["product"] == "ENGIE Opgewekt"


async def test_diagnostics_redact_the_welcome_name_and_the_house(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    """Neither is caught by a token-shaped key name, so both are listed explicitly."""
    await _setup(hass, config_entry)
    payload = await async_get_config_entry_diagnostics(hass, config_entry)
    text = str(payload)
    assert "Goedenavond" not in text
    assert "Etagewoning" not in text
    assert "1935" not in text
    assert payload["welcome"]["message"] == "**REDACTED**"
    assert payload["house"] == {"fetched": True}
    # The weather alongside it is not personal and stays readable.
    assert payload["welcome"]["meteorological_context"]["weather_description"] == "RAINY"

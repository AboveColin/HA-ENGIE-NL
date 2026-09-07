"""Config flow: password login, the MFA browser detour, reauth, options."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from engie_nl import (
    EmailChallenge,
    EngieAuthError,
    EngieEmailCodeRequired,
    EngieMfaRequiredError,
    EngieNetworkError,
)

from custom_components.engie_nl.const import (
    CONF_CUSTOMER_ID,
    CONF_INCLUDE_DAY_AHEAD,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKENS,
    DOMAIN,
)
from tests.conftest import CUSTOMER


async def test_user_flow_creates_entry(hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "goed"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"ENGIE {CUSTOMER}"
    assert result["data"][CONF_CUSTOMER_ID] == CUSTOMER
    assert result["data"][CONF_TOKENS]["access_token"] == "okta-access"
    assert "password" not in result["data"]
    assert result["result"].unique_id == CUSTOMER


async def test_user_flow_wrong_password(hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "fout"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_network_error(hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock) -> None:
    mock_auth.login.side_effect = EngieNetworkError("down")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "goed"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_mfa_goes_to_browser_step(hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock) -> None:
    mock_auth.login.side_effect = EngieMfaRequiredError("mfa", status="MFA_REQUIRED")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "goed"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "browser"
    assert result["description_placeholders"]["url"].startswith("https://login.engie.nl/")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": "engie://login/okta/callback?code=abc&state=st"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mock_auth.finish_browser_login.assert_awaited_once()


async def _to_email_code_step(hass: HomeAssistant, mock_auth: MagicMock) -> dict:
    """Walk the user step for an account that needs an emailed code."""
    challenge = EmailChallenge(state_handle="sh-1", answer_href="https://okta/answer", verifier="v-1")
    mock_auth.login.side_effect = EngieEmailCodeRequired("code sent", challenge)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "goed"}
    )


async def test_email_code_step_finishes_the_login(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    result = await _to_email_code_step(hass, mock_auth)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "email_code"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKENS]["access_token"] == "okta-access"
    challenge, code = mock_auth.submit_email_code.await_args.args
    assert challenge.state_handle == "sh-1"
    assert code == "123456"


async def test_email_code_rejected_keeps_the_form(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    result = await _to_email_code_step(hass, mock_auth)
    mock_auth.submit_email_code.side_effect = EngieAuthError("Ongeldige code")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_code"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "goed"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_tokens(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "goed"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKENS]["access_token"] == "okta-access"


async def test_options_flow(hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL_MINUTES: 30, CONF_INCLUDE_DAY_AHEAD: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_SCAN_INTERVAL_MINUTES: 30, CONF_INCLUDE_DAY_AHEAD: True}

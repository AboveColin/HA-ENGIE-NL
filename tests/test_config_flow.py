"""Config flow: password login, the MFA browser detour, reauth, options."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

import pytest
from engie_nl import (
    EmailChallenge,
    EngieApiError,
    EngieAuthError,
    EngieEmailCodeRequired,
    EngieMfaRequiredError,
    EngieNetworkError,
    TokenSet,
    User,
)

from custom_components.engie_nl.const import (
    CONF_CUSTOMER_ID,
    CONF_INCLUDE_DAY_AHEAD,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKENS,
    DOMAIN,
)
from tests.conftest import CUSTOMER, make_user


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


async def test_the_browser_fallback_logs_what_okta_offered(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Without this line the browser step shows up with no sign of why the code step was skipped."""
    mock_auth.login.side_effect = EngieMfaRequiredError(
        "Okta wants an authenticator this client cannot answer; it offered: Okta Verify, Telefoon",
        status="select-authenticator-authenticate",
        factors=[{"factorType": "Okta Verify"}, {"factorType": "Telefoon"}],
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with caplog.at_level(logging.WARNING, logger="custom_components.engie_nl.config_flow"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "klant@example.com", "password": "geheim-wachtwoord"}
        )
    assert result["step_id"] == "browser"

    # The harness logs its own warning about custom integrations, so pick ours by logger name.
    ours = [r for r in caplog.records if r.name == "custom_components.engie_nl.config_flow"]
    assert [r.levelno for r in ours] == [logging.WARNING]
    text = ours[0].getMessage()
    assert "Okta Verify, Telefoon" in text
    assert "select-authenticator-authenticate" in text
    assert "falls back to the browser" in text
    # The username and password are in scope where this logs; neither may reach it.
    assert "klant@example.com" not in text
    assert "geheim-wachtwoord" not in text


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


def _fail_reads(mock_client: MagicMock, times: int) -> None:
    """Make ``get_user`` fail ``times`` times, then succeed for every later read, setup included."""
    failures = [EngieNetworkError(f"gateway stalled {n}") for n in range(times)]

    async def get_user() -> User:
        if failures:
            raise failures.pop(0)
        return make_user()

    mock_client.get_user.side_effect = get_user


async def test_email_code_retry_after_a_failed_account_read_reuses_the_tokens(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    """The code is single use: once it is exchanged, a gateway failure must not cost the login."""
    result = await _to_email_code_step(hass, mock_auth)
    _fail_reads(mock_client, 1)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "email_code"
    assert result["errors"] == {"base": "cannot_connect"}

    # Whatever the user types now, the spent code is not sent to Okta again.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKENS]["access_token"] == "okta-access"
    mock_auth.submit_email_code.assert_awaited_once()


async def test_email_code_stays_exchanged_across_repeated_account_read_failures(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    """A rejected code is retried with the new code; an exchanged one never is, however often the read fails."""
    result = await _to_email_code_step(hass, mock_auth)
    mock_auth.submit_email_code.side_effect = [EngieAuthError("Ongeldige code"), mock_auth.submit_email_code.return_value]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["errors"] == {"base": "invalid_code"}

    _fail_reads(mock_client, 2)
    for _ in range(2):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
        assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_auth.submit_email_code.await_count == 2
    assert [call.args[1] for call in mock_auth.submit_email_code.await_args_list] == ["000000", "123456"]


REFRESHED = TokenSet(access_token="okta-access-2", refresh_token="okta-refresh-2", expires_at=9_999_999_999.0)


def _refresh_then_stall(mock_client: MagicMock) -> None:
    """A 401 makes the client refresh, Okta rotates the refresh token, and then the gateway stalls.

    The first read leaves the refreshed pair on the client and fails; every later read works.
    """
    calls = 0

    async def get_user() -> User:
        nonlocal calls
        calls += 1
        if calls == 1:
            mock_client.tokens = REFRESHED
            raise EngieNetworkError("gateway stalled after the refresh")
        return make_user()

    mock_client.get_user.side_effect = get_user


async def test_a_refresh_during_a_stalled_read_is_what_the_retry_and_the_entry_use(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    """The pre-refresh refresh token is dead once Okta rotates it, so the retry must not start from it."""
    result = await _to_email_code_step(hass, mock_auth)
    _refresh_then_stall(mock_client)

    with patch("custom_components.engie_nl.config_flow.EngieClient", return_value=mock_client) as client_cls:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKENS]["access_token"] == "okta-access-2"
    assert result["data"][CONF_TOKENS]["refresh_token"] == "okta-refresh-2"
    mock_auth.submit_email_code.assert_awaited_once()
    first, second = (call.args[0] for call in client_cls.call_args_list)
    assert first.access_token == "okta-access"
    assert second.access_token == "okta-access-2"


async def test_the_entry_stores_the_tokens_the_client_holds_after_a_refresh(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    """A refresh inside the account read succeeds; the entry must hold the new pair, not the one passed in."""

    async def get_user() -> User:
        mock_client.tokens = REFRESHED
        return make_user()

    mock_client.get_user.side_effect = get_user
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "goed"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKENS]["access_token"] == "okta-access-2"
    assert result["data"][CONF_TOKENS]["refresh_token"] == "okta-refresh-2"


async def test_the_browser_step_retries_only_the_read_with_the_refreshed_tokens(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    """The pasted callback code is single use too: after a good exchange a stalled read must not spend it again."""
    mock_auth.login.side_effect = EngieMfaRequiredError("mfa", status="MFA_REQUIRED")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "goed"}
    )
    assert result["step_id"] == "browser"
    _refresh_then_stall(mock_client)

    callback = {"callback_url": "engie://login/okta/callback?code=abc&state=st"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], callback)
    assert result["step_id"] == "browser"
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], callback)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKENS]["access_token"] == "okta-access-2"
    mock_auth.finish_browser_login.assert_awaited_once()


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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (EngieNetworkError("gateway down"), "cannot_connect"),
        (EngieAuthError("401 Unauthorized"), "invalid_auth"),
        (EngieApiError("GET /api/v1/user failed", status=500, body={}), "unknown"),
    ],
)
async def test_the_account_read_can_fail_after_a_good_password(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock,
    error: Exception, expected: str,
) -> None:
    """Okta and the gateway are two hosts, so the login can work and the read not.

    Unwrapped this escaped _finish and Home Assistant showed "Unexpected error"
    with a traceback, for three conditions the flow already has messages for.
    """
    mock_client.get_user.side_effect = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "klant@example.com", "password": "goed"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}


async def test_an_account_with_no_identifier_is_refused(
    hass: HomeAssistant, mock_auth: MagicMock, mock_client: MagicMock
) -> None:
    """No customer number and no email left both accounts keyed on one literal.

    The second one then aborted as already_configured with nothing said about
    why, so the flow now stops at the account it cannot name.
    """
    mock_client.get_user.return_value = User.from_api({**make_user().raw, "customer_id": None})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "   ", "password": "goed"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_customer_id"}


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

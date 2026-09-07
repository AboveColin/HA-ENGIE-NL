"""Config flow for the ENGIE Energie NL integration.

Step ``user`` takes the Mijn ENGIE email and password. ENGIE's Okta then emails
a one-time code, so step ``email_code`` collects it and finishes the login. An
account carrying a factor this client cannot answer falls back to step
``browser``: the user opens a URL, logs in, and pastes back the
``engie://login/okta/callback?code=...`` URL the browser could not open.

Only the token pair is stored. The code is needed once, at setup and at reauth;
the refresh token keeps every later poll out of the mailbox.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from engie_nl import (
    BrowserLogin,
    EmailChallenge,
    EngieAuthError,
    EngieClient,
    EngieEmailCodeRequired,
    EngieError,
    EngieMfaRequiredError,
    EngieNetworkError,
    OktaAuth,
    TokenSet,
)

from .const import (
    CONF_CUSTOMER_ID,
    CONF_INCLUDE_DAY_AHEAD,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKENS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_CALLBACK = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=False))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD,
    }
)
BROWSER_SCHEMA = vol.Schema({vol.Required("callback_url"): _CALLBACK})
EMAIL_CODE_SCHEMA = vol.Schema({vol.Required("code"): str})
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD})


class EngieConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ENGIE config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._browser: BrowserLogin | None = None
        self._email_challenge: EmailChallenge | None = None
        self._auth: OktaAuth | None = None

    def _get_auth(self) -> OktaAuth:
        if self._auth is None:
            self._auth = OktaAuth(session=async_get_clientsession(self.hass))
        return self._auth

    async def _finish(self, tokens: TokenSet) -> ConfigFlowResult:
        """Common tail: read the customer id, set the unique id, create or update."""
        client = EngieClient(tokens, auth=self._get_auth(), session=async_get_clientsession(self.hass))
        user = await client.get_user()
        customer_id = user.customer_id or self._username or "engie"
        await self.async_set_unique_id(customer_id)

        data = {CONF_USERNAME: self._username, CONF_CUSTOMER_ID: customer_id, CONF_TOKENS: tokens.to_dict()}
        if self.source == "reauth":
            entry = self._get_reauth_entry()
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data_updates=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"ENGIE {customer_id}", data=data)

    async def _login(self, username: str, password: str, errors: dict[str, str]) -> TokenSet | None:
        """Password login.

        Returns the tokens only when no second factor is involved. Otherwise it
        arms the next step and returns ``None`` with no error: an emailed code
        (the normal case for ENGIE) or, for a factor this client cannot answer,
        the browser flow.
        """
        try:
            return await self._get_auth().login(username, password)
        except EngieEmailCodeRequired as err:
            self._email_challenge = err.challenge
        except EngieMfaRequiredError:
            self._browser = self._get_auth().begin_browser_login()
        except EngieAuthError:
            errors["base"] = "invalid_auth"
        except EngieNetworkError:
            errors["base"] = "cannot_connect"
        except EngieError:
            _LOGGER.exception("Unexpected error while logging in to ENGIE")
            errors["base"] = "unknown"
        return None

    async def _next_step_after_password(self) -> ConfigFlowResult | None:
        """Whichever second step the password attempt armed, if any."""
        if self._email_challenge is not None:
            return await self.async_step_email_code()
        if self._browser is not None:
            return await self.async_step_browser()
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Username and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            tokens = await self._login(self._username, user_input[CONF_PASSWORD], errors)
            if tokens is not None:
                return await self._finish(tokens)
            nxt = await self._next_step_after_password()
            if nxt is not None:
                return nxt
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_email_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take the one-time code ENGIE emailed and finish the login."""
        errors: dict[str, str] = {}
        if user_input is not None and self._email_challenge is not None:
            try:
                tokens = await self._get_auth().submit_email_code(self._email_challenge, user_input["code"])
            except EngieNetworkError:
                errors["base"] = "cannot_connect"
            except EngieAuthError:
                # Okta rejects a wrong code and an expired one the same way, and
                # the code dies with the transaction after a few minutes.
                errors["base"] = "invalid_code"
            else:
                return await self._finish(tokens)
        return self.async_show_form(step_id="email_code", data_schema=EMAIL_CODE_SCHEMA, errors=errors)

    async def async_step_browser(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """MFA accounts: open the URL, paste the callback."""
        if self._browser is None:
            self._browser = self._get_auth().begin_browser_login()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens = await self._get_auth().finish_browser_login(user_input["callback_url"].strip(), self._browser)
            except EngieAuthError:
                errors["base"] = "invalid_callback"
            except EngieNetworkError:
                errors["base"] = "cannot_connect"
            else:
                return await self._finish(tokens)
        return self.async_show_form(
            step_id="browser",
            data_schema=BROWSER_SCHEMA,
            errors=errors,
            description_placeholders={"url": self._browser.url},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The stored session can no longer be refreshed."""
        self._username = entry_data.get(CONF_USERNAME)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the password again."""
        errors: dict[str, str] = {}
        if user_input is not None and self._username:
            tokens = await self._login(self._username, user_input[CONF_PASSWORD], errors)
            if tokens is not None:
                return await self._finish(tokens)
            nxt = await self._next_step_after_password()
            if nxt is not None:
                return nxt
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"username": self._username or ""},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return EngieOptionsFlow()


class EngieOptionsFlow(OptionsFlow):
    """Polling interval and the optional day-ahead prices."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL_MINUTES,
                    default=options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MINUTES, max=1440, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
                    )
                ),
                vol.Optional(
                    CONF_INCLUDE_DAY_AHEAD, default=options.get(CONF_INCLUDE_DAY_AHEAD, False)
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

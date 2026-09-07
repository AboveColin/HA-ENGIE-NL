"""Fixtures for the ENGIE Energie NL integration tests.

The harness is pytest-homeassistant-custom-component. The integration is
symlinked into its ``testing_config/custom_components`` (see the README's
development section), and ``enable_custom_integrations`` is on for every test.
The engie_nl library is faked at its two entry points, ``OktaAuth`` and
``EngieClient``, with objects that return the models the coordinator expects.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from engie_nl import (
    Consumption,
    ConsumptionSeries,
    EngieAuthError,
    EstimationCosts,
    MeteringPoint,
    MeterReadings,
    Reading,
    Register,
    TokenSet,
    Transaction,
    User,
)

from custom_components.engie_nl.const import CONF_CUSTOMER_ID, CONF_TOKENS, DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"

EAN_E = "871694840000000001"
EAN_G = "871694840000000002"
CUSTOMER = "K01234567"
TOKENS = TokenSet(access_token="okta-access", refresh_token="okta-refresh", expires_at=9_999_999_999.0)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Every test loads the custom component."""


def make_user() -> User:
    return User.from_api(
        {
            "customer_id": CUSTOMER,
            "email": "klant@example.com",
            "delivery_addresses": [
                {
                    "id": "adr-1",
                    "street": "Straat",
                    "house_nr": "1",
                    "zip_code": "1234AB",
                    "city": "Stad",
                    "metering_points": [
                        {"ean": EAN_E, "type": "E", "smart": True, "prepayment_amount": 187},
                        {"ean": EAN_G, "type": "G", "smart": True},
                    ],
                }
            ],
        }
    )


def make_consumptions() -> list[ConsumptionSeries]:
    return [
        ConsumptionSeries(
            ean=EAN_E,
            data=[
                Consumption(day=date(2026, 9, 5), normal=4.0, low=3.0, return_normal=1.0, return_low=0.5),
                Consumption(day=date(2026, 9, 6), normal=5.0, low=2.0, return_normal=2.0, return_low=0.0),
            ],
            error=None,
        ),
        ConsumptionSeries(ean=EAN_G, data=[Consumption(day=date(2026, 9, 6), normal=1.5, low=None, return_normal=None, return_low=None)], error=None),
    ]


def make_readings() -> list[MeterReadings]:
    def reg(name: str, kind: str, seq: int, value: int) -> Register:
        return Register(name=name, kind=kind, sequence=seq, readings=[
            Reading(day=date(2026, 9, 1), value=value - 40, source="P4", description=None),
            Reading(day=date(2026, 9, 7), value=value, source="P4", description=None),
        ])

    return [
        MeterReadings(ean=EAN_E, registers=[
            reg("Normaal", "consumption", 1, 12000),
            reg("Dal", "consumption", 2, 9000),
            reg("Normaal", "return", 1, 3000),
            reg("Dal", "return", 2, 1000),
        ]),
        MeterReadings(ean=EAN_G, registers=[reg("Gas", "consumption", 1, 700)]),
    ]


def make_estimations() -> EstimationCosts:
    return EstimationCosts.from_api(
        {"prepayment_amount_current": 187, "prepayment_amount_advice": 195.5, "prepayment_amount_min": 150,
         "prepayment_amount_max": 250, "total_estimated_nota_amount": 2244.0, "number_of_prepayments_left": 11}
    )


def make_transactions() -> list[Transaction]:
    return [
        Transaction.from_api({"id": "t1", "date": "2026-09-27", "amount": -187.0, "status": "OPEN", "type": "prepayment",
                              "description": "Termijnbedrag september"}),
        Transaction.from_api({"id": "t0", "date": "2026-08-27", "amount": -187.0, "status": "PAID", "type": "prepayment"}),
    ]


@pytest.fixture
def mock_client() -> Generator[MagicMock, None, None]:
    """A fake EngieClient whose reads return the fixtures above."""
    client = MagicMock()
    client.get_user = AsyncMock(return_value=make_user())
    client.get_consumptions = AsyncMock(return_value=make_consumptions())
    client.get_meter_readings = AsyncMock(return_value=make_readings())
    client.get_estimations = AsyncMock(return_value=make_estimations())
    client.get_transactions = AsyncMock(return_value=make_transactions())
    client.get_day_ahead_prices = AsyncMock(return_value=[])
    client.tokens = TOKENS
    with (
        patch("custom_components.engie_nl.EngieClient", return_value=client),
        patch("custom_components.engie_nl.config_flow.EngieClient", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_auth() -> Generator[MagicMock, None, None]:
    """A fake OktaAuth that logs in for the right password and refuses the rest."""
    auth = MagicMock()

    async def login(username: str, password: str) -> TokenSet:
        if password == "goed":
            return TOKENS
        raise EngieAuthError("nope")

    auth.login = AsyncMock(side_effect=login)
    auth.refresh = AsyncMock(return_value=TOKENS)
    browser = MagicMock()
    browser.url = "https://login.engie.nl/oauth2/default/v1/authorize?x=1"
    browser.state = "st"
    browser.verifier = "v"
    auth.begin_browser_login = MagicMock(return_value=browser)
    auth.finish_browser_login = AsyncMock(return_value=TOKENS)
    with (
        patch("custom_components.engie_nl.OktaAuth", return_value=auth),
        patch("custom_components.engie_nl.config_flow.OktaAuth", return_value=auth),
    ):
        yield auth


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"ENGIE {CUSTOMER}",
        unique_id=CUSTOMER,
        data={"username": "klant@example.com", CONF_CUSTOMER_ID: CUSTOMER, CONF_TOKENS: TOKENS.to_dict()},
    )

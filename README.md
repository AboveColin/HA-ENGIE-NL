# ENGIE Energie NL for Home Assistant

Reads your Mijn ENGIE account into Home Assistant: meter readings per register,
the last day's consumption and return per connection, the contract's own rates
and standing charge, your termijnbedrag and ENGIE's advice for it, the projected
year total, open invoices, outages, and optionally the day-ahead prices of
ENGIE's dynamic contract.

It uses the same private gateway the ENGIE app uses, through the
[engie-nl](https://github.com/AboveColin/engie-nl) Python package. ENGIE does
not document or support that API; when it changes, this integration breaks
until it is updated.

[![Validate](https://github.com/AboveColin/HA-ENGIE-NL/actions/workflows/validate.yaml/badge.svg)](https://github.com/AboveColin/HA-ENGIE-NL/actions/workflows/validate.yaml)
[![Tests](https://github.com/AboveColin/HA-ENGIE-NL/actions/workflows/tests.yml/badge.svg)](https://github.com/AboveColin/HA-ENGIE-NL/actions/workflows/tests.yml)

## What you get

One device per connection (EAN) and one for the account.

| Device | Entity | Unit | Notes |
|---|---|---|---|
| Elektriciteit | Meter reading normal / low | kWh | cumulative, usable in the Energy dashboard |
| Elektriciteit | Meter reading return normal / low | kWh | teruglevering, cumulative |
| Elektriciteit | Consumption last day, Return last day | kWh | the newest day ENGIE has; attributes hold the date and the split |
| Elektriciteit | Tariff, Tariff low, Feed-in tariff | EUR/kWh | the contract's own rates, all-in |
| Gas | Meter reading | m3 | cumulative |
| Gas | Consumption last day | m3 | |
| Gas | Tariff | EUR/m3 | all-in |
| both | Standing charge | EUR/day | vastrecht |
| both | Product | | the supplying product, with start and end date as attributes |
| both | Delivering | on/off | whether ENGIE actually supplies this connection yet |
| both | Smart meter, Data mandate | on/off | diagnostic |
| both | Last reading date | | diagnostic |
| Account | Monthly payment, Monthly payment advice | EUR | termijnbedrag now and ENGIE's advice, with min/max as attributes |
| Account | Estimated year total | EUR | |
| Account | Open amount | EUR | sum of invoices with status OPEN |
| Account | Last transaction | EUR | date, description and status as attributes |
| Account | Message of the day | | ENGIE's daily line, which names tomorrow's cheapest hour |
| Account | Outage | on/off | ENGIE has posted a message |
| Account | Outage messages, Documents, Monthly reports, Energy label | | diagnostic |
| Account | Day-ahead electricity / gas price | EUR/kWh, EUR/m3 | only when enabled in options |

### Before a contract starts

ENGIE reports a connection as a smart, readable meter well before it supplies
it, and refuses every data endpoint until it does. The **Delivering** binary
sensor is that state: it reads `has_data` from the account record, which is what
the gateway itself checks. While it is off, the consumption, reading and tariff
sensors have nothing to show, and the integration does not ask for them.

### The tariff sensors carry a caveat

`GET /api/v1/tariffs` refuses an EAN the customer does not supply yet, so the
rules that sort its entries into "normal", "low", "feed-in" and "standing
charge" were read from the app's model rather than from a response. Each sensor
is unavailable when nothing matched, never a guessed number, and the `entries`
attribute on the Tariff sensor lists every component ENGIE sent so the total can
be checked by hand.

ENGIE receives smart-meter data once a day, so the default update interval is
one hour. Values are what ENGIE has processed, not a live meter; for live power
use your P1 reader.

## Installation

Through HACS as a custom repository (`https://github.com/AboveColin/HA-ENGIE-NL`,
category Integration), then restart Home Assistant and add "ENGIE Energie NL"
under Settings, Devices & services.

## Login

Enter the email address and password of your Mijn ENGIE account. ENGIE then
emails a one-time code, and the next screen asks for it. The code expires after
a few minutes; if it does, start the setup again to get a new one.

The password and the code are used once and are not stored. The integration
keeps only the resulting session and renews it by itself, so you are not asked
for a code again unless the session is lost, which triggers a reauth prompt.

If your account uses a factor other than email, the flow shows a link instead.
Open it, log in, and copy the address the browser then fails to open (it starts
with `engie://login/okta/callback?code=`) into the form.

## Development

```sh
uv venv .venv && uv pip install pytest-homeassistant-custom-component -e ../engie-nl
# the harness loads custom components from its own testing_config directory
ln -s "$PWD/custom_components/engie_nl" \
  "$(.venv/bin/python -c 'import pytest_homeassistant_custom_component as p, pathlib; print(pathlib.Path(p.__file__).parent / "testing_config" / "custom_components")')/engie_nl"
.venv/bin/pytest
```

`pylint` run from this directory reports `E0611: No name ... in module 'engie_nl'`
for every import from the library. That is a name clash: the component directory
is also called `engie_nl`, and pylint puts `custom_components/` on its path. Home
Assistant imports the component as `custom_components.engie_nl`, so the clash does
not exist at runtime; the tests are the check.

## Privacy

Diagnostics redact tokens, your customer number, EANs, names, address and bank
account, along with your payment method, energy label profile and the
standaardjaarverbruik of each register. The netbeheerder's name and EAN are
kept: they name a public company, not you, and they are what a connection
problem is read from. Nothing is sent anywhere except to ENGIE's own servers.

## Credits

Uses the [`engie-nl`](https://github.com/AboveColin/engie-nl) client library.

## License

MIT

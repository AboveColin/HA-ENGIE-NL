# ENGIE Energie NL for Home Assistant

Reads your Mijn ENGIE account into Home Assistant: meter readings per register,
the last day's consumption and return per connection, your termijnbedrag and
ENGIE's advice for it, the projected year total, open invoices, and optionally
the day-ahead prices of ENGIE's dynamic contract.

It uses the same private gateway the ENGIE app uses, through the
[engie-nl](https://github.com/AboveColin/engie-nl) Python package. ENGIE does
not document or support that API; when it changes, this integration breaks
until it is updated.

## What you get

One device per connection (EAN) and one for the account.

| Device | Sensor | Unit | Notes |
|---|---|---|---|
| Elektriciteit | Meter reading normal / low | kWh | cumulative, usable in the Energy dashboard |
| Elektriciteit | Meter reading return normal / low | kWh | teruglevering, cumulative |
| Elektriciteit | Consumption last day, Return last day | kWh | the newest day ENGIE has; attributes hold the date and the split |
| Gas | Meter reading | m3 | cumulative |
| Gas | Consumption last day | m3 | |
| both | Last reading date | | diagnostic |
| Account | Monthly payment, Monthly payment advice | EUR | termijnbedrag now and ENGIE's advice, with min/max as attributes |
| Account | Estimated year total | EUR | |
| Account | Open amount | EUR | sum of invoices with status OPEN |
| Account | Last transaction | EUR | date, description and status as attributes |
| Account | Day-ahead electricity / gas price | EUR/kWh, EUR/m3 | only when enabled in options |

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
account. Nothing is sent anywhere except to ENGIE's own servers.

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

Enter the email address and password of your Mijn ENGIE account. The password
is used once to log in through ENGIE's login server (Okta) and is not stored;
the integration keeps only the resulting session and renews it itself.

If your account has two-factor authentication, the flow shows a link. Open it,
log in, and copy the address the browser then fails to open (it starts with
`engie://login/okta/callback?code=`) into the form.

## Privacy

Diagnostics redact tokens, your customer number, EANs, names, address and bank
account. Nothing is sent anywhere except to ENGIE's own servers.

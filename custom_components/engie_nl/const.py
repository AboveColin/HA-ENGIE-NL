"""Constants for the ENGIE Energie NL integration."""

from __future__ import annotations

DOMAIN = "engie_nl"
MANUFACTURER = "ENGIE Nederland Retail B.V."

CONF_TOKENS = "tokens"
CONF_CUSTOMER_ID = "customer_id"
CONF_INCLUDE_DAY_AHEAD = "include_day_ahead"
CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"

# ENGIE receives P4 (smart meter) data once a day and the app shows yesterday
# at the earliest. Polling more often than hourly finds nothing new; the
# minimum exists so a misconfigured entry cannot hammer the gateway.
DEFAULT_SCAN_INTERVAL_MINUTES = 60
MIN_SCAN_INTERVAL_MINUTES = 15

# Window sizes for the per-poll reads. Two weeks of consumption covers the
# P4 lag with room; 60 days of readings always contains the latest one.
CONSUMPTION_DAYS = 14
READINGS_DAYS = 60

# The window asked of /api/v1/tariffs. Both dates are required (the gateway
# answers 422 and names them otherwise), and a tariff entry carries its own
# date_start and date_end, so a month forward covers a contract that changes
# mid-period without pulling in a year of history.
TARIFF_WINDOW_DAYS = 31

# Values of MeteringPoint.kind seen or expected. The app's model stores it as
# a free string; the live fixture decides which spelling is real.
ELECTRICITY_KINDS = {"e", "elk", "electricity", "elektriciteit", "stroom", "power"}
GAS_KINDS = {"g", "gas"}

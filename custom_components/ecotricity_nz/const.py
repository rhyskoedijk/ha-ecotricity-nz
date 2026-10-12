"""Constants for the Ecotricity NZ integration."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Final

DOMAIN: Final = "ecotricity_nz"
LOGGER = logging.getLogger(__package__)

CONF_ICP: Final = "icp"
CONF_ACCOUNT_NUMBER: Final = "account_number"
CONF_PASSWORD: Final = "password"  # noqa: S105 - a config key, not a secret
CONF_PLAN_ID: Final = "plan_id"
CONF_INCLUDE_GST: Final = "include_gst"

DEFAULT_INCLUDE_GST: Final = True
GST_FACTOR: Final = 1.15

LOCAL_TIME_ZONE: Final = "Pacific/Auckland"
# The portal labels NZ wall-clock times with its server's offset; see api/portal.py.
PORTAL_SERVER_TIME_ZONE: Final = "Europe/Berlin"

PRICING_UPDATE_INTERVAL: Final = timedelta(hours=24)
CONSUMPTION_UPDATE_INTERVAL: Final = timedelta(hours=24)
# After a failed update, try again this soon instead of waiting a whole update interval.
RETRY_AFTER_FAILURE: Final = timedelta(minutes=30)
# The portal is asked for at most this much half-hourly data per request.
CONSUMPTION_CHUNK: Final = timedelta(days=7)
# A missing half-hour newer than this (before the portal's latest data) holds back the import in
# case the portal fills it in; an older one is imported as it stands.
CONSUMPTION_SETTLE_PERIOD: Final = timedelta(days=7)
# Pause between backfill requests so a first run doesn't hammer the portal.
BACKFILL_PAUSE_SECONDS: Final = 2.0

# Raise a repair issue after this many consecutive failures to read TOU charts or mint tokens.
FAILURE_ISSUE_THRESHOLD: Final = 3

SERVICE_REFRESH: Final = "refresh"

ISSUE_ATTESTATION: Final = "attestation_failed"
ISSUE_TOU_CHART: Final = "tou_chart_unreadable"
ISSUE_PLAN_NOT_FOUND: Final = "plan_not_found"

# 10 digits, 2 letters (network), 3 alphanumeric checksum characters.
ICP_PATTERN: Final = re.compile(r"^\d{10}[A-Z]{2}[A-Z0-9]{3}$")

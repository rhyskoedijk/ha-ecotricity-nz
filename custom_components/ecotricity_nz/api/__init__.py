"""Clients for the Ecotricity onboarding API and customer portal.

This package has no Home Assistant imports so it can be split out into a standalone library.
"""

from .attestation import AttestationTokenManager, solve_challenge
from .exceptions import (
    AttestationError,
    AuthError,
    EcotricityConnectionError,
    EcotricityError,
    EcotricityResponseError,
    IcpNotFoundError,
)
from .models import (
    ConsumptionData,
    IcpDetails,
    IntervalReading,
    MeterChannel,
    Plan,
    PlanList,
    PortalIndicator,
    Rate,
    RateUnit,
    Season,
)
from .onboarding import OnboardingClient
from .portal import PortalClient

__all__ = [
    "AttestationError",
    "AttestationTokenManager",
    "AuthError",
    "ConsumptionData",
    "EcotricityConnectionError",
    "EcotricityError",
    "EcotricityResponseError",
    "IcpDetails",
    "IcpNotFoundError",
    "IntervalReading",
    "MeterChannel",
    "OnboardingClient",
    "Plan",
    "PlanList",
    "PortalClient",
    "PortalIndicator",
    "Rate",
    "RateUnit",
    "Season",
    "solve_challenge",
]

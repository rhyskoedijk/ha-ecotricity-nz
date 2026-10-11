"""Exceptions raised by the Ecotricity API clients."""


class EcotricityError(Exception):
    """Base class for all Ecotricity API errors."""


class EcotricityConnectionError(EcotricityError):
    """The service could not be reached or returned an unexpected response."""


class EcotricityResponseError(EcotricityError):
    """The service responded, but the payload was not in the expected shape."""


class AttestationError(EcotricityError):
    """The onboarding attestation token could not be minted or was rejected."""


class IcpNotFoundError(EcotricityError):
    """The onboarding service has no record of the requested ICP."""


class AuthError(EcotricityError):
    """The customer portal rejected the account number or password."""

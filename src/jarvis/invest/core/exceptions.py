"""Core exceptions for invest platform."""

from __future__ import annotations


class PlatformError(Exception):
    """Base class for domain-specific platform exceptions."""


class ConfigurationError(PlatformError):
    """Raised when runtime configuration is invalid."""


class DataValidationError(PlatformError):
    """Raised when inbound data does not satisfy schema requirements."""


class RiskCheckError(PlatformError):
    """Raised when a risk rule blocks order generation or submission."""


class BrokerError(PlatformError):
    """Raised when the broker adapter fails."""


class ExternalServiceError(PlatformError):
    """Raised when an external market or broker data source fails."""


class UnsupportedModeError(PlatformError):
    """Raised when a broker mode or model family is unsupported."""

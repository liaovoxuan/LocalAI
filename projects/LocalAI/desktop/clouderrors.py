"""Cloud error taxonomy for the merged LocalAI cloud mode.

This module is the single owner of cloud error classification.  It is
standard-library only and performs no I/O at import time, so it can be
imported from tests, from the desktop app and from WorkBuddy integration
code without side effects.

Contract
--------
* ``missing_api_key`` is the cross-language sentinel also used by the
  CloudAI C++ core (``std::runtime_error("missing_api_key")``) and by the
  pure-Python core (``PermissionError("missing_api_key")``).
* Every cloud failure maps onto exactly one of :data:`CLOUD_ERROR_KEYS`,
  which are also the i18n keys used by the UI.
"""
from __future__ import annotations

from typing import Any, Mapping

CLOUD_NO_KEY = "cloud_no_key"
CLOUD_AUTH_ERROR = "cloud_auth_error"
CLOUD_REQUEST_ERROR = "cloud_request_error"

CLOUD_ERROR_KEYS = (CLOUD_NO_KEY, CLOUD_AUTH_ERROR, CLOUD_REQUEST_ERROR)

#: Sentinel string shared with the CloudAI C++ core and pure-Python core.
MISSING_API_KEY_SENTINEL = "missing_api_key"

_AUTH_STATUS_CODES = frozenset({401, 403})

_NO_KEY_HINTS = (
    MISSING_API_KEY_SENTINEL,
    "missing api key",
    "api key is empty",
    "apikey is empty",
    "no api key",
    "api key not configured",
    "api key missing",
)

_AUTH_HINTS = (
    "401",
    "403",
    "unauthorized",
    "authorization",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "authentication failed",
    "permission denied",
)

_REQUEST_HINTS = (
    "timeout",
    "timed out",
    "connection",
    "refused",
    "unreachable",
    "dns",
    "resolve",
    "network",
    "ssl",
    "proxy",
    "bad gateway",
    "service unavailable",
)

_NETWORK_EXCEPTION_NAMES = frozenset(
    {
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "RequestException",
        "URLError",
        "HTTPError",
        "SSLError",
        "socket.timeout",
        "timeout",
    }
)


class CloudAIError(RuntimeError):
    """A cloud failure carrying a stable, translatable classification key."""

    def __init__(self, key: str = CLOUD_REQUEST_ERROR, detail: str = "") -> None:
        if key not in CLOUD_ERROR_KEYS:
            raise ValueError(f"unknown cloud error key: {key!r}")
        self.key = key
        self.detail = str(detail or "")
        super().__init__(self.key if not self.detail else f"{self.key}: {self.detail}")

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "detail": self.detail}


def missing_api_key(detail: str = "") -> CloudAIError:
    """Build the canonical "provider has no API key" error."""
    return CloudAIError(CLOUD_NO_KEY, detail or MISSING_API_KEY_SENTINEL)


def is_missing_api_key(exc: BaseException) -> bool:
    return cloud_exception_key(exc) == CLOUD_NO_KEY


def _status_code(exc: BaseException) -> Any:
    """Best-effort status code lookup without importing an HTTP library."""
    for holder in (getattr(exc, "response", None), exc):
        if holder is None:
            continue
        for attribute in ("status_code", "status"):
            value = getattr(holder, attribute, None)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
    return None


def _text(exc: BaseException) -> str:
    try:
        return str(exc or "").strip().lower()
    except Exception:  # pragma: no cover - defensive, __str__ must not explode
        return ""


def cloud_exception_key(exc: BaseException) -> str:
    """Map any exception onto one of :data:`CLOUD_ERROR_KEYS`."""
    if isinstance(exc, CloudAIError):
        return exc.key
    if isinstance(exc, PermissionError):
        return CLOUD_NO_KEY

    text = _text(exc)
    if any(hint in text for hint in _NO_KEY_HINTS):
        return CLOUD_NO_KEY

    status = _status_code(exc)
    if status in _AUTH_STATUS_CODES:
        return CLOUD_AUTH_ERROR
    if isinstance(status, int) and status >= 400:
        return CLOUD_REQUEST_ERROR

    if any(hint in text for hint in _AUTH_HINTS):
        return CLOUD_AUTH_ERROR

    names = {type(exc).__name__, type(exc).__module__ + "." + type(exc).__name__}
    if names & _NETWORK_EXCEPTION_NAMES or isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return CLOUD_REQUEST_ERROR

    if any(hint in text for hint in _REQUEST_HINTS):
        return CLOUD_REQUEST_ERROR

    return CLOUD_REQUEST_ERROR


def cloud_error_message_key(exc: BaseException) -> str:
    """Alias kept for call sites that speak in terms of "message keys"."""
    return cloud_exception_key(exc)


def cloud_error_from(exc: BaseException) -> CloudAIError:
    """Coerce any exception into a :class:`CloudAIError` without losing it."""
    if isinstance(exc, CloudAIError):
        return exc
    return CloudAIError(cloud_exception_key(exc), _text(exc))


def describe(exc: BaseException) -> Mapping[str, str]:
    """Small helper for structured logging / WorkBuddy hand-off."""
    error = cloud_error_from(exc)
    return {"key": error.key, "detail": error.detail, "type": type(exc).__name__}

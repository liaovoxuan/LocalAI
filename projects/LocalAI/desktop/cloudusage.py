"""Cloud usage / billing probing with stable summaries.

Standard-library only; the HTTP client is injected or lazily imported so that
importing this module never touches the network or pulls in ``requests``.

Contract
--------
* URL probing order is identical to the CloudAI C++ core (``usageUrls``) and
  the pure-Python core, so all three implementations agree.
* :func:`summarize_usage_data` implements the *simple* normative form (first
  matching key -> ``key=value``).  The CloudAI desktop variant that emitted
  ``credits=..,used=..`` was deliberately not carried over, so the same
  response always yields the same string.
* ``404``/``405`` mean "this endpoint does not exist" and are skipped; any
  other failure is remembered and probed past, and only re-raised when no URL
  produced a summary.
* Only a real ``http(s)`` base URL is used to build generic probes, so a blank
  or malformed base never produces nonsense endpoints like ``not a url/usage``.
* ``KeyboardInterrupt`` / ``SystemExit`` always propagate: probing must never
  turn a user interrupt into a cloud request error.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from clouderrors import CLOUD_REQUEST_ERROR, CloudAIError, missing_api_key
from cloudproviders import (
    normalize_base_url,
    normalize_cloud_provider,
    provider_url_is_valid,
)

PROVIDER_USAGE_URLS: dict[str, tuple[str, ...]] = {
    "openai_official": (
        "https://api.openai.com/v1/dashboard/billing/usage",
        "https://api.openai.com/v1/usage",
    ),
    "openrouter": ("https://openrouter.ai/api/v1/credits",),
    "deepseek": ("https://api.deepseek.com/user/balance",),
    "siliconflow": (
        "https://api.siliconflow.cn/v1/user/info",
        "https://api.siliconflow.cn/v1/user/balance",
    ),
}

GENERIC_USAGE_SUFFIXES = ("/usage", "/dashboard/billing/usage", "/user/usage")

#: Key probe order; first hit wins.
SUMMARY_KEYS = (
    "total_usage",
    "total_credits",
    "balance",
    "total_granted",
    "total_used",
    "limit",
    "used",
    "remaining",
    "total",
)

SKIPPED_STATUS_CODES = frozenset({404, 405})

DEFAULT_TIMEOUT = 20


def cloud_usage_urls(provider: Any, base_url: Any) -> list[str]:
    """Ordered, de-duplicated usage endpoints for one provider/base URL."""
    name = normalize_cloud_provider(provider)
    base = normalize_base_url(base_url)
    urls: list[str] = list(PROVIDER_USAGE_URLS.get(name, ()))
    if base and provider_url_is_valid(base):
        urls.extend(base + suffix for suffix in GENERIC_USAGE_SUFFIXES)
    return list(dict.fromkeys(urls))


def summarize_usage_data(data: Any) -> str:
    """Reduce a usage payload to a short, stable summary string."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return ""
    if not isinstance(data, Mapping):
        return ""

    nested = data.get("data")
    if isinstance(nested, Mapping):
        inner = summarize_usage_data(nested)
        if inner:
            return inner

    for key in SUMMARY_KEYS:
        if key in data:
            return f"{key}={data[key]}"

    if isinstance(nested, list):
        return f"{len(nested)} records"
    return ""


def _ensure_ok(response: Any) -> None:
    """Raise for HTTP failure responses, mirroring ``raise_for_status``."""
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
        return
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 400:
        raise CloudAIError(CLOUD_REQUEST_ERROR, f"HTTP {status}")


def _default_http_get(url: str, *, headers: Mapping[str, str], timeout: int) -> Any:
    import requests  # lazy: keeps module import side-effect free

    return requests.get(url, headers=dict(headers), timeout=timeout)


def fetch_cloud_usage(
    provider: Any,
    base_url: Any,
    api_key: Any,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    http_get: Callable[..., Any] | None = None,
) -> str:
    """Probe usage endpoints until one yields a summary.

    Returns ``""`` when every endpoint answered but none had usable data.
    Raises :class:`CloudAIError` with ``cloud_no_key`` when no key is set, or
    ``cloud_request_error`` when every endpoint failed.  ``BaseException``
    subclasses that are not ``Exception`` (``KeyboardInterrupt``,
    ``SystemExit``) always propagate untouched.
    """
    key = str(api_key or "").strip()
    if not key:
        raise missing_api_key()

    urls = cloud_usage_urls(provider, base_url)
    if not urls:
        return ""

    getter = http_get or _default_http_get
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    last_error: Exception | None = None
    for url in urls:
        try:
            response = getter(url, headers=headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - any transport failure is probed past
            last_error = exc
            continue
        try:
            status = getattr(response, "status_code", None)
            if status in SKIPPED_STATUS_CODES:
                continue
            _ensure_ok(response)
            summary = summarize_usage_data(response.json())
        except Exception as exc:  # noqa: BLE001 - same: remember, keep probing
            last_error = exc
            continue
        if summary:
            return summary

    if last_error is not None:
        raise CloudAIError(CLOUD_REQUEST_ERROR, str(last_error)) from last_error
    return ""

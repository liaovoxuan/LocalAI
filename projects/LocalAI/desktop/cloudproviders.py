"""Cloud provider presets, id normalisation and URL validation.

Standard-library only, no I/O at import time.

Host contract
-------------
The host standardises on::

    config["cloud"] = {"provider": ..., "default_model": ..., "providers": {...}}

A temporary/legacy shape carrying the same fields under ``cloud["public"]``
must keep working.  :func:`resolve_active_provider` accepts either shape (or
the bare inner mapping), merges them, and gives **canonical fields
precedence** on every conflict while letting the legacy shape fill gaps.

Security
--------
No provider credential is ever read from a config mapping.  Only the explicit
``api_key`` argument is used, and a masked placeholder (``********``) is
treated as "absent" so a UI-rendered mask can never become a bearer token.

Other design notes
------------------
* ``openai_compatible`` is the single id for "custom OpenAI-compatible
  endpoint"; the legacy CloudAI id ``custom`` is an alias, never a second
  provider.
* ``openai_compatible`` keeps an **empty** preset ``base_url`` to match
  LocalAI's existing semantics (the user supplies the endpoint).
* This module deliberately does **not** define ``openai_chat_url``; LocalAI
  already owns that helper.  :func:`chat_completions_url` exists for
  integration callers and is locked to LocalAI's behaviour by a parity test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

DEFAULT_CLOUD_PROVIDER = "openai_official"

#: UI placeholder only; never applied as an implicit default.
COMPATIBLE_SUGGESTED_BASE_URL = "http://localhost:8000/v1"

#: Canonical masked placeholder accepted from hosts/UI state.
MASK = "********"


@dataclass(frozen=True)
class CloudProviderInfo:
    """One selectable cloud vendor."""

    id: str
    name: str
    base_url: str
    models: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "models": list(self.models),
        }


CLOUD_PROVIDER_PRESETS: dict[str, CloudProviderInfo] = {
    "openai_official": CloudProviderInfo(
        "openai_official",
        "OpenAI",
        "https://api.openai.com/v1",
        ("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o"),
    ),
    "openai_compatible": CloudProviderInfo(
        "openai_compatible",
        "OpenAI Compatible",
        "",
        ("gpt-4.1-mini", "qwen-plus", "deepseek-chat"),
    ),
    "deepseek": CloudProviderInfo(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        ("deepseek-chat", "deepseek-reasoner"),
    ),
    "openrouter": CloudProviderInfo(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        ("openai/gpt-4o-mini", "deepseek/deepseek-chat", "anthropic/claude-3.5-sonnet"),
    ),
    "siliconflow": CloudProviderInfo(
        "siliconflow",
        "SiliconFlow",
        "https://api.siliconflow.cn/v1",
        ("Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"),
    ),
}

_ALIASES: dict[str, str] = {
    # OpenAI official
    "openai": "openai_official",
    "openai-official": "openai_official",
    "official": "openai_official",
    "gpt": "openai_official",
    "chatgpt": "openai_official",
    # OpenAI compatible / custom
    "openai-compatible": "openai_compatible",
    "openai-compat": "openai_compatible",
    "compatible": "openai_compatible",
    "custom": "openai_compatible",
    "custom-openai-compatible": "openai_compatible",
    "custom-openai": "openai_compatible",
    "local": "openai_compatible",
    "localhost": "openai_compatible",
    "self-hosted": "openai_compatible",
    "兼容": "openai_compatible",
    "自定义": "openai_compatible",
    # DeepSeek
    "deepseek": "deepseek",
    "deep-seek": "deepseek",
    "deepseek-ai": "deepseek",
    "深度求索": "deepseek",
    # OpenRouter
    "openrouter": "openrouter",
    "open-router": "openrouter",
    # SiliconFlow
    "siliconflow": "siliconflow",
    "silicon-flow": "siliconflow",
    "silicon": "siliconflow",
    "硅基流动": "siliconflow",
}

_CHAT_SUFFIX = "/chat/completions"


def normalize_cloud_provider(value: Any) -> str:
    """Return a canonical cloud provider id, defaulting to OpenAI official."""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_CLOUD_PROVIDER
    lowered = text.lower()
    if lowered in CLOUD_PROVIDER_PRESETS:
        return lowered
    key = lowered.replace("_", "-").replace(" ", "-")
    while "--" in key:
        key = key.replace("--", "-")
    key = key.strip("-")
    if key in CLOUD_PROVIDER_PRESETS:
        return key
    return _ALIASES.get(key, DEFAULT_CLOUD_PROVIDER)


def is_cloud_provider(value: Any) -> bool:
    """True only when ``value`` is (or aliases) a known cloud provider."""
    text = str(value or "").strip()
    if not text:
        return False
    if text.lower() in CLOUD_PROVIDER_PRESETS:
        return True
    key = text.lower().replace("_", "-").replace(" ", "-")
    return key.strip("-") in _ALIASES


def cloud_provider_info(provider: Any) -> CloudProviderInfo:
    """Look up a preset; unknown ids fall back to the default provider."""
    return CLOUD_PROVIDER_PRESETS[normalize_cloud_provider(provider)]


def provider_models(provider: Any) -> tuple[str, ...]:
    return cloud_provider_info(provider).models


def provider_names() -> dict[str, str]:
    """``{id: display name}`` for settings UIs."""
    return {key: info.name for key, info in CLOUD_PROVIDER_PRESETS.items()}


def provider_ids() -> Sequence[str]:
    """Canonical provider ids in stable, preset-declaration order."""
    return tuple(CLOUD_PROVIDER_PRESETS)


def normalize_base_url(base_url: Any) -> str:
    """Trim whitespace, trailing slashes and a trailing ``/chat/completions``."""
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith(_CHAT_SUFFIX):
        base = base[: -len(_CHAT_SUFFIX)].rstrip("/")
    return base


def chat_completions_url(base_url: Any) -> str:
    """Compose the chat endpoint.

    Kept behaviourally identical to LocalAI's ``openai_chat_url``; the parity
    is asserted by ``testcloudcontract`` so this never becomes a fork.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith(_CHAT_SUFFIX):
        return base
    return base + _CHAT_SUFFIX


def provider_url_is_valid(value: Any) -> bool:
    """True for any absolute http(s) URL, including localhost / LAN hosts."""
    try:
        parsed = urlparse(str(value or "").strip())
    except (TypeError, ValueError):
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_masked_secret(value: Any) -> bool:
    """Whether ``value`` is a display placeholder rather than a credential.

    Kept local on purpose: ``cloudsecrets`` imports this module, so importing
    it back would create a cycle.  A parity test pins the two definitions
    together so they cannot drift.
    """
    text = str(value or "").strip()
    return text == MASK or (bool(text) and set(text) == {"*"})


@dataclass(frozen=True)
class ActiveProvider:
    """The resolved request target for the active cloud provider."""

    provider: str
    base_url: str
    model: str
    api_key: str = ""

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def url_is_valid(self) -> bool:
        return provider_url_is_valid(self.base_url)

    def to_dict(self) -> dict[str, Any]:
        """Safe for logging/UI: the API key is never included in clear text."""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": self.has_api_key,
        }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _split_cloud_sections(cloud_config: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Return ``(canonical, legacy_public)`` mappings from any accepted input.

    Accepts the whole config, ``config["cloud"]``, or the bare legacy mapping.
    """
    outer = _as_mapping(cloud_config)
    if not outer:
        return {}, {}
    nested = outer.get("cloud")
    canonical = nested if isinstance(nested, Mapping) else outer
    public = canonical.get("public")
    legacy = public if isinstance(public, Mapping) else {}
    return canonical, legacy


def _first_present(*values: Any) -> Any:
    """First value that carries information (not ``None``, not blank)."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _provider_item(providers_map: Any, provider: str) -> Mapping[str, Any]:
    return _as_mapping(_as_mapping(providers_map).get(provider))


def _clean_api_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text or is_masked_secret(text):
        return ""
    return text


def resolve_active_provider(cloud_config: Any, api_key: str = "") -> ActiveProvider:
    """Resolve provider/base_url/model with preset fallbacks.

    Precedence, highest first:

    1. canonical ``cloud[field]`` / ``cloud.providers[provider][field]``
    2. legacy ``cloud.public[field]`` / ``cloud.public.providers[provider][field]``
    3. the provider preset

    Credentials in the config mapping are ignored entirely; ``api_key`` must be
    passed explicitly (a masked placeholder is treated as absent).

    Validation is deliberately *not* performed here: callers check
    :attr:`ActiveProvider.url_is_valid` so a missing URL can be reported with
    the caller's own i18n message instead of a generic error.
    """
    canonical, legacy = _split_cloud_sections(cloud_config)

    provider = normalize_cloud_provider(
        _first_present(canonical.get("provider"), legacy.get("provider"))
    )
    info = CLOUD_PROVIDER_PRESETS[provider]

    canonical_item = _provider_item(canonical.get("providers"), provider)
    legacy_item = _provider_item(legacy.get("providers"), provider)

    base_url = _first_present(
        canonical_item.get("base_url"),
        legacy_item.get("base_url"),
        info.base_url,
    )
    model = _first_present(
        canonical_item.get("model"),
        legacy_item.get("model"),
        canonical.get("default_model"),
        legacy.get("default_model"),
        info.models[0] if info.models else "",
    )
    return ActiveProvider(
        provider=provider,
        base_url=str(base_url or "").strip(),
        model=str(model or "").strip(),
        api_key=_clean_api_key(api_key),
    )


def default_provider_config(provider: Any = DEFAULT_CLOUD_PROVIDER) -> dict[str, Any]:
    """Public (key-free) config block for one provider; safe to persist."""
    info = cloud_provider_info(provider)
    return {
        "enabled": info.id == DEFAULT_CLOUD_PROVIDER,
        "base_url": info.base_url,
        "model": info.models[0],
        "api_key": "",
    }


def legacy_public_view(cloud_config: Any) -> dict[str, Any]:
    """Project the canonical section into the temporary ``cloud.public`` shape.

    Provided so the host can keep emitting the legacy shape during rollout
    without hand-rolling the projection.  Credentials are never included.
    """
    canonical, _legacy = _split_cloud_sections(cloud_config)
    providers_view: dict[str, Any] = {}
    for name, item in _as_mapping(canonical.get("providers")).items():
        entry = _as_mapping(item)
        # Normalise keys so a legacy-shaped view resolves back to the same
        # provider (alias keys such as "custom" would otherwise be missed).
        providers_view[normalize_cloud_provider(name)] = {
            "base_url": str(entry.get("base_url") or ""),
            "model": str(entry.get("model") or ""),
        }
    return {
        "provider": normalize_cloud_provider(canonical.get("provider")),
        "default_model": str(canonical.get("default_model") or ""),
        "providers": providers_view,
    }

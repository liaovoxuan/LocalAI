"""Version-one CloudAI secret vault and lossless legacy migration.

Format contract (must match the CloudAI C++ core and pure-Python core)::

    value  = "v1:" + base64_standard(plaintext XOR salt)      salt = b"CloudAI"
    file   = <data_dir>/config/cloudai_secrets.json
    shape  = {"<provider_id>": "v1:..."}          mode 0600 (best effort)

Guarantees
----------
* **No plaintext fallback.**  :func:`decode_secret` only ever decodes a
  well-formed ``v1:`` blob and returns ``""`` otherwise; it never echoes the
  stored value back as if it were a key.  :func:`read_secret` is the lenient
  variant used *only* by migration and lookups.
* **No data loss.**  Values that cannot be interpreted are preserved verbatim
  and reported as ``unrecoverable`` rather than dropped.  A malformed vault
  file is copied aside before anything is rewritten.
* **Idempotent.**  Re-running :func:`migrate_legacy_secrets` fills only empty
  slots, so a second run reports ``migrated == []`` and rewrites nothing.
* **Best-effort permissions.**  ``chmod`` failures (Windows, foreign FS) never
  raise.

Standard-library only; no I/O at import time.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from cloudproviders import is_cloud_provider, normalize_cloud_provider

MASK = "********"
SECRET_PREFIX = "v1:"
SECRET_SALT = b"CloudAI"

CONFIG_SUBDIR = "config"
SECRET_FILENAME = "cloudai_secrets.json"
LEGACY_PUBLIC_FILENAME = "cloudai_config.json"
MIGRATION_MARKER = ".legacy_migrated"

#: LocalAI's own provider ids that carry an API key, mapped to cloud ids.
LOCAL_PROVIDER_KEY_SLOTS = {
    "openai_official": "openai_official",
    "openai_compatible": "openai_compatible",
    "lm_studio": "openai_compatible",
}


# --------------------------------------------------------------------------- #
# primitive codec
# --------------------------------------------------------------------------- #
def _xor(data: bytes, salt: bytes) -> bytes:
    if not salt:
        salt = b"cloudai"
    return bytes(byte ^ salt[index % len(salt)] for index, byte in enumerate(data))


def encode_secret(value: str) -> str:
    """Encode a key with the canonical v1 format. Empty input -> ""."""
    if value is None:
        return ""
    raw = str(value)
    if not raw:
        return ""
    mixed = _xor(raw.encode("utf-8"), SECRET_SALT)
    return SECRET_PREFIX + base64.b64encode(mixed).decode("ascii")


def decode_with_salt(value: str, salt: bytes) -> str:
    """Decode a ``v1:`` blob with an explicit salt; ``""`` on any failure."""
    text = str(value or "").strip()
    if not text.startswith(SECRET_PREFIX):
        return ""
    payload = text[len(SECRET_PREFIX) :]
    if not payload:
        return ""
    try:
        raw = base64.b64decode(payload.encode("ascii"), validate=True)
        plain = _xor(raw, salt).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError, TypeError):
        return ""
    if not plain or any(ord(ch) < 32 and ch not in "\r\n\t" for ch in plain):
        return ""
    return plain


def decode_secret(value: str) -> str:
    """Strict canonical decode. Never falls back to the raw value."""
    return decode_with_salt(value, SECRET_SALT)


def mask_key(value: Any) -> str:
    return MASK if value else ""


def is_masked_key(value: Any) -> bool:
    text = str(value or "").strip()
    return text == MASK or (bool(text) and set(text) == {"*"})


def looks_like_plain_secret(value: Any) -> bool:
    """Heuristic used to recognise a legacy key stored in clear text."""
    text = str(value or "").strip()
    if not text or is_masked_key(text):
        return False
    if len(text) < 12 or any(ch.isspace() for ch in text):
        return False
    if text.startswith(("{", "[")):
        return False
    if text.lower() in {"none", "null", "undefined", "true", "false"}:
        return False
    return True


def read_secret(value: Any) -> str:
    """Lenient read: masked -> "", ``v1:`` -> decoded, plaintext -> verbatim."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or is_masked_key(text):
        return ""
    if text.startswith(SECRET_PREFIX):
        return decode_secret(text)
    return text if looks_like_plain_secret(text) else ""


# --------------------------------------------------------------------------- #
# paths, json io
# --------------------------------------------------------------------------- #
def config_dir(data_dir: Any) -> Path:
    return Path(data_dir) / CONFIG_SUBDIR


def secret_store_path(data_dir: Any) -> Path:
    return config_dir(data_dir) / SECRET_FILENAME


def legacy_public_config_path(data_dir: Any) -> Path:
    return config_dir(data_dir) / LEGACY_PUBLIC_FILENAME


def migration_marker_path(data_dir: Any) -> Path:
    return config_dir(data_dir) / MIGRATION_MARKER


def read_json_dict(path: Any) -> dict[str, Any]:
    """Read a JSON object; ``{}`` for missing, unreadable or malformed files."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _chmod_best_effort(path: Path, mode: int) -> bool:
    try:
        os.chmod(path, mode)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


def write_json_atomic(path: Any, data: Mapping[str, Any], mode: int = 0o600) -> Path:
    """Atomically write ``data`` and try to restrict permissions."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temporary = tempfile.mkstemp(prefix=".cloudai-", dir=str(target.parent))
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(data), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_best_effort(Path(temporary), mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
    _chmod_best_effort(target, mode)
    return target


# --------------------------------------------------------------------------- #
# vault
# --------------------------------------------------------------------------- #
@dataclass
class SecretNormalization:
    """Outcome of inspecting a stored vault."""

    entries: dict[str, Any] = field(default_factory=dict)
    changed: bool = False
    dropped: list[str] = field(default_factory=list)
    unrecoverable: list[str] = field(default_factory=list)
    malformed: bool = False


def normalize_secret_store(data: Any) -> SecretNormalization:
    """Canonicalise a vault mapping without losing anything it cannot read."""
    result = SecretNormalization()
    if not isinstance(data, Mapping):
        result.malformed = bool(data)
        return result

    for provider, raw in data.items():
        name = str(provider)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            result.dropped.append(name)
            continue
        if is_masked_key(raw):
            result.dropped.append(name)
            continue

        if not isinstance(raw, str):
            # Preserve the original value verbatim: never silently discard.
            result.entries[name] = raw
            result.unrecoverable.append(name)
            continue

        if raw.startswith(SECRET_PREFIX):
            decoded = decode_secret(raw)
            if not decoded:
                result.entries[name] = raw
                result.unrecoverable.append(name)
                continue
            canonical = encode_secret(decoded)
            result.entries[name] = canonical
            if canonical != raw:
                result.changed = True
            continue

        if looks_like_plain_secret(raw):
            result.entries[name] = encode_secret(raw)
            result.changed = True
            continue

        result.entries[name] = raw
        result.unrecoverable.append(name)

    return result


def load_secrets(data_dir: Any) -> dict[str, Any]:
    """Load the vault, normalising as we go. Never raises."""
    return normalize_secret_store(read_json_dict(secret_store_path(data_dir))).entries


def save_secrets(data_dir: Any, data: Mapping[str, Any]) -> Path:
    return write_json_atomic(secret_store_path(data_dir), dict(data or {}))


def get_api_key(data_dir: Any, provider: Any) -> str:
    """Lenient lookup so hand-written plaintext still works."""
    stored = read_json_dict(secret_store_path(data_dir))
    return read_secret(stored.get(normalize_cloud_provider(provider), ""))


def set_api_key(data_dir: Any, provider: Any, api_key: Any) -> None:
    """Store or clear one provider key, preserving every other entry.

    A masked placeholder is ignored, and nothing is written when the effective
    store is unchanged, so a UI round-trip can neither create an empty vault
    nor rewrite an identical file.
    """
    name = normalize_cloud_provider(provider)
    normalization = normalize_secret_store(read_json_dict(secret_store_path(data_dir)))
    stored = dict(normalization.entries)
    updated = dict(stored)
    value = str(api_key or "").strip()
    if value and not is_masked_key(value):
        updated[name] = encode_secret(value)
    elif not value:
        updated.pop(name, None)
    if updated == stored and not normalization.changed:
        return
    save_secrets(data_dir, updated)


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #
@dataclass
class MigrationReport:
    """Structured, log-safe migration outcome."""

    migrated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unrecoverable: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    config_plaintext_keys: list[str] = field(default_factory=list)
    changed: bool = False
    marker_written: bool = False
    vault_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "migrated": list(self.migrated),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "unrecoverable": list(self.unrecoverable),
            "sources": list(self.sources),
            "config_plaintext_keys": list(self.config_plaintext_keys),
            "changed": self.changed,
            "marker_written": self.marker_written,
            "vault_path": self.vault_path,
        }


def _candidate_vault_files(data_dir: Any, extra_paths: Iterable[Any]) -> list[Path]:
    """Legacy locations, in priority order. Read-only probing."""
    home = Path(data_dir)
    candidates = [secret_store_path(home)]
    candidates.extend(Path(item) for item in extra_paths if item)
    candidates.append(legacy_public_config_path(home))
    # A separate CloudAI installation previously lived beside LocalAI's data dir.
    candidates.append(home.parent / "CloudAI" / CONFIG_SUBDIR / SECRET_FILENAME)
    candidates.append(home.parent / "CloudAI" / CONFIG_SUBDIR / LEGACY_PUBLIC_FILENAME)

    seen: set[str] = set()
    ordered: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    return ordered


def _quarantine(path: Path) -> bool:
    """Copy a malformed vault aside so a rewrite cannot destroy evidence."""
    try:
        shutil.copy2(path, path.with_suffix(path.suffix + ".corrupt"))
        return True
    except OSError:
        return False


def _harvest_public_config(
    data: Mapping[str, Any],
    entries: dict[str, Any],
    report: MigrationReport,
    source: str,
) -> None:
    providers = data.get("providers")
    if not isinstance(providers, Mapping):
        return
    for provider, item in providers.items():
        if not isinstance(item, Mapping):
            continue
        name = normalize_cloud_provider(provider)
        raw = item.get("api_key")
        if not name or not raw:
            continue
        if name in entries and read_secret(entries[name]):
            report.skipped.append(name)
            continue
        recovered = read_secret(raw)
        if recovered:
            entries[name] = encode_secret(recovered)
            report.migrated.append(name)
        else:
            report.failed.append(name)


def migrate_legacy_secrets(
    data_dir: Any,
    local_config: Mapping[str, Any] | None = None,
    *,
    hostname_salt: str | None = None,
    hostname_salt_fallback: bool = True,
    legacy_vault_paths: Iterable[Any] = (),
    quarantines_corrupt: bool = True,
) -> MigrationReport:
    """Merge every known legacy secret source into the canonical vault.

    Sources, highest priority first: the existing vault, caller-supplied
    paths, ``config/cloudai_config.json``, a sibling ``CloudAI`` data dir, and
    the plaintext ``api_key`` of LocalAI's own ``config.json``.

    ``config_plaintext_keys`` lists the LocalAI config keys that held a real
    key and were absorbed; the caller decides when to scrub them so that a
    failure here can never lose the original value.
    """
    report = MigrationReport(vault_path=str(secret_store_path(data_dir)))
    vault = secret_store_path(data_dir)

    # 1. Existing vault wins per provider.
    entries: dict[str, Any] = {}
    if vault.exists():
        raw_data = read_json_dict(vault)
        if raw_data:
            normalization = normalize_secret_store(raw_data)
            entries.update(normalization.entries)
            report.unrecoverable.extend(normalization.unrecoverable)
            report.sources.append(str(vault))
        else:
            report.failed.append(str(vault))
            if quarantines_corrupt and _quarantine(vault):
                report.sources.append(str(vault) + ".corrupt")

    # 2. Legacy public configs (CloudAI wrote these next to LocalAI's config).
    for path in _candidate_vault_files(data_dir, legacy_vault_paths):
        if path == vault or not path.exists():
            continue
        data = read_json_dict(path)
        if not data:
            continue
        if str(path).endswith(SECRET_FILENAME):
            normalization = normalize_secret_store(data)
            added = False
            for provider, value in normalization.entries.items():
                name = normalize_cloud_provider(provider)
                if read_secret(entries.get(name, "")):
                    report.skipped.append(name)
                    continue
                if read_secret(value):
                    entries[name] = encode_secret(read_secret(value))
                    report.migrated.append(name)
                    added = True
                else:
                    # Preserve the raw value so the hostname-salt fallback below
                    # can still recover it.  Dropping it here silently lost the
                    # keys of CloudAI 1.0.1 vaults found in legacy locations.
                    entries.setdefault(name, value)
                    report.unrecoverable.append(name)
            report.unrecoverable.extend(normalization.unrecoverable)
            if added or normalization.entries:
                report.sources.append(str(path))
        else:
            _harvest_public_config(data, entries, report, str(path))
            report.sources.append(str(path))

    # 3. LocalAI's own plaintext api_key (single global slot).
    local_config = local_config if isinstance(local_config, Mapping) else {}
    local_provider = str(local_config.get("provider") or "").strip().lower()
    slot = LOCAL_PROVIDER_KEY_SLOTS.get(local_provider)
    for key_name in ("api_key", "openai_api_key"):
        raw = local_config.get(key_name)
        if not raw or is_masked_key(raw):
            continue
        if not slot:
            report.skipped.append(f"{key_name}:{local_provider or 'unknown'}")
            continue
        if read_secret(entries.get(slot, "")):
            report.skipped.append(slot)
            continue
        recovered = read_secret(raw)
        if not recovered:
            report.unrecoverable.append(slot)
            continue
        entries[slot] = encode_secret(recovered)
        report.migrated.append(slot)
        report.config_plaintext_keys.append(key_name)
        report.sources.append(f"localai_config:{key_name}")

    # 4. Last resort: blobs written by CloudAI 1.0.1 desktop (hostname salt).
    if hostname_salt_fallback:
        salt_text = hostname_salt
        if salt_text is None:
            try:
                salt_text = platform.node()
            except Exception:  # pragma: no cover - platform.node is safe
                salt_text = ""
        if salt_text:
            salt = str(salt_text).encode("utf-8")
            for provider, value in list(entries.items()):
                if read_secret(value):
                    continue
                legacy = decode_with_salt(value, salt) if isinstance(value, str) else ""
                if legacy:
                    entries[provider] = encode_secret(legacy)
                    report.migrated.append(provider)
                else:
                    report.failed.append(provider)

    report.migrated = sorted(set(report.migrated))
    report.skipped = sorted(set(report.skipped))
    report.failed = sorted(set(report.failed))
    # A provider that was recovered or explicitly failed is no longer an
    # unresolved "unrecoverable" entry, so the report stays unambiguous.
    resolved = set(report.migrated) | set(report.failed)
    report.unrecoverable = sorted({name for name in report.unrecoverable if name not in resolved})

    current = read_json_dict(vault)
    if dict(entries) != current:
        save_secrets(data_dir, entries)
        report.changed = True

    marker = migration_marker_path(data_dir)
    if report.sources and not marker.exists():
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("migrated\n", encoding="utf-8")
            _chmod_best_effort(marker, 0o600)
            report.marker_written = True
        except OSError:
            report.marker_written = False

    return report

"""Bounded text/``.docx`` attachment extraction for cloud requests.

Standard-library only; no I/O at import time.

Limits (the cross-implementation contract shared with the CloudAI cores)
-----------------------------------------------------------------------
* ``MAX_FILE_BYTES = 256 * 1024`` — at most this many bytes are ever read
  from a single attachment, including the ``word/document.xml`` entry of a
  ``.docx`` archive, so a zip bomb or a huge log file cannot exhaust memory.
  The C++ core applies the same ``256 * 1024`` bound per file.
* ``MAX_FILE_CHARS = 12000`` is a **per-file** cap on the text inlined into
  the last message, marked with ``...[truncated]``.  This matches both
  existing implementations: the C++ core truncates each file inside the
  assembly loop and the pure-Python core caps each file before joining.

Aggregate bounding
------------------
The cap above is per file, so a prompt with many attachments can still grow
without limit (20 files -> up to ~240 000 characters).  That matches the two
existing cores, so it stays the default.  Hosts that need a hard bound pass
``total_limit`` to :func:`build_file_context` / :func:`messages_with_files`
to cap the *aggregate* as well.

Failure handling is deliberately quiet: unreadable, binary, empty or
unsupported attachments produce no text instead of raising, and
:func:`messages_with_files` tells the model when a file could not be read.
"""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree

MAX_FILE_BYTES = 256 * 1024
MAX_FILE_CHARS = 12_000

TRUNCATION_MARKER = "\n...[truncated]"

TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
        ".html", ".htm", ".rtf", ".log", ".ini", ".cfg", ".conf", ".toml", ".py", ".js", ".ts",
        ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".swift", ".kt", ".php",
        ".rb", ".sh", ".bat", ".ps1", ".sql", ".css",
    }
)

IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".heic", ".heif", ".avif", ".ico"}
)

NATIVE_FILE_MODEL_HINTS = (
    "gpt-4o", "gpt-4.1", "vision", "vl", "qwen-vl", "qwen2-vl", "qwen2.5-vl",
    "gemini", "claude", "llava", "minicpm-v", "pixtral", "omni",
)

DOCX_DOCUMENT_ENTRY = "word/document.xml"
UNREADABLE_NOTICE = (
    "The attached file(s) could not be read as text by this app: {names}. "
    "If you cannot access them directly, say that clearly."
)
FILE_CONTEXT_HEADER = "Use the following file content to answer:"


def file_extension(path: Any) -> str:
    try:
        return os.path.splitext(str(path))[1].lower()
    except (TypeError, ValueError):
        return ""


def is_image_file(path: Any) -> bool:
    return file_extension(path) in IMAGE_EXTENSIONS


def model_supports_native_files(model: Any) -> bool:
    name = str(model or "").lower()
    return any(hint in name for hint in NATIVE_FILE_MODEL_HINTS)


def _read_bounded_bytes(path: Any, limit: int = MAX_FILE_BYTES) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(limit)


def _extract_docx_text(raw: bytes) -> str:
    """Pull ``word/document.xml`` text out of a bounded ``.docx`` byte blob."""
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        # Truncated XML cannot be parsed; degrade to a lossy tag strip.
        stripped = re.sub(rb"<[^>]+>", b" ", raw)
        return stripped.decode("utf-8", "ignore").strip()
    texts = [node.text for node in root.iter() if node.tag.endswith("}t") and node.text]
    return "\n".join(texts).strip()


def read_docx_text(path: Any, limit: int = MAX_FILE_BYTES) -> str:
    """Read a ``.docx`` document body, bounded and crash-proof."""
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                with archive.open(DOCX_DOCUMENT_ENTRY) as handle:
                    raw = handle.read(limit)
            except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
                return ""
    except (OSError, zipfile.BadZipFile, ValueError, RuntimeError):
        return ""
    return _extract_docx_text(raw)


def read_text_file(path: Any, limit: int = MAX_FILE_BYTES) -> str:
    """Return extractable text for one attachment, or ``""``.

    Never raises for missing files, directories, binary payloads or
    unsupported extensions.
    """
    try:
        candidate = Path(path)
        if not candidate.is_file():
            return ""
    except (OSError, ValueError, TypeError):
        return ""

    extension = file_extension(candidate)
    if extension == ".docx":
        return read_docx_text(candidate, limit)
    if extension == ".doc":
        return ""
    if extension not in TEXT_EXTENSIONS:
        return ""
    try:
        raw = _read_bounded_bytes(candidate, limit)
    except (OSError, ValueError, TypeError, MemoryError):
        return ""
    return raw.decode("utf-8", "ignore")


def build_file_context(
    paths: Iterable[Any],
    char_limit: int = MAX_FILE_CHARS,
    total_limit: int | None = None,
) -> str:
    """Inline attachment text.

    ``char_limit`` caps each file (the shared-core contract); ``total_limit``
    optionally caps the whole context.  A truncation marker is appended once
    when any cap was hit.
    """
    per_file_cap = max(0, int(char_limit))
    remaining: int | None = None if total_limit is None else max(0, int(total_limit))
    parts: list[str] = []
    truncated = False

    for path in paths or ():
        text = read_text_file(path)
        if not text:
            continue
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            continue

        if len(text) > per_file_cap:
            text = text[:per_file_cap]
            truncated = True

        try:
            name = os.path.basename(str(path))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            name = str(path)
        header = f"[File: {name}]\n"

        if remaining is not None:
            separator = 2 if parts else 0
            budget = remaining - separator - len(header)
            if budget <= 0:
                truncated = True
                break
            if len(text) > budget:
                text = text[:budget]
                truncated = True
            remaining -= separator + len(header) + len(text)
        parts.append(header + text)
        if remaining is not None and remaining <= 0:
            truncated = True
            break

    context = "\n\n".join(parts)
    if truncated:
        context += TRUNCATION_MARKER
    return context.strip()


def encode_file_data_url(path: Any, limit: int = MAX_FILE_BYTES) -> str:
    """Base64 data URL for a native-file request, bounded to ``limit`` bytes."""
    try:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        raw = _read_bounded_bytes(path, limit)
    except (OSError, ValueError, TypeError, MemoryError):
        return ""
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _attachment_names(paths: Sequence[Any]) -> str:
    existing: list[str] = []
    requested: list[str] = []
    for path in paths:
        try:
            name = os.path.basename(str(path))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            name = str(path)
        requested.append(name)
        try:
            if os.path.exists(str(path)):
                existing.append(name)
        except (OSError, ValueError, TypeError):
            continue
    return ", ".join(existing or requested)


def messages_with_files(
    messages: Sequence[Mapping[str, Any]],
    file_paths: Iterable[Any] | None,
    model: Any = "",
    total_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Attach files to the last message.

    Native-file models receive OpenAI content blocks (text / image_url /
    file data URLs); every other model receives the extracted text inlined
    into the last message, or an explicit "could not read" notice.

    ``total_limit`` bounds the aggregate inlined context in characters for both
    the native data-URL path and the extracted-text path; ``None`` keeps the
    shared-core per-file behaviour.
    """
    paths = [path for path in (file_paths or ()) if path]
    if not paths:
        return [dict(item) for item in messages]

    result = [dict(item) for item in messages]
    if not result:
        result.append({"role": "user", "content": ""})
    last = result[-1]
    content = last.get("content", "")

    if model_supports_native_files(model):
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": content if isinstance(content, str) else str(content)}
        ]
        # ``total_limit`` bounds the aggregate inline context in characters.
        # Data URLs are large, so without this a handful of files would sail
        # past the host ceiling; the per-file byte cap still applies.
        remaining = None if total_limit is None else max(0, int(total_limit))
        truncated = False
        for path in paths:
            try:
                if not os.path.exists(str(path)):
                    continue
            except (OSError, ValueError, TypeError):
                continue
            if remaining is not None and remaining <= 0:
                truncated = True
                break
            data_url = encode_file_data_url(path)
            if not data_url:
                continue
            if remaining is not None:
                if len(data_url) > remaining:
                    truncated = True
                    break
                remaining -= len(data_url)
            if is_image_file(path):
                blocks.append({"type": "image_url", "image_url": {"url": data_url}})
            else:
                blocks.append(
                    {
                        "type": "file",
                        "file": {"filename": os.path.basename(str(path)), "file_data": data_url},
                    }
                )
        if truncated:
            blocks.append({"type": "text", "text": TRUNCATION_MARKER.strip()})
        last["content"] = blocks
        return result

    text = content if isinstance(content, str) else str(content)
    context = build_file_context(paths, MAX_FILE_CHARS, total_limit)
    if context:
        last["content"] = f"{text}\n\n{FILE_CONTEXT_HEADER}\n\n{context}".strip()
    else:
        notice = UNREADABLE_NOTICE.format(names=_attachment_names(paths))
        last["content"] = f"{text}\n\n{notice}".strip()
    return result

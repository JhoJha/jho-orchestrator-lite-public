#!/usr/bin/env python3
"""Canonical redaction and safe-JSON persistence boundary (M-SAN-001).

Single shared sanitization policy for every Jho surface that persists,
exports or displays information (worker/validator/reviewer artifacts,
pipeline results, operator summaries, evidence ZIP, TUI output and
notification text). Surfaces must consume this primitive instead of
maintaining independent key lists or regexes.

Policy notes:
- Credential semantics are decided on normalized key names (lowercase with
  every separator removed), so camelCase / snake_case / kebab-case / spaced
  variants fold together and decorated names stay covered (my_secret,
  api_key2, clientSecret, accessToken) while plain "key" words remain
  non-sensitive (keyboard_layout, monkey_patch, primary_key_name, key_count).
- Text redaction hides credential VALUES but keeps enough structure for
  diagnostics (the name/scheme prefix survives, the value becomes REDACTED).
- No arbitrary base64/URL decoding is attempted (out of scope by design).
- Bounded depth/text/container size; never raises on unexpected types.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

MAX_DEPTH = 24
MAX_TEXT_LENGTH = 8000
MAX_ITEMS = 5000

_CREDENTIAL_KEY_CORES = (
    "password",
    "passwd",
    "secret",
    "apikey",
    "credential",
    "privatekey",
    "authorization",
    "cookie",
)

# Model-internals boundary: values under these exact normalized keys never
# persist (chain-of-thought / prompt protection). Exact match on purpose so
# innocuous counters such as thought_chunks survive on stage artifacts.
_PROTECTED_CONTENT_KEYS = frozenset({
    "prompt",
    "reasoning",
    "thought",
    "chainofthought",
    "cot",
    "transcript",
    "rawproviderpayload",
})

_PATTERNS = (
    # Authorization headers/assignments, with or without Bearer/Basic scheme.
    re.compile(r"(?i)(authorization\s*[:=]\s*['\"]?(?:(?:bearer|basic)\s+)?)[^\s,;\"']+"),
    # Bare bearer/basic credentials.
    re.compile(r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9._~+/=\-]{4,}"),
    # name=value / name: value assignments; numeric suffixes (api_key2) and
    # camelCase glue (secretKey) included; JSON-quoted separators tolerated.
    re.compile(
        r"(?i)((?:password|passwd|secret(?:[_-]?key)?|client[_-]?secret|"
        r"access[_-]?token|auth[_-]?token|refresh[_-]?token|session[_-]?token|"
        r"api[_-]?key|credential|private[_-]?key|cookie|token)[0-9]*"
        r"['\"]?\s*[:=]\s*['\"]?)[^\s,;\"']+"
    ),
    # Well-known provider token shapes.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|sk-[A-Za-z0-9_-]{8,}|nvapi-[A-Za-z0-9_-]{8,})\b"),
)


def normalize_key(value: Any) -> str:
    try:
        return re.sub(r"[^a-z0-9]", "", str(value).lower())
    except Exception:
        return ""


def is_credential_key(normalized: str) -> bool:
    if not normalized:
        return False
    if normalized.endswith("token"):
        return True
    return any(core in normalized for core in _CREDENTIAL_KEY_CORES)


def is_sensitive_key(name: Any, *, extra_parts: tuple[str, ...] = ()) -> bool:
    """Canonical sensitive-key classification.

    extra_parts lets a surface layer its own documented minimization policy
    (normalized substrings) on top of the shared credential semantics without
    forking them.
    """
    normalized = normalize_key(name)
    if not normalized:
        return False
    if is_credential_key(normalized):
        return True
    if normalized in _PROTECTED_CONTENT_KEYS:
        return True
    return any(part and part in normalized for part in extra_parts)


def redact_text(value: Any, *, max_length: int | None = None) -> str:
    try:
        out = value if isinstance(value, str) else str(value)
        for pattern in _PATTERNS:
            out = pattern.sub(
                lambda match: (match.group(1) + REDACTED) if match.lastindex else REDACTED,
                out,
            )
    except Exception:
        return REDACTED
    if max_length is not None and len(out) > max_length:
        out = out[:max_length] + "\n" + TRUNCATED
    return out


def sanitize(value: Any, *, extra_sensitive_parts: tuple[str, ...] = (), _depth: int = 0) -> Any:
    try:
        if _depth > MAX_DEPTH:
            return REDACTED
        if value is None or type(value) is bool or isinstance(value, (int, float)):
            return value
        if isinstance(value, (str, Path)):
            return redact_text(value, max_length=MAX_TEXT_LENGTH)
        if callable(value):
            return REDACTED
        if isinstance(value, (list, tuple)):
            items = [
                sanitize(item, extra_sensitive_parts=extra_sensitive_parts, _depth=_depth + 1)
                for item in list(value)[:MAX_ITEMS]
            ]
            if len(value) > MAX_ITEMS:
                items.append(TRUNCATED)
            return items
        if isinstance(value, (set, frozenset)):
            items = [
                sanitize(item, extra_sensitive_parts=extra_sensitive_parts, _depth=_depth + 1)
                for item in sorted(value, key=str)[:MAX_ITEMS]
            ]
            if len(value) > MAX_ITEMS:
                items.append(TRUNCATED)
            return items
        if isinstance(value, dict):
            clean: dict[str, Any] = {}
            for key, item in list(value.items())[:MAX_ITEMS]:
                try:
                    raw_label = key if isinstance(key, str) else str(key)
                except Exception:
                    raw_label = "key"
                label = redact_text(raw_label, max_length=MAX_TEXT_LENGTH)
                if is_sensitive_key(raw_label, extra_parts=extra_sensitive_parts):
                    clean[label] = REDACTED
                elif callable(item):
                    clean[label] = REDACTED
                else:
                    clean[label] = sanitize(
                        item, extra_sensitive_parts=extra_sensitive_parts, _depth=_depth + 1
                    )
            if len(value) > MAX_ITEMS:
                clean["__jho_truncated__"] = TRUNCATED
            return clean
        if isinstance(value, (bytes, bytearray)):
            return redact_text(
                bytes(value).decode("utf-8", "replace"), max_length=MAX_TEXT_LENGTH
            )
        return redact_text(str(value), max_length=MAX_TEXT_LENGTH)
    except Exception:
        return REDACTED


def sanitize_dict(value: Any, *, extra_sensitive_parts: tuple[str, ...] = ()) -> dict[str, Any]:
    clean = sanitize(value, extra_sensitive_parts=extra_sensitive_parts)
    return clean if isinstance(clean, dict) else {"value": clean}


def write_json(path: Path, value: Any) -> dict[str, Any]:
    clean = sanitize_dict(value)
    path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return clean

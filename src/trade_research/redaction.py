"""Small, deterministic redaction boundary for human-facing text."""

from __future__ import annotations

import re

from pydantic import JsonValue

_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|client[_-]?ip|ip[_-]?address|"
    r"account(?:[_-]?id)?|secret|token|password)\s*[:=]\s*[^\s,;]+"
)
_IP_ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_ACCOUNT_PHRASE = re.compile(r"(?i)\baccount(?:\s+id)?\s+(?:is\s+)?[A-Za-z0-9][A-Za-z0-9._-]{2,}")
_SENSITIVE_JSON_KEYS = frozenset(
    {
        "account",
        "account_id",
        "api_key",
        "authorization",
        "bearer",
        "client_ip",
        "ip_address",
        "password",
        "secret",
        "token",
    }
)
_POSITION_JSON_KEYS = frozenset({"portfolio", "positions"})


def redact_text(value: str) -> str:
    """Remove common secret, account, and client-address representations."""

    redacted = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    redacted = _BEARER_TOKEN.sub("Bearer [REDACTED]", redacted)
    redacted = _ACCOUNT_PHRASE.sub("account id=[REDACTED]", redacted)
    return _IP_ADDRESS.sub("[REDACTED]", redacted)


def redact_json(value: JsonValue) -> JsonValue:
    """Recursively redact textual leaves in a JSON-compatible value."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        redacted: dict[str, JsonValue] = {}
        for key, item in value.items():
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _POSITION_JSON_KEYS:
                continue
            if normalized in _SENSITIVE_JSON_KEYS:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_json(item)
        return redacted
    return value
